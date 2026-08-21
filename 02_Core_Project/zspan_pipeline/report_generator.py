"""S-122 cited-report generator — Report-V0-1 (branded template, no Stitch).

Turns one operator natural-language query into a single-file, source-cited
HTML civic report. Rides the V1.5-OperatorSearch-1 machinery unchanged up
to and including `operator_search.dedup_and_rerank_chunks`, then forks:
instead of one cross-meeting chat answer, it runs one `claude -p` Sonnet
pass per report section (prompts/report_*.md) over the shared ranked-chunk
union, renders the outputs into a self-contained dark-broadcast-aesthetic
HTML artifact with `[City · YYYY-MM-DD · MM:SS]` citation chips that
deep-link to BroadcastPage moments, and stamps the whole thing with an
umbrella `kind="report"` provenance row so `/api/verify-run/{run_id}`
covers the artifact end-to-end.

Neutrality shape per D-144: the section taxonomy is record-not-pitch
(synopsis / findings / jurisdictions / quotes / decisions / methodology /
sources). The fractal-framework lineage's advocacy sections were
deliberately not ported — see S-122.

Safety per S-119 (portable-artifact amplification): the section prompts
carry the private-citizen guard from first draft; this module additionally
never emits meeting_id integers into visible artifact prose (chips carry
them only inside href URLs, which is how BroadcastPage links work
everywhere).

V0.5 (Report-Stitch-1) layers Stitch generative chrome on top of this
module's outputs; render_report_html() is the fixed-template fallback that
per the source project's own architecture must always keep working.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .prompt_loader import strip_explicit_model_boundaries

logger = logging.getLogger(__name__)

REPORT_PIPELINE_VERSION = "v0-report-2026-07-02"

# prompts/ lives at 02_Core_Project/prompts/ — sibling of zspan_pipeline/.
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Absolute-link base for citation chips + verify pointers in the portable
# artifact. Overridable for lab/tunnel testing.
DEFAULT_PUBLIC_BASE_URL = "https://zspan.org"


def public_base_url() -> str:
    return os.environ.get("ZSPAN_PUBLIC_BASE_URL", DEFAULT_PUBLIC_BASE_URL).rstrip("/")


# ── Section registry ────────────────────────────────────────────────────
# Ordered. (key, prompt stem, artifact heading, include_searched_cities).
# The jurisdictions section receives the full searched-city list so its
# per-city honest-empty rule ("searched, nothing there" ≠ "not searched")
# has the ground truth to enforce.
REPORT_SECTIONS: list[tuple[str, str, str, bool]] = [
    ("synopsis", "report_synopsis", "Executive synopsis", False),
    ("findings", "report_findings", "Findings", False),
    ("jurisdictions", "report_jurisdictions", "By jurisdiction", True),
    ("quotes", "report_quotes", "Key quotes", False),
    ("decisions", "report_decisions", "Decisions & votes", False),
]


class SectionPromptMissing(FileNotFoundError):
    """A report_*.md prompt file is absent — fail loud, never synthesize
    a section without its reviewed-or-review-queued template."""


def load_section_prompt(stem: str) -> tuple[str, str]:
    """Load prompts/<stem>.md → (body, version).

    Frontmatter-strips like rag_search.load_prompt_template; the version
    is parsed from the frontmatter `version:` line (falls back to the
    pipeline version so provenance never carries an empty field).
    """
    p = PROMPTS_DIR / f"{stem}.md"
    if not p.exists():
        raise SectionPromptMissing(
            f"Report section prompt missing at {p} — S-122 Report-V0-1 "
            f"expects the report_*.md family in prompts/."
        )
    text = p.read_text(encoding="utf-8")
    version = REPORT_PIPELINE_VERSION
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            frontmatter, body = parts[1], parts[2]
            m = re.search(r"^version:\s*(\S+)\s*$", frontmatter, re.MULTILINE)
            if m:
                version = m.group(1)
    return strip_explicit_model_boundaries(body), version


# ── Context block (appended below each section prompt body) ─────────────

def _format_timecode(seconds: Optional[float]) -> str:
    s = int(seconds or 0)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _scope_label(interpretation: dict) -> str:
    bits = [
        interpretation.get(k)
        for k in ("state", "county", "city")
        if interpretation.get(k)
    ]
    return " · ".join(bits) if bits else "all locations"


def build_section_context(
    *,
    query: str,
    interpretation: dict,
    ranked: list[dict],
    searched_cities: Optional[list[str]] = None,
    contributing_cities: Optional[list[str]] = None,
) -> str:
    """The shared context block: query + scope + chunk blocks.

    Chunk headers carry the [City · YYYY-MM-DD] source tag + a timecode=
    field (report_quotes.md's three-part citations read it). Unlike the
    operator-search synthesis prompt, meeting_id is NOT shown — the
    renderer resolves (city, date) → meeting_id itself, and a portable
    artifact wants less internal plumbing in the model's view, not more.
    """
    chunk_blocks: list[str] = []
    for i, pair in enumerate(ranked, start=1):
        leg = pair["leg"]
        c = pair["chunk"]
        tag = f"{leg.city_name} · {leg.meeting_date}"
        body = c.body if isinstance(c.body, str) else str(c.body)
        chunk_blocks.append(
            f"[CHUNK {i}] [{tag}] [timecode={_format_timecode(c.start_seconds)}]\n{body}"
        )
    chunks_text = "\n\n".join(chunk_blocks)

    meeting_count = len({pair["leg"].meeting_id for pair in ranked})
    lines = [
        f'Operator query: "{query}"',
        "",
        f"Scope: {_scope_label(interpretation)}",
    ]
    if searched_cities is not None:
        lines += [
            "",
            "Cities searched in this scope (every one of these must appear "
            f"in your output, per the honest-empty rule): {', '.join(searched_cities)}",
        ]
        if contributing_cities is not None:
            lines.append(
                "Cities whose meetings contributed chunks below: "
                + (", ".join(contributing_cities) if contributing_cities else "none")
            )
    lines += [
        "",
        f"Sources retrieved ({len(ranked)} chunks across {meeting_count} "
        f"meeting{'s' if meeting_count != 1 else ''}):",
        "",
        chunks_text,
    ]
    return "\n".join(lines)


def build_section_prompt(section_body: str, context_block: str) -> str:
    return f"{section_body}\n\n---\n\n{context_block}"


# ── Citation chips (server-side render of the client grammar) ───────────
# Mirrors OperatorSearchModal.tsx's CITATION_RE: [City · YYYY-MM-DD] with
# an optional third `· MM:SS` / `· H:MM:SS` segment. Here the timecode is
# NOT swallowed — it drives the ?t= deep-link so a report chip lands on
# the moment, not just the meeting.
CITATION_RE = re.compile(
    r"\[([A-Z][A-Za-z .'\-]+?)\s*·\s*(\d{4}-\d{2}-\d{2})"
    r"(?:\s*·\s*(\d{1,2}:\d{2}(?::\d{2})?))?\]"
)


def _timecode_to_seconds(tc: str) -> int:
    parts = [int(p) for p in tc.split(":")]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return parts[0] * 60 + parts[1]


def build_meeting_lookup(citations: list[dict]) -> dict[tuple[str, str], dict]:
    """(city_lower, date) → {meeting_id, best_start_seconds, chunks[]}.

    best_start_seconds is the highest-scored chunk's start — the chip's
    landing moment when the inline tag carries no explicit timecode.
    Citations arrive pre-sorted by score desc (ranked order), so the
    first chunk seen per meeting is the best one.
    """
    lookup: dict[tuple[str, str], dict] = {}
    for c in citations:
        key = ((c["city_name"] or "").lower(), c["meeting_date"] or "")
        entry = lookup.setdefault(
            key,
            {
                "meeting_id": c["meeting_id"],
                "city_name": c["city_name"],
                "meeting_date": c["meeting_date"],
                "best_start_seconds": c.get("start_seconds") or 0,
                "video_url": c.get("video_url"),
                "chunks": [],
            },
        )
        entry["chunks"].append(c)
    return lookup


def _broadcast_href(base_url: str, meeting_id: int, seconds: Optional[int]) -> str:
    href = f"{base_url}/?view=broadcast&meetingId={meeting_id}"
    if seconds is not None and seconds > 0:
        href += f"&t={int(seconds)}"
    return href


def render_citation_chip(
    city: str,
    date: str,
    timecode: Optional[str],
    lookup: dict[tuple[str, str], dict],
    base_url: str,
) -> str:
    entry = lookup.get((city.lower(), date))
    label = f"{html.escape(city)} · {html.escape(date)}"
    if timecode:
        label += f" · {html.escape(timecode)}"
    if entry is None:
        # Honest-unresolved — same posture as the source project's bare
        # chips: visibly a citation, visibly not linkable.
        return (
            f'<span class="cite cite-unresolved" '
            f'title="Source tag did not resolve to a retrieved meeting">{label}</span>'
        )
    seconds = (
        _timecode_to_seconds(timecode)
        if timecode
        else int(entry["best_start_seconds"] or 0)
    )
    href = _broadcast_href(base_url, entry["meeting_id"], seconds)
    tip = (
        f"{html.escape(entry['city_name'])} council meeting, "
        f"{html.escape(entry['meeting_date'])} — opens the broadcast page "
        f"at {_format_timecode(seconds)}"
    )
    return (
        f'<a class="cite" href="{html.escape(href)}" target="_blank" '
        f'rel="noopener noreferrer" title="{tip}">{label}</a>'
    )


def _render_inline(
    text: str,
    lookup: dict[tuple[str, str], dict],
    base_url: str,
) -> str:
    """Escape + inline-render one run of text: citations first (on the RAW
    text so the grammar never fights escaped entities), then **bold** /
    *italic* on the escaped remainder."""
    out: list[str] = []
    pos = 0
    for m in CITATION_RE.finditer(text):
        out.append(_render_emphasis(html.escape(text[pos : m.start()])))
        out.append(
            render_citation_chip(m.group(1).strip(), m.group(2), m.group(3), lookup, base_url)
        )
        pos = m.end()
    out.append(_render_emphasis(html.escape(text[pos:])))
    return "".join(out)


_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")


def _render_emphasis(escaped: str) -> str:
    escaped = _BOLD_RE.sub(r"<strong>\1</strong>", escaped)
    return _ITALIC_RE.sub(r"<em>\1</em>", escaped)


# ── Markdown → HTML (the bounded subset the prompts request) ────────────
# Hand-rolled for the same reason the client hand-rolls its renderer: the
# surface is bounded (### headings, lists, blockquotes, paragraphs, hr)
# and citation-chip tokenization needs control of the inline pass. No new
# dependency per CLAUDE.md.

def render_markdown_section(
    md: str,
    lookup: dict[tuple[str, str], dict],
    base_url: str,
) -> str:
    inline: Callable[[str], str] = lambda s: _render_inline(s, lookup, base_url)
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped:
            i += 1
            continue
        if re.match(r"^---+$", stripped):
            out.append("<hr>")
            i += 1
            continue
        h = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if h:
            level = min(len(h.group(1)) + 1, 5)  # ### city → h4 under the h2 section
            out.append(f"<h{level}>{inline(h.group(2))}</h{level}>")
            i += 1
            continue
        if re.match(r"^\d+[.)]\s+", stripped):
            items = []
            while i < len(lines) and re.match(r"^\d+[.)]\s+", lines[i].strip()):
                items.append(re.sub(r"^\d+[.)]\s+", "", lines[i].strip()))
                i += 1
            out.append(
                "<ol>" + "".join(f"<li>{inline(it)}</li>" for it in items) + "</ol>"
            )
            continue
        if re.match(r"^[-*]\s+", stripped):
            items = []
            while i < len(lines) and re.match(r"^[-*]\s+", lines[i].strip()):
                items.append(re.sub(r"^[-*]\s+", "", lines[i].strip()))
                i += 1
            out.append(
                "<ul>" + "".join(f"<li>{inline(it)}</li>" for it in items) + "</ul>"
            )
            continue
        if stripped.startswith(">"):
            quote_lines = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote_lines.append(lines[i].strip().lstrip(">").strip())
                i += 1
            # Attribution convention from report_quotes.md: a trailing
            # "— …" line renders as the chip-carrying byline.
            body_ls = [l for l in quote_lines if l]
            attribution = None
            if body_ls and body_ls[-1].startswith("—"):
                attribution = body_ls.pop()
            quote_html = "<br>".join(inline(l) for l in body_ls)
            attr_html = (
                f'<footer class="quote-attr">{inline(attribution)}</footer>'
                if attribution
                else ""
            )
            out.append(f"<blockquote>{quote_html}{attr_html}</blockquote>")
            continue
        # Paragraph — accumulate until a blank/special line.
        para = [stripped]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if (
                not nxt
                or re.match(r"^---+$", nxt)
                or re.match(r"^#{1,4}\s+", nxt)
                or re.match(r"^[-*]\s+", nxt)
                or re.match(r"^\d+[.)]\s+", nxt)
                or nxt.startswith(">")
            ):
                break
            para.append(nxt)
            i += 1
        out.append(f"<p>{inline(' '.join(para))}</p>")
    return "\n".join(out)


# ── The artifact template ────────────────────────────────────────────────

_ARTIFACT_CSS = """
:root {
  --canvas: #0A0A0A; --surface: #141416; --surface-2: #18181A;
  --line: rgba(255,255,255,0.07); --line-strong: rgba(255,255,255,0.14);
  --ink: rgba(255,255,255,0.92); --ink-2: rgba(255,255,255,0.70);
  --ink-3: rgba(255,255,255,0.45); --ink-4: rgba(255,255,255,0.30);
  --civic-blue: #1A3A7C; --info-blue: #3B82F6;
  --accent: #E85C41; --green: #22C55E;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--canvas); color: var(--ink);
  font: 15px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  -webkit-font-smoothing: antialiased;
}
main { max-width: 860px; margin: 0 auto; padding: 48px 28px 80px; }
.eyebrow {
  font-size: 10px; letter-spacing: 0.22em; text-transform: uppercase;
  color: var(--ink-3); margin-bottom: 10px;
}
.eyebrow .zspan { color: var(--info-blue); font-weight: 700; }
h1 { font-size: 26px; line-height: 1.3; font-weight: 700; margin-bottom: 10px; }
.meta-line { font-size: 12.5px; color: var(--ink-3); margin-bottom: 6px; }
.coverage {
  margin: 26px 0 34px; padding: 14px 18px; background: var(--surface);
  border: 1px solid var(--line); border-radius: 10px;
  font-size: 13px; color: var(--ink-2);
}
section { margin-bottom: 38px; }
section > h2 {
  font-size: 13px; letter-spacing: 0.16em; text-transform: uppercase;
  color: var(--ink-3); border-bottom: 1px solid var(--line);
  padding-bottom: 8px; margin-bottom: 16px; font-weight: 600;
}
h4 { font-size: 15px; margin: 18px 0 6px; color: var(--ink); }
p { margin-bottom: 12px; color: var(--ink-2); }
p strong, li strong { color: var(--ink); }
ol, ul { margin: 0 0 12px 22px; color: var(--ink-2); }
li { margin-bottom: 10px; }
blockquote {
  margin: 16px 0; padding: 14px 18px; background: var(--surface);
  border-left: 3px solid var(--civic-blue); border-radius: 0 8px 8px 0;
  color: var(--ink); font-size: 15.5px;
}
.quote-attr { margin-top: 8px; font-size: 12.5px; color: var(--ink-3); }
hr { border: 0; border-top: 1px solid var(--line); margin: 18px 0; }
.cite {
  display: inline-block; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px; line-height: 1; padding: 3px 7px; margin: 0 2px;
  border: 1px solid var(--line-strong); border-radius: 999px;
  color: var(--info-blue); background: rgba(59,130,246,0.08);
  text-decoration: none; white-space: nowrap; vertical-align: baseline;
}
.cite:hover { background: rgba(59,130,246,0.18); border-color: var(--info-blue); }
.cite-unresolved { color: var(--ink-3); background: transparent; }
.src-card {
  padding: 14px 16px; background: var(--surface); border: 1px solid var(--line);
  border-radius: 10px; margin-bottom: 12px;
}
.src-card .src-title { font-size: 14.5px; font-weight: 600; color: var(--ink); }
.src-card .src-sub { font-size: 12px; color: var(--ink-3); margin: 3px 0 8px; }
.src-card a { color: var(--info-blue); text-decoration: none; }
.src-times { display: flex; flex-wrap: wrap; gap: 6px; }
.src-times a {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px;
  padding: 2px 7px; border: 1px solid var(--line); border-radius: 6px;
  color: var(--ink-2);
}
.src-times a:hover { border-color: var(--info-blue); color: var(--info-blue); }
.provenance {
  font-size: 12.5px; color: var(--ink-3); background: var(--surface);
  border: 1px solid var(--line); border-radius: 10px; padding: 16px 18px;
}
.provenance code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px;
  color: var(--ink-2); word-break: break-all;
}
.provenance ul { margin-left: 18px; margin-top: 8px; }
.provenance li { margin-bottom: 5px; }
footer.report-footer {
  margin-top: 44px; padding-top: 16px; border-top: 1px solid var(--line);
  font-size: 11.5px; color: var(--ink-4);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
@media print {
  :root { --canvas: #ffffff; --surface: #f6f6f7; --surface-2: #f0f0f2;
    --line: rgba(0,0,0,0.12); --line-strong: rgba(0,0,0,0.25);
    --ink: #111; --ink-2: #333; --ink-3: #555; --ink-4: #777; }
  body { font-size: 12.5px; }
  .cite { background: transparent; }
}
"""


def build_report_fragments(
    *,
    query: str,
    interpretation: dict,
    sections: dict[str, dict],
    citations: list[dict],
    leg_outcomes: dict,
    searched_cities: list[str],
    run_id: str,
    child_run_count: int,
    generated_at_utc: str,
    base_url: Optional[str] = None,
) -> dict:
    """Render the report's content pieces WITHOUT the page shell.

    Shared by render_report_html (the V0 fixed template) and the
    Report-Stitch-1 fragments endpoint — the Stitch driver injects these
    exact HTML fragments into the generative chrome, so both artifacts
    carry identical content, chips, and provenance. Returns JSON-able:
    {section_fragments: {key: {heading, html, status}}, sources_html,
     provenance_html, coverage_line, css, title, scope_label, meta_line,
     run_id}.
    """
    base = base_url or public_base_url()
    lookup = build_meeting_lookup(citations)

    section_fragments: dict[str, dict] = {}
    for key, _stem, heading, _needs in REPORT_SECTIONS:
        s = sections.get(key)
        if not s or s.get("status") != "ok" or not (s.get("markdown") or "").strip():
            # A failed section renders an honest failure line — never a
            # silent gap that reads as "nothing to report".
            body = (
                '<p class="section-failed" style="color:var(--ink-3);font-style:italic;">'
                "This section could not be generated for this run "
                f"({html.escape((s or {}).get('error') or 'no output')}). "
                "The rest of the report is unaffected.</p>"
            )
            status = "failed"
        else:
            body = render_markdown_section(s["markdown"], lookup, base)
            status = "ok"
        section_fragments[key] = {"heading": heading, "html": body, "status": status}

    # Sources — one card per contributing meeting, ranked-order chunks.
    src_cards: list[str] = []
    for entry in lookup.values():
        mid = entry["meeting_id"]
        chunk_links = "".join(
            f'<a href="{html.escape(_broadcast_href(base, mid, int(c.get("start_seconds") or 0)))}" '
            f'target="_blank" rel="noopener noreferrer">'
            f"{_format_timecode(c.get('start_seconds'))}</a>"
            for c in entry["chunks"][:8]
        )
        more = (
            f'<span style="font-size:11px;color:var(--ink-4);">+{len(entry["chunks"]) - 8} more</span>'
            if len(entry["chunks"]) > 8
            else ""
        )
        page_href = _broadcast_href(base, mid, None)
        video_note = (
            "source video available on the meeting page"
            if entry.get("video_url")
            else "no direct video archive for this meeting"
        )
        src_cards.append(
            f'<div class="src-card">'
            f'<div class="src-title">{html.escape(entry["city_name"])} — City Council meeting, {html.escape(entry["meeting_date"])}</div>'
            f'<div class="src-sub">{len(entry["chunks"])} transcript chunk{"s" if len(entry["chunks"]) != 1 else ""} '
            f"contributed · {video_note} · "
            f'<a href="{html.escape(page_href)}" target="_blank" rel="noopener noreferrer">open the meeting page</a></div>'
            f'<div class="src-times">{chunk_links}{more}</div>'
            f"</div>"
        )

    # Coverage line — honest counts incl. the legs that returned nothing.
    ok = leg_outcomes.get("ok_count", 0)
    nomatch = leg_outcomes.get("indexed_no_match_count", 0)
    down = leg_outcomes.get("qdrant_down_count", 0)
    coverage_bits = [
        f"{ok + nomatch + down} indexed meeting{'s' if ok + nomatch + down != 1 else ''} searched "
        f"across {len(searched_cities)} cit{'ies' if len(searched_cities) != 1 else 'y'} "
        f"({html.escape(', '.join(searched_cities))})",
        f"{len(lookup)} meeting{'s' if len(lookup) != 1 else ''} contributed {len(citations)} chunk{'s' if len(citations) != 1 else ''}",
    ]
    if nomatch:
        coverage_bits.append(f"{nomatch} searched meeting{'s' if nomatch != 1 else ''} had no matching content")
    if down:
        coverage_bits.append(
            f"{down} meeting{'s' if down != 1 else ''} could not be searched (retrieval node unreachable) — "
            "this report may be incomplete for those meetings"
        )
    coverage = " · ".join(coverage_bits)

    prompt_rows = "".join(
        f"<li><code>{html.escape(s.get('prompt_version') or '?')}</code> "
        f"(<code>{html.escape((s.get('prompt_hash') or '')[:23])}…</code>)</li>"
        for s in sections.values()
        if s.get("prompt_version")
    )
    verify_api = f"{base}/api/verify-run/{run_id}"
    audit_page = f"{base}/?view=audit"

    sources_html = (
        "".join(src_cards)
        if src_cards
        else '<p style="color:var(--ink-3);font-style:italic;">No meetings contributed content to this report.</p>'
    )
    provenance_html = f"""<div class="provenance">
      <p>This report was generated by Z-SPAN's retrieval pipeline from verbatim
      meeting transcripts: per-meeting semantic retrieval over indexed transcript
      chunks, followed by one synthesis pass per section
      (claude-sonnet-4-6) constrained to cite only the retrieved record.
      It was <strong>not</strong> reviewed by a human before generation completed.
      Verify citations before relying on them — every chip opens the source moment.</p>
      <ul>
        <li>Report run: <code>{html.escape(run_id)}</code></li>
        <li>Verify this run: <code>{html.escape(verify_api)}</code> (or the audit page at {html.escape(audit_page)})</li>
        <li>{child_run_count} per-meeting retrieval run{"s" if child_run_count != 1 else ""} recorded as children of this run</li>
        <li>Pipeline <code>{REPORT_PIPELINE_VERSION}</code> · section prompt versions: <ul>{prompt_rows}</ul></li>
      </ul>
      <p style="margin-top:10px;">A verify-run match confirms Z-SPAN's pipeline produced this
      content, unmodified — it attests origin and integrity. It does not by itself
      certify the underlying claims; the source layer (the cities' own meeting
      recordings, linked from each meeting page) is where the record itself is
      verifiable.</p>
    </div>"""

    scope = _scope_label(interpretation)
    return {
        "title": query,
        "scope_label": scope,
        "meta_line": f"Scope: {scope} · Generated {generated_at_utc} (UTC)",
        "coverage_line": coverage,
        "section_fragments": section_fragments,
        "sources_html": sources_html,
        "provenance_html": provenance_html,
        "css": _ARTIFACT_CSS,
        "run_id": run_id,
        "footer_line": f"{run_id} · generated by Z-SPAN · {base}",
    }


def render_report_html(
    *,
    query: str,
    interpretation: dict,
    sections: dict[str, dict],
    citations: list[dict],
    leg_outcomes: dict,
    searched_cities: list[str],
    run_id: str,
    child_run_count: int,
    generated_at_utc: str,
    base_url: Optional[str] = None,
) -> str:
    """Assemble the single-file V0 artifact from the shared fragments.
    sections[key] = {markdown, prompt_version, prompt_hash, status}."""
    frags = build_report_fragments(
        query=query,
        interpretation=interpretation,
        sections=sections,
        citations=citations,
        leg_outcomes=leg_outcomes,
        searched_cities=searched_cities,
        run_id=run_id,
        child_run_count=child_run_count,
        generated_at_utc=generated_at_utc,
        base_url=base_url,
    )
    section_html = "\n".join(
        f"<section><h2>{html.escape(f['heading'])}</h2>\n{f['html']}\n</section>"
        for f in frags["section_fragments"].values()
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Z-SPAN civic report — {html.escape(query[:80])}</title>
<style>{frags["css"]}</style>
</head>
<body>
<main>
  <div class="eyebrow"><span class="zspan">Z-SPAN</span> · CIVIC RECORD REPORT</div>
  <h1>{html.escape(query)}</h1>
  <div class="meta-line">{html.escape(frags["meta_line"])}</div>
  <div class="meta-line">Every citation chip links to the moment in the source meeting it came from.</div>
  <div class="coverage">{frags["coverage_line"]}</div>

{section_html}

  <section>
    <h2>Sources</h2>
    {frags["sources_html"]}
  </section>

  <section>
    <h2>Methodology &amp; provenance</h2>
    {frags["provenance_html"]}
  </section>

  <footer class="report-footer">
    {html.escape(frags["footer_line"])}
  </footer>
</main>
</body>
</html>"""


def fragments_for_stored_run(run: dict) -> dict:
    """Reconstitute build_report_fragments inputs from a report_runs row
    (the Report-Stitch-1 endpoint's path). searched_cities re-derives from
    meeting_ids; generated_at uses the row's updated_at."""
    _ensure_parsers_on_path()
    from database import get_connection

    meeting_ids = run.get("meeting_ids") or []
    searched_cities: list[str] = []
    if meeting_ids:
        conn = get_connection()
        try:
            placeholders = ",".join("?" * len(meeting_ids))
            rows = conn.execute(
                f"SELECT DISTINCT city_name FROM meetings WHERE id IN ({placeholders})",
                meeting_ids,
            ).fetchall()
        finally:
            conn.close()
        searched_cities = sorted(r[0] for r in rows if r[0])
    return build_report_fragments(
        query=run["query"],
        interpretation=run.get("interpretation") or {},
        sections=run.get("sections") or {},
        citations=run.get("citations") or [],
        leg_outcomes=run.get("leg_outcomes") or {},
        searched_cities=searched_cities,
        run_id=run.get("run_id") or run["id"],
        child_run_count=len(run.get("child_run_ids") or []),
        generated_at_utc=run.get("updated_at") or "",
    )


# ── The run driver ───────────────────────────────────────────────────────

def _ensure_parsers_on_path() -> None:
    """database.py lives in council_navigator/parsers — Flask already has
    it on sys.path; module-level smoke runs need the defensive insert."""
    import sys

    parsers = Path(__file__).resolve().parent.parent / "council_navigator" / "parsers"
    if str(parsers) not in sys.path:
        sys.path.insert(0, str(parsers))


def run_report_run(report_run_id: str) -> None:
    """Execute one report run end-to-end. Designed to run in a daemon
    thread spawned by the Flask endpoint; all state lands on the
    report_runs row (poll target) and all provenance in byok_audit_runs.
    Never raises — errors land on the row."""
    _ensure_parsers_on_path()
    # database is imported OUTSIDE the try: fail() needs update_report_run,
    # and if even this import breaks there is no row-writing possible —
    # log-only is the honest floor.
    from database import (
        get_report_run,
        update_report_run,
        save_byok_audit_run,
        apply_city_corrections,
    )

    def fail(msg: str) -> None:
        logger.error("[report-run %s] %s", report_run_id, msg)
        try:
            update_report_run(report_run_id, status="error", progress="Failed", error=msg[:2000])
        except Exception:
            logger.exception("[report-run %s] could not record error", report_run_id)

    try:
        # Inside the try so an import failure (wrong interpreter, missing
        # dep) lands on the row as an error instead of killing the daemon
        # thread silently and leaving the row stuck in "pending" — the
        # exact failure mode the first e2e smoke hit (2026-07-02).
        from zspan_pipeline import operator_search, qdrant_synthesizer, rag_search

        run = get_report_run(report_run_id, include_artifact=False)
        if not run:
            return fail("report run row not found")
        query: str = run["query"]
        interpretation: dict = run.get("interpretation") or {}
        meeting_ids: list[int] = run.get("meeting_ids") or []
        if not meeting_ids:
            return fail("no meeting_ids on the run row")

        update_report_run(
            report_run_id, status="running", progress="Retrieving from meetings...",
        )

        # Scope join — same shape as /api/operator-search/execute.
        from database import get_connection

        conn = get_connection()
        try:
            placeholders = ",".join("?" * len(meeting_ids))
            rows = conn.execute(
                f"""SELECT id, city_name, meeting_date, video_url
                    FROM meetings WHERE id IN ({placeholders})""",
                meeting_ids,
            ).fetchall()
        finally:
            conn.close()
        scope_by_id = {r[0]: (r[1], r[2] or "") for r in rows}
        video_url_by_id = {r[0]: r[3] for r in rows}
        scopes = [
            operator_search.MeetingScope(
                meeting_id=mid,
                city_name=scope_by_id[mid][0],
                meeting_date=scope_by_id[mid][1],
            )
            for mid in meeting_ids
            if mid in scope_by_id
        ]
        if not scopes:
            return fail("no meetings resolved from meeting_ids")
        searched_cities = sorted({s.city_name for s in scopes})

        # Fan-out (the shared OperatorSearch machinery).
        template_body = rag_search.load_prompt_template()
        legs = operator_search.fan_out_retrieve(query=query, scopes=scopes)

        # Vocabulary substitutions on raw chunk bodies (F1 discipline —
        # this is a new verbatim-chunk surface, same as operator-search).
        try:
            for _leg in legs:
                for _chunk in _leg.chunks:
                    _chunk.body, _ = apply_city_corrections(_leg.city_name, _chunk.body)
        except Exception as corr_exc:
            logger.warning(
                "[report-run %s] apply_city_corrections failed (non-fatal): %s",
                report_run_id, corr_exc,
            )

        # Per-leg child audit rows (identical hygiene to operator-search).
        child_run_ids: list[str] = []
        for leg in legs:
            leg_prov = rag_search.make_provenance_packet(
                meeting_id=leg.meeting_id, query=query,
                chunks=leg.chunks, template_body=template_body,
            )
            leg.retrieval_run_id = leg_prov["run_id"]
            child_run_ids.append(leg_prov["run_id"])
            try:
                save_byok_audit_run(
                    run_id=leg_prov["run_id"], kind="retrieval",
                    meeting_id=leg.meeting_id,
                    timestamp_utc=leg_prov["timestamp_utc"],
                    prompt_template_version=leg_prov["prompt_template_version"],
                    prompt_template_hash=leg_prov["prompt_template_hash"],
                    vector_ids=leg_prov["vector_ids"],
                    query_hash=leg_prov["query_hash"],
                    provider="anthropic", model=qdrant_synthesizer.SONNET_MODEL_ID,
                )
            except Exception as audit_exc:
                logger.warning(
                    "[report-run %s] leg audit-row write failed for %s: %s",
                    report_run_id, leg_prov["run_id"], audit_exc,
                )

        ranked = operator_search.dedup_and_rerank_chunks(legs)
        leg_outcomes = {
            "ok_count": sum(1 for l in legs if l.interpreted_as == "ok"),
            "indexed_no_match_count": sum(1 for l in legs if l.interpreted_as == "indexed_no_match"),
            "qdrant_down_count": sum(1 for l in legs if l.interpreted_as == "qdrant_down"),
            "details": [
                {
                    "meeting_id": l.meeting_id, "city_name": l.city_name,
                    "meeting_date": l.meeting_date, "interpreted_as": l.interpreted_as,
                    "chunks_used": len(l.chunks), "retrieval_run_id": l.retrieval_run_id,
                }
                for l in legs
            ],
        }

        # Citations list (ranked order = score desc; the renderer's
        # best-chunk-per-meeting rule depends on this ordering).
        citations: list[dict] = []
        union_vector_ids: list[str] = []
        for pair in ranked:
            leg, c = pair["leg"], pair["chunk"]
            vid = rag_search.chunk_to_vector_id(leg.meeting_id, c.chunk_index)
            union_vector_ids.append(vid)
            citations.append(
                {
                    "meeting_id": leg.meeting_id,
                    "city_name": leg.city_name,
                    "meeting_date": leg.meeting_date,
                    "chunk_index": c.chunk_index,
                    "vector_id": vid,
                    "start_seconds": c.start_seconds,
                    "end_seconds": c.end_seconds,
                    "score": round(c.score, 4),
                    "video_url": video_url_by_id.get(leg.meeting_id),
                }
            )
        contributing_cities = sorted({c["city_name"] for c in citations})

        # Per-section synthesis. Sections are independent — one failed
        # pass records its error and the run continues (the artifact
        # renders an honest failure line for it).
        sections: dict[str, dict] = {}
        section_hashes: list[str] = []
        for idx, (key, stem, heading, wants_cities) in enumerate(REPORT_SECTIONS, start=1):
            update_report_run(
                report_run_id,
                progress=f"Writing section {idx}/{len(REPORT_SECTIONS)}: {heading}...",
                current_section=key,
                sections=sections,
            )
            try:
                body, version = load_section_prompt(stem)
                p_hash = "sha256:" + rag_search.prompt_template_hash(body)
                section_hashes.append(p_hash)
                if not ranked:
                    # Honest-empty short-circuit: no chunks at all → the
                    # per-section prompts would each re-derive the same
                    # empty; state it deterministically instead of
                    # spending five Sonnet passes on it.
                    sections[key] = {
                        "status": "ok",
                        "markdown": "The retrieved record from the scoped meetings "
                        "contains no content matching this query.",
                        "prompt_version": version,
                        "prompt_hash": p_hash,
                    }
                    continue
                context = build_section_context(
                    query=query,
                    interpretation=interpretation,
                    ranked=ranked,
                    searched_cities=searched_cities if wants_cities else None,
                    contributing_cities=contributing_cities if wants_cities else None,
                )
                started = datetime.now(timezone.utc)
                md = qdrant_synthesizer.synthesize_via_claude_p(
                    build_section_prompt(body, context), timeout_seconds=300.0,
                )
                sections[key] = {
                    "status": "ok",
                    "markdown": md.strip(),
                    "prompt_version": version,
                    "prompt_hash": p_hash,
                    "duration_ms": int(
                        (datetime.now(timezone.utc) - started).total_seconds() * 1000
                    ),
                }
            except Exception as sec_exc:
                logger.exception(
                    "[report-run %s] section %s failed", report_run_id, key
                )
                sections[key] = {"status": "error", "error": str(sec_exc)[:500]}

        # Umbrella provenance row — kind="report". The combined hash
        # commits to the exact section-template set this run used.
        ts_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        umbrella_run_id = (
            "zspan-report-"
            + datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S")
            + "-"
            + rag_search.query_hash(query)[:6]
        )
        combined_hash = "sha256:" + rag_search.prompt_template_hash(
            "\n".join(section_hashes)
        )
        try:
            save_byok_audit_run(
                run_id=umbrella_run_id, kind="report", meeting_id=None,
                timestamp_utc=ts_utc,
                prompt_template_version=REPORT_PIPELINE_VERSION,
                prompt_template_hash=combined_hash,
                vector_ids=union_vector_ids,
                query_hash="sha256:" + rag_search.query_hash(query),
                provider="anthropic", model=qdrant_synthesizer.SONNET_MODEL_ID,
                child_run_ids=child_run_ids,
            )
        except Exception as audit_exc:
            logger.warning(
                "[report-run %s] umbrella audit-row write failed: %s",
                report_run_id, audit_exc,
            )

        update_report_run(report_run_id, progress="Rendering the report...")
        artifact = render_report_html(
            query=query,
            interpretation=interpretation,
            sections=sections,
            citations=citations,
            leg_outcomes=leg_outcomes,
            searched_cities=searched_cities,
            run_id=umbrella_run_id,
            child_run_count=len(child_run_ids),
            generated_at_utc=ts_utc,
        )

        update_report_run(
            report_run_id,
            status="complete",
            progress="Report complete.",
            current_section=None,
            sections=sections,
            citations=citations,
            leg_outcomes=leg_outcomes,
            run_id=umbrella_run_id,
            child_run_ids=child_run_ids,
            artifact_html=artifact,
        )
        logger.info(
            "[report-run %s] complete — %d chunks, %d meetings, run_id=%s",
            report_run_id, len(citations),
            len({c["meeting_id"] for c in citations}), umbrella_run_id,
        )
    except Exception as exc:  # noqa: BLE001 — thread boundary, land on row
        logger.exception("[report-run %s] unhandled failure", report_run_id)
        fail(f"unhandled: {exc}")
