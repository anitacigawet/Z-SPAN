"""The hologram boot — terminal form.

The choreography follows a set of hand-drawn reference frames: two
bases spark in →
Ionic columns rise in parallel →
volute capitals + entablature arrive → a teal "Loading: <what it's
actually loading>" line — teal rather than a warning colour, so a
working boot never reads as an error → between the columns, the ocean:
alternating teal/indigo
waves, each holding the position of a future text line — the waves ARE
the loading bar, literally the absent data → the waves splash down into
the real content → the header settles to grey:
"Z-SPAN: connected to local workspace".

IMPORTANT design note: the scribble strokes in the sketch frames are
drawing shorthand for "a column materializes here" — not the target
rendering. The target is a real column, drawn as beautifully as the
medium allows, materializing the way a polished intro sequence builds a
scene. Concretely:

  * cylindrical light — every column row is shaded per-cell across its
    width, palest at the lit face (left-of-center) falling into deep
    violet shadow at the right edge. A shaded cylinder is what makes
    13 terminal cells read as round marble instead of a flat slab;
  * regular fluting — ridge/groove alternation down the shaft (the
    photo's vertical grooves), carried by glyph density, no noise;
  * the laser scanline — the newest shaft row renders white-hot for one
    tick before settling into its shading, with a spark floating above:
    the hologram writing matter into place;
  * Ionic anatomy — entablature slab, volute scrolls in the vivid
    accent violet flanking an egg-and-dart band, fluted shaft, torus
    ring, then a wider plinth.

Honesty discipline (unchanged from the web boot's contract):
  * the status line names the ACTUAL step running — no fake steps;
  * any keypress skips straight to the resolved state (a dim hint on
    the pad row says so during the animation — the resolved frame is
    quiet);
  * an exception or a slow step (>20s watchdog) demotes to the plain
    log — captured lines surface, nothing is hidden behind art;
  * non-TTY / dumb / narrow terminals never see art at all (plain tier
    prints the same facts as ordinary lines).

Everything is generated with a FIXED seed — the same columns every boot
(consistent, testable); motion comes from the tick index, not per-boot
randomness. NFO/block art is the terminal's native medium; the FULL
glyph set is cp437-only, so the aesthetic and the oldest-Windows-console
floor are the same character set. Pure stdlib.
"""
from __future__ import annotations

import math
import os
import random
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

_SEED = 20260710  # fixed — the boot is a drawing, not a dice roll

# ── palette (truecolor → 256 → 16 fallbacks) ───────────────────────────
_TEAL = ((34, 211, 238), 45, 36)     # #22D3EE — the data arriving
_INDIGO = ((99, 102, 241), 63, 34)   # #6366F1 — the alternate wave
_GREY = ((156, 163, 175), 245, 37)   # #9CA3AF — status / footnotes
_WHITE_HOT = ((244, 238, 255), 255, 37)  # the laser scanline
_WHITE = ((244, 244, 245), 255, 37)  # the disclaimer's frame (final.png)
_RED = ((239, 68, 68), 196, 31)      # its load-bearing core — red + bold

# The violet ramp, light → deep. The inspiration photo's column catches
# light from the left: pale marble at the lit face, falling into deep
# violet shadow on the right. Index 0 is the lit edge; index 3 the
# shadow. The volutes take the vivid magenta as the carved accent.
_VIOLET_RAMP = (
    ((216, 180, 254), 183, 35),  # #D8B4FE — lit face
    ((176, 106, 240), 141, 35),  # #B06AF0
    ((168, 85, 247), 135, 35),   # #A855F7
    ((124, 58, 237), 93, 35),    # #7C3AED — shadow edge
)
_VIOLET_ACCENT = ((201, 73, 232), 171, 35)  # #C949E8 — volute scrolls

# ── glyph sets ──────────────────────────────────────────────────────────
# FULL is deliberately cp437-only (█▓▒░▀▄║≈∙ all exist in code page 437),
# so the one encoding that can't take it is plain ASCII — which gets the
# ASCII set below. ~ and () are ASCII and shared.
_GLYPHS_FULL = {
    "block": "█", "hi": "▓", "mid": "▒", "lo": "░",
    "top": "▀", "bot": "▄", "edge": "║", "groove": "│",
    "wave_hi": "≈", "wave_lo": "~", "volute": "@", "dot": "∙",
}
_GLYPHS_ASCII = {
    "block": "#", "hi": "%", "mid": ":", "lo": ".",
    "top": "=", "bot": "_", "edge": "|", "groove": "|",
    "wave_hi": "~", "wave_lo": "-", "volute": "o", "dot": ".",
}

_ASCII_TRANSLATION = str.maketrans({
    "→": "->", "·": "-", "…": "...",
    "✓": "OK", "✗": "X", "—": "-", "–": "-",
    "“": '"', "”": '"', "‘": "'", "’": "'",
})


def _ascii_text(text: str) -> str:
    """Transliterate terminal copy, with a safe fallback for odd input."""
    translated = text.translate(_ASCII_TRANSLATION)
    return translated.encode("ascii", errors="replace").decode("ascii")

