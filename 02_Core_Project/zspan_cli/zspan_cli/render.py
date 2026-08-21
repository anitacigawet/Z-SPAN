"""The local broadcast view — the workspace rendered the way zspan.org
presents broadcasts, mirroring the site's BroadcastPage as built. Keep
in sync when the site's show page changes.

The as-built show flow this mirrors: title + date + status pill → video
(75% width, rounded, tab-strip vocabulary) → Key Decisions
(highway-sign-blue header; <core>/<nuance> natural-marker washes;
**bold** → white semibold) → Community Calls to Action (hides entirely
when empty — the honest-empty discipline) → provenance. The synopsis
paragraph is deliberately ABSENT here — the site removed it from the
show page ("felt like prompt-debug noise, not a viewer surface");
synopsis + episode_tagline instead describe meetings on the INDEX page,
the local equivalent of the channels surface where the site actually
uses them.

Two deliberate local departures, named so they read as choices:
  - A click-to-seek transcript strip rides under the player. The site's
    show page doesn't karaoke the main player; locally it's the only
    navigation aid over the user's own transcript, so it earns its place.
  - Numbered circles stand in for the site's wax-seal brand asset — the
    page is fully self-contained (no /brand/ fetches, no CDN, no fonts
    fetched; the Inter/JetBrains-Mono stacks fall back to system faces).

Every piece of model/user content passes html.escape before it touches
the page. The only remote asset is the YouTube embed itself.
"""
from __future__ import annotations

import html
import json
import math
import re
from typing import Any, Dict, Optional

