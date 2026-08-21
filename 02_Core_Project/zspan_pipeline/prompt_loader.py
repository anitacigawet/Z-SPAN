"""Prompt-file loader utilities.

Extracted from the retired client module as part of the S-109
removal (2026-07-01) so fetcher.py + other consumers can load prompt
files with front-matter without pulling in the retired client class.

Prompt files live in `02_Core_Project/prompts/` and have YAML-ish
front-matter followed by the instruction body.
"""
from __future__ import annotations

from pathlib import Path


MODEL_CONTENT_START = "<!-- ZSPAN_MODEL_CONTENT_START -->"
MODEL_CONTENT_END = "<!-- ZSPAN_MODEL_CONTENT_END -->"


def strip_explicit_model_boundaries(text: str) -> str:
    """Return only the text inside the optional explicit prompt boundary."""
    content_start = text.find(MODEL_CONTENT_START)
    if content_start != -1:
        text = text[content_start + len(MODEL_CONTENT_START):]
    content_end = text.find(MODEL_CONTENT_END)
    if content_end != -1:
        text = text[:content_end]
    return text.strip()


def _parse_simple_yaml(text: str) -> dict:
    """Minimal YAML-ish parser for prompt front-matter when PyYAML isn't
    available. Handles top-level `key: value` and one level of nesting
    under `key:`. Indentation must be consistent; values are strings."""
    result: dict = {}
    cur_key: str | None = None
    cur_indent: int | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.lstrip()
        if ":" not in stripped:
            continue
        key, _, val = stripped.partition(":")
        key = key.strip()
        val = val.strip()
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1]
        if indent == 0:
            if val:
                result[key] = val
                cur_key = None
                cur_indent = None
            else:
                result[key] = {}
                cur_key = key
                cur_indent = None
        else:
            if cur_key is None:
                continue
            if cur_indent is None:
                cur_indent = indent
            if isinstance(result.get(cur_key), dict):
                result[cur_key][key] = val
    return result


def load_prompt_with_meta(prompt_filename: str) -> tuple[dict, str]:
    """Load a prompt file. Returns (front_matter_dict, body_text).

    Front-matter is parsed leniently: YAML if PyYAML is available,
    otherwise `_parse_simple_yaml` covers the simple cases we need.

    The body has the bridge's `## Instructions (sent to Studio)` or
    similar heading stripped. Explicit ``MODEL_CONTENT_START`` and
    ``MODEL_CONTENT_END`` markers can bound heading-less prompts; the end
    marker also separates model-facing content from human-facing notes.
    Unmarked instruction files retain the legacy next-``##`` cutoff as a
    fail-safe.
    """
    prompts_dir = Path(__file__).resolve().parent.parent / "prompts"
    path = (prompts_dir / prompt_filename).resolve()
    # Path containment (defense-in-depth) — mirrors qdrant_synthesizer's
    # load_canonical_prompt. Callers currently pass filenames from a static
    # registry, but this closes any latent traversal via prompt_filename.
    try:
        path.relative_to(prompts_dir.resolve())
    except ValueError:
        raise ValueError(
            f"Invalid prompt_filename {prompt_filename!r}: escapes the prompts directory"
        )
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")

    text = path.read_text(encoding="utf-8")

    front_matter_raw = ""
    body = text
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            front_matter_raw = text[3:end].strip()
            body = text[end + 3:].lstrip()

    meta: dict = {}
    try:
        import yaml  # type: ignore
        meta = yaml.safe_load(front_matter_raw) or {}
    except ImportError:
        meta = _parse_simple_yaml(front_matter_raw)
    except Exception:
        meta = {}

    instructions_heading_markers = (
        "## Instructions (sent to Studio)",
        "## Instructions (sent as the chat query / configure prompt)",
        "## Instructions (sent as the synthesis prompt)",
        "## Instructions (sent to NotebookLM)",
        "## Instructions (sent to the model)",
        "## STRUCTURAL GUIDANCE — sent to Studio",
        "## STRUCTURAL GUIDANCE",
        "## DESIGN BLOCK — sent to Studio",
        "## DESIGN BLOCK",
        "## Instructions",
    )
    heading_found = False
    explicit_start = body.find(MODEL_CONTENT_START)
    if explicit_start != -1:
        body = body[explicit_start + len(MODEL_CONTENT_START):].lstrip()
    else:
        for marker in instructions_heading_markers:
            idx = body.find(marker)
            if idx != -1:
                body = body[idx + len(marker):].lstrip()
                heading_found = True
                break

    content_end = body.find(MODEL_CONTENT_END)
    if content_end != -1:
        body = strip_explicit_model_boundaries(body)
    elif heading_found:
        next_heading = body.find("\n## ")
        if next_heading != -1:
            body = body[:next_heading].strip()

    return meta, body.strip()


def load_prompt_file(prompt_filename: str) -> str:
    """Convenience: load just the body of a prompt file
    (front-matter stripped)."""
    _, body = load_prompt_with_meta(prompt_filename)
    return body
