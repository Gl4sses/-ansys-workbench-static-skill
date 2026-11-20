"""Verify that CAD inspection cannot silently become a guessed simulation."""
import ast
import io
import json
import sys
import tempfile
import traceback
import types
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ansys_fea_runner as runner
from simulation_intake import capture_geometry, require_runtime_user_input


def fake_geometry():
    vertices = [types.SimpleNamespace(X=0, Y=0, Z=0), types.SimpleNamespace(X=100, Y=10, Z=10)]
    faces = [types.SimpleNamespace(Id=10, Area=100, Centroid=[0, 5, 5]),
             types.SimpleNamespace(Id=11, Area=100, Centroid=[100, 5, 5])]
    body = types.SimpleNamespace(Id=1, Name="test CAD body", Vertices=vertices, Faces=faces)
    part = types.SimpleNamespace(Bodies=[body])
    assembly = types.SimpleNamespace(Parts=[part])
    return types.SimpleNamespace(Assemblies=[assembly], Unit="mm")


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="intake_test_", dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ("short_path", "output_short_path"):
            mocker = patch.object(runner, name, side_effect=lambda path: Path(path).resolve())
            mocker.start()
            self.addCleanup(mocker.stop)
        mocker = patch.object(runner, "AUTOMATION_DIR", self.root / "automation")
        mocker.start()
        self.addCleanup(mocker.stop)
        self.geo = fake_geometry()
        self.base = {"schema_version": 1, "task_name": "intake_test", "cad_path": str(self.root / "part.step"),
                     "project_path": str(self.root / "case.wbpj"), "workbench_exe": str(self.root / "RunWB2.exe"),
                     "project_mode": "reuse", "session": {"open_visible_after": False}}
        for key in ("cad_path", "project_path", "workbench_exe"):
            Path(self.base[key]).write_text("offline fixture", encoding="utf-8")
        geometry = capture_geometry(self.geo)
        self.review = {"status": "geometry_review", "geometry": geometry,
                       "geometry_sha256": runner.intake_digest(geometry),
                       "cad_sha256": runner.cad_digest(self.base["cad_path"]), "project_path": self.base["project_path"]}
        self.review_path = self.root / "case_geometry_review.json"
        runner.write_json(self.review_path, self.review)
        self.config = deepcopy(self.base)
        self.config.update({
            "material": {"name": "Test Steel", "heat_treatment": "test specified state", "property_source": "synthetic user data",
                         "youngs_modulus_gpa": 200, "poisson_ratio": 0.3, "density_kg_m3": 7850,
                         "yield_strength_mpa": 1000, "ultimate_strength_mpa": 1200},
            "geometry": {"axis": "X", "length_unit": "mm", "fixed_face_id": 10, "loaded_face_id": 11},
            "boundary": {"fixed_type": "fixed_support", "fixed_description": "left end fixed",
                         "loaded_description": "right end, no extra guide", "guide_loaded_end": False},
            "load": {"type": "moment", "unit": "N m", "axis": "X", "value_nm": 100, "sign": -1},
            "analysis": {"type": "static_structural", "mesh_size_mm": 2.5, "large_deflection": False},
            "retry": {"mesh_sizes_mm": [1.5], "solver_substeps": [{"initial": 20, "minimum": 10, "maximum": 200}]},
            "user_input": {"confirmed": True, "confirmation_note": "Synthetic post-import user answers, offline test only",
                           "geometry_review_path": str(self.review_path), "cad_sha256": self.review["cad_sha256"],
                           "geometry_sha256": self.review["geometry_sha256"]},
        })
        self.config["user_input"]["parameters_sha256"] = runner.input_digest(self.config)

    def inspect_runtime(self):
        config = dict(self.base, workflow="inspect")
        runtime, path = runner.prepare_runtime(config)
        runner.prepare_attempt(runtime, path, 1)
        request_id, _ = runner.submit_queue_script(runtime)
        request = runner.load_json(Path(runtime["runtime_paths"]["queue_root"]) / "requests" / (request_id + ".json"))
        # Deliberately no Project/Model/mesh/load API: any model mutation fails.
        ext_api = types.SimpleNamespace(DataModel=types.SimpleNamespace(GeoData=self.geo))
        exec(request["code"], {"ExtAPI": ext_api})
        return config, runtime, path

    def test_unconfirmed_inputs_stop_before_any_process_launch(self):
        config = deepcopy(self.config)
        config["user_input"]["confirmed"] = False
        with patch.object(runner.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(runner.RunnerError, "Awaiting user input"):
                runner.execute(config, False, True)
        launch.assert_not_called()
        self.assertFalse(runner.controller_claim_path(Path(config["project_path"])).exists())

    def test_reuse_inspection_requires_matching_cad_provenance(self):
        config = dict(self.base, workflow="inspect")
        self.review_path.unlink()
        with patch.object(runner.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(runner.RunnerError, "provenance"):
                runner.execute(config, False, True)
        launch.assert_not_called()

    def test_missing_inputs_cannot_be_filled_by_defaults(self):
        for group, key in (("material", "heat_treatment"), ("material", "property_source"),
                           ("geometry", "fixed_face_id"), ("geometry", "loaded_face_id"),
                           ("boundary", "fixed_type"), ("boundary", "guide_loaded_end"),
                           ("load", "unit"), ("load", "axis"), ("load", "sign"), ("analysis", "large_deflection")):
            config = deepcopy(self.config)
            del config[group][key]
            config["user_input"]["parameters_sha256"] = runner.input_digest(config)
            with self.subTest(group=group, key=key), self.assertRaises(runner.RunnerError):
                runner.validate_user_inputs(config)

    def test_auto_axis_and_unsupported_supports_are_not_substituted(self):
        for group, key, value in (("load", "axis", "auto"), ("boundary", "fixed_type", "hinged"),
                                  ("load", "type", "pressure"), ("analysis", "type", "modal")):
            config = deepcopy(self.config)
            config[group][key] = value
            config["user_input"]["parameters_sha256"] = runner.input_digest(config)
            with self.subTest(value=value), self.assertRaises(runner.RunnerError):
                runner.validate_user_inputs(config)

    def test_edited_parameters_require_actual_new_answers(self):
        config = deepcopy(self.config)
        config["load"]["value_nm"] = 999
        with self.assertRaisesRegex(runner.RunnerError, "changed after"):
            runner.validate_user_inputs(config)

    def test_changed_cad_file_invalidates_recorded_answers(self):
        Path(self.config["cad_path"]).write_text("different CAD", encoding="utf-8")
        with self.assertRaisesRegex(runner.RunnerError, "CAD changed"):
            runner.validate_user_inputs(self.config)

    def test_stale_review_geometry_fingerprint_is_rejected(self):
        self.review["geometry"]["bodies"][0]["faces"][0]["id"] = 99
        runner.write_json(self.review_path, self.review)
        with self.assertRaisesRegex(runner.RunnerError, "fingerprint"):
            runner.validate_user_inputs(self.config)

    def test_user_faces_must_belong_to_current_geometry(self):
        config = deepcopy(self.config)
        config["geometry"]["fixed_face_id"] = 999
        config["user_input"]["parameters_sha256"] = runner.input_digest(config)
        with self.assertRaisesRegex(runner.RunnerError, "inspected current geometry"):
            runner.validate_user_inputs(config)

    def test_known_geometry_unit_conflict_cannot_be_treated_as_mm(self):
        self.review["geometry"]["coordinate_unit"] = "m"
        self.review["geometry_sha256"] = runner.intake_digest(self.review["geometry"])
        self.config["user_input"]["geometry_sha256"] = self.review["geometry_sha256"]
        runner.write_json(self.review_path, self.review)
        with self.assertRaisesRegex(runner.RunnerError, "not mm"):
            runner.validate_user_inputs(self.config)

    def test_runtime_allows_only_recorded_numerical_recovery(self):
        runtime, _ = runner.prepare_runtime(self.config)
        require_runtime_user_input(runtime, self.geo)
        runtime["analysis"]["mesh_size_mm"] = 1.5
        runtime["analysis"]["substeps"] = self.config["retry"]["solver_substeps"][0]
        require_runtime_user_input(runtime, self.geo)
        runtime["load"]["sign"] = 1
        with self.assertRaisesRegex(ValueError, "load"):
            require_runtime_user_input(runtime, self.geo)

    def test_runtime_blocks_material_and_unapproved_mesh_changes(self):
        runtime, _ = runner.prepare_runtime(self.config)
        runtime["material"]["yield_strength_mpa"] = 1
        with self.assertRaisesRegex(ValueError, "material"):
            require_runtime_user_input(runtime, self.geo)
        runtime["material"]["yield_strength_mpa"] = 1000
        runtime["analysis"]["mesh_size_mm"] = 99
        with self.assertRaisesRegex(ValueError, "numerical"):
            require_runtime_user_input(runtime, self.geo)

    def test_runtime_rechecks_actual_imported_geometry(self):
        runtime, _ = runner.prepare_runtime(self.config)
        self.geo.Assemblies[0].Parts[0].Bodies[0].Faces[0].Centroid[0] = 1
        with self.assertRaisesRegex(ValueError, "differs from"):
            require_runtime_user_input(runtime, self.geo)

    def test_actual_inspection_script_reads_only_geometry(self):
        _, runtime, _ = self.inspect_runtime()
        self.assertNotIn("material_xml", runtime["runtime_paths"])
        self.assertNotIn("material", runtime)
        self.assertEqual(runner.parse_result(Path(runtime["runtime_paths"]["result"]))["workflow"], "inspect")
        self.assertFalse(Path(runtime["runtime_paths"]["done_flag"]).exists())
        self.assertEqual(runner.load_json(Path(runtime["runtime_paths"]["geometry_review"]))["geometry"], capture_geometry(self.geo))

    def test_artifacts_are_questions_and_blank_inputs_not_example_physics(self):
        config, runtime, _ = self.inspect_runtime()
        artifacts = runner.write_intake_artifacts(config, runtime)
        draft = runner.load_json(Path(artifacts["simulation_draft"]))
        self.assertEqual(artifacts["status"], "awaiting_user_input")
        self.assertFalse(draft["user_input"]["confirmed"])
        self.assertIsNone(draft["material"]["name"])
        self.assertIsNone(draft["load"]["value_nm"])
        self.assertIsNone(draft["boundary"]["guide_loaded_end"])
        self.assertIsNone(draft["geometry"]["fixed_face_id"])
        self.assertEqual(len(artifacts["questions"]), 5)
        self.assertIn("100", Path(artifacts["questions_file"]).read_text(encoding="utf-8"))
        with self.assertRaisesRegex(runner.RunnerError, "Awaiting user input"):
            runner.validate_user_inputs(draft)

    def test_mechanical_entry_checks_answers_before_accessing_project(self):
        source = ast.parse(runner.MECHANICAL_SCRIPT.read_text(encoding="utf-8"))
        entry = next(node for node in source.body if isinstance(node, ast.Try))
        # Module has no earlier top-level try blocks; the main try is the entry.
        writes = Mock()
        environment = {
            "CONFIG": {"user_input": {"confirmed": False}}, "STAGE": "user_input", "FAILURE_KIND": "",
            "ExtAPI": types.SimpleNamespace(DataModel=types.SimpleNamespace(GeoData=self.geo)),
            "require_runtime_user_input": require_runtime_user_input,
            "sys": sys, "traceback": traceback, "unicode": str, "json": json,
            "PATHS": {"result": "unused", "diagnostic": "unused"}, "write_text": writes,
            "progress": Mock(), "new_messages": lambda: [], "solver_log_tails": lambda analysis: {},
        }
        # Supply bookkeeping so the exception path can record the blocked entry.
        environment["CONFIG"].update(run_id="test", attempt=1)
        with self.assertRaisesRegex(ValueError, "No recorded user answers"):
            exec(compile(ast.Module(body=[entry], type_ignores=[]), "entry", "exec"), environment)
        self.assertGreater(writes.call_count, 0)

    def test_complete_inspection_flow_saves_and_returns_to_user_without_solving(self):
        config = dict(self.base, workflow="inspect")
        process = Mock(pid=42)
        process.poll.return_value = None
        captured = []

        def respond(runtime, response_path, *args):
            captured.append(runtime)
            request = runner.load_json(Path(runtime["runtime_paths"]["queue_root"]) / "requests" / (response_path.stem + ".json"))
            exec(request["code"], {"ExtAPI": types.SimpleNamespace(DataModel=types.SimpleNamespace(GeoData=self.geo))})
            return {"ok": True}

        def save(timeout=None):
            runtime = captured[-1]
            project = Path(config["project_path"])
            project.write_bytes(b"saved imported CAD")
            database = project.parent / (project.stem + "_files") / "SYS.mechdb"
            database.parent.mkdir()
            database.write_bytes(b"geometry-only mechanical database")
            Path(runtime["runtime_paths"]["hold_status"]).write_text("5/5 saved and exited", encoding="utf-8")
            process.poll.return_value = 0

        process.wait.side_effect = save
        with patch.object(runner, "preflight", return_value={"project_locked": False}), \
             patch.object(runner, "start_hold_journal", return_value=(process, io.StringIO(), io.StringIO())), \
             patch.object(runner, "wait_for_mechanical"), \
             patch.object(runner, "wait_for_response", side_effect=respond), \
             patch.object(runner, "mechanical_attempts") as solve, patch.object(runner, "open_visible") as gui:
            delivery = runner.execute(config, False, True)
        solve.assert_not_called()
        gui.assert_not_called()
        self.assertEqual(delivery["status"], "awaiting_user_input")
        self.assertEqual(runner.load_json(Path(delivery["retry_history"]))["status"], "awaiting_user_input")
        self.assertFalse(runner.controller_claim_path(Path(config["project_path"])).exists())

    def test_cli_import_only_returns_distinct_awaiting_input_code(self):
        config_path = self.root / "import.json"
        runner.write_json(config_path, self.base)
        with patch.object(sys, "argv", ["runner", "--config", str(config_path), "--inspect-only", "--no-open"]), \
             patch.object(runner, "preflight", return_value={"ready": True}), \
             patch.object(runner, "execute", return_value={"status": "awaiting_user_input"}) as execute, \
             patch.object(sys, "stdout", io.StringIO()):
            self.assertEqual(runner.main(), 4)
        self.assertEqual(execute.call_args.args[0]["workflow"], "inspect")


if __name__ == "__main__":
    unittest.main()
