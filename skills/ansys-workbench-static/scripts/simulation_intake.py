# encoding: utf-8
"""Shared CPython / ANSYS 19.2 IronPython input and geometry checks.

No ANSYS imports and no model mutations. Confirmation represents answers actually
received from the user, never a tool-generated substitute for asking them.
"""
import hashlib
import json
import math

try:
    text_type = unicode
except NameError:
    text_type = str


def intake_digest(value):
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def cad_digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def confirmed_parameters(config):
    return dict((key, config.get(key)) for key in ("material", "geometry", "boundary", "load", "analysis", "retry"))


def input_digest(config):
    return intake_digest(confirmed_parameters(config))


def blank_simulation_inputs():
    return {
        "material": {"name": None, "heat_treatment": None, "property_source": None,
                     "youngs_modulus_gpa": None, "poisson_ratio": None, "density_kg_m3": None,
                     "yield_strength_mpa": None, "ultimate_strength_mpa": None},
        "geometry": {"axis": None, "length_unit": None, "fixed_face_id": None, "loaded_face_id": None},
        "boundary": {"fixed_type": None, "fixed_description": None, "loaded_description": None,
                     "guide_loaded_end": None},
        "load": {"type": None, "unit": None, "axis": None, "value_nm": None, "sign": None},
        "analysis": {"type": None, "expected_body_count": 1, "mesh_size_mm": None, "large_deflection": None},
    }


def require_user_input(config, review):
    confirmation = config.get("user_input", {})
    if not isinstance(confirmation, dict) or confirmation.get("confirmed") is not True:
        raise ValueError("Awaiting user input after CAD inspection: material, supports and loads must be supplied by the user")
    note = confirmation.get("confirmation_note")
    if not isinstance(note, text_type) or not note.strip():
        raise ValueError("user_input.confirmation_note must record the user's actual answers")
    for group, fields in (
        ("material", ("name", "heat_treatment", "property_source", "youngs_modulus_gpa", "poisson_ratio",
                      "density_kg_m3", "yield_strength_mpa", "ultimate_strength_mpa")),
        ("geometry", ("axis", "length_unit", "fixed_face_id", "loaded_face_id")),
        ("boundary", ("fixed_type", "fixed_description", "loaded_description", "guide_loaded_end")),
        ("load", ("type", "unit", "axis", "value_nm", "sign")),
        ("analysis", ("type", "mesh_size_mm", "large_deflection")),
    ):
        values = config.get(group, {})
        if not isinstance(values, dict):
            raise ValueError("%s must contain explicit user-confirmed inputs" % group)
        for key in fields:
            if key not in values or values[key] is None or values[key] == "":
                raise ValueError("Missing user-confirmed input: %s.%s" % (group, key))
    for group, key in (("boundary", "guide_loaded_end"), ("analysis", "large_deflection")):
        if type(config[group][key]) is not bool:
            raise ValueError("%s.%s must explicitly be true or false" % (group, key))
    if config["boundary"]["fixed_type"] != "fixed_support":
        raise ValueError("This runner supports fixed_support only; do not replace the user's requested support")
    if config["analysis"]["type"] != "static_structural" or config["load"]["type"] != "moment":
        raise ValueError("This runner supports static structural moment loading only; extend it for the user's requested analysis")
    if config["load"]["unit"] != "N m":
        raise ValueError("load.unit must explicitly be N m; convert the user's stated units before recording")
    for group, key in (("load", "value_nm"), ("analysis", "mesh_size_mm")):
        value = float(config[group][key])
        if isinstance(config[group][key], bool) or math.isnan(value) or math.isinf(value) or value <= 0:
            raise ValueError("Invalid explicit input: %s.%s" % (group, key))
    if isinstance(config["load"]["sign"], bool) or config["load"]["sign"] not in (-1, 1):
        raise ValueError("The load sign must explicitly be -1 or 1")
    if config["geometry"]["length_unit"] != "mm":
        raise ValueError("This runner requires geometry coordinates verified as mm against the inspected dimensions")
    if config["geometry"]["axis"] not in ("X", "Y", "Z") or config["load"]["axis"] != config["geometry"]["axis"]:
        raise ValueError("The explicit load axis must match the user-confirmed torsion axis")
    for key in ("youngs_modulus_gpa", "density_kg_m3", "yield_strength_mpa", "ultimate_strength_mpa"):
        value = float(config["material"][key])
        if isinstance(config["material"][key], bool) or math.isnan(value) or math.isinf(value) or value <= 0:
            raise ValueError("Invalid user-confirmed material property: " + key)
    poisson = float(config["material"]["poisson_ratio"])
    if not 0 < poisson < 0.5:
        raise ValueError("Invalid user-confirmed Poisson ratio")
    if not isinstance(review, dict) or review.get("status") != "geometry_review":
        raise ValueError("A completed post-import geometry review is required before simulation")
    geometry = review.get("geometry", {})
    if review.get("geometry_sha256") != intake_digest(geometry):
        raise ValueError("Geometry review fingerprint is invalid")
    native_unit = geometry.get("coordinate_unit", "unverified_native_unit").lower().split(".")[-1]
    if native_unit not in ("mm", "millimeter", "millimeters", "millimetre", "millimetres", "unverified_native_unit"):
        raise ValueError("Inspected native coordinates are not mm; verify/convert geometry units before simulation")
    for key in ("cad_sha256", "geometry_sha256"):
        if confirmation.get(key) != review.get(key):
            raise ValueError("User input belongs to a different CAD/geometry review: " + key)
    bodies = geometry.get("bodies", [])
    if len(bodies) != 1:
        raise ValueError("The confirmed geometry must contain exactly one body for this runner")
    ids = set(face["id"] for face in bodies[0]["faces"])
    fixed_id = config["geometry"]["fixed_face_id"]
    loaded_id = config["geometry"]["loaded_face_id"]
    if type(fixed_id) is bool or type(loaded_id) is bool or fixed_id not in ids or loaded_id not in ids or fixed_id == loaded_id:
        raise ValueError("Support/load faces must be distinct faces from the inspected current geometry")
    if confirmation.get("parameters_sha256") != input_digest(config):
        raise ValueError("Simulation inputs changed after the user's answers were recorded; ask about the changed inputs")