# Values from the site (index.css :root + BroadcastPage's deliberate
# inline hexes — the page shell is #0E0E10, NOT --canvas).
_CSS = """
:root {
  --radius: 0.5rem;
  --civic-blue: #1A3A7C;
  --highway-sign-blue: #3361A6;
  --success-green: #22C55E;
  --canvas: #0E0E10;
  --surface: #141416;
  --surface-2: #18181A;
  --surface-3: #1C1C1E;
  --line: rgba(255,255,255,0.05);
  --line-strong: rgba(255,255,255,0.10);
  --muted-foreground: #A1A1AA;
  --amber: #F5A524;
  --teal: #2DD4BF;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
html { color-scheme: dark; }
body {
  background: var(--canvas); color: #FFFFFF;
  font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", "Roboto", sans-serif;
  line-height: 1.55;
}
.mono { font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.wrap { max-width: 1100px; margin: 0 auto; padding: 0 24px 80px; }

.terminal-band {
  background: var(--surface); border-bottom: 1px solid var(--line-strong);
  padding: 10px 20px; display: flex; align-items: center; gap: 12px;
  font-size: 12px; color: var(--muted-foreground);
}
.terminal-band .dots { display: flex; gap: 6px; }
.terminal-band .dots span { width: 10px; height: 10px; border-radius: 50%; background: var(--surface-3); border: 1px solid var(--line-strong); }
.terminal-band .status { color: var(--teal); }

header.show { padding: 40px 0 8px; }
h1 { font-size: 34px; font-weight: 300; letter-spacing: 0.02em; margin: 0 0 12px; }
.sub { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.date { font-size: 15px; color: #9CA3AF; font-weight: 500; letter-spacing: 0.02em; text-transform: uppercase; }
.pill {
  display: inline-flex; align-items: center; gap: 7px; padding: 3px 12px;
  border-radius: 999px; border: 1px solid rgba(34,197,94,0.3);
  background: rgba(34,197,94,0.1); color: var(--success-green);
  font-size: 10px; letter-spacing: 0.14em; text-transform: uppercase;
}
.pill .dot {
  width: 6px; height: 6px; border-radius: 50%; background: var(--success-green);
  box-shadow: 0 0 10px rgba(34,197,94,0.4);
}
.meta { font-size: 13px; color: var(--muted-foreground); margin-top: 10px; }

.video-wrap { max-width: 75%; margin: 24px 0 48px; }
@media (max-width: 800px) { .video-wrap { max-width: 100%; } }
.video-box {
  aspect-ratio: 16 / 9; background: #000; border: 1px solid var(--line-strong);
  border-radius: 1rem; overflow: hidden; box-shadow: 0 24px 48px rgba(0,0,0,0.5);
}
.video-box iframe, .video-box video { width: 100%; height: 100%; border: 0; display: block; }
.karaoke {
  max-height: 140px; overflow-y: auto; margin-top: 10px; padding: 12px 14px;
  background: var(--surface-2); border: 1px solid var(--line);
  border-radius: var(--radius); font-size: 13.5px; color: var(--muted-foreground);
}
.karaoke span { cursor: pointer; }
.karaoke span.read { color: #E5E7EB; }
.karaoke span.now { color: #fff; background: rgba(245,165,36,0.35); border-radius: 3px; }

section.block { margin-top: 40px; }
section.block > h3 {
  font-size: 11px; font-weight: 700; letter-spacing: 0.14em;
  text-transform: uppercase; color: var(--highway-sign-blue); margin-bottom: 14px;
}
.decision { display: flex; gap: 20px; align-items: flex-start; padding: 10px 0; }
.decision .num {
  flex: 0 0 auto; width: 30px; height: 30px; border-radius: 50%;
  background: var(--civic-blue); border: 1px solid var(--line-strong);
  color: #fff; font-size: 13px; font-weight: 600;
  display: flex; align-items: center; justify-content: center; margin-top: 1px;
}
.decision-copy { min-width: 0; flex: 1; }
.decision .txt { font-size: 15px; color: #E5E7EB; line-height: 1.65; }
.decision strong { color: #fff; font-weight: 600; }
mark.core { background: rgba(123,168,94,0.22); color: inherit; -webkit-box-decoration-break: clone; box-decoration-break: clone; border-radius: 3px; }
mark.nuance { background: rgba(201,146,78,0.22); color: inherit; -webkit-box-decoration-break: clone; box-decoration-break: clone; border-radius: 3px; }
.decision-evidence-host {
  text-indent: 0;
  text-align: left;
  letter-spacing: normal;
  font-style: normal;
  white-space: normal;
}
.decision-evidence-trigger { width: 16px; height: 16px; margin-left: 6px; border-radius: 50%; border: 1px solid currentColor; background: transparent; color: inherit; opacity: .55; font-size: 10px; cursor: pointer; }
.decision-evidence-disclosure {
  width: min(100%, 72ch);
  margin-top: 12px;
  overflow: hidden;
  background: var(--surface-2);
  border: 1px solid rgba(255,255,255,.10);
  border-radius: var(--radius);
  color: #fff;
}
.decision-evidence-disclosure[hidden] { display: none; }
.decision-evidence-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 11px 16px;
  border-bottom: 1px solid rgba(255,255,255,.10);
  background: linear-gradient(90deg, rgba(51,97,166,.18), rgba(51,97,166,.04));
}
.decision-evidence-title {
  color: rgba(255,255,255,.90);
  font-size: 14px;
  font-weight: 600;
}
.decision-evidence-badge {
  flex: 0 0 auto;
  padding: 2px 7px;
  border: 1px solid rgba(255,255,255,.12);
  border-radius: 999px;
  color: rgba(255,255,255,.55);
  font-size: 11px;
  font-weight: 500;
}
.decision-evidence-body { padding: 16px; }
.decision-evidence-item + .decision-evidence-item { margin-top: 24px; }
.decision-evidence-blockquote {
  max-width: 72ch;
  margin: 0;
  padding: 1px 0 1px 16px;
  border-left: 3px solid var(--highway-sign-blue);
  color: rgba(255,255,255,.88);
  font-size: 16px;
  line-height: 1.75;
}
.decision-evidence-paragraph { margin: 0; }
.decision-evidence-paragraph + .decision-evidence-paragraph { margin-top: 1em; }
.decision-evidence-divider {
  display: grid;
  grid-template-columns: minmax(20px, 1fr) auto minmax(20px, 1fr);
  align-items: center;
  gap: 12px;
  margin: 24px 0;
}
.decision-evidence-divider-rule {
  display: block;
  height: 1px;
  background: rgba(255,255,255,.10);
}
.decision-evidence-divider-text {
  color: rgba(255,255,255,.55);
  font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 12px;
  line-height: 1.4;
  text-align: center;
}
.decision-evidence-collapse {
  display: block;
  margin: 18px 0 0 auto;
  padding: 5px 9px;
  border: 1px solid rgba(255,255,255,.12);
  border-radius: calc(var(--radius) - 2px);
  background: transparent;
  color: rgba(255,255,255,.60);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}
.decision-evidence-collapse:hover,
.decision-evidence-collapse:focus-visible {
  border-color: rgba(255,255,255,.24);
  color: rgba(255,255,255,.88);
}
@media (max-width: 640px) {
  .decision-evidence-header { align-items: flex-start; padding: 10px 12px; }
  .decision-evidence-body { padding: 14px 12px; }
  .decision-evidence-blockquote { padding-left: 13px; font-size: 15px; }
  .decision-evidence-divider { grid-template-columns: minmax(10px, 1fr) minmax(0, auto) minmax(10px, 1fr); gap: 8px; }
}
.empty { font-size: 13px; color: #4B5563; font-style: italic; }

.chip {
  display: inline-flex; align-items: center; padding: 1px 7px; margin: 0 2px;
  border-radius: 6px; background: rgba(34,197,94,0.15); color: var(--success-green);
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 11px; font-weight: 700; cursor: pointer; white-space: nowrap;
}
.chip:hover { background: rgba(34,197,94,0.3); }

.cta { padding: 12px 0; border-top: 1px solid var(--line); }
.cta:first-child { border-top: 0; }
.cta .who { font-size: 13px; color: var(--muted-foreground); margin-bottom: 2px; }
.cta .quote { font-size: 15px; color: #E5E7EB; }
.cta .kind {
  display: inline-block; margin-top: 6px; padding: 1px 8px; border-radius: 999px;
  border: 1px solid rgba(245,165,36,0.35); font-size: 11px; letter-spacing: 0.08em;
  text-transform: uppercase; color: var(--amber);
}

.provenance { margin-top: 48px; font-size: 12px; color: var(--muted-foreground); }
.provenance .ok { color: var(--success-green); }
.provenance .degraded { color: var(--amber); }
.provenance .empty-status { color: #6B7280; }

.index-panel { background: var(--surface-2); border: 1px solid var(--line); border-radius: 1rem; margin-top: 24px; overflow: hidden; }
.index-row { display: block; padding: 16px 20px; border-top: 1px solid var(--line); color: inherit; text-decoration: none; }
.index-row:first-child { border-top: 0; }
.index-row:hover { background: var(--surface-3); }
.index-row .t { font-weight: 600; }
.index-row .tag { font-size: 13px; color: var(--amber); margin-top: 2px; }
.index-row .syn { font-size: 13px; color: var(--muted-foreground); margin-top: 4px; }
.index-row .d { font-size: 12px; color: #6B7280; margin-top: 6px; letter-spacing: 0.04em; }
"""

