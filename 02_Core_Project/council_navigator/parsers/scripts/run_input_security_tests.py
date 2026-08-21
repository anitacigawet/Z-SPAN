#!/usr/bin/env python3.11
"""Run the S-008 V0 input-security test pass.

Discovers every ``test_*.py`` module under ``parsers/input_security/`` and
fires the unittest suite. Exits non-zero on any failure.

Usage::

    python3.11 parsers/scripts/run_input_security_tests.py

Or, with verbose output::

    python3.11 parsers/scripts/run_input_security_tests.py -v

This is the CI-style discipline named in
``01_Project_Overview/S008_INPUT_SECURITY_SPEC.md`` chunk 2. Run it after
every related code change. ``parsers/INPUT_SECURITY_TESTS.md`` documents
how the test pass fits into the V0 acceptance gate.
"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path


# Resolve the parsers root regardless of where this script is invoked from.
PARSERS_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = PARSERS_ROOT.parent
INPUT_SECURITY_DIR = PARSERS_ROOT / "input_security"


def _ensure_importable() -> None:
    """Make sure ``parsers.*`` imports resolve.

    The script runs from ``parsers/scripts/`` so we need the parent
    directory on sys.path for ``parsers.input_security.*`` to import.
    """
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))


def discover_suites(verbosity: int) -> unittest.TestSuite:
    if not INPUT_SECURITY_DIR.exists():
        raise SystemExit(
            f"input_security directory not found at {INPUT_SECURITY_DIR}; "
            "C2.0 foundation must land first"
        )
    loader = unittest.TestLoader()
    suite = loader.discover(
        start_dir=str(INPUT_SECURITY_DIR),
        pattern="test_*.py",
        top_level_dir=str(PROJECT_ROOT),
    )
    return suite


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="S-008 V0 input-security test runner"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="verbose test output",
    )
    args = parser.parse_args(argv)

    _ensure_importable()

    suite = discover_suites(verbosity=2 if args.verbose else 1)
    runner = unittest.TextTestRunner(
        verbosity=2 if args.verbose else 1,
        stream=sys.stdout,
    )
    result = runner.run(suite)

    print("-" * 60)
    print(
        f"S-008 V0 input-security pass: "
        f"{result.testsRun} run, "
        f"{len(result.failures)} failed, "
        f"{len(result.errors)} errored, "
        f"{len(result.skipped)} skipped"
    )
    if not result.wasSuccessful():
        print("FAIL - S-008 V0 acceptance gate NOT clear")
        return 1
    print("OK - S-008 V0 acceptance gate clear for the surfaces tested")
    return 0


if __name__ == "__main__":
    sys.exit(main())
