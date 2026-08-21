"""S-012 chunk 8 V0 — launch-day placeholder scanner tests.

Exercises `parsers.scripts.check_creator_placeholders`:
- A directory with no placeholder strings → exit 0.
- A directory with placeholder strings outside doc-references → exit 1.
- A directory where placeholder strings appear ONLY in doc-reference
  paths → exit 0 (doc references are intentionally excluded).
- The scanner respects EXCLUDED_DIR_NAMES (does not descend into .git/).

Per [D-100](../../../../01_Project_Overview/DECISIONS.md#d-100): defensive
unit tests for the pre-flight gate.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType


_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _load_module() -> ModuleType:
    path = _SCRIPTS_DIR / "check_creator_placeholders.py"
    if not path.exists():
        raise unittest.SkipTest(f"{path} not found")
    spec = importlib.util.spec_from_file_location(
        "_zspan_check_creator_placeholders_under_test", path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("spec build failed")
    module = importlib.util.module_from_spec(spec)
    sys.modules["_zspan_check_creator_placeholders_under_test"] = module
    spec.loader.exec_module(module)
    return module


class PlaceholderScanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_module()

    def _write(self, root: Path, rel: str, content: str) -> None:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_clean_tree_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._write(tmp_path, "client/src/CreatorSignup.tsx",
                        "const TOS = 'Real Terms of Service text here.';")
            rc = self.mod.main(["--root", str(tmp_path)])
        self.assertEqual(rc, 0)

    def test_placeholder_in_user_facing_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._write(
                tmp_path,
                "client/src/CreatorSignup.tsx",
                "const TOS = 'placeholder terms of service';",
            )
            rc = self.mod.main(["--root", str(tmp_path)])
        self.assertEqual(rc, 1)

    def test_doc_reference_path_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # ACCOUNT_SYSTEM_SPEC.md is in the DOC_REFERENCE_PATHS allowlist.
            self._write(
                tmp_path,
                "01_Project_Overview/ACCOUNT_SYSTEM_SPEC.md",
                "We ship the literal string 'placeholder terms of service'.",
            )
            rc = self.mod.main(["--root", str(tmp_path)])
        self.assertEqual(rc, 0)

    def test_excluded_dirs_not_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # placeholder string inside an excluded dir → should not fail.
            self._write(
                tmp_path,
                "node_modules/some-pkg/index.js",
                "var x = 'placeholder terms of service';",
            )
            rc = self.mod.main(["--root", str(tmp_path)])
        self.assertEqual(rc, 0)

    def test_disclaimer_text_variant_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._write(
                tmp_path,
                "client/src/Disclaimer.tsx",
                "const DISC = 'placeholder disclaimer text v1';",
            )
            rc = self.mod.main(["--root", str(tmp_path)])
        self.assertEqual(rc, 1)

    def test_case_insensitive_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            self._write(
                tmp_path,
                "client/src/X.tsx",
                "const TOS = 'PLACEHOLDER TERMS OF SERVICE';",
            )
            rc = self.mod.main(["--root", str(tmp_path)])
        self.assertEqual(rc, 1)

    def test_binary_file_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # .png file with the literal bytes — should be skipped by
            # suffix filter.
            (tmp_path / "image.png").write_bytes(
                b"placeholder terms of service raw bytes"
            )
            rc = self.mod.main(["--root", str(tmp_path)])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