_TIME_CITATION = re.compile(r"\[at\s+(\d{1,3}):(\d{2})\]")
_KEY_DECISION_CITATION = re.compile(
    r"\s*\[at\s+(?:(?:\d+):)?\d{1,3}:\d{2}\]", re.IGNORECASE,
)
# The site's stripCitations: NotebookLM-style numeric cites (" [1]",
# " [1, 3-5]") — removed before display; [at MM:SS] survives it.
_NUMERIC_CITATION = re.compile(r"\s*\[\d+(?:[-,\s\d]*)\]")
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _esc(s: Any) -> str:
    return html.escape(str(s or ""), quote=True)


def _page(title: str, body: str, status_line: str) -> str:
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<meta name=\"theme-color\" content=\"#0E0E10\">"
        f"<title>{_esc(title)}</title><style>{_CSS}</style></head><body>"
        "<div class=\"terminal-band mono\"><div class=\"dots\">"
        "<span></span><span></span><span></span></div>"
        f"<div class=\"status\">{_esc(status_line)}</div></div>"
        f"<div class=\"wrap\">{body}</div>"
        "</body></html>"
    )


# ---------------------------------------------------------------- pieces


def _rich_text(escaped: str) -> str:
    """Post-escape decoration: **bold** → <strong>, [at MM:SS] → green
    seek chips (the site's chip vocabulary). Runs on ALREADY-ESCAPED text
    so only these constructs can produce markup."""
    out = _BOLD.sub(r"<strong>\1</strong>", escaped)
    out = _TIME_CITATION.sub(
        lambda m: (
            f"<span class=\"chip\" data-seek=\"{int(m.group(1)) * 60 + int(m.group(2))}\">"
            f"at {m.group(1)}:{m.group(2)}</span>"
        ),
        out,
    )
    return out


