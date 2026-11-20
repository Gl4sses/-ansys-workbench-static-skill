"""Exercise the actual IronPython helper bodies with .NET-free fakes."""
import ast
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


SOURCE = Path(__file__).resolve().parents[1] / "mechanical_static_torsion.py"


def actual_function(name, **bindings):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
    env = {"unicode": str, **bindings}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE), "exec"), env)
    return env[name]


class EnumValue:
    def GetType(self):
        return "installed-19.2-enum-type"


class BoundedSettings:
    def __init__(self, initial, minimum, maximum):
        object.__setattr__(self, "values", {"InitialSubsteps": initial, "MinimumSubsteps": minimum,
                                            "MaximumSubsteps": maximum, "AutomaticTimeStepping": EnumValue()})

    def __getattr__(self, name):
        return self.values[name]

    def __setattr__(self, name, value):
        candidate = dict(self.values, **{name: value})
        if not candidate["MinimumSubsteps"] <= candidate["InitialSubsteps"] <= candidate["MaximumSubsteps"]:
            raise RuntimeError("Invalid intermediate substep bounds")
        self.values[name] = value


class MechanicalContractTests(unittest.TestCase):
    def test_profiles_widen_bounds_before_changing_initial_substeps(self):
        system = types.ModuleType("System")
        system.Enum = Mock()
        system.Enum.Parse.return_value = EnumValue()
        function = actual_function("configure_substeps")
        settings = BoundedSettings(5, 1, 10)
        with patch.dict(sys.modules, {"System": system}):
            function(settings, {"initial": 20, "minimum": 10, "maximum": 200})
            function(settings, {"initial": 5, "minimum": 1, "maximum": 100})
        self.assertEqual((settings.InitialSubsteps, settings.MinimumSubsteps, settings.MaximumSubsteps), (5, 1, 100))
        system.Enum.Parse.assert_called_with("installed-19.2-enum-type", "On")

    def test_unsupported_substep_api_fails_without_changing_physics(self):
        function = actual_function("configure_substeps")
        with patch.dict(sys.modules, {"System": types.SimpleNamespace(Enum=Mock())}):
            with self.assertRaises(AttributeError):
                function(object(), {"initial": 20, "minimum": 10, "maximum": 200})

    def test_no_profile_does_not_touch_installed_settings(self):
        actual_function("configure_substeps")(object(), None)

    def test_cleanup_preserves_user_objects(self):
        user = types.SimpleNamespace(Name="User Support", Delete=Mock())
        automated = types.SimpleNamespace(Name="MCP Fixed Support", Delete=Mock())
        function = actual_function("remove_previous_automation")
        function(types.SimpleNamespace(Children=[user, automated]), types.SimpleNamespace(Children=[]))
        user.Delete.assert_not_called()
        automated.Delete.assert_called_once()

    def test_cleanup_failure_is_not_silently_ignored(self):
        automated = types.SimpleNamespace(Name="MCP Fixed Support", Delete=Mock(side_effect=RuntimeError("locked object")))
        with self.assertRaisesRegex(RuntimeError, "Cannot remove"):
            actual_function("remove_previous_automation")(
                types.SimpleNamespace(Children=[automated]), types.SimpleNamespace(Children=[]))

    def test_solver_logs_ignore_old_attempt_and_bound_size(self):
        import os
        import time
        with tempfile.TemporaryDirectory(prefix="solver_log_test_", dir=Path(__file__).parent) as directory:
            folder = Path(directory)
            (folder / "file.err").write_text("old singular matrix error")
            os.utime(folder / "file.err", (1, 1))
            (folder / "solve.out").write_text("x" * 80000 + "failed to converge")
            tails = actual_function("solver_log_tails", os=os, ATTEMPT_STARTED=time.time() - 10)(
                types.SimpleNamespace(WorkingDir=directory))
        self.assertNotIn("file.err", tails)
        self.assertEqual(len(tails["solve.out"]), 65536)
        self.assertTrue(tails["solve.out"].endswith("failed to converge"))


if __name__ == "__main__":
    unittest.main()