# Column anatomy (per the inspiration photo): capital slab + volutes
# WIDER than the shaft, fluted shaft centered, torus ring, wider plinth.
_COL_W = 13          # full footprint (capital + plinth width)
_SHAFT_W = 9         # fluted shaft, centered in the footprint
_ART_ROWS = 13       # capital(2) + shaft(9) + base(2)
_SHAFT_ROWS = 9
_FRAME_DT = 0.08     # ~12 fps
_WATCHDOG_S = 20.0   # a frozen wave counts as failure — demote past this


@dataclass(frozen=True)
class BootLayout:
    """Immutable stage geometry derived only from terminal dimensions."""

    canvas_width: int
    left_margin: int
    wscale: int
    shaft_w: int
    col_w: int
    ocean_w: int
    hscale: int
    shaft_rows: int
    capital_rows: int
    base_rows: int
    art_rows: int
    canvas_h: int

    @classmethod
    def derive(cls, width: int, rows: int) -> "BootLayout":
        canvas_width = min(width, 132)
        left_margin = (width - canvas_width) // 2
        wscale = min(4, (canvas_width - 76) // 12)
        shaft_w = _SHAFT_W + 2 * wscale
        col_w = shaft_w + 4
        ocean_w = canvas_width - 2 * (col_w + 2)

        hscale = min(4, (rows - 24) // 4)
        shaft_rows = _SHAFT_ROWS + 2 * hscale
        capital_rows = 2 + (1 if hscale >= 1 else 0) + (1 if hscale >= 3 else 0)
        base_rows = 2 + (1 if hscale >= 1 else 0) + (1 if hscale >= 3 else 0)
        art_rows = capital_rows + shaft_rows + base_rows
        return cls(
            canvas_width=canvas_width,
            left_margin=left_margin,
            wscale=wscale,
            shaft_w=shaft_w,
            col_w=col_w,
            ocean_w=ocean_w,
            hscale=hscale,
            shaft_rows=shaft_rows,
            capital_rows=capital_rows,
            base_rows=base_rows,
            art_rows=art_rows,
            canvas_h=art_rows + 3,
        )


@dataclass
class BootSpec:
    """Everything the pure renderers need. Built by detect_capabilities;
    constructed directly in tests."""
    width: int = 80
    color_depth: int = 24          # 24 | 8 | 4 | 0 (0 = mono)
    glyphs: dict = field(default_factory=lambda: dict(_GLYPHS_FULL))
    rows: int = 24                 # terminal height — the runner centers on it
    ascii_only: bool = False       # content + glyphs must encode as ASCII
    layout: BootLayout = field(init=False)

    def __post_init__(self) -> None:
        self.layout = BootLayout.derive(self.width, self.rows)

    def color(self, spec: Tuple[Tuple[int, int, int], int, int],
              bold: bool = False) -> str:
        if self.color_depth == 0:
            return ""
        (r, g, b), c256, c16 = spec
        if self.color_depth >= 24:
            base = f"\x1b[38;2;{r};{g};{b}m"
        elif self.color_depth >= 8:
            base = f"\x1b[38;5;{c256}m"
        else:
            base = f"\x1b[{c16}m"
        return ("\x1b[1m" + base) if bold else base

    @property
    def reset(self) -> str:
        return "" if self.color_depth == 0 else "\x1b[0m"

    @property
    def ocean_width(self) -> int:
        return self.layout.ocean_w


def detect_capabilities(out=None) -> Optional[BootSpec]:
    """None → plain tier (no art). A BootSpec → art tier."""
    out = out or sys.stdout
    try:
        if not out.isatty():
            return None
    except Exception:
        return None
    if os.environ.get("TERM", "") == "dumb":
        return None
    cols, rows = shutil.get_terminal_size(fallback=(80, 24))
    # 76 columns leaves a 46-cell ocean: sized for the longest home line
    # (historically the 46-char framework line, removed per D-182; the
    # width floor stays so the layout doesn't shift when it returns).
    if cols < 76 or rows < 24:
        return None
    if sys.platform == "win32" and not _enable_windows_vt():
        return None

    glyphs = dict(_GLYPHS_FULL)
    ascii_only = False
    enc = getattr(out, "encoding", None) or "utf-8"
    try:
        "".join(glyphs.values()).encode(enc)
    except (UnicodeEncodeError, LookupError):
        glyphs = dict(_GLYPHS_ASCII)
        ascii_only = True

    if os.environ.get("NO_COLOR"):
        depth = 0
    else:
        colorterm = os.environ.get("COLORTERM", "").lower()
        term = os.environ.get("TERM", "")
        if "truecolor" in colorterm or "24bit" in colorterm:
            depth = 24
        elif "256" in term:
            depth = 8
        else:
            depth = 8 if sys.platform == "win32" else 4  # modern conhost does 256

    return BootSpec(width=cols, color_depth=depth, glyphs=glyphs,
                    rows=rows, ascii_only=ascii_only)


def _enable_windows_vt() -> bool:
    """Turn on ANSI processing for classic conhost. Windows Terminal has
    it on already; failure means a console we shouldn't draw on."""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        ENABLE_VT = 0x0004
        if mode.value & ENABLE_VT:
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | ENABLE_VT))
    except Exception:
        return False


# ── pure geometry (deterministic per seed + tick) ───────────────────────
#
# A column row is a list of "cells": (glyph, paint) pairs where paint is
# a semantic name the painter maps to the palette. Keeping paint semantic
# (not raw escapes) keeps the geometry pure and the tests readable.