def strip_numeric_citations(text: str) -> str:
    return _NUMERIC_CITATION.sub("", text or "").strip()


def strip_key_decision_citations(text: str) -> str:
    return _KEY_DECISION_CITATION.sub("", strip_numeric_citations(text))


def _key_decisions_html(
    content: str,
    decision_evidence: Any = None,
    transcript_words: Any = None,
) -> str:
    items = re.split(r"(?m)^\s*\d+[.)]\s+", strip_key_decision_citations(content))
    items = [it.strip() for it in items[1:] if it.strip()]
    if not items:
        items = [content.strip()]
    rows = []
    for n, item in enumerate(items, start=1):
        escaped = _esc(item)
        # <core>/<nuance> arrive escaped; swap the known tags back into
        # the site's natural-marker washes (green core, orange nuance).
        escaped = escaped.replace("&lt;core&gt;", "<mark class=\"core\">")
        escaped = escaped.replace("&lt;/core&gt;", "</mark>")
        escaped = escaped.replace("&lt;nuance&gt;", "<mark class=\"nuance\">")
        escaped = escaped.replace("&lt;/nuance&gt;", "</mark>")
        evidence = (
            [
                decision for decision in decision_evidence
                if isinstance(decision, dict) and decision.get("index") == n
            ]
            if isinstance(decision_evidence, list)
            else []
        )
        evidence_trigger, evidence_disclosure = _decision_evidence_parts(
            evidence, transcript_words,
        )
        rows.append(
            f"<div class=\"decision\"><div class=\"num\">{n}</div>"
            f"<div class=\"decision-copy decision-evidence-host\" data-state=\"closed\">"
            f"<div class=\"txt\">{_rich_text(escaped)}{evidence_trigger}</div>"
            f"{evidence_disclosure}</div></div>"
        )
    return "".join(rows)


def _paragraphize_verbatim_words(
    words: Any,
    stored_span_text: str,
    pause_seconds: float = 1.5,
) -> Optional[list[str]]:
    """Group exact transcript tokens by pauses, or fail closed."""
    if (
        not isinstance(words, list)
        or not words
        or not isinstance(pause_seconds, (int, float))
        or isinstance(pause_seconds, bool)
        or not math.isfinite(float(pause_seconds))
    ):
        return None
    paragraphs: list[list[str]] = [[]]
    tokens: list[str] = []
    previous_start = -math.inf
    previous_end = -math.inf
    for timing in words:
        if not isinstance(timing, dict):
            return None
        word = timing.get("word")
        start = timing.get("start")
        end = timing.get("end")
        if (
            not isinstance(word, str)
            or not isinstance(start, (int, float))
            or isinstance(start, bool)
            or not isinstance(end, (int, float))
            or isinstance(end, bool)
            or not math.isfinite(float(start))
            or not math.isfinite(float(end))
            or start < 0
            or end < start
            or start < previous_start
            or end < previous_end
        ):
            return None
        if tokens and start - previous_end >= pause_seconds:
            paragraphs.append([])
        paragraphs[-1].append(word)
        tokens.append(word)
        previous_start = start
        previous_end = end
    joined = " ".join(tokens)
    if joined != stored_span_text:
        return None
    result = [" ".join(paragraph) for paragraph in paragraphs]
    return result if " ".join(result) == stored_span_text else None


def _span_word_timings(span: dict, transcript_words: Any) -> list[dict]:
    attached = span.get("word_timings")
    if isinstance(attached, list):
        return attached
    if not isinstance(transcript_words, list):
        return []
    start_index = span.get("start_word_index")
    end_index = span.get("end_word_index")
    if start_index is None and end_index is None:
        start_seconds = span.get("start_seconds")
        end_seconds = span.get("end_seconds")
        if (
            not isinstance(start_seconds, (int, float))
            or isinstance(start_seconds, bool)
            or not isinstance(end_seconds, (int, float))
            or isinstance(end_seconds, bool)
            or end_seconds < start_seconds
        ):
            return []
        return [
            word for word in transcript_words
            if isinstance(word, dict)
            and isinstance(word.get("start"), (int, float))
            and not isinstance(word.get("start"), bool)
            and isinstance(word.get("end"), (int, float))
            and not isinstance(word.get("end"), bool)
            and start_seconds <= word["start"]
            and word["end"] <= end_seconds
        ]
    if (
        not isinstance(start_index, int)
        or isinstance(start_index, bool)
        or not isinstance(end_index, int)
        or isinstance(end_index, bool)
        or start_index < 0
        or end_index < start_index
        or end_index >= len(transcript_words)
    ):
        return []
    return transcript_words[start_index:end_index + 1]