def capture_geometry(geo_data):
    """Use strings for stable float serialization between Python 2 and 3."""
    def number(value):
        return "%.12g" % float(value)

    bodies = []
    for assembly in list(geo_data.Assemblies):
        for part in list(assembly.Parts):
            for body in list(part.Bodies):
                vertices = sorted([[number(vertex.X), number(vertex.Y), number(vertex.Z)] for vertex in list(body.Vertices)])
                faces = []
                for face in list(body.Faces):
                    faces.append({"id": int(face.Id), "area": number(face.Area),
                                  "centroid": [number(value) for value in list(face.Centroid)]})
                faces.sort(key=lambda face: face["id"])
                dimensions = []
                if vertices:
                    dimensions = [number(max(float(point[i]) for point in vertices) -
                                         min(float(point[i]) for point in vertices)) for i in range(3)]
                bodies.append({"id": int(body.Id), "name": text_type(getattr(body, "Name", "")),
                               "vertices": vertices, "faces": faces, "extents_xyz": dimensions})
    bodies.sort(key=lambda body: body["id"])
    return {"coordinate_unit": text_type(getattr(geo_data, "Unit", "unverified_native_unit")), "bodies": bodies}


def require_runtime_user_input(runtime, geo_data):
    approved = runtime.get("approved_inputs")
    if not isinstance(approved, dict):
        raise ValueError("No recorded user answers; stop before changing materials, supports or loads")
    review = runtime.get("reviewed_geometry")
    require_user_input(approved, review)
    if cad_digest(runtime["runtime_paths"]["cad"]) != review["cad_sha256"]:
        raise ValueError("CAD file changed after user input was recorded")
    if intake_digest(capture_geometry(geo_data)) != review["geometry_sha256"]:
        raise ValueError("Current imported geometry differs from the geometry shown to the user")
    for group in ("geometry", "boundary", "load", "retry"):
        if runtime.get(group) != approved.get(group):
            raise ValueError("Unconfirmed runtime input change: " + group)
    expected_material = dict(approved["material"])
    material_name = expected_material["name"]
    material_keys = ("name", "youngs_modulus_gpa", "poisson_ratio", "density_kg_m3", "yield_strength_mpa", "ultimate_strength_mpa")
    signature = dict((key, expected_material[key]) for key in material_keys)
    suffix = hashlib.sha256(json.dumps(signature, sort_keys=True, ensure_ascii=True).encode("ascii")).hexdigest()[:8]
    expected_material["base_name"] = material_name
    expected_material["name"] = "%s__cfg_%s" % (material_name, suffix)
    if runtime["material"] != expected_material:
        raise ValueError("Runtime material differs from user-confirmed material")
    analysis = dict(runtime["analysis"])
    initial = approved["analysis"]
    retry = approved.get("retry", {})
    allowed_sizes = [initial["mesh_size_mm"]] + retry.get("mesh_sizes_mm", [])
    allowed_profiles = [initial.get("substeps")] + retry.get("solver_substeps", [])
    if analysis.get("mesh_size_mm") not in allowed_sizes or analysis.get("substeps") not in allowed_profiles:
        raise ValueError("Unapproved numerical recovery settings")
    for key in ("mesh_size_mm", "substeps"):
        analysis.pop(key, None)
    baseline = dict(initial)
    for key in ("mesh_size_mm", "substeps"):
        baseline.pop(key, None)
    if analysis != baseline:
        raise ValueError("Unconfirmed analysis change")
