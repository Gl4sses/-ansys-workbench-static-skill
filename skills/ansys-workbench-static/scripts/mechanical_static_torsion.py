# encoding: utf-8
from __future__ import print_function

import codecs
import json
import os
import sys
import time
import traceback

import clr
clr.AddReference("Ans.Core")
clr.AddReference("Ans.EngineeringData")
clr.AddReference("Ans.EngineeringData.Library")
clr.AddReference("Ans.EngineeringData.Core")
clr.AddReference("Ans.EngineeringData.MatMLReaderWriter")

from Ansys.Core.Units import Quantity
from Ansys.ACT.Interfaces.Common import SelectionTypeEnum
from Ansys.ACT.Automation.Mechanical.Enums import LoadDefineBy
from Ansys.EngineeringData import Library
from Ansys.EngineeringData.Core import MatMLXmlTextReader


CONFIG_PATH = globals().get("_MCP_CONFIG_PATH") or os.environ.get("ANSYS_FEA_RUNTIME_CONFIG")
if not CONFIG_PATH:
    raise RuntimeError("ANSYS_FEA_RUNTIME_CONFIG is not set")
with codecs.open(CONFIG_PATH, "r", "utf-8") as config_stream:
    CONFIG = json.loads(config_stream.read())
PATHS = CONFIG["runtime_paths"]
AXIS_NAMES = ("X", "Y", "Z")
STAGE = "user_input"
ATTEMPT_STARTED = time.time()
FAILURE_KIND = ""


def application_messages():
    try:
        return [u"%s: %s" % (getattr(message, "Severity", "Unknown"),
                                getattr(message, "DisplayString", unicode(message)))
                for message in list(ExtAPI.Application.Messages)]
    except Exception:
        return []


INITIAL_MESSAGES = application_messages()


def new_messages():
    messages = application_messages()
    if messages[:len(INITIAL_MESSAGES)] == INITIAL_MESSAGES:
        return messages[len(INITIAL_MESSAGES):]
    return messages


def solver_log_tails(analysis):
    logs = {}
    try:
        working_dir = analysis.WorkingDir
        for name in ("solve.out", "file.err"):
            path = os.path.join(working_dir, name)
            if os.path.isfile(path) and os.path.getmtime(path) >= ATTEMPT_STARTED:
                with open(path, "rb") as stream:
                    stream.seek(max(0, os.path.getsize(path) - 65536))
                    logs[name] = stream.read().decode("utf-8", "replace")
    except Exception:
        pass
    return logs


def configure_substeps(settings, profile):
    if profile is None:
        return
    # Resolve the enum from the installed 19.2 property type, not a newer namespace.
    from System import Enum
    settings.AutomaticTimeStepping = Enum.Parse(settings.AutomaticTimeStepping.GetType(), "On")
    # Widen bounds first so changing a previous profile cannot violate them midway.
    settings.MaximumSubsteps = max(int(settings.MaximumSubsteps), int(profile["maximum"]), int(profile["initial"]))
    settings.MinimumSubsteps = min(max(1, int(settings.MinimumSubsteps)), int(profile["minimum"]), int(profile["initial"]))
    settings.InitialSubsteps = int(profile["initial"])
    settings.MinimumSubsteps = int(profile["minimum"])
    settings.MaximumSubsteps = int(profile["maximum"])
    for key, property_name in (("initial", "InitialSubsteps"), ("minimum", "MinimumSubsteps"), ("maximum", "MaximumSubsteps")):
        if int(getattr(settings, property_name)) != int(profile[key]):
            raise RuntimeError("Substep setting was not accepted: " + property_name)


def write_text(path, text):
    with codecs.open(path, "w", "utf-8") as stream:
        stream.write(unicode(text))


def progress(text):
    write_text(PATHS["progress"], text)


def quantity_text(value):
    try:
        return unicode(value)
    except Exception:
        return u"<unavailable>"


def face_selection(face_id):
    selection = ExtAPI.SelectionManager.CreateSelectionInfo(
        SelectionTypeEnum.GeometryEntities
    )
    selection.Ids = [int(face_id)]
    return selection


def get_geo_bodies():
    bodies = []
    for assembly in list(ExtAPI.DataModel.GeoData.Assemblies):
        for part in list(assembly.Parts):
            bodies.extend(list(part.Bodies))
    return bodies


def get_tree_bodies(model):
    bodies = []
    for group in list(model.Geometry.Children):
        bodies.extend(list(group.Children))
    return bodies


def coordinates(vertex):
    return [float(vertex.X), float(vertex.Y), float(vertex.Z)]


def centroid(face):
    point = face.Centroid
    return [float(point[0]), float(point[1]), float(point[2])]