_Cell = Tuple[str, str]  # (glyph, paint) — paint ∈ ramp0..ramp3|accent|hot|""


def _cylinder_ramp(x: int, width: int) -> int:
    """Cylindrical shading across a row: palest just left of center (the
    lit face), deepening toward both edges, deepest on the right. The
    curve — not a linear gradient — is what makes the column look round."""
    if width <= 1:
        return 1
    t = x / (width - 1)                # 0..1 across the row
    lit = 0.34                          # the lit face sits left of center
    d = abs(t - lit) / max(lit, 1.0 - lit)
    idx = d * d * 2.4 + (0.9 if t > lit else 0.0) * d
    return max(0, min(len(_VIOLET_RAMP) - 1, int(idx + 0.25)))


def _shaded(cells: List[str], deepen_grooves: bool = False,
            groove_at: Optional[set] = None,
            highlight_at: Optional[set] = None) -> List[_Cell]:
    """Apply the cylinder ramp to a row of glyphs. Grooves (fluting)
    sit one step deeper than their neighbors — carved shadow."""
    w = len(cells)
    out: List[_Cell] = []
    for x, ch in enumerate(cells):
        if ch == " ":
            out.append((" ", ""))
            continue
        idx = _cylinder_ramp(x, w)
        if deepen_grooves and groove_at and x in groove_at:
            idx = min(idx + 1, len(_VIOLET_RAMP) - 1)
        elif highlight_at and x in highlight_at:
            idx = max(idx - 1, 0)
        out.append((ch, f"ramp{idx}"))
    return out


def _center_cells(cells: List[_Cell], width: int) -> List[_Cell]:
    pad = width - len(cells)
    left = pad // 2
    return [(" ", "")] * left + cells + [(" ", "")] * (pad - left)


def _shaft_row(g: dict, shaft_w: int = _SHAFT_W,
               col_w: int = _COL_W, hot: bool = False) -> List[_Cell]:
    """One fluted shaft row: ║ contours, ridge/groove alternation inside
    (the photo's vertical grooves), cylinder-shaded. `hot` renders the
    whole row as the laser scanline — matter being written."""
    interior = shaft_w - 2
    glyphs = [g["edge"]]
    grooves: set = set()
    highlights: set = set()
    for x in range(interior):
        absolute_x = x + 1
        if shaft_w <= 11:
            is_groove = x % 3 == 1
            is_highlight = False
        else:
            unit = x % 3
            is_groove = unit == 2
            is_highlight = unit == 0
        if is_groove:
            glyphs.append(g["groove"])
            grooves.add(absolute_x)
        else:
            # Solid marble. At wider sizes the flute highlight is paint,
            # not a density-pattern glyph, so cylinder roundness survives.
            glyphs.append(g["block"])
            if is_highlight:
                highlights.add(absolute_x)
    glyphs.append(g["edge"])
    if hot:
        return _center_cells([(ch, "hot") for ch in glyphs], col_w)
    return _center_cells(_shaded(glyphs, deepen_grooves=True,
                                 groove_at=grooves,
                                 highlight_at=highlights), col_w)


def _capital_geometry(g: dict, layout: BootLayout) -> List[List[_Cell]]:
    """Ionic capital scaled by the derived capital height."""
    slab = [g["bot"]] + [g["block"]] * (layout.col_w - 2) + [g["bot"]]
    rows = [_shaded(slab)]

    if layout.capital_rows == 2:
        band_w = layout.col_w - 6
        band = [(g["dot"] if i % 2 == 0 else g["bot"], "ramp1")
                for i in range(band_w)]
        rows.append([("(", "accent"), ("@", "accent"), (")", "accent")]
                    + band
                    + [("(", "accent"), ("@", "accent"), (")", "accent")])
        return rows

    scroll_gap = [(" ", "")] * max(layout.col_w - 4, 1)
    rows.append([("(", "accent"), ("@", "accent")]
                + scroll_gap + [("@", "accent"), (")", "accent")])
    if layout.capital_rows == 4:
        lower_gap = [(" ", "")] * max(layout.col_w - 4, 1)
        rows.append([("(", "accent"), ("o", "accent")]
                    + lower_gap + [("o", "accent"), (")", "accent")])

    band_w = layout.col_w - 4
    band = [(g["dot"] if i % 2 == 0 else g["bot"], "ramp1")
            for i in range(band_w)]
    rows.append(_center_cells(band, layout.col_w))
    return rows


def _base_geometry(g: dict, layout: BootLayout,
                   base_stage: int) -> List[List[_Cell]]:
    """Attic-style base: optional fillet and step around torus/plinth."""
    blank = [(" ", "")] * layout.col_w
    if base_stage < 2:
        rows = [blank[:] for _ in range(layout.base_rows - 1)]
        rows.append(_shaded([g["lo"]] * layout.col_w)
                    if base_stage == 1 else blank[:])
        return rows

    rows: List[List[_Cell]] = []
    if layout.base_rows == 4:
        fillet_w = layout.shaft_w + 2
        fillet = [g["bot"]] + [g["block"]] * (fillet_w - 2) + [g["bot"]]
        rows.append(_center_cells(_shaded(fillet), layout.col_w))
    torus_w = layout.shaft_w + 2
    torus = [g["bot"]] + [g["block"]] * (torus_w - 2) + [g["bot"]]
    rows.append(_center_cells(_shaded(torus), layout.col_w))
    if layout.base_rows >= 3:
        step_w = layout.col_w - 2
        step = [g["top"]] + [g["block"]] * (step_w - 2) + [g["top"]]
        rows.append(_center_cells(_shaded(step), layout.col_w))
    rows.append(_shaded([g["top"]] * layout.col_w))
    return rows


