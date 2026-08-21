/**
 * cityDeskTheme — the shared City Desk paper theme (S-123), used by the
 * desk page and the try-it demo.
 *
 * Palette: PrisonBreak's inner-page dark warm walnut (operator-picked
 * over the bright front-page cream, 2026-07-02).
 *
 * Fonts (operator feedback, same day): body/UI runs **Atkinson
 * Hyperlegible** — the locked "Hyperlegible" preset from the operator's
 * The-Cacti project (D-014 there: Atkinson for display+body, JetBrains
 * Mono for mono; picked for maximum readability) — because Newsreader
 * serif on the dark ground was hard on the eyes. Caveat stays ONLY as
 * the hand-drawn accent (big titles, pills, the plant callout, the
 * growth indicator) — that's the PrisonBreak artifact language, at
 * display sizes where it reads comfortably.
 */
import { useEffect } from "react";

export const CITY_DESK_FONT_HREF =
  "https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:ital,wght@0,400;0,700;1,400;1,700&family=Caveat:wght@400..700&family=JetBrains+Mono:wght@400;600&display=swap";

export function usePaperFonts(): void {
  useEffect(() => {
    if (document.querySelector('link[data-city-desk-fonts="2"]')) return;
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = CITY_DESK_FONT_HREF;
    link.setAttribute("data-city-desk-fonts", "2");
    document.head.appendChild(link);
  }, []);
}