def axis_index(axis_name):
    return AXIS_NAMES.index(axis_name.upper())


def determine_axis(body, requested):
    vertices = list(body.Vertices)
    if not vertices:
        raise RuntimeError("Geometry body has no vertices")
    points = [coordinates(vertex) for vertex in vertices]
    minimums = [min(point[index] for point in points) for index in range(3)]
    maximums = [max(point[index] for point in points) for index in range(3)]
    extents = [maximums[index] - minimums[index] for index in range(3)]
    selected_index = axis_index(requested)
    if extents[selected_index] <= 0:
        raise RuntimeError("Selected geometry axis has zero extent")
    return AXIS_NAMES[selected_index], selected_index, minimums, maximums, extents


def face_summary(faces, selected_axis):
    rendered = []
    for face in faces:
        rendered.append(
            "id=%s coord=%.9g area=%.9g" % (
                face.Id,
                centroid(face)[selected_axis],
                float(face.Area),
            )
        )
    return "; ".join(rendered)


def select_end_faces(body, axis, minimums, maximums, extent):
    geometry = CONFIG["geometry"]
    faces = list(body.Faces)
    by_id = dict((int(face.Id), face) for face in faces)
    if geometry.get("fixed_face_id") is None or geometry.get("loaded_face_id") is None:
        raise RuntimeError("Ask the user for support/load locations after CAD inspection; no automatic face assignment")
    fixed_id = int(geometry["fixed_face_id"])
    loaded_id = int(geometry["loaded_face_id"])
    if fixed_id not in by_id or loaded_id not in by_id:
        raise RuntimeError("Confirmed face IDs are absent. Faces: " + face_summary(faces, axis))
    return by_id[fixed_id], by_id[loaded_id], "user_confirmed"


def remove_previous_automation(analysis, solution):
    for child in list(solution.Children):
        try:
            if unicode(child.Name).startswith(u"MCP "):
                child.Delete()
        except Exception as error:
            raise RuntimeError("Cannot remove previous MCP result: " + unicode(error))
    for child in list(analysis.Children):
        try:
            if unicode(child.Name).startswith(u"MCP "):
                child.Delete()
        except Exception as error:
            raise RuntimeError("Cannot remove previous MCP boundary: " + unicode(error))


def set_zero(component, unit):
    component.Output.DiscreteValues = [Quantity("0 [%s]" % unit)]


