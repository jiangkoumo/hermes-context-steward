#!/usr/bin/env python3
"""Offline local checks: unit tests, then the real `hermes plugins validate`.

Everything mutable stays inside this repository, and the validator is invoked as a
subprocess (never imported) so this script introduces no private core imports of
its own. Point `--hermes-root` at any Hermes checkout — including a clean one — to
reproduce the catalog CI shape.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


def _payload_copy(root: Path, staging: Path) -> Path:
    """A copy of the tracked tree only: no ignored artifacts, like a fresh clone."""
    payload = staging / "context-steward"
    shutil.copytree(root, payload, ignore=shutil.ignore_patterns(
        ".git", ".tmp", ".test-hermes", "__pycache__", "*.py[cod]", ".pytest_cache",
    ))
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-root", type=Path,
                        default=Path.home() / ".hermes" / "hermes-agent")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    hermes_root = Path(args.hermes_root).resolve()
    for name in (".tmp", ".test-hermes"):
        (root / name).mkdir(exist_ok=True)
    sys.dont_write_bytecode = True
    env = dict(os.environ)
    env.update(
        PYTHONDONTWRITEBYTECODE="1", HERMES_DISABLE_LAZY_INSTALLS="1",
        HERMES_HOME=str(root / ".test-hermes"), TMPDIR=str(root / ".tmp"),
        PYTHONPATH=os.pathsep.join((str(root), str(hermes_root))),
    )
    # The in-process test run needs the same isolation as the validator subprocess:
    # without HERMES_HOME/HERMES_DISABLE_LAZY_INSTALLS here, importing host modules
    # would activate the user's own runtime.
    os.environ.update({key: env[key] for key in
                       ("PYTHONDONTWRITEBYTECODE", "HERMES_DISABLE_LAZY_INSTALLS",
                        "HERMES_HOME", "TMPDIR", "PYTHONPATH")})
    sys.path[:0] = [str(root), str(hermes_root)]
    if not args.validate_only:
        result = unittest.TextTestRunner(verbosity=2).run(
            unittest.defaultTestLoader.discover(str(root / "tests"))
        )
        if not result.wasSuccessful():
            return 1
    with tempfile.TemporaryDirectory(dir=root / ".tmp") as staging:
        payload = _payload_copy(root, Path(staging))
        completed = subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", "plugins", "validate", str(payload)],
            cwd=payload, env=env, capture_output=True, text=True,
        )
    print(completed.stdout, end="")
    if completed.stderr.strip():
        print(completed.stderr, end="", file=sys.stderr)
    print(f"Validation: {'PASS' if completed.returncode == 0 else 'FAIL'}")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
