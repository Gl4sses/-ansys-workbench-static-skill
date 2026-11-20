"""Checks observable release contents and rejects altered/private packages."""
from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_release import build_release
from validate_repo import ROOT, PLUGIN, release_paths, validate_repo


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="ansys_skill_package_")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "repo"
        for relative in release_paths():
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)

    def test_archive_contains_installable_skill_not_runtime_or_private_config(self):
        runtime = self.root / "skills/ansys-workbench-static/scripts/runtime"
        runtime.mkdir()
        (runtime / "private.local.json").write_text('{"case":"private"}', encoding="utf-8")
        output = self.root.parent / "release.zip"
        path, digest = build_release(self.root, output)
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            expected = {"ansys-workbench-static-skill/" + p.as_posix() for p in release_paths()}
            self.assertEqual(names, expected)
            self.assertTrue(all("/runtime/" not in name for name in names))
            self.assertIsNone(archive.testzip())
        self.assertEqual(digest, hashlib.sha256(output.read_bytes()).hexdigest())
        self.assertIn(digest, output.with_suffix(".zip.sha256").read_text(encoding="ascii"))

    def test_modified_plugin_fails_validation_and_prevents_build(self):
        target = self.root / PLUGIN / "main.py"
        target.write_text(target.read_text(encoding="utf-8") + "\n# altered\n", encoding="utf-8")
        self.assertTrue(any("checksum mismatch" in error for error in validate_repo(self.root)))
        with self.assertRaises(ValueError):
            build_release(self.root, self.root.parent / "invalid.zip")

    def test_private_home_path_or_config_is_rejected_without_echoing_contents(self):
        target = self.root / "README.md"
        private_path = "C:" + chr(92) + "Users" + chr(92) + "PrivatePerson" + chr(92) + "case"
        target.write_text(target.read_text(encoding="utf-8") + "\n" + private_path + "\n", encoding="utf-8")
        (self.root / ".env").write_text("PRIVATE_VALUE=not-for-release\n", encoding="utf-8")
        errors = validate_repo(self.root)
        self.assertTrue(any("personal Windows home" in error for error in errors))
        self.assertTrue(any("Private config" in error for error in errors))
        self.assertNotIn("PrivatePerson", "\n".join(errors))
        self.assertNotIn("not-for-release", "\n".join(errors))

    def test_existing_archive_is_preserved_without_force(self):
        output = self.root.parent / "existing.zip"
        output.write_bytes(b"preserve")
        with self.assertRaises(FileExistsError):
            build_release(self.root, output)
        self.assertEqual(output.read_bytes(), b"preserve")


if __name__ == "__main__":
    unittest.main()
