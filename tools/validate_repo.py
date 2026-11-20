"""Offline release checks. This module never imports or starts ANSYS."""
from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote

import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILL = Path("skills/ansys-workbench-static")
SCRIPTS = SKILL / "scripts"
PLUGIN = SKILL / "assets/workbench-act"
PLUGIN_NAMES = ("WorkbenchMCP.xml", "main.py", "mechanical_queue_processor.py", "mechanical_socket_timer_v7.py")
RUNTIME_NAMES = {".git", "__pycache__", ".pytest_cache", ".venv", "runtime", "workbench_queue", "jobs", "dist"}
ROOT_FILES = (
    "README.md", "LICENSE", "THIRD_PARTY_NOTICES.md", ".gitignore", ".gitattributes", "requirements-dev.txt",
    "docs/RELEASE_CHECKLIST.md", "docs/VALIDATION_REPORT.md",
    "examples/codex_config.example.toml", "examples/workbench.env.example", ".github/workflows/validate.yml",
    "tools/validate_repo.py", "tools/build_release.py", "tools/tests/test_release.py",
)
SCRIPT_NAMES = (
    "ansys_fea_runner.py", "retry_policy.py", "simulation_intake.py", "mechanical_static_torsion.py",
    "mechanical_inspect_geometry.py", "workbench_create_project.wbjn", "workbench_hold_mechanical.wbjn",
)
REFERENCE_NAMES = ("fea-workflow.md", "environment.md", "ansys-192.md", "runner.md", "validation.md")
TEST_NAMES = ("test_retry_controller.py", "test_mechanical_recovery_contract.py", "test_simulation_intake.py")