try:
    # Check recorded answers and actual imported geometry before any model mutation.
    require_runtime_user_input(CONFIG, ExtAPI.DataModel.GeoData)
    STAGE = "geometry"
    progress(u"1/7 Validating current geometry and selecting end faces")
    project = ExtAPI.DataModel.Project
    project.SaveProjectAfterSolution = True
    model = project.Model
    analyses = list(model.Analyses)
    if len(analyses) != 1:
        raise RuntimeError("Expected one analysis; found %d" % len(analyses))
    analysis = analyses[0]
    solution = analysis.Solution

    geo_bodies = get_geo_bodies()
    expected_body_count = int(CONFIG.get("analysis", {}).get("expected_body_count", 1))
    if len(geo_bodies) != expected_body_count:
        raise RuntimeError(
            "Expected %d solid body; found %d" % (expected_body_count, len(geo_bodies))
        )
    geo_body = geo_bodies[0]
    axis_name, axis, minimums, maximums, extents = determine_axis(
        geo_body, CONFIG["geometry"]["axis"]
    )
    fixed_face, loaded_face, selection_method = select_end_faces(
        geo_body, axis, minimums, maximums, extents[axis]
    )
    fixed_face_id = int(fixed_face.Id)
    loaded_face_id = int(loaded_face.Id)
    if fixed_face_id == loaded_face_id:
        raise RuntimeError("Fixed and loaded face IDs are identical")
    remove_previous_automation(analysis, solution)

    STAGE = "material"
    progress(u"2/7 Validating and importing material XML")
    material_config = CONFIG["material"]
    material_name = unicode(material_config["name"])
    validation_library = Library()
    validation_reader = MatMLXmlTextReader()
    validation_reader.Load(
        PATHS["material_xml"], validation_library.GetMaterials(), "", False, None
    )
    validation_materials = list(validation_library.Materials)
    if len(validation_materials) != 1:
        raise RuntimeError("Generated material XML did not load exactly one material")
    if unicode(validation_materials[0].DisplayName) != material_name:
        raise RuntimeError("Generated material XML name mismatch")
    if len(list(validation_materials[0].MaterialProperties)) < 6:
        raise RuntimeError("Generated material XML is missing required properties")

    engineering_library = project.EngineeringDataLibrary
    material_names = [
        unicode(item.DisplayName) for item in list(engineering_library.Materials)
    ]
    if material_name not in material_names:
        active_reader = MatMLXmlTextReader()
        active_reader.Load(
            PATHS["material_xml"], engineering_library.GetMaterials(), "", False, None
        )
        model.RefreshMaterials()
    tree_bodies = get_tree_bodies(model)
    if len(tree_bodies) != expected_body_count:
        raise RuntimeError("Mechanical tree body count changed after material refresh")
    body = tree_bodies[0]
    body.Material = material_name

    STAGE = "boundary"
    progress(u"3/7 Applying validated constraints and moment")
    fixed_support = analysis.AddFixedSupport()
    fixed_support.Name = "MCP Fixed Support - Face %d" % fixed_face_id
    fixed_support.Location = face_selection(fixed_face_id)

    boundary_config = CONFIG["boundary"]
    guided_end = None
    if boundary_config["guide_loaded_end"]:
        guided_end = analysis.AddRemoteDisplacement()
        guided_end.Name = "MCP Guided Loaded End - Axial Rotation Free"
        guided_end.Location = face_selection(loaded_face_id)
        set_zero(guided_end.XComponent, "mm")
        set_zero(guided_end.YComponent, "mm")
        set_zero(guided_end.ZComponent, "mm")
        for candidate in AXIS_NAMES:
            if candidate != axis_name:
                set_zero(getattr(guided_end, "Rotation" + candidate), "degree")

    load_config = CONFIG["load"]
    load_axis = str(load_config["axis"]).upper()
    if load_axis != axis_name:
        raise RuntimeError(
            "Moment axis %s does not match the validated geometry axis %s"
            % (load_axis, axis_name)
        )
    moment_value_nm = float(load_config["value_nm"]) * float(load_config["sign"])
    moment = analysis.AddMoment()
    moment.Name = "MCP Moment - %.9g N m %s" % (moment_value_nm, load_axis)
    moment.Location = face_selection(loaded_face_id)
    moment.DefineBy = LoadDefineBy.Components
    set_zero(moment.XComponent, "N m")
    set_zero(moment.YComponent, "N m")
    set_zero(moment.ZComponent, "N m")
    getattr(moment, load_axis + "Component").Output.DiscreteValues = [
        Quantity("%.15g [N m]" % moment_value_nm)
    ]

    STAGE = "mesh"
    progress(u"4/7 Generating configured mesh")
    analysis_config = CONFIG["analysis"]
    mesh_size_mm = float(analysis_config["mesh_size_mm"])
    model.Mesh.ElementSize = Quantity("%.15g [mm]" % mesh_size_mm)
    FAILURE_KIND = "mesh_failed"
    model.Mesh.GenerateMesh()
    node_count = int(model.Mesh.Nodes)
    element_count = int(model.Mesh.Elements)
    if node_count <= 0 or element_count <= 0:
        raise RuntimeError("Mesh generation returned empty counts")
    if any(message.startswith(u"Error:") for message in new_messages()):
        raise RuntimeError("Mesh generation reported errors: " + u"; ".join(new_messages()))
    FAILURE_KIND = ""

    STAGE = "results"
    equivalent_stress = solution.AddEquivalentStress()
    equivalent_stress.Name = "MCP Equivalent Stress"
    total_deformation = solution.AddTotalDeformation()
    total_deformation.Name = "MCP Total Deformation"
    stress_tool = solution.AddStressTool()
    stress_tool.Name = "MCP Stress Tool"
    native_safety = stress_tool.AddSafetyFactor()
    native_safety.Name = "MCP Native Safety Factor"

    STAGE = "solver_settings"
    large_deflection = analysis_config["large_deflection"]
    analysis.AnalysisSettings.LargeDeflection = large_deflection
    configure_substeps(analysis.AnalysisSettings, analysis_config.get("substeps"))
    STAGE = "solve"
    progress(u"5/7 Solving static structural analysis")
    analysis.Solve(True)
    if unicode(analysis.State) != u"UpToDate" or unicode(solution.Status) != u"Done":
        raise RuntimeError("Solve did not complete: analysis=%s solution=%s" % (analysis.State, solution.Status))

    STAGE = "results"
    progress(u"6/7 Evaluating stress, deformation and safety factor")
    solution.EvaluateAllResults()
    yield_strength_mpa = float(material_config["yield_strength_mpa"])
    maximum_stress_text = quantity_text(equivalent_stress.Maximum)
    if u"[MPa]" not in maximum_stress_text:
        raise RuntimeError(
            "Equivalent-stress result is not displayed in MPa: " + maximum_stress_text
        )
    uncapped_safety = solution.AddUserDefinedResult()
    uncapped_safety.Name = "MCP Uncapped Safety Factor"
    uncapped_safety.Expression = "%.15g/seqv" % yield_strength_mpa
    uncapped_safety.EvaluateAllResults()
    safety_quantity = unicode(uncapped_safety.GetQuantityName())
    if safety_quantity != u"Dimensionless":
        raise RuntimeError("Safety-factor result is not dimensionless: " + safety_quantity)

    result_lines = [
        u"status=success",
        u"run_id=" + unicode(CONFIG["run_id"]),
        u"attempt=%d" % int(CONFIG.get("attempt", 1)),
        u"solid_bodies=%d" % len(geo_bodies),
        u"geometry_axis=" + axis_name,
        u"geometry_extent_x_mm=%.15g" % extents[0],
        u"geometry_extent_y_mm=%.15g" % extents[1],
        u"geometry_extent_z_mm=%.15g" % extents[2],
        u"face_selection_method=" + selection_method,
        u"fixed_face_id=%d" % fixed_face_id,
        u"fixed_face_centroid=" + unicode(fixed_face.Centroid),
        u"fixed_face_area_mm2=%.15g" % float(fixed_face.Area),
        u"loaded_face_id=%d" % loaded_face_id,
        u"loaded_face_centroid=" + unicode(loaded_face.Centroid),
        u"loaded_face_area_mm2=%.15g" % float(loaded_face.Area),
        u"material=" + unicode(body.Material),
        u"material_base_name=" + unicode(material_config.get("base_name", material_name)),
        u"material_heat_treatment=" + unicode(material_config["heat_treatment"]),
        u"material_property_source=" + unicode(material_config["property_source"]),
        u"user_input_note=" + unicode(CONFIG["approved_inputs"]["user_input"]["confirmation_note"]),
        u"youngs_modulus_gpa=%.15g" % float(material_config["youngs_modulus_gpa"]),
        u"poisson_ratio=%.15g" % float(material_config["poisson_ratio"]),
        u"density_kg_m3=%.15g" % float(material_config["density_kg_m3"]),
        u"yield_strength_mpa=%.15g" % yield_strength_mpa,
        u"ultimate_strength_mpa=%.15g" % float(material_config["ultimate_strength_mpa"]),
        u"fixed_end=fully fixed",
        u"loaded_end_guided=" + unicode(bool(guided_end is not None)),
        u"free_rotation_axis=" + axis_name,
        u"moment_axis=" + load_axis,
        u"moment_nm=%.15g" % moment_value_nm,
        u"large_deflection=" + unicode(large_deflection),
        u"substeps=" + json.dumps(analysis_config.get("substeps"), sort_keys=True),
        u"mesh_size_mm=%.15g" % mesh_size_mm,
        u"nodes=%d" % node_count,
        u"elements=%d" % element_count,
        u"maximum_equivalent_stress=" + maximum_stress_text,
        u"maximum_total_deformation=" + quantity_text(total_deformation.Maximum),
        u"ansys_native_safety_factor_minimum=" + quantity_text(native_safety.Minimum),
        u"ansys_native_safety_factor_maximum=" + quantity_text(native_safety.Maximum),
        u"uncapped_safety_factor_expression=" + unicode(uncapped_safety.Expression),
        u"minimum_safety_factor=" + quantity_text(uncapped_safety.Minimum),
        u"maximum_safety_factor=" + quantity_text(uncapped_safety.Maximum),
        u"safety_factor_quantity=" + safety_quantity,
        u"analysis_status=" + unicode(analysis.State),
        u"solution_status=" + unicode(solution.Status),
        u"save_project_after_solution=" + unicode(project.SaveProjectAfterSolution),
    ]
    write_text(PATHS["result"], u"\n".join(result_lines))
    uncapped_safety.Activate()
    # The controller signals save only after the terminal response and validation.
    progress(u"7/7 Complete; waiting for controller validation and save")
except Exception:
    exception_type = sys.exc_info()[0].__name__
    error = unicode(traceback.format_exc())
    write_text(PATHS["result"], u"status=error\n" + error)
    diagnostic = {
        "run_id": CONFIG["run_id"],
        "attempt": int(CONFIG.get("attempt", 1)),
        "stage": STAGE,
        "kind": FAILURE_KIND,
        "exception_type": exception_type,
        "error": error,
        "messages": new_messages(),
        "solver_logs": solver_log_tails(locals().get("analysis")),
    }
    write_text(PATHS["diagnostic"], json.dumps(diagnostic, ensure_ascii=True, indent=2))
    progress(u"ERROR\n" + error)
    raise
