from __future__ import annotations

import argparse
import csv
import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from retry_policy import backoff_seconds, classify_failure, recovery_action, retry_settings, positive_number
from simulation_intake import cad_digest, intake_digest, input_digest, require_user_input, blank_simulation_inputs


AUTOMATION_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = AUTOMATION_DIR.parent
CREATE_JOURNAL = AUTOMATION_DIR / "workbench_create_project.wbjn"
HOLD_JOURNAL = AUTOMATION_DIR / "workbench_hold_mechanical.wbjn"
MECHANICAL_SCRIPT = AUTOMATION_DIR / "mechanical_static_torsion.py"
INSPECTION_SCRIPT = AUTOMATION_DIR / "mechanical_inspect_geometry.py"
INTAKE_HELPERS = AUTOMATION_DIR / "simulation_intake.py"
DEFAULT_WORKBENCH = Path(os.environ["ANSYS_WB_EXE"]) if os.environ.get("ANSYS_WB_EXE") else None
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PROCESS_NAMES = {"runwb2.exe", "ansysfw.exe", "ansysfww.exe", "ansyswbu.exe", "ansys192.exe", "ansys.exe"}


class RunnerError(RuntimeError):
    pass


class RunInProgress(RunnerError):
    """The wait budget expired; the existing request/session must be preserved."""


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".writing")
    temporary.write_text(json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def is_ascii(value: str) -> bool:
    try:
        value.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def short_path(path: Path) -> Path:
    resolved = path.resolve()
    if os.name != "nt":
        return resolved
    buffer = ctypes.create_unicode_buffer(32768)
    result = ctypes.windll.kernel32.GetShortPathNameW(str(resolved), buffer, len(buffer))
    if result == 0:
        raise RunnerError("Cannot obtain Windows short path: %s" % resolved)
    candidate = Path(buffer.value)
    if not is_ascii(str(candidate)):
        raise RunnerError("Short path is still non-ASCII: %s" % candidate)
    return candidate


def output_short_path(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not is_ascii(path.name):
        raise RunnerError("ANSYS output filename must be ASCII: %s" % path.name)
    return short_path(path.parent) / path.name


def active_ansys_processes() -> list[dict[str, str]]:
    completed = subprocess.run(
        ["tasklist", "/V", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=20,
    )
    rows: list[dict[str, str]] = []
    for row in csv.reader(completed.stdout.splitlines()):
        if len(row) >= 2 and row[0].lower() in PROCESS_NAMES:
            rows.append({"name": row[0], "pid": row[1], "cpu_time": row[7] if len(row) > 7 else "unknown"})
    return rows


def project_lock_path(project_path: Path) -> Path:
    return project_path.parent / (project_path.stem + "_files") / ".lock"


def controller_claim_path(project_path: Path) -> Path:
    return project_path.with_suffix(".automation.lock")


def validate_user_inputs(config: dict[str, Any]) -> dict[str, Any]:
    confirmation = config.get("user_input", {})
    if not isinstance(confirmation, dict) or confirmation.get("confirmed") is not True:
        raise RunnerError("Awaiting user input: import/inspect CAD with --inspect-only, then ask the user for material, support locations and loads; no simulation assumptions allowed")
    review_path = confirmation.get("geometry_review_path")
    if not review_path or not Path(review_path).is_file():
        raise RunnerError("A post-import geometry_review_path is required; inspect CAD before collecting simulation inputs")
    try:
        review = load_json(Path(review_path))
        require_user_input(config, review)
        if cad_digest(config["cad_path"]) != review["cad_sha256"]:
            raise ValueError("CAD changed since the user's answers; import/inspect and ask again")
        if Path(review["project_path"]).resolve() != Path(config["project_path"]).resolve():
            raise ValueError("The geometry review belongs to a different saved project")
    except (ValueError, KeyError, TypeError) as exc:
        raise RunnerError(str(exc)) from exc
    return review


def validate_config(config: dict[str, Any], config_path: Path) -> None:
    if config.get("schema_version") != 1:
        raise RunnerError("schema_version must be 1")

    task_name = str(config.get("task_name", ""))
    if not re.match(r"^[A-Za-z0-9_-]+$", task_name):
        raise RunnerError("task_name must contain only ASCII letters, digits, '_' or '-'")

    cad_path = Path(config.get("cad_path", ""))
    if not cad_path.is_file():
        raise RunnerError("CAD file does not exist: %s" % cad_path)
    if cad_path.suffix.lower() not in {".stp", ".step", ".iges", ".igs", ".x_t", ".sat"}:
        raise RunnerError("Unsupported CAD extension: %s" % cad_path.suffix)

    project_path = Path(config.get("project_path", ""))
    if project_path.suffix.lower() != ".wbpj":
        raise RunnerError("project_path must end with .wbpj")
    if not is_ascii(project_path.name):
        raise RunnerError("Project filename must be ASCII for ANSYS 19.2")

    project_mode = config.get("project_mode", "create")
    if project_mode not in {"create", "reuse"}:
        raise RunnerError("project_mode must be 'create' or 'reuse'")
    if project_mode == "reuse" and not project_path.is_file():
        raise RunnerError("Reuse project does not exist: %s" % project_path)

    inspecting = config.get("workflow") == "inspect"
    if not inspecting:
        validate_user_inputs(config)
    else:
        if project_mode == "reuse":
            provenance_path = project_path.with_name(project_path.stem + "_geometry_review.json")
            if not provenance_path.is_file():
                raise RunnerError("Existing project has no verified CAD provenance; import into a new project with project_mode=create")
            provenance = load_json(provenance_path)
            if (provenance.get("status") != "geometry_review" or provenance.get("cad_sha256") != cad_digest(cad_path) or
                    Path(provenance.get("project_path", "")).resolve() != project_path.resolve()):
                raise RunnerError("Existing project is not associated with this CAD; import the CAD into a new project")
        try:
            retry_settings(config)
            for key in ("startup_timeout_seconds", "solve_timeout_seconds", "save_timeout_seconds"):
                positive_number(config.get("session", {}).get(key, 180), "session." + key)
        except ValueError as exc:
            raise RunnerError(str(exc)) from exc

    if not inspecting:
        validate_simulation_config(config)

    workbench_value = config.get("workbench_exe") or (str(DEFAULT_WORKBENCH) if DEFAULT_WORKBENCH else "")
    if not workbench_value:
        raise RunnerError("Set config.workbench_exe or ANSYS_WB_EXE to the local RunWB2.exe path")
    workbench = Path(workbench_value)
    for required in (CREATE_JOURNAL, HOLD_JOURNAL, MECHANICAL_SCRIPT, INSPECTION_SCRIPT, INTAKE_HELPERS, workbench):
        if not required.exists():
            raise RunnerError("Required file is missing: %s" % required)


def validate_simulation_config(config: dict[str, Any]) -> None:
    material = config.get("material", {})
    if not is_ascii(str(material.get("name", ""))):
        raise RunnerError("Material name must be ASCII for ANSYS 19.2")
    for key in ("youngs_modulus_gpa", "density_kg_m3", "yield_strength_mpa", "ultimate_strength_mpa"):
        if float(material.get(key, 0)) <= 0:
            raise RunnerError("material.%s must be positive" % key)
    poisson = float(material.get("poisson_ratio", -1))
    if not 0 < poisson < 0.5:
        raise RunnerError("material.poisson_ratio must be between 0 and 0.5")

    geometry = config.get("geometry", {})
    axis = str(geometry["axis"]).upper()
    if axis not in {"X", "Y", "Z"}:
        raise RunnerError("geometry.axis must explicitly be X, Y or Z")
    fixed_id = geometry.get("fixed_face_id")
    loaded_id = geometry.get("loaded_face_id")
    if (fixed_id is None) != (loaded_id is None):
        raise RunnerError("fixed_face_id and loaded_face_id must both be null or both be integers")
    if fixed_id is not None and (int(fixed_id) <= 0 or int(loaded_id) <= 0):
        raise RunnerError("Explicit face IDs must be positive")
    load = config.get("load", {})
    if load["type"] != "moment":
        raise RunnerError("This fast-path runner currently supports moment loads only")
    if float(load.get("value_nm", 0)) <= 0:
        raise RunnerError("load.value_nm must be positive")
    load_axis = str(load["axis"]).upper()
    if load_axis not in {"X", "Y", "Z"}:
        raise RunnerError("load.axis must explicitly be X, Y or Z")
    if float(load["sign"]) not in {-1.0, 1.0}:
        raise RunnerError("load.sign must be 1 or -1")

    analysis = config.get("analysis", {})
    if float(analysis.get("mesh_size_mm", 0)) <= 0:
        raise RunnerError("analysis.mesh_size_mm must be positive")
    if int(analysis.get("expected_body_count", 1)) != 1:
        raise RunnerError("The current fast path supports one solid body")

    try:
        retry_settings(config)
        for key in ("startup_timeout_seconds", "solve_timeout_seconds", "save_timeout_seconds"):
            positive_number(config.get("session", {}).get(key, 180), "session." + key)
        for key in ("youngs_modulus_gpa", "density_kg_m3", "yield_strength_mpa", "ultimate_strength_mpa"):
            positive_number(material.get(key), "material." + key)
        positive_number(load.get("value_nm"), "load.value_nm")
    except ValueError as exc:
        raise RunnerError(str(exc)) from exc

def preflight(config: dict[str, Any], owned_claim: bool = False) -> dict[str, Any]:
    cad_path = Path(config["cad_path"]).resolve()
    project_path = Path(config["project_path"]).resolve()
    mode = config.get("project_mode", "create")
    lock = project_lock_path(project_path)
    blockers: list[str] = []
    if lock.exists():
        blockers.append("Target project is locked: %s" % lock)
    claim = controller_claim_path(project_path)
    if claim.exists() and not owned_claim:
        blockers.append("Automation controller claim exists; inspect the existing run: %s" % claim)
    if mode == "create" and project_path.exists() and not config.get("overwrite_project", False):
        blockers.append("Target project exists and overwrite_project is false")
    if config.get("workflow") != "inspect":
        try:
            validate_user_inputs(config)
        except RunnerError as exc:
            blockers.append(str(exc))

    cad_short = None
    cad_will_stage = False
    try:
        cad_short = str(short_path(cad_path))
    except RunnerError:
        cad_will_stage = True

    return {
        "ready": not blockers,
        "blockers": blockers,
        "cad_path": str(cad_path),
        "cad_size": cad_path.stat().st_size,
        "cad_modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cad_path.stat().st_mtime)),
        "cad_short_path": cad_short,
        "cad_will_stage": cad_will_stage,
        "project_path": str(project_path),
        "project_mode": mode,
        "project_exists": project_path.exists(),
        "project_lock": str(lock),
        "project_locked": lock.exists(),
        "controller_claim": str(claim),
        "create_journal_short": str(short_path(CREATE_JOURNAL)),
        "hold_journal_short": str(short_path(HOLD_JOURNAL)),
        "active_ansys_processes": active_ansys_processes(),
        "retry": retry_settings(config),
    }


def qualifier(parent: ET.Element, name: str, value: str) -> None:
    node = ET.SubElement(parent, "Qualifier", {"name": name})
    node.text = value


def parameter_value(parent: ET.Element, parameter: str, value: str, units: str = "", offset: str = "0") -> None:
    item = ET.SubElement(parent, "ParameterValue", {"parameter": parameter, "format": "float"})
    ET.SubElement(item, "Data").text = value
    qualifier(item, "Variable Type", "Dependent")
    qualifier(item, "Offset", offset)
    qualifier(item, "Scale", "1")
    qualifier(item, "Units", units)


def add_scalar_property(bulk: ET.Element, prop: str, parameter: str, value: str, units: str, offset: str) -> None:
    item = ET.SubElement(bulk, "PropertyData", {"property": prop})
    ET.SubElement(item, "Data", {"format": "string"}).text = "-"
    parameter_value(item, parameter, value, units, offset)


def write_material_xml(material: dict[str, Any], path: Path) -> None:
    name = str(material["name"])
    e_pa = float(material["youngs_modulus_gpa"]) * 1.0e9
    nu = float(material["poisson_ratio"])
    density = float(material["density_kg_m3"])
    yield_pa = float(material["yield_strength_mpa"]) * 1.0e6
    ultimate_pa = float(material["ultimate_strength_mpa"]) * 1.0e6
    bulk_modulus = e_pa / (3.0 * (1.0 - 2.0 * nu))
    shear_modulus = e_pa / (2.0 * (1.0 + nu))

    root = ET.Element("EngineeringData", {"version": "19.2.0.133", "versiondate": "2018/8/8 15:38:00"})
    ET.SubElement(root, "Notes").text = "%s; source: %s" % (material["heat_treatment"], material["property_source"])
    materials = ET.SubElement(root, "Materials")
    doc = ET.SubElement(materials, "MatML_Doc")
    material_node = ET.SubElement(doc, "Material")
    bulk = ET.SubElement(material_node, "BulkDetails")
    ET.SubElement(bulk, "Name").text = name
    ET.SubElement(bulk, "Description").text = str(material.get("description", material.get("assumption", "")))

    add_scalar_property(bulk, "pr1", "pa4", "%.15g" % ultimate_pa, "Pa", "0 [Pa]")
    add_scalar_property(bulk, "pr2", "pa5", "%.15g" % yield_pa, "Pa", "0 [Pa]")

    density_prop = ET.SubElement(bulk, "PropertyData", {"property": "pr3"})
    ET.SubElement(density_prop, "Data", {"format": "string"}).text = "-"
    qualifier(density_prop, "Field Variable Compatible", "Temperature")
    options = ET.SubElement(density_prop, "ParameterValue", {"parameter": "pa6", "format": "string"})
    ET.SubElement(options, "Data").text = "Interpolation Options"
    qualifier(options, "AlgorithmType", "Linear Multivariate")
    qualifier(options, "Normalized", "True")
    qualifier(options, "Cached", "True")
    parameter_value(density_prop, "pa7", "%.15g" % density, "kg m^-3", "0 [kg m^-3]")
    temperature = ET.SubElement(density_prop, "ParameterValue", {"parameter": "pa8", "format": "float"})
    ET.SubElement(temperature, "Data").text = "7.88860905221012e-31"
    qualifier(temperature, "Variable Type", "Independent")
    qualifier(temperature, "Offset", "0 [C]")
    qualifier(temperature, "Scale", "1")
    qualifier(temperature, "Units", "C")
    qualifier(temperature, "Field Variable", "Temperature")
    qualifier(temperature, "Default Data", "22")
    qualifier(temperature, "Field Units", "C")
    qualifier(temperature, "Upper Limit", "Program Controlled")
    qualifier(temperature, "Lower Limit", "Program Controlled")

    add_scalar_property(bulk, "pr4", "pa9", "%.15g" % yield_pa, "Pa", "0 [Pa]")
    add_scalar_property(bulk, "pr5", "pa10", "%.15g" % ultimate_pa, "Pa", "0 [Pa]")

    elasticity = ET.SubElement(bulk, "PropertyData", {"property": "pr13"})
    ET.SubElement(elasticity, "Data", {"format": "string"}).text = "-"
    qualifier(elasticity, "Behavior", "Isotropic")
    qualifier(elasticity, "Derive from", "Young's Modulus and Poisson's Ratio")
    qualifier(elasticity, "Field Variable Compatible", "Temperature")
    elastic_options = ET.SubElement(elasticity, "ParameterValue", {"parameter": "pa6", "format": "string"})
    ET.SubElement(elastic_options, "Data").text = "Interpolation Options"
    qualifier(elastic_options, "AlgorithmType", "Linear Multivariate")
    qualifier(elastic_options, "Normalized", "True")
    qualifier(elastic_options, "Cached", "True")
    parameter_value(elasticity, "pa25", "%.15g" % e_pa, "Pa", "0 [Pa]")
    parameter_value(elasticity, "pa26", "%.15g" % nu)
    parameter_value(elasticity, "pa27", "%.15g" % bulk_modulus, "Pa", "0 [Pa]")
    parameter_value(elasticity, "pa28", "%.15g" % shear_modulus, "Pa", "0 [Pa]")
    elastic_temp = ET.SubElement(elasticity, "ParameterValue", {"parameter": "pa8", "format": "float"})
    ET.SubElement(elastic_temp, "Data").text = "7.88860905221012e-31"
    qualifier(elastic_temp, "Variable Type", "Independent")
    qualifier(elastic_temp, "Offset", "0 [C]")
    qualifier(elastic_temp, "Scale", "1")
    qualifier(elastic_temp, "Units", "C")
    qualifier(elastic_temp, "Field Variable", "Temperature")
    qualifier(elastic_temp, "Default Data", "22")
    qualifier(elastic_temp, "Field Units", "C")
    qualifier(elastic_temp, "Upper Limit", "Program Controlled")
    qualifier(elastic_temp, "Lower Limit", "Program Controlled")

    metadata = ET.SubElement(doc, "Metadata")
    parameter_definitions = [
        ("pa4", "Compressive Ultimate Strength", "Stress"),
        ("pa5", "Compressive Yield Strength", "Stress"),
        ("pa6", "Options Variable", None),
        ("pa7", "Density", "Density"),
        ("pa8", "Temperature", "Temperature"),
        ("pa9", "Tensile Yield Strength", "Stress"),
        ("pa10", "Tensile Ultimate Strength", "Stress"),
        ("pa25", "Young's Modulus", "Stress"),
        ("pa26", "Poisson's Ratio", None),
        ("pa27", "Bulk Modulus", "Stress"),
        ("pa28", "Shear Modulus", "Stress"),
    ]
    for identifier, label, unit_name in parameter_definitions:
        detail = ET.SubElement(metadata, "ParameterDetails", {"id": identifier})
        ET.SubElement(detail, "Name").text = label
        if unit_name:
            ET.SubElement(detail, "Units", {"name": unit_name})
    property_definitions = [
        ("pr1", "Compressive Ultimate Strength"),
        ("pr2", "Compressive Yield Strength"),
        ("pr3", "Density"),
        ("pr4", "Tensile Yield Strength"),
        ("pr5", "Tensile Ultimate Strength"),
        ("pr13", "Elasticity"),
    ]
    for identifier, label in property_definitions:
        detail = ET.SubElement(metadata, "PropertyDetails", {"id": identifier})
        ET.SubElement(detail, "Name").text = label

    transfer = ET.SubElement(root, "ANSYSWBTransferData")
    transfer_materials = ET.SubElement(transfer, "Materials")
    transfer_material = ET.SubElement(transfer_materials, "Material")
    ET.SubElement(transfer_material, "Name").text = name
    ET.SubElement(transfer_material, "DataTransferID").text = str(uuid.uuid4())
    ET.SubElement(transfer_material, "PreviousTransferIDs")
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def stage_cad(cad_path: Path, run_dir: Path) -> Path:
    try:
        return short_path(cad_path)
    except RunnerError:
        staged = run_dir / ("geometry" + cad_path.suffix.lower())
        shutil.copy2(cad_path, staged)
        return short_path(staged)


def prepare_runtime(config: dict[str, Any]) -> tuple[dict[str, Any], Path]:
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    run_id = "%s_%s_%s" % (config["task_name"], timestamp, uuid.uuid4().hex[:8])
    runtime_root = Path(os.environ.get("ANSYS_FEA_RUNTIME_ROOT", str(AUTOMATION_DIR / "runtime")))
    run_dir = runtime_root.expanduser().resolve() / run_id
    queue_root = run_dir / "queue"
    for directory in (run_dir, queue_root / "requests", queue_root / "responses", queue_root / "archive"):
        directory.mkdir(parents=True, exist_ok=True)

    cad_path = Path(config["cad_path"]).resolve()
    project_path = Path(config["project_path"]).resolve()
    project_ansys = short_path(project_path) if project_path.exists() else output_short_path(project_path)
    runtime = deepcopy(config)
    runtime["run_id"] = run_id
    runtime["cad_sha256"] = cad_digest(cad_path)
    material_xml = None
    if config.get("workflow") != "inspect":
        runtime["reviewed_geometry"] = validate_user_inputs(config)
        runtime["approved_inputs"] = deepcopy(config)
        material_fingerprint_source = {
            key: runtime["material"][key]
            for key in ("name", "youngs_modulus_gpa", "poisson_ratio", "density_kg_m3",
                        "yield_strength_mpa", "ultimate_strength_mpa")
        }
        material_fingerprint = hashlib.sha256(
            json.dumps(material_fingerprint_source, sort_keys=True, ensure_ascii=True).encode("ascii")
        ).hexdigest()[:8]
        runtime["material"]["base_name"] = runtime["material"]["name"]
        runtime["material"]["name"] = "%s__cfg_%s" % (runtime["material"]["name"], material_fingerprint)
        material_xml = run_dir / "material.xml"
        write_material_xml(runtime["material"], material_xml)
    runtime["runtime_paths"] = {
        "cad": str(stage_cad(cad_path, run_dir)),
        "project": str(project_ansys),
        "create_status": str(output_short_path(run_dir / "create_status.txt")),
        "hold_status": str(output_short_path(run_dir / "hold_status.txt")),
        "progress": str(output_short_path(run_dir / "progress.txt")),
        "result": str(output_short_path(run_dir / "result.txt")),
        "diagnostic": str(output_short_path(run_dir / "failure.json")),
        "geometry_review": str(output_short_path(run_dir / "geometry_raw.json")),
        "done_flag": str(output_short_path(run_dir / "done.flag")),
        "abort_flag": str(output_short_path(run_dir / "abort.flag")),
        "queue_root": str(short_path(queue_root)),
    }
    if material_xml is not None:
        runtime["runtime_paths"]["material_xml"] = str(short_path(material_xml))
    runtime_path = run_dir / "runtime_config.json"
    write_json(runtime_path, runtime)
    return runtime, runtime_path


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def run_create_journal(workbench: Path, runtime: dict[str, Any], runtime_path: Path, timeout: float) -> None:
    env = os.environ.copy()
    env["ANSYS_FEA_RUNTIME_CONFIG"] = str(short_path(runtime_path))
    env["WORKBENCH_MCP_QUEUE_ROOT"] = runtime["runtime_paths"]["queue_root"]
    run_dir = runtime_path.parent
    stdout_path = run_dir / "create_stdout.log"
    stderr_path = run_dir / "create_stderr.log"
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        try:
            process = subprocess.Popen(
                [str(workbench), "-B", "-R", str(short_path(CREATE_JOURNAL))],
                cwd=str(short_path(WORKSPACE_DIR)),
                env=env,
                stdout=stdout,
                stderr=stderr,
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as exc:
            raise RunnerError("Cannot launch Workbench for project creation: %s" % exc) from exc
        write_json(run_dir / "create_process.json", {"pid": process.pid})
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            # subprocess.run(timeout=...) kills its child, which is unsafe here.
            raise RunInProgress("Project creation is still running; pid=%s" % process.pid) from exc
    status_path = Path(runtime["runtime_paths"]["create_status"])
    status = read_text(status_path)
    if returncode != 0 or "5/5 saved" not in status:
        raise RunnerError(
            "Project creation failed. returncode=%s status=%r stderr=%r"
            % (returncode, status, read_text(stderr_path)[-4000:])
        )


def start_hold_journal(workbench: Path, runtime: dict[str, Any], runtime_path: Path) -> tuple[subprocess.Popen[bytes], Any, Any]:
    env = os.environ.copy()
    env["ANSYS_FEA_RUNTIME_CONFIG"] = str(short_path(runtime_path))
    env["WORKBENCH_MCP_QUEUE_ROOT"] = runtime["runtime_paths"]["queue_root"]
    run_dir = runtime_path.parent
    stdout = (run_dir / "hold_stdout.log").open("wb")
    stderr = (run_dir / "hold_stderr.log").open("wb")
    try:
        process = subprocess.Popen(
            [str(workbench), "-B", "-R", str(short_path(HOLD_JOURNAL))],
            cwd=str(short_path(WORKSPACE_DIR)),
            env=env,
            stdout=stdout,
            stderr=stderr,
            stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
    except OSError as exc:
        stdout.close()
        stderr.close()
        raise RunnerError("Cannot launch controlled Workbench: %s" % exc) from exc
    except Exception:
        stdout.close()
        stderr.close()
        raise
    return process, stdout, stderr


def wait_for_mechanical(runtime: dict[str, Any], process: subprocess.Popen[bytes], timeout: float) -> None:
    status_path = Path(runtime["runtime_paths"]["hold_status"])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = read_text(status_path)
        if "3/5 Mechanical opened" in status:
            return
        if "ERROR" in status:
            raise RunnerError("Mechanical startup failed:\n" + status)
        if process.poll() is not None:
            raise RunnerError("Workbench hold journal exited before Mechanical was ready:\n" + status)
        time.sleep(1.0)
    raise RunInProgress("Mechanical startup wait budget expired; session remains open")


def submit_queue_script(runtime: dict[str, Any], request_id: str | None = None) -> tuple[str, Path]:
    queue_root = Path(runtime["runtime_paths"]["queue_root"])
    request_id = request_id or uuid.uuid4().hex
    request_path = queue_root / "requests" / (request_id + ".json")
    response_path = queue_root / "responses" / (request_id + ".json")
    attempt_dir = Path(runtime["runtime_paths"]["result"]).parent
    config_snapshot = short_path(attempt_dir / "runtime_config.json")
    # A failed archive move in the ACT bridge must not execute the same request
    # again. Claim this attempt on disk before touching the model. Bind its
    # immutable config so replay cannot read a later attempt's parameters.
    guarded_code = """import os as _claim_os
import errno as _claim_errno
_MCP_CONFIG_PATH = %r
try:
    _claim_fd = _claim_os.open(%r, _claim_os.O_CREAT | _claim_os.O_EXCL | _claim_os.O_WRONLY)
except OSError as _claim_error:
    if _claim_error.errno != _claim_errno.EEXIST:
        raise
    if not _claim_os.path.isfile(%r):
        raise RuntimeError('Attempt already claimed; outcome is uncertain')
    with open(%r, 'r') as _prior_stream:
        _prior_status = _prior_stream.readline().strip()
    if _prior_status != 'status=success':
        raise RuntimeError('Attempt already claimed; prior attempt failed or outcome is uncertain')
else:
    _claim_os.close(_claim_fd)
    exec(%r, globals(), globals())
    exec(%r, globals(), globals())
""" % (str(config_snapshot), str(output_short_path(attempt_dir / "execution.claim")),
       runtime["runtime_paths"]["result"], runtime["runtime_paths"]["result"],
       INTAKE_HELPERS.read_text(encoding="utf-8"),
       (INSPECTION_SCRIPT if runtime.get("workflow") == "inspect" else MECHANICAL_SCRIPT).read_text(encoding="utf-8"))
    request = {
        "id": request_id,
        "action": "execute_python",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "code": guarded_code,
        "attempt": runtime.get("attempt", 1),
    }
    temporary = request_path.with_suffix(".json.tmp")
    write_json(temporary, request)
    temporary.replace(request_path)
    return request_id, response_path


def wait_for_response(runtime: dict[str, Any], response_path: Path, hold_process: subprocess.Popen[bytes], timeout: float) -> dict[str, Any]:
    progress_path = Path(runtime["runtime_paths"]["progress"])
    deadline = time.monotonic() + timeout
    last_progress = ""
    next_snapshot = time.monotonic()
    while time.monotonic() < deadline:
        progress = read_text(progress_path).strip()
        if progress and progress != last_progress:
            print("[Mechanical] " + progress.splitlines()[0], flush=True)
            last_progress = progress
        if time.monotonic() >= next_snapshot:
            snapshot: dict[str, Any] = {
                "request_id": response_path.stem, "observed_at": time.time(),
                "hold_pid": hold_process.pid, "progress": progress[:2048],
            }
            try:
                snapshot["processes"] = active_ansys_processes()
            except Exception as exc:
                snapshot["telemetry_error"] = str(exc)
            write_json(progress_path.parent / "monitor.json", snapshot)
            next_snapshot = time.monotonic() + 30
        if response_path.exists():
            try:
                response = load_json(response_path)
            except (json.JSONDecodeError, OSError):
                # The 19.2 ACT queue may still be writing the response file.
                time.sleep(0.2)
                continue
            if not isinstance(response, dict) or type(response.get("ok")) is not bool:
                raise RunInProgress("Unusable queue response; preserve session for inspection: %s" % response_path)
            if response.get("request_id") != response_path.stem:
                raise RunInProgress("Queue response request_id mismatch; no resubmission")
            return response
        if hold_process.poll() is not None:
            raise RunInProgress("Workbench exited without a terminal queue response; solver state is uncertain")
        time.sleep(1.5)
    raise RunInProgress("Solve wait budget expired; monitor the existing request: %s" % response_path)


def wait_for_hold_exit(runtime: dict[str, Any], process: subprocess.Popen[bytes], timeout: float) -> None:
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RunInProgress("Workbench is still saving; do not submit another solve") from exc
    status = read_text(Path(runtime["runtime_paths"]["hold_status"]))
    if "5/5 saved and exited" not in status:
        raise RunnerError("Workbench save verification failed:\n" + status)


def parse_result(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in read_text(path).splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values


def validate_result(config: dict[str, Any], runtime: dict[str, Any]) -> dict[str, str]:
    runtime_result = Path(runtime["runtime_paths"]["result"])
    result = parse_result(runtime_result)
    if result.get("status") != "success":
        raise RunnerError("Mechanical result did not report success:\n" + read_text(runtime_result))
    if result.get("analysis_status") != "UpToDate" or result.get("solution_status") != "Done":
        raise RunnerError("Solver status verification failed: %r" % result)
    if result.get("safety_factor_quantity") != "Dimensionless":
        raise RunnerError("Safety factor is not dimensionless")
    if result.get("run_id") != runtime["run_id"] or result.get("attempt") != str(runtime["attempt"]):
        raise RunnerError("Result does not belong to the current run/attempt")
    try:
        stress_mpa = positive_number(result["maximum_equivalent_stress"].split()[0], "maximum stress")
        reported_sf = positive_number(result["minimum_safety_factor"].split()[0], "minimum safety factor")
        for key in ("ansys_native_safety_factor_minimum", "ansys_native_safety_factor_maximum"):
            positive_number(result[key].split()[0], key)
        positive_number(result["maximum_total_deformation"].split()[0], "maximum deformation", allow_zero=True)
        for key in ("nodes", "elements"):
            if int(result[key]) <= 0:
                raise ValueError("%s must be positive" % key)
    except (KeyError, ValueError, IndexError) as exc:
        raise RunnerError("Missing or invalid numerical result: %s" % exc) from exc
    if "[MPa]" not in result["maximum_equivalent_stress"]:
        raise RunnerError("Equivalent stress is not in MPa")
    try:
        expected_sf = positive_number(float(config["material"]["yield_strength_mpa"]) / stress_mpa,
                                      "independent safety factor")
    except ValueError as exc:
        raise RunnerError(str(exc)) from exc
    relative_error = abs(reported_sf - expected_sf) / max(abs(expected_sf), 1.0e-12)
    if relative_error > 1.0e-8:
        raise RunnerError("Independent safety-factor check failed")
    return result


def verify_saved_project(config: dict[str, Any], run_started: float) -> tuple[Path, Path]:
    project_path = Path(config["project_path"]).resolve()
    mechdb_candidates = list((project_path.parent / (project_path.stem + "_files")).rglob("SYS.mechdb"))
    if not project_path.exists() or not mechdb_candidates:
        raise RunnerError("Saved Workbench or Mechanical database is missing")
    mechdb = max(mechdb_candidates, key=lambda item: item.stat().st_mtime)
    if project_path.stat().st_size <= 0 or mechdb.stat().st_size <= 0:
        raise RunnerError("Saved project or Mechanical database is empty")
    if project_path.stat().st_mtime < run_started - 2 or mechdb.stat().st_mtime < run_started - 2:
        raise RunnerError("Saved project timestamps were not updated")
    return project_path, mechdb


def verify_delivery(config: dict[str, Any], runtime: dict[str, Any], run_started: float) -> dict[str, Any]:
    result = validate_result(config, runtime)
    runtime_result = Path(runtime["runtime_paths"]["result"])
    stress_mpa = float(result["maximum_equivalent_stress"].split()[0])
    reported_sf = float(result["minimum_safety_factor"].split()[0])
    expected_sf = float(config["material"]["yield_strength_mpa"]) / stress_mpa
    project_path, mechdb = verify_saved_project(config, run_started)

    result_output = Path(config.get("result_path") or project_path.with_name(project_path.stem + "_results.txt"))
    result_output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(runtime_result, result_output)

    done_flag = Path(runtime["runtime_paths"]["done_flag"])
    if done_flag.exists():
        done_flag.replace(done_flag.with_name("done.completed"))

    return {
        "project": str(project_path),
        "project_saved": project_path.stat().st_mtime,
        "mechdb": str(mechdb),
        "mechdb_saved": mechdb.stat().st_mtime,
        "result": str(result_output),
        "maximum_equivalent_stress_mpa": stress_mpa,
        "minimum_safety_factor": reported_sf,
        "independent_safety_factor": expected_sf,
        "maximum_total_deformation": result.get("maximum_total_deformation"),
        "nodes": result.get("nodes"),
        "elements": result.get("elements"),
        "mesh_size_mm": result.get("mesh_size_mm"),
        "substeps": result.get("substeps"),
    }


def open_visible(workbench: Path, project_path: Path, timeout: int = 90) -> dict[str, Any]:
    started = time.time()
    process = subprocess.Popen(
        [str(workbench), "-F", str(short_path(project_path))],
        cwd=str(short_path(project_path.parent)),
        env=os.environ.copy(),
        stdin=subprocess.DEVNULL,
    )
    lock = project_lock_path(project_path)
    session_dir = project_path.parent / (project_path.stem + "_files") / "session_files"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        journals = list(session_dir.glob("*.wbjn")) if session_dir.exists() else []
        fresh_journal = any(item.stat().st_mtime >= started - 2 for item in journals)
        if lock.exists() and fresh_journal:
            return {"pid": process.pid, "lock": str(lock), "session_journal_verified": True}
        if process.poll() is not None:
            raise RunnerError("Visible Workbench exited before project-open verification")
        time.sleep(1.0)
    raise RunnerError("Visible Workbench started, but lock/session journal verification timed out")


def checkpoint(history: dict[str, Any], path: Path, status: str | None = None) -> None:
    if status is not None:
        history["status"] = status
    history["updated_at"] = time.time()
    write_json(path, history)


def wait_for_project_release(config: dict[str, Any]) -> None:
    lock = project_lock_path(Path(config["project_path"]).resolve())
    deadline = time.monotonic() + float(config.get("session", {}).get("save_timeout_seconds", 300))
    while lock.exists() and time.monotonic() < deadline:
        time.sleep(1.0)
    if lock.exists():
        raise RunnerError("Project remains locked after controlled process exit; no restart: %s" % lock)


def startup_with_retries(operation: Callable[[], Any], stage: str, config: dict[str, Any],
                         runtime: dict[str, Any], history: dict[str, Any], history_path: Path) -> Any:
    settings = retry_settings(config)
    limit = settings["max_startup_attempts"] if settings["enabled"] else 1
    for attempt in range(1, limit + 1):
        event: dict[str, Any] = {"stage": stage, "attempt": attempt, "status": "running", "started_at": time.time()}
        history["startup"].append(event)
        checkpoint(history, history_path, stage)
        try:
            value = operation()
        except RunInProgress:
            event["status"] = "pending"
            checkpoint(history, history_path, "pending")
            raise
        except RunnerError as exc:
            diagnostic = {"stage": stage, "error": str(exc)}
            if stage == "startup":
                diagnostic["error"] += "\n" + read_text(Path(runtime["runtime_paths"]["hold_status"]))
                diagnostic["error"] += "\n" + read_text(history_path.parent / "hold_stderr.log")[-65536:]
            code = classify_failure(diagnostic)
            event.update(status="failed", diagnostic=diagnostic, failure_code=code, ended_at=time.time())
            retry = code == "license_unavailable" and attempt < limit
            event["action"] = "restart_controlled_session" if retry else "stop"
            checkpoint(history, history_path)
            if not retry:
                raise
            # operation must have observed process exit. Never delete a .lock file.
            wait_for_project_release(config)
            time.sleep(backoff_seconds(settings, attempt))
        except Exception as exc:
            event.update(status="pending", error=str(exc))
            checkpoint(history, history_path, "pending")
            raise RunInProgress("Startup outcome is uncertain; inspect owned process records before restarting") from exc
        else:
            event.update(status="success", ended_at=time.time())
            checkpoint(history, history_path)
            return value


def start_controlled_once(workbench: Path, runtime: dict[str, Any], runtime_path: Path,
                          startup_timeout: float, exit_timeout: float) -> tuple[Any, Any, Any]:
    process, stdout, stderr = start_hold_journal(workbench, runtime, runtime_path)
    try:
        write_json(runtime_path.parent / "hold_process.json", {"pid": process.pid})
    except Exception as exc:
        stdout.close()
        stderr.close()
        raise RunInProgress("Controlled process started (pid=%s), but metadata write failed" % process.pid) from exc
    try:
        wait_for_mechanical(runtime, process, startup_timeout)
    except RunInProgress:
        stdout.close()
        stderr.close()
        raise
    except Exception:
        try:
            # An ERROR marker can precede actual process exit. Do not overlap sessions.
            process.wait(timeout=exit_timeout)
        except subprocess.TimeoutExpired as exc:
            raise RunInProgress("Startup error reported, but controlled process is still running; no restart") from exc
        finally:
            stdout.close()
            stderr.close()
        raise
    return process, stdout, stderr


def prepare_attempt(runtime: dict[str, Any], runtime_path: Path, attempt: int) -> None:
    attempt_dir = runtime_path.parent / ("attempt_%02d" % attempt)
    attempt_dir.mkdir()
    runtime["attempt"] = attempt
    for key, name in (("progress", "progress.txt"), ("result", "result.txt"), ("diagnostic", "failure.json")):
        runtime["runtime_paths"][key] = str(output_short_path(attempt_dir / name))
    # Keep a complete parameter snapshot and update the file read by the next ACT script.
    write_json(attempt_dir / "runtime_config.json", runtime)
    write_json(runtime_path, runtime)


def mechanical_attempts(config: dict[str, Any], runtime: dict[str, Any], runtime_path: Path,
                        hold_process: Any, history: dict[str, Any], history_path: Path,
                        solve_timeout: float) -> None:
    settings = retry_settings(config)
    limit = settings["max_attempts"] if settings["enabled"] else 1
    mesh_index = solver_index = 0
    for attempt in range(1, limit + 1):
        if hold_process.poll() is not None:
            raise RunInProgress("Controlled process exited; inspect solver state before any resubmission")
        prepare_attempt(runtime, runtime_path, attempt)
        request_id = uuid.uuid4().hex
        response_path = Path(runtime["runtime_paths"]["queue_root"]) / "responses" / (request_id + ".json")
        record: dict[str, Any] = {
            "attempt": attempt, "status": "dispatching", "started_at": time.time(),
            "request_id": request_id, "response_path": str(response_path),
            "analysis": deepcopy(runtime["analysis"]),
            "diagnostic_path": runtime["runtime_paths"]["diagnostic"],
        }
        history["attempts"].append(record)
        checkpoint(history, history_path, "running")
        submit_queue_script(runtime, request_id)
        record["status"] = "in_progress"
        checkpoint(history, history_path)
        print("[Retry] Attempt %d/%d request=%s" % (attempt, limit, request_id), flush=True)
        try:
            response = wait_for_response(runtime, response_path, hold_process, solve_timeout)
        except RunInProgress:
            record["status"] = "pending"
            checkpoint(history, history_path, "pending")
            raise
        record["ended_at"] = time.time()
        if response["ok"]:
            # Terminal response alone is not proof of a valid, current solution.
            record["status"] = "validating"
            try:
                validate_result(config, runtime)
            except RunnerError as exc:
                record.update(status="failed", failure_code="results_error", error=str(exc), recovery={"action": "stop"})
                checkpoint(history, history_path)
                raise
            record["status"] = "success"
            checkpoint(history, history_path, "saving")
            Path(runtime["runtime_paths"]["done_flag"]).write_text("success", encoding="ascii")
            return

        diagnostic_path = Path(runtime["runtime_paths"]["diagnostic"])
        diagnostic: dict[str, Any] = {"stage": "unknown", "error": str(response.get("traceback", response))}
        if diagnostic_path.exists():
            try:
                candidate = load_json(diagnostic_path)
                if candidate.get("run_id") == runtime["run_id"] and candidate.get("attempt") == attempt:
                    diagnostic = candidate
            except (ValueError, OSError, AttributeError):
                pass  # Untrustworthy diagnostic never authorizes a recovery.
        if diagnostic.get("stage") == "unknown" and "Attempt already claimed" in str(response.get("traceback", "")):
            raise RunInProgress("A replay was suppressed, but the original outcome is uncertain; inspect %s" % response_path)
        code = classify_failure(diagnostic)
        action = recovery_action(settings, code, attempt, mesh_index, solver_index)
        record.update(status="failed", failure_code=code, diagnostic=diagnostic, recovery=action)
        checkpoint(history, history_path)
        if action["action"] == "stop":
            raise RunnerError("Mechanical failed (%s), attempt %d: %s\nDiagnostics: %s" % (
                code, attempt, diagnostic.get("error", ""), diagnostic_path))
        if action["action"] == "change_mesh":
            runtime["analysis"]["mesh_size_mm"] = action["mesh_size_mm"]
            mesh_index += 1
        elif action["action"] == "change_substeps":
            runtime["analysis"]["substeps"] = action["substeps"]
            solver_index += 1
        print("[Retry] %s -> %s" % (code, action["action"]), flush=True)
        time.sleep(backoff_seconds(settings, attempt))


def mechanical_inspection(config: dict[str, Any], runtime: dict[str, Any], runtime_path: Path,
                           process: Any, history: dict[str, Any], history_path: Path, timeout: float) -> None:
    prepare_attempt(runtime, runtime_path, 1)
    request_id = uuid.uuid4().hex
    response_path = Path(runtime["runtime_paths"]["queue_root"]) / "responses" / (request_id + ".json")
    record = {"attempt": 1, "workflow": "inspect", "request_id": request_id,
              "response_path": str(response_path), "status": "dispatching", "started_at": time.time()}
    history["attempts"].append(record)
    checkpoint(history, history_path, "inspecting")
    submit_queue_script(runtime, request_id)
    record["status"] = "in_progress"
    checkpoint(history, history_path)
    response = wait_for_response(runtime, response_path, process, timeout)
    if not response["ok"] and "Attempt already claimed" in str(response.get("traceback", "")):
        record["status"] = "pending"
        raise RunInProgress("Inspection replay was suppressed; inspect the original request outcome")
    record["status"] = "validating"
    record["ended_at"] = time.time()
    if not response["ok"]:
        record["status"] = "failed"
        raise RunnerError("Geometry inspection failed: " + str(response.get("traceback", response)))
    payload = load_json(Path(runtime["runtime_paths"]["geometry_review"]))
    result = parse_result(Path(runtime["runtime_paths"]["result"]))
    if (payload.get("run_id") != runtime["run_id"] or payload.get("attempt") != 1 or
            result.get("status") != "success" or result.get("run_id") != runtime["run_id"] or
            result.get("attempt") != "1" or not payload.get("geometry", {}).get("bodies")):
        record["status"] = "failed"
        raise RunnerError("Geometry inspection result is missing or belongs to a different request")
    if cad_digest(config["cad_path"]) != runtime["cad_sha256"]:
        raise RunnerError("CAD changed during import/inspection; do not use this geometry review")
    record["status"] = "success"
    checkpoint(history, history_path, "saving")
    Path(runtime["runtime_paths"]["done_flag"]).write_text("inspection complete", encoding="ascii")


def write_intake_artifacts(config: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    project = Path(config["project_path"]).resolve()
    payload = load_json(Path(runtime["runtime_paths"]["geometry_review"]))
    review = {"status": "geometry_review", "run_id": runtime["run_id"], "project_path": str(project),
              "cad_sha256": runtime["cad_sha256"], "geometry": payload["geometry"],
              "geometry_sha256": intake_digest(payload["geometry"])}
    review_path = project.with_name(project.stem + "_geometry_review.json")
    draft_path = project.with_name(project.stem + "_simulation_draft.json")
    questions_path = project.with_name(project.stem + "_simulation_questions.md")
    write_json(review_path, review)
    draft = {key: deepcopy(value) for key, value in config.items()
             if key in {"schema_version", "task_name", "display_name", "cad_path", "project_path",
                        "result_path", "workbench_exe", "session", "retry"}}
    draft.update(blank_simulation_inputs())
    draft.update(project_mode="reuse", overwrite_project=False)
    draft["user_input"] = {"confirmed": False, "confirmation_note": None,
                           "geometry_review_path": str(review_path), "cad_sha256": review["cad_sha256"],
                           "geometry_sha256": review["geometry_sha256"], "parameters_sha256": None}
    write_json(draft_path, draft)
    questions = [
        "工件是什么材料牌号、热处理/供货状态？材料参数使用你提供的数据，还是你指定并认可的数据源？",
        "工件哪些位置受约束？约束是完全固定、铰接、滑动还是其他类型？加载位置还允许哪些位移和转动？",
        "载荷是什么类型，作用在哪些位置？请给出大小、单位、方向/轴线和正负号，以及是否随时间变化。",
        "需要静力、模态、屈曲还是其他分析？是否考虑大变形、接触、摩擦、重力或其他工况？",
        "请核对导入后的尺寸和长度单位，并说明关注的结果及精度要求；位置可直接用自然语言描述。",
    ]
    lines = ["# CAD 已导入，等待用户提供仿真信息", "", "尚未指定材料、施加约束/载荷、划网格或求解。", ""]
    lines.extend("%d. %s" % (index + 1, question) for index, question in enumerate(questions))
    lines.extend(["", "## 当前几何信息", "", "坐标原生单位：%s（未核验时必须询问，不能默认毫米）。" % review["geometry"]["coordinate_unit"], ""])
    for body in review["geometry"]["bodies"]:
        lines.extend(["实体 %s %s；X/Y/Z 尺寸：%s。" % (body["id"], body["name"], body["extents_xyz"]), "",
                      "| 面编号 | 质心（原生坐标） | 面积（原生单位平方） |", "| --- | --- | --- |"])
        lines.extend("| %s | %s | %s |" % (face["id"], face["centroid"], face["area"]) for face in body["faces"][:30])
        if len(body["faces"]) > 30:
            lines.append("其余面见 geometry_review.json；上表只列前 30 个面。")
        lines.append("")
    questions_path.write_text("\n".join(lines), encoding="utf-8")
    return {"status": "awaiting_user_input", "project": str(project), "geometry_review": str(review_path),
            "simulation_draft": str(draft_path), "questions_file": str(questions_path), "questions": questions}


def _execute(config: dict[str, Any], allow_overwrite: bool, no_open: bool) -> dict[str, Any]:
    report = preflight(config, owned_claim=True)
    if report["project_locked"]:
        raise RunnerError(report["blockers"][0])
    if config.get("project_mode", "create") == "create" and Path(config["project_path"]).exists():
        if not (config.get("overwrite_project", False) and allow_overwrite):
            raise RunnerError("Existing project requires both overwrite_project=true and --allow-overwrite")

    runtime, runtime_path = prepare_runtime(config)
    print("Runtime: %s" % runtime_path.parent, flush=True)
    history_path = runtime_path.parent / "retry_history.json"
    history: dict[str, Any] = {
        "run_id": runtime["run_id"], "status": "prepared", "started_at": time.time(),
        "project": config["project_path"], "policy": retry_settings(config), "startup": [], "attempts": [],
    }
    checkpoint(history, history_path)
    workbench_value = config.get("workbench_exe") or (str(DEFAULT_WORKBENCH) if DEFAULT_WORKBENCH else "")
    if not workbench_value:
        raise RunnerError("Set config.workbench_exe or ANSYS_WB_EXE to the local RunWB2.exe path")
    workbench = Path(workbench_value).resolve()
    startup_timeout = float(config.get("session", {}).get("startup_timeout_seconds", 180))
    solve_timeout = float(config.get("session", {}).get("solve_timeout_seconds", 7200))
    save_timeout = float(config.get("session", {}).get("save_timeout_seconds", 300))
    hold_process = hold_stdout = hold_stderr = None
    try:
        if config.get("project_mode", "create") == "create":
            print("[Workbench] Importing CAD and creating project", flush=True)
            startup_with_retries(
                lambda: run_create_journal(workbench, runtime, runtime_path, startup_timeout),
                "create", config, runtime, history, history_path)
        print("[Workbench] Starting controlled Mechanical session", flush=True)
        hold_process, hold_stdout, hold_stderr = startup_with_retries(
            lambda: start_controlled_once(workbench, runtime, runtime_path, startup_timeout, save_timeout),
            "startup", config, runtime, history, history_path)
        inspecting = config.get("workflow") == "inspect"
        if inspecting:
            mechanical_inspection(config, runtime, runtime_path, hold_process, history, history_path, solve_timeout)
        else:
            mechanical_attempts(config, runtime, runtime_path, hold_process, history, history_path, solve_timeout)
        wait_for_hold_exit(runtime, hold_process, save_timeout)
        if inspecting:
            verify_saved_project(config, history["attempts"][-1]["started_at"])
            delivery = write_intake_artifacts(config, runtime)
        else:
            delivery = verify_delivery(config, runtime, history["attempts"][-1]["started_at"])
        gui_requested = not no_open and config.get("session", {}).get("open_visible_after", True)
        checkpoint(history, history_path, "opening_gui" if gui_requested else "verified")
        if gui_requested:
            delivery["visible_workbench"] = open_visible(workbench, Path(config["project_path"]).resolve())
        delivery["runtime_dir"] = str(runtime_path.parent)
        delivery["retry_history"] = str(history_path)
        delivery["attempt_count"] = len(history["attempts"])
        delivery["parameters_changed"] = not inspecting and runtime["analysis"] != config["analysis"]
        history["delivery"] = delivery
        checkpoint(history, history_path, "awaiting_user_input" if inspecting else "success")
        return delivery
    except RunInProgress as exc:
        history["pending_reason"] = str(exc)
        checkpoint(history, history_path, "pending")
        raise RunInProgress("%s\nHistory: %s" % (exc, history_path)) from exc
    except KeyboardInterrupt as exc:
        history["pending_reason"] = "Controller interrupted; execution may still be active"
        checkpoint(history, history_path, "pending")
        raise RunInProgress("Controller interrupted; inspect existing run: %s" % history_path) from exc
    except Exception as exc:
        history["error"] = str(exc)
        # Only stop our holder after a terminal response (or before dispatch).
        in_flight = any(item["status"] in {"dispatching", "in_progress", "pending"} for item in history["attempts"])
        if in_flight:
            checkpoint(history, history_path, "pending")
            raise RunInProgress("Controller error with an uncertain request; no restart: %s\nHistory: %s" % (exc, history_path)) from exc
        if hold_process is not None and hold_process.poll() is None and history["status"] != "saving":
            Path(runtime["runtime_paths"]["abort_flag"]).write_text("terminal failure", encoding="ascii")
            try:
                hold_process.wait(timeout=save_timeout)
            except subprocess.TimeoutExpired:
                history["cleanup_pending"] = True
                checkpoint(history, history_path, "pending")
                raise RunInProgress("Terminal failure recorded, but owned holder cleanup is pending; inspect %s" % history_path) from exc
        checkpoint(history, history_path, "failed")
        raise RunnerError("%s\nHistory: %s" % (exc, history_path)) from exc
    finally:
        for stream in (hold_stdout, hold_stderr):
            if stream is not None:
                stream.close()


def execute(config: dict[str, Any], allow_overwrite: bool, no_open: bool) -> dict[str, Any]:
    # Even programmatic callers cannot bypass input collection by skipping main().
    if config.get("workflow") != "inspect":
        validate_user_inputs(config)
    else:
        validate_config(config, Path("<programmatic-inspection>"))
    # Protect the interval before Workbench creates its own project .lock as well.
    claim = controller_claim_path(Path(config["project_path"]).resolve())
    claim.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RunnerError("Another run owns this project; inspect rather than retry: %s" % claim) from exc
    retain_claim = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "started_at": time.time(), "task_name": config["task_name"]}, stream)
        return _execute(config, allow_overwrite, no_open)
    except RunInProgress:
        retain_claim = True
        raise
    except KeyboardInterrupt as exc:
        retain_claim = True
        raise RunInProgress("Controller interrupted; retained claim: %s" % claim) from exc
    except Exception as exc:
        if not isinstance(exc, RunnerError):
            # An unexpected controller/IO failure must not authorize a new run.
            retain_claim = True
        raise
    finally:
        if not retain_claim:
            claim.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="Fast, config-driven ANSYS 19.2 static torsion runner")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--allow-overwrite", action="store_true")
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--inspect-only", action="store_true", help="Import CAD, export geometry/questions, then stop for user input")
    args = parser.parse_args()

    try:
        config_path = args.config.resolve()
        config = load_json(config_path)
        if args.inspect_only:
            config["workflow"] = "inspect"
        validate_config(config, config_path)
        report = preflight(config)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if args.preflight_only:
            return 0 if report["ready"] else 2
        if args.prepare_only:
            runtime, runtime_path = prepare_runtime(config)
            print(json.dumps({"runtime_config": str(runtime_path), "runtime": runtime}, ensure_ascii=False, indent=2))
            return 0
        delivery = execute(config, args.allow_overwrite, args.no_open)
        print(json.dumps(delivery, ensure_ascii=False, indent=2))
        return 4 if delivery.get("status") == "awaiting_user_input" else 0
    except RunInProgress as exc:
        print("IN_PROGRESS: %s" % exc, file=sys.stderr)
        return 3
    except RunnerError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    except Exception:
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