def _column_rows(g: dict, layout: BootLayout, grown: int, tick: int,
                 rising: bool, base_stage: int = 2) -> List[List[_Cell]]:
    """One column as ``layout.art_rows`` rows, ``layout.col_w`` wide.

    grown: shaft rows built so far (0.._SHAFT_ROWS); the capital arrives
    at full growth. rising: growth animation active — the newest row is
    the white-hot scanline and a spark floats above it. base_stage:
    0 = nothing, 1 = plinth sketching in, 2 = torus + plinth set.
    """
    rows: List[List[_Cell]] = []
    cap_on = grown >= layout.shaft_rows

    if cap_on:
        rows.extend(_capital_geometry(g, layout))
    else:
        rows.extend([[(' ', '')] * layout.col_w
                     for _ in range(layout.capital_rows)])

    first_grown = layout.shaft_rows - grown
    for s in range(layout.shaft_rows):
        if s < first_grown:
            cells: List[_Cell] = [(" ", "")] * layout.col_w
            if rising and grown and s == first_grown - 1:
                # the spark above the scanline — one bright mote drifting
                spark = random.Random(_SEED + tick * 17 + s)
                cells[2 + spark.randrange(layout.col_w - 4)] = (g["dot"], "hot")
            rows.append(cells)
            continue
        newest = s == first_grown
        rows.append(_shaft_row(g, layout.shaft_w, layout.col_w,
                               hot=rising and newest))

    rows.extend(_base_geometry(g, layout, base_stage))
    return rows


def _paint_row(spec: BootSpec, cells: List[_Cell]) -> str:
    """Cells → one escaped string. Adjacent same-paint cells share one
    escape; mono tier emits bare glyphs."""
    if spec.color_depth == 0:
        return "".join(ch for ch, _ in cells)
    palette = {
        "ramp0": _VIOLET_RAMP[0], "ramp1": _VIOLET_RAMP[1],
        "ramp2": _VIOLET_RAMP[2], "ramp3": _VIOLET_RAMP[3],
        "accent": _VIOLET_ACCENT, "hot": _WHITE_HOT,
    }
    out: List[str] = []
    current = None
    for ch, paint in cells:
        if ch == " ":
            out.append(" ")
            continue
        if paint != current:
            out.append(spec.color(palette.get(paint, _GREY),
                                  bold=(paint == "hot")))
            current = paint
        out.append(ch)
    out.append(spec.reset)
    return "".join(out)


# ── the ocean — WHOLE LINES, ONE PER BEAT ──
#
# Each squiggle arrives fully formed on its beat, one line after another
# going down (frame 6 = three complete lines, frame 7 = the whole stack).
# No line extends left-to-right and nothing churns, flickers, or re-shines.
# Once every line has appeared, the finished drawing simply holds under
# the teal Loading line.
#
# The envelope is a centered sonar cone opening DOWNWARD: the narrowest
# line sits at the top and every successive line is wider. (An earlier
# reading had this as a wide-top funnel — it is the other way round.)

_LOADING_SLOTS = 8         # the stack's depth (frame 7 shows eight)
# The operator frames this as a tide of data: gravity accelerates the
# whole-line landings as the stack falls. Taste tweaks belong in this tuple.
_TIDE_LANDING_TICKS = (0, 7, 13, 18, 22, 25, 27, 29)
_CRASH_HOLD_TICKS = 4      # let the last wave settle before text splashes
_TIDE_MIN_TICKS = _TIDE_LANDING_TICKS[-1] + 1 + _CRASH_HOLD_TICKS
_INTER_ROW_BEAT_TICKS = 3  # brief, skippable hold between atomic text rows


def _fan(width: int, row: int, n_rows: int) -> Tuple[int, int]:
    """Return the centered sonar-cone envelope for wave slot `row`.

    The top is 40% of the drawable width; the bottom leaves a fixed
    two-cell inset on each side. Smoothstep makes the widening monotonic
    without giving the endpoints any wobble — the squiggle supplies the
    texture.
    """
    if width <= 0:
        return 0, 0
    t = row / max(n_rows - 1, 1)
    t = 1.0 - t
    ease = t * t * (3.0 - 2.0 * t)
    wide_length = max(1, width - 4)
    narrow_length = min(wide_length, max(1, round(width * 0.40)))
    length = round(wide_length - (wide_length - narrow_length) * ease)
    indent = (width - length) // 2
    return indent, length


def _wave_line(g: dict, width: int, row: int, n_rows: int) -> str:
    """One complete line in its fan slot, fixed for the whole drawing."""
    r = random.Random(_SEED * 31 + row * 7)
    indent, length = _fan(width, row, n_rows)
    phase = row * 1.7                 # each line its own fixed curve
    chars: List[str] = [" "] * width
    lo = indent
    hi = min(indent + length, width)
    for x in range(lo, hi):
        h = math.sin(x * 0.31 + phase) + r.random() * 0.55 - 0.27
        if h > 0.62:
            chars[x] = g["wave_hi"]
        else:
            chars[x] = g["wave_lo"]
    return "".join(chars).rstrip().ljust(width)