def _decision_evidence_parts(
    decisions: Any,
    transcript_words: Any = None,
) -> tuple[str, str]:
    """Render transcript excerpts as an in-flow two-state disclosure."""
    if not isinstance(decisions, list):
        return "", ""
    excerpts = []
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        spans = decision.get("verbatim_spans")
        if not isinstance(spans, list) or not spans:
            continue
        valid = [
            span for span in spans
            if isinstance(span, dict)
            and span.get("source") == "item_quote_to_action_quote"
            and isinstance(span.get("text"), str) and span["text"]
            and span.get("structure") in {"contiguous", "elided"}
            and isinstance(span.get("label"), str)
        ]
        if not valid:
            continue
        passages = []
        for span_index, span in enumerate(valid):
            paragraphs = _paragraphize_verbatim_words(
                _span_word_timings(span, transcript_words),
                span["text"],
                1.5,
            ) or [span["text"]]
            passage = ""
            if span_index > 0:
                previous = valid[span_index - 1]
                previous_end = previous.get("end_seconds")
                current_start = span.get("start_seconds")
                gap_minutes = (
                    math.floor(((current_start - previous_end) / 60) + 0.5)
                    if isinstance(previous_end, (int, float))
                    and not isinstance(previous_end, bool)
                    and isinstance(current_start, (int, float))
                    and not isinstance(current_start, bool)
                    and math.isfinite(float(previous_end))
                    and math.isfinite(float(current_start))
                    else 0
                )
                passage += (
                    "<div class=\"decision-evidence-divider\">"
                    "<span class=\"decision-evidence-divider-rule\"></span>"
                    "<span class=\"decision-evidence-divider-text\">"
                    f"Verbatim transcript resumes about {_esc(gap_minutes)} minutes later"
                    "</span><span class=\"decision-evidence-divider-rule\"></span>"
                    "</div>"
                )
            passage += (
                "<blockquote class=\"decision-evidence-blockquote\">"
                + "".join(
                    "<p class=\"decision-evidence-paragraph\">"
                    f"{_esc(paragraph)}</p>"
                    for paragraph in paragraphs
                )
                + "</blockquote>"
            )
            passages.append(
                f"<div class=\"decision-evidence-passage\">{passage}</div>"
            )
        word_count = sum(len(span["text"].split()) for span in valid)
        collapse = (
            "<button class=\"decision-evidence-collapse\" type=\"button\">"
            "Collapse transcript source</button>"
            if len(valid) == 2 or word_count > 180 else ""
        )
        body = (
            "<section class=\"decision-evidence-item\">"
            f"{''.join(passages)}{collapse}</section>"
        )
        excerpts.append((decision.get("index"), body))
    if not excerpts:
        return "", ""
    title = "Verbatim transcript source"
    disclosure_id = f"decision-evidence-{_esc(excerpts[0][0])}"
    trigger = (
        " <button class=\"decision-evidence-trigger\" type=\"button\" "
        "aria-label=\"Show verbatim transcript source for this decision\" "
        f"aria-expanded=\"false\" aria-controls=\"{disclosure_id}\">i</button>"
    )
    disclosure = (
        f"<div class=\"decision-evidence-disclosure\" id=\"{disclosure_id}\" "
        "data-state=\"closed\" hidden>"
        "<div class=\"decision-evidence-header\">"
        f"<span class=\"decision-evidence-title\">{title}</span>"
        "<span class=\"decision-evidence-badge\">Words unchanged</span></div>"
        f"<div class=\"decision-evidence-body\">{''.join(body for _, body in excerpts)}</div>"
        "</div>"
    )
    return trigger, disclosure


def _decision_evidence_html(decisions: Any, transcript_words: Any = None) -> str:
    """Return the trigger and adjacent disclosure for focused render tests."""
    return "".join(_decision_evidence_parts(decisions, transcript_words))


