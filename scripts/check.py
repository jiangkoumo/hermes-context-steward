#!/usr/bin/env python3
"""Offline local checks with all mutable Hermes/test state inside this repo."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import unittest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-root", type=Path,
                        default=Path.home() / ".hermes" / "hermes-agent")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    for name in (".tmp", ".test-hermes"):
        (root / name).mkdir(exist_ok=True)
    sys.dont_write_bytecode = True
    os.environ.update(
        PYTHONDONTWRITEBYTECODE="1", HERMES_DISABLE_LAZY_INSTALLS="1",
        HERMES_HOME=str(root / ".test-hermes"), TMPDIR=str(root / ".tmp"),
    )
    # Needed for the real Hermes validator's isolated subprocess probe as well.
    os.environ["PYTHONPATH"] = os.pathsep.join((str(root), str(args.hermes_root)))
    sys.path[:0] = [str(root), str(args.hermes_root)]
    if not args.validate_only:
        result = unittest.TextTestRunner(verbosity=2).run(
            unittest.defaultTestLoader.discover(str(root / "tests"))
        )
        if not result.wasSuccessful():
            return 1
    from hermes_cli.plugin_validate import validate_plugin_dir

    report = validate_plugin_dir(root)
    for name, ok, detail in report.checks:
        print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")
    for warning in report.warnings:
        print(f"WARNING {warning}")
    print(f"Validation: {'PASS' if report.ok else 'FAIL'}; "
          f"{len(report.checks)} checks, {len(report.warnings)} warnings")
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
