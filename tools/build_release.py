"""Build a source-only GitHub archive after offline validation."""
from __future__ import annotations

import argparse
import hashlib
import os
import tempfile
import zipfile
from pathlib import Path

from validate_repo import ROOT, release_paths, validate_repo


def build_release(root: Path, output: Path, force: bool = False) -> tuple[Path, str]:
    root, output = root.resolve(), output.resolve()
    errors = validate_repo(root)
    if errors:
        raise ValueError("Release validation failed:\n" + "\n".join(errors))
    if output.suffix.lower() != ".zip":
        raise ValueError("Output must be a .zip archive")
    checksum = output.with_suffix(output.suffix + ".sha256")
    if output in [(root / relative).resolve() for relative in release_paths()]:
        raise ValueError("Output cannot replace a source file")
    if not force and (output.exists() or checksum.exists()):
        raise FileExistsError("Output/checksum already exists; choose another path or explicitly pass --force")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix="skill_release_", suffix=".zip", dir=output.parent)
    temporary = Path(temporary_name)
    os.close(descriptor)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for relative in release_paths():
                source = root / relative
                if source.is_symlink() or not source.resolve().is_relative_to(root):
                    raise ValueError("Release source must remain inside the repository")
                archive.write(source, (Path("ansys-workbench-static-skill") / relative).as_posix())
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        temporary.replace(output)
        checksum.write_text(f"{digest}  {output.name}\n", encoding="ascii")
    finally:
        if temporary.exists():
            temporary.unlink()
    return output, digest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--force", action="store_true", help="Replace an earlier generated archive/checksum")
    args = parser.parse_args()
    output, digest = build_release(ROOT, args.output, args.force)
    print(f"Built {output.name}: {len(release_paths())} source files")
    print(f"SHA256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