def _ccta_html(content: str) -> Optional[str]:
    """CCTA cards, or None when the list is empty/unparseable-empty (the
    site hides the section entirely when there are no asks). Parsing goes
    through gate.split_ccta — ONE tolerant CCTA parser for the whole CLI,
    so fence-wrapped model output (and pre-normalization workspace rows)
    read identically at the gate and at the render."""
    from zspan_cli.gate import split_ccta

    elements = split_ccta(content)
    if elements is None:
        text = (content or "").strip()
        return f"<div class=\"cta\">{_rich_text(_esc(text))}</div>" if text else None
    if not elements:
        return None
    cards = []
    for el in elements:
        if isinstance(el, str):
            cards.append(f"<div class=\"cta\"><div class=\"quote\">{_esc(el)}</div></div>")
            continue
        who_bits = [b for b in (el.get("speaker_name"), el.get("speaker_role")) if b]
        who = f"<div class=\"who\">{_esc(' · '.join(str(b) for b in who_bits))}</div>" if who_bits else ""
        quote = el.get("quote_text") or el.get("quote") or ""
        extras = " — ".join(
            str(x) for x in (el.get("actionable_hook"), el.get("deadline"), el.get("contact")) if x
        )
        kind = el.get("ask_kind")
        cards.append(
            "<div class=\"cta\">" + who
            + (f"<div class=\"quote\">“{_esc(quote)}”</div>" if quote else "")
            + (f"<div class=\"who\">{_esc(extras)}</div>" if extras else "")
            + (f"<span class=\"kind\">{_esc(kind)}</span>" if kind else "")
            + "</div>"
        )
    return "".join(cards)


def _video_html(video_url: str, words_json: str) -> str:
    yt = re.search(r"(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]{6,})", video_url or "")
    if yt:
        mount = f"<div class=\"video-box\"><div id=\"yt\" data-video=\"{_esc(yt.group(1))}\"></div></div>"
        player_js = _YT_PLAYER_JS
    elif video_url:
        mount = (
            f"<div class=\"video-box\"><video id=\"vid\" controls "
            f"src=\"{_esc(video_url)}\"></video></div>"
        )
        player_js = _HTML5_PLAYER_JS
    else:
        return ""
    return (
        f"<div class=\"video-wrap\">{mount}"
        f"<div class=\"karaoke mono\" id=\"karaoke\"></div></div>"
        f"<script type=\"application/json\" id=\"words\">{words_json}</script>"
        f"<script>{_KARAOKE_JS}{player_js}</script>"
    )


_KARAOKE_JS = """
var WORDS = JSON.parse(document.getElementById('words').textContent || '[]');
var box = document.getElementById('karaoke');
var spans = [];
if (box && WORDS.length) {
  var frag = document.createDocumentFragment();
  WORDS.forEach(function (w) {
    var s = document.createElement('span');
    s.textContent = w.word + ' ';
    s.dataset.start = w.start;
    s.addEventListener('click', function () { seekTo(w.start); });
    frag.appendChild(s); spans.push(s);
  });
  box.appendChild(frag);
} else if (box) { box.remove(); }
var lastIdx = -1;
function paint(t) {
  if (!spans.length) return;
  var lo = 0, hi = spans.length - 1, idx = -1;
  while (lo <= hi) {
    var mid = (lo + hi) >> 1;
    if (parseFloat(spans[mid].dataset.start) <= t) { idx = mid; lo = mid + 1; }
    else { hi = mid - 1; }
  }
  if (idx === lastIdx) return;
  if (lastIdx >= 0) spans[lastIdx].classList.remove('now');
  for (var i = Math.max(0, lastIdx); i <= idx; i++) spans[i].classList.add('read');
  if (idx >= 0) {
    spans[idx].classList.add('now');
    spans[idx].scrollIntoView({ block: 'nearest' });
  }
  lastIdx = idx;
}
document.addEventListener('click', function (e) {
  var chip = e.target.closest('[data-seek]');
  if (chip) seekTo(parseFloat(chip.dataset.seek));
});
"""

_YT_PLAYER_JS = """
var ytPlayer = null;
function seekTo(t) { if (ytPlayer && ytPlayer.seekTo) { ytPlayer.seekTo(t, true); ytPlayer.playVideo(); } }
window.onYouTubeIframeAPIReady = function () {
  var mount = document.getElementById('yt');
  ytPlayer = new YT.Player('yt', {
    videoId: mount.dataset.video,
    playerVars: { rel: 0, modestbranding: 1, playsinline: 1 }
  });
  setInterval(function () {
    if (ytPlayer && ytPlayer.getCurrentTime) paint(ytPlayer.getCurrentTime());
  }, 250);
};
(function () {
  var s = document.createElement('script');
  s.src = 'https://www.youtube.com/iframe_api';
  document.head.appendChild(s);
})();
"""

