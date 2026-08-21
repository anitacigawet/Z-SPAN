"""Build hook: vendor the canonical prompts corpus into built distributions.

The CLI synthesizes through prompts/<type>.md VERBATIM (CLI spec call #7).
Run-from-clone resolves the corpus repo-relative; the pip form has no repo,
so build time copies an explicit allowlist of prompts from the repo corpus
into zspan_cli/_prompts/ (shipped as package data; resolve_prompts_dir's
last candidate). The single source of truth stays the repo corpus — the
copy exists only inside built wheels, and clones never even consult it
(repo-relative wins first).

Prompts are copied by an explicit ALLOWLIST, never a wildcard. A previous
wildcard copy (`sorted(src.glob("*.md"))`) pulled everything under
prompts/ into the wheel, including internal review material that was
never meant to be distributed. Now:
  1. Every build EMPTIES zspan_cli/_prompts/ first so stale files from a
     previous build can't persist (defense against local-tree drift).
  2. Only files in RUNTIME_PROMPTS are copied. Anything not on the list
     never ships — no PII, no operator-only ledger, no experimental
     drafts.
  3. If a runtime-required prompt is missing from the source corpus,
     the build fails loudly rather than silently shipping a broken CLI.

Both shipped install paths run this hook with the full repo tree present:
`pip install git+...#subdirectory=02_Core_Project/zspan_cli` builds inside
a full clone, and the release wheel is built from this working tree.
"""
import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


# The exact set of prompts the CLI needs at runtime. Adding here is a
# deliberate act — every prompt in this list ships publicly inside every
# built wheel. Personal identifying material, operator-only ledgers,
# experimental drafts, and internal review notes NEVER go on this list.
#
# What each one is:
#   synopsis.md, key_decisions.md, community_calls_to_action.md,
#   episode_tagline.md — the four rendered output types (per
#   RENDERED_OUTPUT_TYPES in synthesize.py).
#   _civic_meeting_sections.md, _design.md — shared partials the
#   above templates include.
#   rag_search_v1.md — the Librarian (BYOK RAG) mode prompt (per
#   serve.py:1574).
#   README.md — public documentation for the corpus.
RUNTIME_PROMPTS = frozenset({
    "synopsis.md",
    "key_decisions.md",
    "community_calls_to_action.md",
    "episode_tagline.md",
    "_civic_meeting_sections.md",
    "_design.md",
    "rag_search_v1.md",
    "README.md",
})


class BuildPyWithPrompts(build_py):
    def run(self):
        pkg_dir = Path(__file__).parent / "zspan_cli"
        vendor = pkg_dir / "_prompts"
        src = Path(__file__).resolve().parents[1] / "prompts"

        # Empty _prompts first so stale files left by an earlier build can
        # never ride along in the wheel.
        if vendor.is_dir():
            for existing in vendor.iterdir():
                if existing.is_file():
                    existing.unlink()
                elif existing.is_dir():
                    shutil.rmtree(existing)

        if src.is_dir():
            vendor.mkdir(exist_ok=True)
            missing = []
            for name in sorted(RUNTIME_PROMPTS):
                candidate = src / name
                if not candidate.is_file():
                    missing.append(name)
                    continue
                shutil.copy2(candidate, vendor / name)
            if missing:
                raise RuntimeError(
                    "setup.py RUNTIME_PROMPTS references files not present "
                    f"in {src}: {missing}. The build is refusing to ship a "
                    "wheel with a broken CLI — either add the missing "
                    "prompts to the corpus or trim RUNTIME_PROMPTS."
                )

        super().run()


setup(cmdclass={"build_py": BuildPyWithPrompts})
