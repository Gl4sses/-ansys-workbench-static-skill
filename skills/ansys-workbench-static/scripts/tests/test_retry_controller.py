"""Fault injection tests. No test starts, closes, or connects to ANSYS."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ansys_fea_runner as runner
from retry_policy import classify_failure, recovery_action, retry_settings


class FakeProcess:
    pid = 424242

    def __init__(self):
        self.returncode = None
        self.wait = Mock(side_effect=self.finish)

    def finish(self, timeout=None):
        self.returncode = 0
        return 0

    def poll(self):
        return self.returncode


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="retry_test_", dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ("short_path", "output_short_path"):
            mocker = patch.object(runner, name, side_effect=lambda path: Path(path).resolve())
            mocker.start()
            self.addCleanup(mocker.stop)
        self.sleep = patch.object(runner.time, "sleep").start()
        self.addCleanup(patch.stopall)
        self.config = {
            "schema_version": 1, "task_name": "fault_injection",
            "cad_path": str(self.root / "part.step"),
            "project_path": str(self.root / "case.wbpj"), "project_mode": "reuse",
            "workbench_exe": str(self.root / "RunWB2.exe"),
            "material": {"name": "Test Steel", "youngs_modulus_gpa": 200, "poisson_ratio": 0.3,
                         "density_kg_m3": 7850, "yield_strength_mpa": 1000, "ultimate_strength_mpa": 1200,
                         "heat_treatment": "test fixture state", "property_source": "synthetic test data"},
            "geometry": {"axis": "X", "length_unit": "mm", "fixed_face_id": 10, "loaded_face_id": 11},
            "boundary": {"fixed_type": "fixed_support", "fixed_description": "test left end",
                         "loaded_description": "test right end", "guide_loaded_end": True},
            "load": {"type": "moment", "unit": "N m", "axis": "X", "value_nm": 100, "sign": 1},
            "analysis": {"type": "static_structural", "mesh_size_mm": 2.5, "large_deflection": True},
            "session": {"startup_timeout_seconds": 5, "solve_timeout_seconds": 5,
                        "save_timeout_seconds": 5, "open_visible_after": True},
            "retry": {"max_attempts": 3, "backoff_seconds": 5},
        }
        for key in ("cad_path", "project_path", "workbench_exe"):
            Path(self.config[key]).write_text("test fixture", encoding="utf-8")
        geometry = {"coordinate_unit": "mm", "bodies": [{"id": 1, "name": "test body", "vertices": [],
                     "extents_xyz": ["100", "10", "10"], "faces": [{"id": 10}, {"id": 11}]}]}
        review = {"status": "geometry_review", "geometry": geometry, "geometry_sha256": runner.intake_digest(geometry),
                  "cad_sha256": runner.cad_digest(self.config["cad_path"]), "project_path": self.config["project_path"]}
        review_path = self.root / "review.json"
        runner.write_json(review_path, review)
        self.config["user_input"] = {"confirmed": True, "confirmation_note": "Synthetic user answers for offline tests only",
                                     "geometry_review_path": str(review_path), "cad_sha256": review["cad_sha256"],
                                     "geometry_sha256": review["geometry_sha256"]}
        self.record_fixture_answers()
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        queue = self.run_dir / "queue"
        for name in ("requests", "responses", "archive"):
            (queue / name).mkdir(parents=True)
        self.runtime = deepcopy(self.config)
        self.runtime["run_id"] = "test_run"
        self.runtime["runtime_paths"] = {
            "queue_root": str(queue), "done_flag": str(self.run_dir / "done.flag"),
            "abort_flag": str(self.run_dir / "abort.flag"), "hold_status": str(self.run_dir / "hold_status.txt"),
            "create_status": str(self.run_dir / "create_status.txt"),
        }
        self.runtime_path = self.run_dir / "runtime_config.json"
        runner.write_json(self.runtime_path, self.runtime)
        self.history = {"startup": [], "attempts": [], "status": "prepared"}
        self.history_path = self.run_dir / "retry_history.json"
        self.process = FakeProcess()

    def record_fixture_answers(self):
        self.config["user_input"]["parameters_sha256"] = runner.input_digest(self.config)

    def test_runtime_can_live_outside_installed_skill(self):
        external = self.root / "writable_runtime"
        installed = self.root / "installed_skill"
        with patch.object(runner, "AUTOMATION_DIR", installed), patch.dict(
                os.environ, {"ANSYS_FEA_RUNTIME_ROOT": str(external)}):
            runtime, path = runner.prepare_runtime(self.config)
        self.assertEqual(path.parent.parent, external.resolve())
        self.assertTrue(path.is_file())
        self.assertTrue(Path(runtime["runtime_paths"]["queue_root"]).is_dir())
        self.assertFalse(installed.exists())

    def result(self, runtime, **overrides):
        values = {
            "status": "success", "run_id": runtime["run_id"], "attempt": str(runtime["attempt"]),
            "analysis_status": "UpToDate", "solution_status": "Done", "safety_factor_quantity": "Dimensionless",
            "maximum_equivalent_stress": "10 [MPa]", "minimum_safety_factor": "100",
            "ansys_native_safety_factor_minimum": "15", "ansys_native_safety_factor_maximum": "15",
            "maximum_total_deformation": "0.25 [mm]", "nodes": "1000", "elements": "200",
            "mesh_size_mm": str(runtime["analysis"]["mesh_size_mm"]),
            "substeps": json.dumps(runtime["analysis"].get("substeps")),
        }
        values.update(overrides)
        Path(runtime["runtime_paths"]["result"]).write_text(
            "\n".join("%s=%s" % item for item in values.items()), encoding="utf-8")

    def diagnostic(self, runtime, stage, error, **extra):
        value = {"run_id": runtime["run_id"], "attempt": runtime["attempt"],
                 "stage": stage, "error": error, **extra}
        runner.write_json(Path(runtime["runtime_paths"]["diagnostic"]), value)

    def run_attempts(self, outcomes):
        """Use real dispatch/config/history; inject only terminal Mechanical outcomes."""
        outcomes = iter(outcomes)

        def respond(runtime, response_path, process, timeout):
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            if outcome == "success":
                self.result(runtime)
                return {"ok": True, "request_id": response_path.stem}
            self.diagnostic(runtime, **outcome)
            return {"ok": False, "request_id": response_path.stem, "traceback": outcome["error"]}

        with patch.object(runner, "wait_for_response", side_effect=respond) as wait:
            runner.mechanical_attempts(self.config, self.runtime, self.runtime_path, self.process,
                                       self.history, self.history_path, 10)
        return wait

    def test_license_retries_then_success_with_one_request_per_attempt(self):
        original = deepcopy(self.config)
        failure = {"stage": "solve", "error": "License checkout failed"}
        wait = self.run_attempts([failure, failure, "success"])
        self.assertEqual(wait.call_count, 3)
        self.assertEqual(len(list((self.run_dir / "queue/requests").glob("*.json"))), 3)
        self.assertEqual(len({item["request_id"] for item in self.history["attempts"]}), 3)
        self.assertEqual([item["status"] for item in self.history["attempts"]], ["failed", "failed", "success"])
        self.assertEqual(self.sleep.call_args_list, [unittest.mock.call(5), unittest.mock.call(10)])
        self.assertEqual(original, self.config)
        self.assertTrue(Path(self.runtime["runtime_paths"]["done_flag"]).exists())
        for number in range(1, 4):
            snapshot = runner.load_json(self.run_dir / ("attempt_%02d" % number) / "runtime_config.json")
            self.assertEqual(snapshot["attempt"], number)

    def test_mesh_fallback_changes_only_approved_mesh_setting(self):
        self.config["retry"]["mesh_sizes_mm"] = [1.7]
        self.run_attempts([{"stage": "mesh", "kind": "mesh_failed", "error": "Cannot mesh body"}, "success"])
        self.assertEqual(self.runtime["analysis"]["mesh_size_mm"], 1.7)
        self.assertEqual(self.runtime["load"], self.config["load"])
        self.assertEqual(self.runtime["material"], self.config["material"])
        self.assertEqual(self.runtime["boundary"], self.config["boundary"])

    def test_nonconvergence_uses_approved_substeps(self):
        profile = {"initial": 20, "minimum": 10, "maximum": 200}
        self.config["retry"]["solver_substeps"] = [profile]
        self.run_attempts([{"stage": "solve", "error": "Solution failed to converge"}, "success"])
        self.assertEqual(self.runtime["analysis"]["substeps"], profile)
        self.assertTrue(self.runtime["analysis"]["large_deflection"])

    def test_mesh_without_fallback_stops_after_one_attempt(self):
        with self.assertRaises(runner.RunnerError):
            self.run_attempts([{"stage": "mesh", "kind": "mesh_failed", "error": "Failed mesh"}])
        self.assertEqual(len(self.history["attempts"]), 1)

    def test_fallback_exhaustion_stops_before_overall_limit(self):
        self.config["retry"]["mesh_sizes_mm"] = [1.7]
        failure = {"stage": "mesh", "kind": "mesh_failed", "error": "Failed mesh"}
        with self.assertRaises(runner.RunnerError):
            self.run_attempts([failure, failure])
        self.assertEqual(len(self.history["attempts"]), 2)

    def test_attempt_limit_is_enforced(self):
        failure = {"stage": "solve", "error": "License checkout failed"}
        with self.assertRaises(runner.RunnerError):
            self.run_attempts([failure] * 3)
        self.assertEqual(len(self.history["attempts"]), 3)
        self.assertFalse(Path(self.runtime["runtime_paths"]["done_flag"]).exists())

    def test_disabled_retries_allow_exactly_one_attempt(self):
        self.config["retry"]["enabled"] = False
        with self.assertRaises(runner.RunnerError):
            self.run_attempts([{"stage": "solve", "error": "License checkout failed"}])
        self.assertEqual(len(self.history["attempts"]), 1)

    def test_geometry_error_never_retries_even_with_license_text(self):
        with self.assertRaises(runner.RunnerError):
            self.run_attempts([{"stage": "geometry", "error": "License checkout failed"}])
        self.assertEqual(self.history["attempts"][0]["failure_code"], "geometry_error")

    def test_pending_response_preserves_request_and_never_signals_save_or_abort(self):
        with self.assertRaises(runner.RunInProgress):
            self.run_attempts([runner.RunInProgress("still solving")])
        self.assertEqual(len(self.history["attempts"]), 1)
        self.assertEqual(self.history["status"], "pending")
        for key in ("done_flag", "abort_flag"):
            self.assertFalse(Path(self.runtime["runtime_paths"][key]).exists())

    def test_success_with_nan_result_cannot_signal_save_or_retry(self):
        def invalid(runtime, *args):
            self.result(runtime, maximum_equivalent_stress="nan [MPa]")
            return {"ok": True}
        with patch.object(runner, "wait_for_response", side_effect=invalid):
            with self.assertRaises(runner.RunnerError):
                runner.mechanical_attempts(self.config, self.runtime, self.runtime_path, self.process,
                                           self.history, self.history_path, 10)
        self.assertEqual(self.history["attempts"][0]["status"], "failed")
        self.assertFalse(Path(self.runtime["runtime_paths"]["done_flag"]).exists())

    def test_stale_diagnostic_does_not_authorize_retry(self):
        def respond(runtime, *args):
            self.diagnostic(runtime, "solve", "License checkout failed")
            path = Path(runtime["runtime_paths"]["diagnostic"])
            value = runner.load_json(path)
            value["run_id"] = "other_run"
            runner.write_json(path, value)
            return {"ok": False, "traceback": "generic failure"}
        with patch.object(runner, "wait_for_response", side_effect=respond):
            with self.assertRaises(runner.RunnerError):
                runner.mechanical_attempts(self.config, self.runtime, self.runtime_path, self.process,
                                           self.history, self.history_path, 10)
        self.assertEqual(self.history["attempts"][0]["failure_code"], "unknown_error")

    def test_replayed_queue_request_executes_model_script_once(self):
        runner.prepare_attempt(self.runtime, self.runtime_path, 1)
        script = self.root / "fake_mechanical.py"
        script.write_text("calls.append(_MCP_CONFIG_PATH)\nwith open(%r, 'w') as output:\n    output.write('status=success\\n')\n" %
                          self.runtime["runtime_paths"]["result"], encoding="utf-8")
        with patch.object(runner, "MECHANICAL_SCRIPT", script):
            request_id, _ = runner.submit_queue_script(self.runtime)
        request = runner.load_json(self.run_dir / "queue/requests" / (request_id + ".json"))
        calls = []
        exec(request["code"], {"calls": calls})
        # Changing the mutable top-level config must not affect this captured request.
        runner.write_json(self.runtime_path, {"attempt": 99})
        exec(request["code"], {"calls": calls})
        self.assertEqual(calls, [str(self.run_dir / "attempt_01/runtime_config.json")])

    def test_replay_without_prior_terminal_result_is_suppressed(self):
        runner.prepare_attempt(self.runtime, self.runtime_path, 1)
        request_id, _ = runner.submit_queue_script(self.runtime)
        (self.run_dir / "attempt_01/execution.claim").touch()
        request = runner.load_json(self.run_dir / "queue/requests" / (request_id + ".json"))
        with self.assertRaisesRegex(RuntimeError, "outcome is uncertain"):
            exec(request["code"], {})

    def test_short_poll_timeout_does_not_dispatch_or_kill(self):
        runner.prepare_attempt(self.runtime, self.runtime_path, 1)
        path = self.run_dir / "queue/responses/one.json"
        with self.assertRaises(runner.RunInProgress):
            runner.wait_for_response(self.runtime, path, self.process, 0)
        self.process.wait.assert_not_called()
        self.assertEqual(list((self.run_dir / "queue/requests").glob("*.json")), [])

    def test_partial_json_is_polled_until_complete(self):
        runner.prepare_attempt(self.runtime, self.runtime_path, 1)
        path = self.run_dir / "queue/responses/one.json"
        path.write_text('{"ok":', encoding="utf-8")
        self.sleep.side_effect = lambda seconds: runner.write_json(path, {"ok": True, "request_id": "one"})
        with patch.object(runner, "active_ansys_processes", return_value=[]):
            result = runner.wait_for_response(self.runtime, path, self.process, 10)
        self.assertTrue(result["ok"])
        self.assertTrue((self.run_dir / "attempt_01/monitor.json").exists())

    def test_mismatched_response_id_stays_pending(self):
        runner.prepare_attempt(self.runtime, self.runtime_path, 1)
        path = self.run_dir / "queue/responses/one.json"
        runner.write_json(path, {"ok": True, "request_id": "different"})
        with patch.object(runner, "active_ansys_processes", return_value=[]):
            with self.assertRaises(runner.RunInProgress):
                runner.wait_for_response(self.runtime, path, self.process, 10)

    def test_create_timeout_never_kills_child(self):
        process = Mock(pid=42)
        process.wait.side_effect = subprocess.TimeoutExpired("RunWB2", 10)
        with patch.object(runner.subprocess, "Popen", return_value=process):
            with self.assertRaises(runner.RunInProgress):
                runner.run_create_journal(Path(self.config["workbench_exe"]), self.runtime, self.runtime_path, 10)
        process.kill.assert_not_called()
        process.terminate.assert_not_called()

    def test_startup_retry_requires_explicit_transient_failure(self):
        operation = Mock(side_effect=[runner.RunnerError("License checkout failed"), "ready"])
        value = runner.startup_with_retries(operation, "startup", self.config, self.runtime,
                                            self.history, self.history_path)
        self.assertEqual(value, "ready")
        self.assertEqual(operation.call_count, 2)

    def test_pending_startup_is_not_restarted(self):
        operation = Mock(side_effect=runner.RunInProgress("starting"))
        with self.assertRaises(runner.RunInProgress):
            runner.startup_with_retries(operation, "startup", self.config, self.runtime,
                                        self.history, self.history_path)
        operation.assert_called_once()

    def test_uncertain_startup_io_error_is_not_restarted(self):
        operation = Mock(side_effect=OSError("Cannot write process metadata"))
        with self.assertRaises(runner.RunInProgress):
            runner.startup_with_retries(operation, "startup", self.config, self.runtime,
                                        self.history, self.history_path)
        operation.assert_called_once()
        self.assertEqual(self.history["status"], "pending")

    def test_existing_project_lock_prevents_startup_retry(self):
        lock = runner.project_lock_path(Path(self.config["project_path"]))
        lock.parent.mkdir()
        lock.touch()
        self.config["session"]["save_timeout_seconds"] = 0
        operation = Mock(side_effect=runner.RunnerError("License checkout failed"))
        with self.assertRaisesRegex(runner.RunnerError, "remains locked"):
            runner.startup_with_retries(operation, "startup", self.config, self.runtime,
                                        self.history, self.history_path)
        operation.assert_called_once()
        self.assertTrue(lock.exists())

    def test_project_admission_claim_blocks_concurrent_controller(self):
        claim = runner.controller_claim_path(Path(self.config["project_path"]))
        claim.write_text("existing owner", encoding="utf-8")
        with patch.object(runner, "_execute") as execute:
            with self.assertRaises(runner.RunnerError):
                runner.execute(self.config, False, True)
        execute.assert_not_called()
        self.assertEqual(claim.read_text(), "existing owner")

    def test_pending_run_retains_admission_claim(self):
        with patch.object(runner, "_execute", side_effect=runner.RunInProgress("pending")):
            with self.assertRaises(runner.RunInProgress):
                runner.execute(self.config, False, True)
        self.assertTrue(runner.controller_claim_path(Path(self.config["project_path"])).exists())

    def test_live_request_pending_in_full_controller_never_aborts(self):
        with patch.object(runner, "AUTOMATION_DIR", self.root / "automation"), \
             patch.object(runner, "preflight", return_value={"project_locked": False}), \
             patch.object(runner, "start_hold_journal", return_value=(self.process, io.StringIO(), io.StringIO())), \
             patch.object(runner, "wait_for_mechanical"), \
             patch.object(runner, "wait_for_response", side_effect=runner.RunInProgress("still solving")) as wait:
            with self.assertRaises(runner.RunInProgress):
                runner.execute(self.config, False, True)
        wait.assert_called_once()
        self.process.wait.assert_not_called()
        history_path = next((self.root / "automation/runtime").glob("*/retry_history.json"))
        history = runner.load_json(history_path)
        self.assertEqual(history["status"], "pending")
        self.assertFalse((history_path.parent / "abort.flag").exists())
        self.assertFalse((history_path.parent / "done.flag").exists())
        self.assertTrue(runner.controller_claim_path(Path(self.config["project_path"])).exists())

    def test_save_timeout_does_not_resolve_or_abort(self):
        def success(runtime, *args):
            self.result(runtime)
            return {"ok": True}
        self.process.wait.side_effect = subprocess.TimeoutExpired("RunWB2", 5)
        with patch.object(runner, "AUTOMATION_DIR", self.root / "automation"), \
             patch.object(runner, "preflight", return_value={"project_locked": False}), \
             patch.object(runner, "start_hold_journal", return_value=(self.process, io.StringIO(), io.StringIO())), \
             patch.object(runner, "wait_for_mechanical"), \
             patch.object(runner, "wait_for_response", side_effect=success) as wait:
            with self.assertRaises(runner.RunInProgress):
                runner.execute(self.config, False, True)
        wait.assert_called_once()
        self.process.wait.assert_called_once()
        history_path = next((self.root / "automation/runtime").glob("*/retry_history.json"))
        self.assertEqual(runner.load_json(history_path)["status"], "pending")
        self.assertTrue((history_path.parent / "done.flag").exists())
        self.assertFalse((history_path.parent / "abort.flag").exists())

    def test_terminal_failure_closes_only_owned_holder_without_save(self):
        def failure(runtime, *args):
            self.diagnostic(runtime, "boundary", "Invalid support")
            return {"ok": False}
        with patch.object(runner, "AUTOMATION_DIR", self.root / "automation"), \
             patch.object(runner, "preflight", return_value={"project_locked": False}), \
             patch.object(runner, "start_hold_journal", return_value=(self.process, io.StringIO(), io.StringIO())), \
             patch.object(runner, "wait_for_mechanical"), \
             patch.object(runner, "wait_for_response", side_effect=failure) as wait:
            with self.assertRaises(runner.RunnerError):
                runner.execute(self.config, False, True)
        wait.assert_called_once()
        self.process.wait.assert_called_once()
        history_path = next((self.root / "automation/runtime").glob("*/retry_history.json"))
        self.assertEqual(runner.load_json(history_path)["status"], "failed")
        self.assertTrue((history_path.parent / "abort.flag").exists())
        self.assertFalse((history_path.parent / "done.flag").exists())
        self.assertFalse(runner.controller_claim_path(Path(self.config["project_path"])).exists())

    def test_failed_holder_cleanup_retains_claim(self):
        def failure(runtime, *args):
            self.diagnostic(runtime, "boundary", "Invalid support")
            return {"ok": False}
        self.process.wait.side_effect = subprocess.TimeoutExpired("RunWB2", 5)
        with patch.object(runner, "AUTOMATION_DIR", self.root / "automation"), \
             patch.object(runner, "preflight", return_value={"project_locked": False}), \
             patch.object(runner, "start_hold_journal", return_value=(self.process, io.StringIO(), io.StringIO())), \
             patch.object(runner, "wait_for_mechanical"), \
             patch.object(runner, "wait_for_response", side_effect=failure):
            with self.assertRaises(runner.RunInProgress):
                runner.execute(self.config, False, True)
        self.assertTrue(runner.controller_claim_path(Path(self.config["project_path"])).exists())

    def test_old_database_is_rejected(self):
        runner.prepare_attempt(self.runtime, self.runtime_path, 1)
        self.result(self.runtime)
        project = Path(self.config["project_path"])
        db = project.parent / (project.stem + "_files") / "SYS.mechdb"
        db.parent.mkdir()
        db.write_bytes(b"old database")
        os.utime(db, (1, 1))
        with self.assertRaisesRegex(runner.RunnerError, "timestamps"):
            runner.verify_delivery(self.config, self.runtime, runner.time.time())

    def test_holder_waits_for_controller_then_saves_in_order(self):
        self.runtime["runtime_paths"]["project"] = self.config["project_path"]
        runner.write_json(self.runtime_path, self.runtime)
        model = Mock()
        system = Mock()
        system.GetContainer.return_value = model
        operations = Mock()
        operations.attach_mock(model.Exit, "exit")
        operations.attach_mock(Mock(), "save")
        ticks = []

        def advance(seconds):
            ticks.append(seconds)
            if len(ticks) == 3:
                Path(self.runtime["runtime_paths"]["done_flag"]).write_text("success")

        self.sleep.side_effect = advance
        with patch.dict(os.environ, {"ANSYS_FEA_RUNTIME_CONFIG": str(self.runtime_path)}):
            exec(compile(runner.HOLD_JOURNAL.read_text(encoding="utf-8"), str(runner.HOLD_JOURNAL), "exec"),
                 {"SetScriptVersion": Mock(), "Open": Mock(), "GetSystem": Mock(return_value=system),
                  "Save": operations.save})
        self.assertEqual(len(ticks), 3)
        self.assertEqual(operations.mock_calls, [unittest.mock.call.exit(), unittest.mock.call.save(Overwrite=True)])

    def test_main_reports_pending_with_distinct_exit_code(self):
        config_path = self.root / "config.json"
        runner.write_json(config_path, self.config)
        with patch.object(sys, "argv", ["runner", "--config", str(config_path)]), \
             patch.object(runner, "preflight", return_value={"ready": True}), \
             patch.object(runner, "execute", side_effect=runner.RunInProgress("active request")), \
             patch.object(sys, "stderr", io.StringIO()) as errors:
            self.assertEqual(runner.main(), 3)
        self.assertIn("IN_PROGRESS", errors.getvalue())

    def test_full_controller_recovers_and_verifies_delivery(self):
        self.config["retry"]["mesh_sizes_mm"] = [1.7]
        self.record_fixture_answers()
        # Route runtime preparation to this test's isolated directory.
        with patch.object(runner, "AUTOMATION_DIR", self.root / "automation"), \
             patch.object(runner, "preflight", return_value={"project_locked": False}), \
             patch.object(runner, "start_hold_journal", return_value=(self.process, io.StringIO(), io.StringIO())), \
             patch.object(runner, "wait_for_mechanical"), \
             patch.object(runner, "open_visible") as gui:
            captured = []

            def respond(runtime, response_path, *args):
                captured.append(deepcopy(runtime))
                if runtime["attempt"] == 1:
                    self.diagnostic(runtime, "mesh", "mesh generation failed", kind="mesh_failed")
                    return {"ok": False}
                self.result(runtime)
                return {"ok": True}

            def save(timeout=None):
                runtime = captured[-1]
                self.assertTrue(Path(runtime["runtime_paths"]["done_flag"]).exists())
                project = Path(self.config["project_path"])
                project.write_text("saved project", encoding="utf-8")
                db = project.parent / (project.stem + "_files") / "dp0/SYS/MECH/SYS.mechdb"
                db.parent.mkdir(parents=True)
                db.write_bytes(b"saved mechanical database")
                Path(runtime["runtime_paths"]["hold_status"]).write_text("5/5 saved and exited", encoding="utf-8")
                self.process.returncode = 0

            self.process.wait.side_effect = save
            with patch.object(runner, "wait_for_response", side_effect=respond):
                delivery = runner.execute(self.config, False, True)
        gui.assert_not_called()
        self.assertEqual(delivery["attempt_count"], 2)
        self.assertTrue(delivery["parameters_changed"])
        self.assertEqual(runner.load_json(Path(delivery["retry_history"]))["status"], "success")
        self.assertTrue(Path(delivery["result"]).is_file())
        self.assertFalse(runner.controller_claim_path(Path(self.config["project_path"])).exists())


class PolicyTests(unittest.TestCase):
    def test_classifier_prioritizes_hard_failures(self):
        cases = [
            ({"stage": "solve", "error": "zero pivot; License checkout failed"}, "constraint_error"),
            ({"stage": "solve", "error": "out of memory; failed to converge"}, "resource_error"),
            ({"stage": "solve", "solver_logs": {"solve.out": "failed to converge"}}, "nonconvergence"),
            ({"stage": "mesh", "kind": "mesh_failed", "exception_type": "AttributeError"}, "api_error"),
            ({"stage": "results", "error": "License checkout failed"}, "results_error"),
            ({"stage": "solve", "error": "unexpected internal failure"}, "unknown_error"),
        ]
        for diagnostic, expected in cases:
            with self.subTest(diagnostic=diagnostic):
                self.assertEqual(classify_failure(diagnostic), expected)

    def test_invalid_policy_never_reaches_execution(self):
        variants = [
            {"max_attempts": 0}, {"max_attempts": 11}, {"max_attempts": True}, {"max_startup_attempts": 1.5},
            {"mesh_sizes_mm": [float("nan")]}, {"mesh_sizes_mm": [2.5]}, {"mesh_sizes_mm": [1, 1]},
            {"backoff_seconds": -1}, {"backoff_seconds": float("inf")}, {"backoff_multiplier": 0.5},
            {"solver_substeps": [{"initial": 5, "minimum": 10, "maximum": 20}]},
            {"solver_substeps": [{"initial": 5, "minimum": 1, "maximum": 20, "typo": 1}]},
            {"enabled": "false"}, {"typo": 2},
        ]
        for retry in variants:
            with self.subTest(retry=retry), self.assertRaises(ValueError):
                retry_settings({"analysis": {"mesh_size_mm": 2.5}, "retry": retry})

    def test_default_policy_never_invents_mesh_or_solver_settings(self):
        settings = retry_settings({"analysis": {"mesh_size_mm": 2.5}})
        for code in ("mesh_failed", "nonconvergence", "constraint_error", "unknown_error"):
            self.assertEqual(recovery_action(settings, code, 1, 0, 0)["action"], "stop")


if __name__ == "__main__":
    unittest.main()