_HTML5_PLAYER_JS = """
var vid = document.getElementById('vid');
function seekTo(t) { if (vid) { vid.currentTime = t; vid.play(); } }
if (vid) vid.addEventListener('timeupdate', function () { paint(vid.currentTime); });
"""

_EVIDENCE_JS = """
document.querySelectorAll('.decision-evidence-host').forEach(function (host) {
  var trigger = host.querySelector('.decision-evidence-trigger');
  var disclosure = host.querySelector('.decision-evidence-disclosure');
  if (!trigger || !disclosure) return;

  function setOpen(open) {
    host.dataset.state = open ? 'open' : 'closed';
    disclosure.dataset.state = open ? 'open' : 'closed';
    disclosure.hidden = !open;
    trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
    trigger.setAttribute('aria-label', (open ? 'Hide' : 'Show') + ' verbatim transcript source for this decision');
  }
  trigger.addEventListener('click', function () {
    setOpen(trigger.getAttribute('aria-expanded') !== 'true');
  });
  disclosure.querySelectorAll('.decision-evidence-collapse').forEach(function (button) {
    button.addEventListener('click', function () {
      setOpen(false);
      trigger.focus();
    });
  });
  host.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && trigger.getAttribute('aria-expanded') === 'true' && host.contains(document.activeElement)) {
      event.preventDefault();
      setOpen(false);
      trigger.focus();
    }
  });
});
"""


# ---------------------------------------------------------------- pages


def meeting_page(
    row,
    outputs: Dict[str, dict],
    transcript: Optional[dict] = None,
    decision_evidence: Optional[list[dict]] = None,
) -> str:
    """One processed meeting, the as-built show flow: header + pill →
    video → Key Decisions → Community Calls to Action → provenance."""
    title = row["title"] or "(untitled meeting)"
    # The site shows the part of the title before " - " as the display name.
    display_title = title.split(" - ")[0].strip() or title
    kd = (outputs.get("key_decisions") or {}).get("content") or ""
    ccta = (outputs.get("community_calls_to_action") or {}).get("content") or ""

    words = (transcript or {}).get("words") or []
    words_json = json.dumps(
        [{"word": w.get("word", ""), "start": round(float(w.get("start", 0.0)), 2)}
         for w in words],
        ensure_ascii=False,
    ).replace("</", "<\\/")  # never let content close the JSON script tag

    is_processed = bool(row["processed_at"] or outputs)
    processed = (row["processed_at"] or "")[:10] or row["meeting_date"]
    status_pill = (
        f"Processed · {_esc(processed)}" if is_processed else "Not processed yet"
    )
    meta = (
        "Processed on your computer. Z-SPAN's private intake received the "
        "transcript, final outputs, and audit record for review; nothing was "
        "published automatically."
        if is_processed
        else "This factual meeting record is in your private workspace. Local "
             "processing has not been run; Process remains your choice."
    )
    parts = [
        "<header class=\"show\">",
        f"<h1>{_esc(display_title)}</h1>",
        "<div class=\"sub\">",
        f"<span class=\"date\">{_esc(row['city'])} · {_esc(row['meeting_date'])}</span>",
        f"<span class=\"pill\"><span class=\"dot\"></span>{status_pill}</span>",
        "</div>",
        f"<div class=\"meta\">{meta}</div>",
        "</header>",
        _video_html(row["video_url"] or "", words_json),
        "<section class=\"block\"><h3>Key Decisions</h3>",
        (_key_decisions_html(kd, decision_evidence, words) if kd
         else "<div class=\"empty\">Not processed yet — no locally-generated content.</div>"),
        "</section>",
    ]

    ccta_html = _ccta_html(ccta) if ccta else None
    if ccta_html:
        parts.append(
            "<section class=\"block\"><h3>Community Calls to Action</h3>"
            f"{ccta_html}</section>"
        )

    prov_bits = []
    for output_type in ("synopsis", "key_decisions", "community_calls_to_action",
                        "episode_tagline"):
        o = outputs.get(output_type)
        if not o:
            continue
        status = o.get("gate_status") or "?"
        css = {
            "observed_clean": "ok",
            "observed_findings": "degraded",
            "ok": "ok",              # legacy cached rows
            "degraded": "degraded",  # legacy cached rows
        }.get(status, "empty-status")
        prov_bits.append(
            f"{_esc(output_type.replace('_', ' '))} "
            f"<span class=\"{css}\">{_esc(status)}</span>"
        )
    if outputs:
        first = next(iter(outputs.values()))
        parts.append(
            "<div class=\"provenance mono\">"
            f"Synthesized on your machine with your own key via "
            f"{_esc(first.get('provider') or '?')} ({_esc(first.get('model') or '?')}). "
            "Every output passed the deterministic grounding gate before caching — "
            + " · ".join(prov_bits)
            + ". These summaries are AI-generated from the meeting recording; "
            "the recording itself is the record.</div>"
        )

    if _decision_evidence_html(decision_evidence, words):
        parts.append(f"<script>{_EVIDENCE_JS}</script>")

    return _page(
        f"{display_title} — Z-SPAN local workspace",
        "".join(parts),
        "Z-SPAN: your private local workspace (rendered from ~/.zspan — "
        "the flagship was not contacted)",
    )