export const PAPER_CSS = `
.city-desk {
  --ink: oklch(0.93 0.015 85);
  --ink-soft: oklch(0.72 0.014 80);
  --paper: oklch(0.20 0.014 60);
  --paper-deep: oklch(0.24 0.015 65);
  --rule: oklch(0.38 0.013 70);
  --bloom: oklch(0.68 0.13 138);
  --bloom-soft: oklch(0.45 0.09 135);
  --flower: oklch(0.74 0.13 320);
  --hot: oklch(0.66 0.18 25);
  --amber: oklch(0.78 0.15 75);
  --disclaimer: oklch(0.68 0.18 35);

  color: var(--ink);
  font-family: "Atkinson Hyperlegible", -apple-system, "Segoe UI", sans-serif;
  min-height: 100vh;
  background-color: var(--paper);
  background-image:
    radial-gradient(circle at 18% 8%, oklch(0.92 0.05 85 / 0.045), transparent 42%),
    radial-gradient(circle at 82% 92%, oklch(0.92 0.05 85 / 0.055), transparent 46%),
    repeating-linear-gradient(0deg, transparent 0 23px, oklch(0.92 0.05 85 / 0.018) 23px 24px);
}
.city-desk .hand { font-family: "Caveat", cursive; }
.city-desk .kalam {
  font-family: "Atkinson Hyperlegible", sans-serif;
  font-style: italic;
}
.city-desk .mono { font-family: "JetBrains Mono", ui-monospace, monospace; }
.city-desk .ink-frame {
  background: var(--paper-deep);
  border: 1.4px solid var(--ink);
  border-radius: 4px;
  box-shadow: 0 1px 0 oklch(0.93 0.015 85 / 0.05), 0 12px 36px -28px rgb(0 0 0 / 0.6);
}
.city-desk .ink-frame-soft {
  background: var(--paper-deep);
  border: 1px solid var(--rule);
  border-radius: 4px;
}
.city-desk .ink-pill {
  display: inline-flex; align-items: center; gap: 0.5rem;
  padding: 7px 14px;
  font-family: "Atkinson Hyperlegible", sans-serif;
  font-weight: 700;
  font-size: 15px; line-height: 1;
  color: var(--ink); background: var(--paper-deep);
  border: 1.4px solid var(--ink); border-radius: 4px;
}
.city-desk .ink-pulse {
  width: 10px; height: 10px; border-radius: 999px;
  background: var(--hot);
  animation: cd-ink-blink 1s ease-in-out infinite;
}
.city-desk .ink-pulse.is-done { background: var(--bloom); animation: none; }
.city-desk .ink-pulse.is-idle { background: var(--rule); animation: none; }
@keyframes cd-ink-blink { 0%,100% { opacity: 1 } 50% { opacity: 0.25 } }
.city-desk .graph-paper {
  background-color: var(--paper);
  background-image:
    linear-gradient(to right, oklch(0.93 0.015 85 / 0.06) 1px, transparent 1px),
    linear-gradient(to bottom, oklch(0.93 0.015 85 / 0.06) 1px, transparent 1px);
  background-size: 22px 22px;
}
.city-desk .note-band {
  border: 1.4px solid var(--disclaimer);
  color: var(--disclaimer);
  background: oklch(0.68 0.18 35 / 0.08);
  border-radius: 4px;
}
/* Titles + controls run Atkinson (the operator's Hyperlegible preset —
   the earlier Caveat-for-display call read as "unchanged fonts" and was
   corrected 2026-07-02). Caveat survives ONLY inside the tree's own
   artifact language: the plant callout, the growth indicator, and the
   under-tree hand-notes. */
.city-desk h1 {
  font-family: "Atkinson Hyperlegible", sans-serif;
  font-weight: 700; font-size: 30px; line-height: 1.25;
}
.city-desk h2 {
  font-family: "Atkinson Hyperlegible", sans-serif;
  font-weight: 700; font-size: 20px; line-height: 1.3;
}
.city-desk button.ink-btn {
  font-family: "Atkinson Hyperlegible", sans-serif;
  font-weight: 600;
  font-size: 13.5px; line-height: 1;
  padding: 9px 16px;
  color: var(--paper); background: var(--ink);
  border: 1.4px solid var(--ink); border-radius: 4px;
  cursor: pointer;
}
.city-desk button.ink-btn:hover { opacity: 0.88; }
.city-desk button.ink-btn.ghost { color: var(--ink); background: transparent; }
.city-desk button.ink-btn.ghost:hover { background: var(--paper-deep); opacity: 1; }
.city-desk button.ink-btn:disabled { opacity: 0.4; cursor: not-allowed; }
.city-desk .vote-box {
  width: 26px; height: 26px; border: 1.4px solid var(--ink); border-radius: 4px;
  display: inline-flex; align-items: center; justify-content: center;
  font-family: "Caveat", cursive; font-size: 19px; cursor: pointer;
  background: var(--paper-deep); color: var(--ink);
}
.city-desk .vote-box.checked-yes { background: var(--bloom-soft); }
.city-desk .vote-box.checked-no { background: oklch(0.66 0.18 25 / 0.28); }
.city-desk input.ink-input, .city-desk select.ink-input {
  font-family: "Atkinson Hyperlegible", sans-serif;
  font-size: 14.5px;
  color: var(--ink); background: var(--paper);
  border: 1px solid var(--rule); border-radius: 4px;
  padding: 6px 10px; outline: none;
}
.city-desk input.ink-input:focus { border-color: var(--ink); }
.city-desk .drawer-tab {
  appearance: none;
  font-family: "Atkinson Hyperlegible", sans-serif;
  font-weight: 700;
  font-size: 13.5px; line-height: 1;
  padding: 9px 14px;
  color: var(--ink-soft); background: transparent;
  border: 1.4px solid transparent; border-radius: 4px;
  cursor: pointer;
}
.city-desk .drawer-tab:hover { color: var(--ink); }
.city-desk .drawer-tab.active {
  color: var(--ink);
  border-color: var(--ink);
  background: var(--paper);
}
/* Print the minutes: everything hides except the .cd-print-area, which
   remaps to ink-on-white so the draft comes off the office printer
   looking like the document it is. */
@media print {
  body * { visibility: hidden; }
  .cd-print-area, .cd-print-area * { visibility: visible; }
  .cd-print-area {
    position: absolute; left: 0; top: 0; width: 100%;
    background: #fff !important; border: none !important; box-shadow: none !important;
    color: #111 !important; padding: 24px !important;
  }
  .cd-print-area * { color: #111 !important; background: transparent !important; }
  .cd-print-area .no-print { display: none !important; }
}
`;