def release_paths() -> list[Path]:
    paths = [Path(name) for name in ROOT_FILES]
    paths += [SKILL / name for name in ("SKILL.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "agents/openai.yaml")]
    paths += [SCRIPTS / name for name in SCRIPT_NAMES]
    paths += [SCRIPTS / "tests" / name for name in TEST_NAMES]
    paths += [SKILL / "references" / name for name in REFERENCE_NAMES]
    paths += [SKILL / "assets/configs" / name for name in ("cad_import_example.json", "simple_torsion_example.json")]
    paths += [PLUGIN / name for name in (*PLUGIN_NAMES, "LICENSE", "provenance.json")]
    return sorted(paths, key=lambda path: path.as_posix())


def private_content(text: str) -> list[str]:
    # Only report pattern labels, never echo a matching credential or private path.
    patterns = {
        "personal Windows home path": r"(?i)[a-z]:[\\/]+Users[\\/]+(?!Public\b|Default\b)[^\s\\/\"']+",
        "credential-looking token": r"\b(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{24,})\b",
        "private key": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    }
    return [label for label, pattern in patterns.items() if re.search(pattern, text)]


def validate_repo(root: Path = ROOT) -> list[str]:
    root = root.resolve()
    errors: list[str] = []
    paths = release_paths()
    for relative in paths:
        path = root / relative
        if any(parent.is_symlink() for parent in [path, *path.parents] if parent != root.parent):
            errors.append(f"Symlink not allowed: {relative.as_posix()}")
            continue
        if not path.is_file():
            errors.append(f"Missing release file: {relative.as_posix()}")
            continue
        try:
            value = path.read_text(encoding="utf-8")
        except UnicodeError:
            errors.append(f"Non-UTF-8 source: {relative.as_posix()}")
            continue
        for label in private_content(value):
            errors.append(f"{label}: {relative.as_posix()}")
        if "\ufffd" in value:
            errors.append(f"Replacement character indicates an encoding problem: {relative.as_posix()}")
        if path.suffix in {".py", ".wbjn"}:
            try:
                ast.parse(value, filename=str(relative))
            except SyntaxError as exc:
                errors.append(f"Syntax error: {relative.as_posix()}:{exc.lineno}")
        if path.suffix == ".json":
            try:
                json.loads(value)
            except ValueError:
                errors.append(f"Invalid JSON: {relative.as_posix()}")
        if path.suffix == ".md":
            for target in re.findall(r"(?<!!)\[[^\]]+\]\(([^)]+)\)", value):
                if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith("#"):
                    continue
                local = unquote(target.split("#", 1)[0].strip("<>"))
                resolved = (path.parent / local).resolve()
                if not resolved.is_relative_to(root) or not resolved.exists():
                    errors.append(f"Broken local link: {relative.as_posix()} -> {target}")

    # Unexpected local files are never added by the release builder. Report private
    # config/artifacts outside deliberately ignored runtime and VCS directories.
    forbidden_suffixes = {".wbpj", ".wbpz", ".mechdb", ".rst", ".rth", ".step", ".stp", ".exe", ".dll"}
    def walk(directory: Path):
        for item in directory.iterdir():
            if item.name in RUNTIME_NAMES:
                continue
            if item.is_symlink():
                errors.append("Unreviewed symlink in repository")
            elif item.is_dir():
                yield from walk(item)
            elif item.is_file():
                yield item
    for path in walk(root):
        if (path.name in {".env", "auth.json", "config.toml"} or path.suffix.lower() in forbidden_suffixes
                or path.name.endswith((".local.json", ".local.toml"))):
            errors.append(f"Private config or simulation artifact: {path.relative_to(root).as_posix()}")

    try:
        text = (root / SKILL / "SKILL.md").read_text(encoding="utf-8")
        match = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
        metadata = yaml.safe_load(match.group(1)) if match else None
        if not isinstance(metadata, dict) or metadata.get("name") != SKILL.name:
            errors.append("Invalid skill name/frontmatter")
        if not isinstance(metadata, dict) or not isinstance(metadata.get("description"), str) or not metadata["description"].strip():
            errors.append("Missing skill description")
        if "[TODO:" in text:
            errors.append("Unfinished skill scaffold")
        interface = yaml.safe_load((root / SKILL / "agents/openai.yaml").read_text(encoding="utf-8"))["interface"]
        if "$ansys-workbench-static" not in interface["default_prompt"]:
            errors.append("Default prompt must invoke the skill")
        if not 25 <= len(interface["short_description"]) <= 64:
            errors.append("UI short description length must be 25..64")
        for name in ("cad_import_example.json", "simple_torsion_example.json"):
            config = json.loads((root / SKILL / "assets/configs" / name).read_text(encoding="utf-8"))
            if config.get("schema_version") != 1 or config.get("overwrite_project") is not False:
                errors.append("Example schema/overwrite policy invalid")
        torsion = json.loads((root / SKILL / "assets/configs/simple_torsion_example.json").read_text(encoding="utf-8"))
        for group in ("material", "geometry", "boundary", "load"):
            if any(value is not None for value in torsion[group].values()):
                errors.append(f"Example physics must remain blank: {group}")
        for key in ("type", "mesh_size_mm", "large_deflection"):
            if torsion["analysis"][key] is not None:
                errors.append("Example analysis must remain blank")
        if torsion["user_input"]["confirmed"] is not False or torsion["retry"]["mesh_sizes_mm"] or torsion["retry"]["solver_substeps"]:
            errors.append("Example cannot preconfirm inputs or supply numerical fallback defaults")
        provenance = json.loads((root / PLUGIN / "provenance.json").read_text(encoding="utf-8"))
        if set(provenance["files"]) != set(PLUGIN_NAMES):
            errors.append("Plugin manifest must list exactly the four shipped files")
        for name, record in provenance["files"].items():
            if name not in PLUGIN_NAMES:
                continue
            digest = hashlib.sha256((root / PLUGIN / name).read_bytes()).hexdigest()
            if digest != record["packaged_sha256"]:
                errors.append(f"Plugin checksum mismatch: {name}")
        ET.parse(root / PLUGIN / "WorkbenchMCP.xml")
        for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
            if (root / name).read_bytes() != (root / SKILL / name).read_bytes():
                errors.append(f"Standalone skill must retain root notice: {name}")
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError, ET.ParseError) as exc:
        errors.append(f"Metadata/configuration check failed ({type(exc).__name__})")
    return errors


def main() -> int:
    errors = validate_repo()
    if errors:
        for error in errors:
            print("ERROR: " + error)
        return 1
    print(f"PASS: {len(release_paths())} release files; metadata, links, blank inputs, syntax, plugin checksums and privacy patterns")
    return 0


if __name__ == "__main__":
    sys.exit(main())