def index_page(rows, outputs_by_id: Optional[Dict[int, Dict[str, dict]]] = None) -> str:
    """The processed-meetings index — the local channels-surface and the
    site-faithful home for episode_tagline + synopsis. The hologram boot
    plays in the terminal (boot.py) before the browser ever opens, so
    this page is just the list — no second boot, no stage."""
    outputs_by_id = outputs_by_id or {}

    row_html = []
    for r in rows:
        mid = int(r["id"])
        outs = outputs_by_id.get(mid, {})
        tagline = (outs.get("episode_tagline") or {}).get("content") or ""
        synopsis = strip_numeric_citations(
            (outs.get("synopsis") or {}).get("content") or ""
        )
        if len(synopsis) > 220:
            synopsis = synopsis[:220].rsplit(" ", 1)[0] + "…"
        row_html.append(
            f"<a class=\"index-row\" href=\"/meeting/{mid}\">"
            f"<div class=\"t\">{_esc(r['title'] or '(untitled)')}</div>"
            + (f"<div class=\"tag\">{_esc(tagline)}</div>" if tagline else "")
            + (f"<div class=\"syn\">{_esc(synopsis)}</div>" if synopsis else "")
            + f"<div class=\"d\">{_esc(r['city'])} · {_esc(r['meeting_date'])} · "
            + (f"{int(r['output_count'])} outputs" if int(r["output_count"] or 0)
               else "not processed yet")
            + "</div></a>"
        )

    body = (
        "<div class=\"index-panel\">"
        + ("".join(row_html) if row_html else
           "<div class=\"empty\">No meetings are in this workspace yet.</div>")
        + "</div>"
        + "<div class=\"index-foot mono\" style=\"text-align:center; "
          "font-size:13px; margin-top:14px;\">"
          "<a href=\"https://github.com/anitacigawet/Z-SPAN\" "
          "style=\"color:#14B8A6; text-decoration:none;\">Source</a>"
          " &nbsp;·&nbsp; "
          "<a href=\"https://ko-fi.com/zspan\" "
          "style=\"color:#6366F1; text-decoration:none;\">Ko-fi</a>"
          "</div>"
    )
    return _page("Z-SPAN local workspace", body,
                 "Z-SPAN: your private local workspace")


def not_found_page() -> str:
    return _page(
        "Not found — Z-SPAN local workspace",
        "<header class=\"show\"><h1>Nothing at this address</h1>"
        "<div class=\"meta\">The index lists everything in your workspace — "
        "<a href=\"/\" style=\"color:#F5A524\">back to it</a>.</div></header>",
        "Z-SPAN: your private local workspace",
    )


def error_page(detail: str) -> str:
    return _page(
        "Error — Z-SPAN local workspace",
        "<header class=\"show\"><h1>The render hit an error</h1>"
        f"<div class=\"meta mono\">{_esc(detail)}</div>"
        "<div class=\"meta\">The server is still up — this is a page bug, "
        "not data loss. Your workspace file is untouched.</div></header>",
        "Z-SPAN: your private local workspace",
    )