def wave_count(n_rows: int) -> int:
    """Floor 8, cap 9: a full tide, plus the optional ninth home row."""
    return max(_LOADING_SLOTS, min(9, n_rows))


def wrap_spans(spans: List[Tuple[str, str]], width: int) -> List[List[Tuple[str, str]]]:
    """Word-wrap colored spans into rows that fit the ocean — the
    disclaimer is 'a one-liner or two-liner' (CLI.txt) depending on the
    terminal's width. Colors survive the breaks."""
    words: List[Tuple[str, str]] = []
    for kind, text in spans:
        for w in text.split():
            words.append((kind, w))
    # punctuation that opened a span glues to the word before it
    rows: List[List[Tuple[str, str]]] = []
    cur: List[Tuple[str, str]] = []
    cur_len = 0
    for kind, w in words:
        need = len(w) + (1 if cur else 0)
        if cur and cur_len + need > width:
            rows.append(cur)
            cur, cur_len, need = [], 0, len(w)
        if cur:
            if cur[-1][0] == kind:
                cur[-1] = (kind, cur[-1][1] + " " + w)
            else:
                joiner = "" if w[0] in ",.;:!?" else " "
                cur[-1] = (cur[-1][0], cur[-1][1] + joiner)
                cur.append((kind, w))
        else:
            cur.append((kind, w))
        cur_len += need
    if cur:
        rows.append(cur)
    return rows


# Growth timing. The two pillars form PARALLEL to each other and rise
# simultaneously, both reaching the top fully formed from the ground up
# — not one after the other.
_TICKS_PER_ROW = 2


def _slot_row(layout: BootLayout, index: int,
              n_slots: int = _LOADING_SLOTS) -> int:
    """Art-row position of one fixed wave identity inside the shaft."""
    if n_slots <= 1:
        return layout.capital_rows
    return layout.capital_rows + round(
        index * (layout.shaft_rows - 1) / (n_slots - 1)
    )


def _home_rows(layout: BootLayout, n_rows: int) -> List[int]:
    """Resolved row positions, preserving the eight wave ordinals.

    The fixed identities are the landing rails, so the first eight home
    ordinals occupy those exact rows. A possible ninth row uses the first
    base-level ocean row; the capital/base air remains balanced to one row.
    """
    rows = [_slot_row(layout, i) for i in range(min(n_rows, _LOADING_SLOTS))]
    if n_rows > _LOADING_SLOTS:
        rows.append(min(_slot_row(layout, _LOADING_SLOTS - 1) + 1,
                        layout.art_rows - 1))
    return rows


def render_frame(spec: BootSpec,
                 phase: str,
                 tick: int,
                 status: str = "",
                 ocean_lines: Optional[List] = None,
                 splashed: int = 0,
                 hint: str = "",
                 ocean_slots: Optional[int] = None) -> List[str]:
    """One full frame: status + breath + adaptive art + pad row.
    Pure — same inputs, same frame.

    phase: "bases" | "columns" | "capitals" | "ocean" | "resolved"
    ocean_lines: entries are (kind, text) with kind ∈ teal|indigo|grey|
      white|red — OR (kind, [(kind, text), ...]) span-rows (the
      disclaimer's white frame + red core, per final.png). During
      "ocean", rows < `splashed` show their text (the splash lands
      whole and top-down). In "resolved", all rows show text.
    ocean_slots: optional ocean-band height request (floor 8, cap 9).
      It never changes the eight fixed wave identities; a ninth band
      row stays blank until text lands there.
    hint: dim right-aligned note on the pad row (the skip affordance) —
      animation phases only; the resolved frame is quiet.
    """
    g = spec.glyphs
    layout = spec.layout
    grey = spec.color(_GREY)
    teal = spec.color(_TEAL)
    indigo = spec.color(_INDIGO)
    reset = spec.reset

    # Column growth per phase — PARALLEL: both pillars form together,
    # ground up, and top out together.
    base_stage = 2
    if phase == "bases":
        grown = 0
        base_stage = 0 if tick < 1 else (1 if tick < 2 else 2)
    elif phase == "columns":
        grown = min(layout.shaft_rows, tick // _TICKS_PER_ROW + 1)
    else:
        grown = layout.shaft_rows

    # The final shaft row gets the same two-tick scanline beat as every
    # earlier row; the two tail ticks then show the completed shaft settled.
    rising = (phase == "columns"
              and tick < layout.shaft_rows * _TICKS_PER_ROW)
    left = _column_rows(g, layout, grown, tick, rising, base_stage)
    right = _column_rows(g, layout, grown, tick * 3 + 1, rising, base_stage)
    if phase == "columns":  # capitals arrive in their own phase
        blank = [(" ", "")] * layout.col_w
        for row in range(layout.capital_rows):
            left[row] = blank
            right[row] = blank
    if phase == "capitals" and tick < 3:  # slab lands, volutes follow
        blank = [(" ", "")] * layout.col_w
        for row in range(1, layout.capital_rows):
            left[row] = blank
            right[row] = blank

    ow = spec.ocean_width
    ocean_lines = ocean_lines or []
    # Home height controls only the centered resolved block. Loading owns
    # exactly eight stable identities distributed across the shaft.
    if ocean_slots is None:
        band_rows = min(9, max(len(ocean_lines), _LOADING_SLOTS))
    else:
        band_rows = min(9, max(len(ocean_lines), wave_count(ocean_slots)))
    home_rows = _home_rows(layout, band_rows)
    home_at = {row: i for i, row in enumerate(home_rows)}
    wave_at = {_slot_row(layout, i): i for i in range(_LOADING_SLOTS)}
    splash_cutoff = splashed
    kind_color = {"teal": teal, "indigo": indigo, "grey": grey,
                  "white": spec.color(_WHITE),
                  "red": spec.color(_RED, bold=True)}

    def _line_text(entry) -> str:
        text = (entry[1] if isinstance(entry[1], str)
                else "".join(t for _, t in entry[1]))
        return _ascii_text(text) if spec.ascii_only else text

    def _line_colored(entry) -> str:
        if isinstance(entry[1], str):
            text = _ascii_text(entry[1]) if spec.ascii_only else entry[1]
            return kind_color.get(entry[0], grey) + text
        # reset between spans so the red core's bold never bleeds into
        # the white frame that follows it
        return "".join(reset + kind_color.get(k, grey)
                       + (_ascii_text(t) if spec.ascii_only else t)
                       for k, t in entry[1])

    def _wave(identity: int) -> Optional[str]:
        if tick < _TIDE_LANDING_TICKS[identity]:
            return None
        return _wave_line(g, ow, identity, _LOADING_SLOTS)

    frame: List[str] = []
    # The loading line is TEAL; the resolved header settles to grey.
    status = _ascii_text(status) if spec.ascii_only else status
    hint = _ascii_text(hint) if spec.ascii_only else hint
    status_color = grey if phase == "resolved" else teal
    status_pad = (layout.canvas_width - len(status)) // 2 if status else 0
    margin = " " * layout.left_margin
    frame.append((margin + status_color + " " * max(status_pad, 0)
                  + status + reset)
                 if status else margin)
    frame.append(margin)  # breath between the header and the art

    for art_row in range(layout.art_rows):
        mid = " " * ow
        if phase in ("ocean", "resolved"):
            idx = home_at.get(art_row, -1)
            in_home = idx >= 0
            entry = ocean_lines[idx] if in_home and idx < len(ocean_lines) else None
            resolved_here = (phase == "resolved"
                             or (in_home and idx < splash_cutoff))
            if resolved_here and entry is not None and entry[0] != "blank":
                text = _line_text(entry)
                if len(text) > ow:  # honest ellipsis over silent cut
                    marker = "..." if spec.ascii_only else "…"
                    text = text[: ow - len(marker)] + marker
                    colored = kind_color.get(
                        entry[0] if isinstance(entry[1], str) else "white",
                        grey) + text
                else:
                    colored = _line_colored(entry)
                pad = " " * (ow - len(text))
                mid = colored + pad + reset
            else:
                wave_idx = wave_at.get(art_row)
                # Whole-line beats: each row appears complete on its
                # accelerating tide landing. Revealed lines hold.
                # An un-splashed row keeps its held wave even when a blank
                # spacer will land there — blank-ness matters at resolution
                # (HOLD: nothing changes at the seam but the landing row).
                # Waves exist ONLY in the ocean phase: the resolved home
                # renders its blank spacers as air, never as leftover water.
                line = (_wave(wave_idx) if phase == "ocean"
                        and wave_idx is not None
                        and wave_idx >= splash_cutoff else None)
                if line is not None:
                    color = teal if wave_idx % 2 == 0 else indigo
                    mid = color + line + reset
        frame.append(
            margin + " " + _paint_row(spec, left[art_row])
            + " " + mid + " "
            + _paint_row(spec, right[art_row]) + " "
        )
    if hint and phase != "resolved":
        pad = max(layout.canvas_width - len(hint) - 2, 0)
        dim = "\x1b[2m" if spec.color_depth else ""
        frame.append(margin + dim + grey + " " * pad + hint + reset)
    else:
        frame.append(margin)
    return frame


def visible_len(line: str) -> int:
    """Length with ANSI escapes stripped — the width-discipline check."""
    out, i = 0, 0
    while i < len(line):
        if line[i] == "\x1b":
            j = line.find("m", i)
            if j == -1:
                break
            i = j + 1
        else:
            out += 1
            i += 1
    return out


# ── keypress skip (best-effort; absence just means no skip) ────────────

class _SkipPoll:
    def __init__(self) -> None:
        self._restore: Optional[Callable[[], None]] = None
        self.available = False
        try:
            if not sys.stdin.isatty():
                return
            if sys.platform == "win32":
                import msvcrt  # noqa: F401
                self.available = True
            else:
                import termios
                import tty
                fd = sys.stdin.fileno()
                old = termios.tcgetattr(fd)
                tty.setcbreak(fd)
                self._restore = lambda: termios.tcsetattr(
                    fd, termios.TCSADRAIN, old)
                self.available = True
        except Exception:
            self.available = False

    def pressed(self) -> bool:
        if not self.available:
            return False
        try:
            if sys.platform == "win32":
                import msvcrt
                if msvcrt.kbhit():
                    msvcrt.getch()
                    return True
                return False
            import select
            r, _, _ = select.select([sys.stdin], [], [], 0)
            if r:
                sys.stdin.read(1)
                return True
            return False
        except Exception:
            return False

    def close(self) -> None:
        if self._restore:
            try:
                self._restore()
            except Exception:
                pass


# ── the runner ──────────────────────────────────────────────────────────

class TerminalBoot:
    """Plays the choreography around real work.

        boot = TerminalBoot()
        result = boot.step("the local server", start_it)   # ocean + status
        boot.finish("Z-SPAN: connected to local workspace",
                    [("teal", url), ("indigo", gh), ("grey", hint)])

    Plain tier (non-TTY / dumb / narrow / VT-less): the same facts as
    ordinary lines, no art. say() lines are captured under art and
    surface only on demote — nothing is ever hidden by the drawing.
    """

    _HINT = "any key skips"

    def __init__(self, *, out=None, force_plain: bool = False) -> None:
        self.out = out or sys.stdout
        self.spec: Optional[BootSpec] = (
            None if force_plain else detect_capabilities(self.out))
        self._captured: List[str] = []
        self._demoted = False
        self._intro_done = False
        self._skip = False
        self._tick = 0
        self._ocean_ticks = 0   # the drawing's own clock — starts at line one
        self._poll: Optional[_SkipPoll] = None
        self._terminal_cleaned = False
        # status + breath + art + pad/hint
        self._canvas_h = (self.spec.layout.canvas_h if self.spec is not None
                          else _ART_ROWS + 3)

    @property
    def art(self) -> bool:
        return self.spec is not None and not self._demoted

    # -- say: captured under art, immediate in plain tier ---------------
    def say(self, msg: str = "") -> None:
        if self.art:
            self._captured.append(msg)
        else:
            self._print(msg)

    # -- internals -------------------------------------------------------
    def _w(self, s: str) -> None:
        self.out.write(s)

    def _output_text(self, text: str) -> str:
        """Keep plain output writable even on ASCII or unusual streams."""
        try:
            encoding = getattr(self.out, "encoding", None) or "utf-8"
            text.encode(encoding)
        except Exception:
            return _ascii_text(text)
        return text

    def _print(self, text: str = "") -> None:
        print(self._output_text(text), file=self.out)

    def _draw(self, frame: List[str]) -> None:
        self._w(f"\x1b[{self._canvas_h}A")
        for line in frame:
            self._w("\x1b[2K" + line + "\n")
        self.out.flush()

    def _frame_sleep(self) -> None:
        time.sleep(_FRAME_DT)
        self._tick += 1
        if self._poll and self._poll.pressed():
            self._skip = True

    def _hint(self) -> str:
        return self._HINT if (self._poll and self._poll.available) else ""

    def _play_intro(self) -> None:
        """The boot takes the stage: clear the console (prompt echo,
        login banner — the terminal IS the frame now), center the canvas
        vertically, then: bases spark in → both pillars rise in parallel
        → capitals arrive."""
        if self._intro_done or not self.art:
            return
        self._intro_done = True
        self._poll = _SkipPoll()
        spec = self.spec
        assert spec is not None
        try:
            self._w("\x1b[?25l")             # hide cursor
            # Clear the visible stage but preserve acknowledgments and bundle
            # messages in scrollback above it.
            self._w("\x1b[H\x1b[2J")
            pad_top = max((spec.rows - self._canvas_h) // 2 - 1, 0)
            self._w("\n" * (pad_top + self._canvas_h))   # center the canvas
            columns_ticks = spec.layout.shaft_rows * _TICKS_PER_ROW + 2
            for phase, ticks in (("bases", 4),
                                 ("columns", columns_ticks),
                                 ("capitals", 6)):
                for t in range(ticks):
                    if self._skip:
                        return
                    self._draw(render_frame(spec, phase, t,
                                            hint=self._hint()))
                    self._frame_sleep()
        except BaseException:
            self._cleanup_terminal()
            raise

    def _demote(self, reason: str) -> None:
        """Art steps aside; the plain log takes over. Nothing lost."""
        if not self.art:
            return
        self._demoted = True
        self._cleanup_terminal()
        self._print(reason)
        for line in self._captured:
            self._print(line)
        self._captured.clear()

    def _cleanup_terminal(self) -> None:
        if self._terminal_cleaned:
            return
        self._terminal_cleaned = True
        try:
            self._w("\x1b[?25h")  # cursor back
            self.out.flush()
        finally:
            if self._poll:
                self._poll.close()
                self._poll = None

    # -- the public choreography -----------------------------------------
    def step(self, label: str, fn: Callable[[], object]) -> object:
        """Run fn under the ocean with an honest status line. The waves
        ARE the loading bar. Exceptions demote first, then propagate;
        a slow step demotes at the watchdog instead of freezing a wave."""
        if not self.art:
            self._print(f"→ {label} ...")
            result = fn()          # exceptions propagate plainly
            self._print("  ✓")
            return result

        try:
            self._play_intro()
            spec = self.spec
            assert spec is not None
            status = f"Loading: {label}"
            box: dict = {}

            def _run() -> None:
                try:
                    box["result"] = fn()
                except BaseException as e:  # surfaced by the main thread
                    box["error"] = e

            worker = threading.Thread(target=_run, daemon=True)
            started = time.monotonic()
            worker.start()
            # Even instant work plays the complete tide through its crash and
            # four settling frames before text may land. Slow work naturally
            # leaves the completed tide holding calm beneath the status line.
            min_ticks = _TIDE_MIN_TICKS
            t = 0
            while worker.is_alive() or t < min_ticks:
                if not worker.is_alive() and "error" in box:
                    break
                if time.monotonic() - started > _WATCHDOG_S:
                    self._demote(f"(still working: {label} — the boot art "
                                 "stepped aside so you can watch it plainly)")
                    worker.join()
                    break
                if self.art and not self._skip:
                    self._draw(render_frame(
                        spec, "ocean", self._ocean_ticks, status=status,
                        hint=self._hint(), ocean_slots=_LOADING_SLOTS))
                    self._ocean_ticks += 1
                    self._frame_sleep()
                else:
                    time.sleep(0.05)
                t += 1
            worker.join()

            if "error" in box:
                self._demote(f"✗ {label} failed — plain log:")
                raise box["error"]
            if not self.art:
                self._print(f"  ✓ {label}")
            return box.get("result")
        except BaseException:
            self._cleanup_terminal()
            raise

    def finish(self, header: str, lines: List) -> None:
        """The splash: each row atomically replaces its held wave, proceeding
        top-down with a short skippable beat between rows. Span entries
        ("spans", [(kind, text), ...]) — the
        disclaimer's white frame + red core — wrap to the ocean width
        first ("a one-liner or two-liner", CLI.txt). The resolved frame
        persists in scrollback; the terminal then just sits as the
        server."""
        if not self.art:
            self._print()
            self._print(header)
            for entry in lines:
                text = (entry[1] if isinstance(entry[1], str)
                        else "".join(t for _, t in entry[1]))
                self._print(f"  {text}")
            return

        try:
            self._play_intro()
            spec = self.spec
            assert spec is not None

            # Wrap span entries to this terminal's ocean width, then lay the
            # home out with final.png's breathing room: a blank row after the
            # disclaimer block, another before the quiet grey status lines.
            rows: List = []
            seen_grey = False
            for entry in lines:
                if isinstance(entry[1], str):
                    if entry[0] == "grey" and not seen_grey and rows:
                        rows.append(("blank", ""))
                        seen_grey = True
                    rows.append(entry)
                else:
                    for row_spans in wrap_spans(entry[1], spec.ocean_width):
                        rows.append(("white", row_spans))
                    rows.append(("blank", ""))
            if rows and rows[-1][0] == "blank":
                rows.pop()
            if len(rows) > 9:
                # The band holds nine — breathing room yields first.
                rows = [r for r in rows if r[0] != "blank"][:9]

            band_rows = min(9, max(len(rows), _LOADING_SLOTS))
            # The drawing's clock freezes here — un-splashed lines hold their
            # exact drawn state while the text lands through them.
            held = self._ocean_ticks
            # Named exception to the crash gate: impatience wins. A keypress
            # may skip every remaining splash frame straight to resolved text.
            if not self._skip:
                for ordinal in range(len(rows)):
                    # One complete row lands per frame. Previously landed rows
                    # stay text-only; unsplashed waves below hold byte-stable.
                    self._draw(render_frame(
                        spec, "ocean", held, status=header,
                        ocean_lines=rows, splashed=ordinal + 1,
                        hint=self._hint(), ocean_slots=band_rows))
                    if ordinal < len(rows) - 1:
                        for _ in range(_INTER_ROW_BEAT_TICKS):
                            self._frame_sleep()
                            if self._skip:
                                break
                    if self._skip:
                        break
            self._draw(render_frame(spec, "resolved", held,
                                    status=header, ocean_lines=rows))
        finally:
            self._cleanup_terminal()

    def fail(self, message: str) -> None:
        """External failure path (validation etc.) — same demotion."""
        if self.art:
            self._demote(message)
        else:
            self._print(message)


# The gate's acknowledgment sentence, verbatim (final.png's disclaimer
# line: white frame, red load-bearing core). Kept in lockstep with
# client/src/lib/projectMeta.ts § DISCLAIMER_ACK_SEGMENTS — the operator
# named that wording "really perfect... exactly verbatim" (CLI.txt).
DISCLAIMER_SPANS: List[Tuple[str, str]] = [
    ("white", "I understand the data presented on this website "),
    ("red", "may not be 100% accurate"),
    ("white", ", and I accept the consequence of treating it as such."),
]


if __name__ == "__main__":
    # Replay the full choreography with sample content, no server —
    # the operator-eyeball affordance:  python -m zspan_cli.boot
    _demo = TerminalBoot()
    if not _demo.art:
        print("(this terminal gets the plain tier — run in a real "
              "terminal to see the boot)")
    _demo.step("your local server", lambda: time.sleep(1.2))
    _demo.finish("Z-SPAN: connected to local workspace", [
        ("spans", DISCLAIMER_SPANS),
        ("teal", "Your workspace → http://127.0.0.1:8741/"),
        # framework GitHub line removed per D-182 (repo private) — mirrors cli.py
        ("indigo", "Support the work → ko-fi.com/zspan"),
        ("grey", "5 processed meetings ready · private intake complete"),
        ("grey", "Ctrl-C stops the server"),
    ])
