# Press-TV Financial Infographic — NotebookLM Pro · Cinematic prompt

**Tier:** NotebookLM Pro (Cinematic image generation).
**Refresh cadence:** daily, 09:00 local (driven by the Balance Auditor).
**Data source:** `parsers/financial_reports/infographic_data.csv` (attached to the dedicated *Z-SPAN Financial Infographic* notebook).
**Output:** PNG placed at `client/public/hq/press_infographic.png` for the HQ press TV.

**Status:** *Draft v0.* James 2026-06-02 — iterate on this prompt as the rendered output gets evaluated. The component lands first; this prompt becomes the dial.

---

## The prompt (paste into NotebookLM after attaching the CSV)

<!-- ZSPAN_MODEL_CONTENT_START -->

```
Generate a cinematic dark-mode press-conference infographic visualizing
Z-SPAN's current financial state, based on the attached CSV.

== AESTHETIC ==
- 2:1 aspect ratio (e.g. 2000×1000) — matches the in-app TV chrome
  aspect and avoids letterbox bars when scaled down. NOT 16:9.
- Dark base palette: deep navy backgrounds (#07142a → #0a1a36), with
  cyan accent (#8be9fd), amber highlights (#ffb84d), green for positive
  trends (#5cf08a), red for warnings/down trends (#ff8c8c).
- Subtle CRT-pixel-grid texture in the background for a "live data
  feed" feel; faint scanlines acceptable.
- Sharp pixel-art / cinematic news-broadcast aesthetic — NOT photo-
  realistic, NOT hand-drawn-watercolor. Think "Bloomberg Terminal
  meets a retro arcade marquee."
- Type: monospace for numbers + labels; clean sans for any prose.

== LAYOUT (top to bottom) ==
1. EYEBROW (small, top-left): "Z-SPAN · TREASURY" in cyan, all caps,
   wide letter-spacing. NO "LIVE" pill — the TV chrome owns the LIVE
   indicator in its own top-right corner; including one in the image
   creates a double-LIVE visual conflict (Opus finding 2026-06-02).

2. HEADLINE (middle, large): the current balance in neon cyan
   typography — the dominant visual element. Format: $XX,XXX (no
   decimals on display; CSV has the cents).

3. SUPPORTING STAT ROW (middle-lower, ~3 stats in a row):
   - Monthly Burn (USD/month)
   - Runway (months)
   - Days Since Last Deposit (compute from recent_event rows if
     available; "—" if not present)

4. RECENT ACTIVITY (bottom-third, narrow strip): mini-timeline or
   list of the last 3-5 ledger events (type + amount + relative time).

5. FOOTER (bottom edge, tiny): "Source: balance_ledger · audited"
   left-aligned, "Compiled <data_compiled_at>" right-aligned.

== TONE ==
- Transparent + sober. This is a real-numbers transparency surface, not
  a marketing piece. No upselling adjectives, no fake growth arrows.
- "Public" and "audited" are the truth-claim posture — match it.

== EXPLICITLY DO NOT ==
- Don't invent data not in the CSV.
- Don't include logos other than the Z-SPAN wordmark if used.
- Don't add stock-photo people or buildings — pure data visualization.
- Don't render in a light-mode palette.
- Don't add fictional projections / forecasts. Past + current state only.

== INTERACTION NOTE ==
This image renders at small scale inside a UI panel (~180×85 px in the
viewport), so legibility at small sizes matters. The headline number
should remain readable. The supporting stat row labels should be sized
*generously* — at downscale, anything that looked "small but fine" in
the 2000×1000 source becomes sub-pixel illegible. Render labels at
~4-5% of canvas height (so ~40-50px in the 1000px-tall source). The
PNG itself can be high-resolution; the page scales it down.
```

<!-- ZSPAN_MODEL_CONTENT_END -->

---

## What to iterate on (when the operator looks at the rendered output)

- **Headline number legibility at small render scale.** If the cyan glow is too soft, ask for "harder edge / less bloom."
- **Color hierarchy.** If amber + cyan compete for attention, demote one (subtle for stats, dominant for headline).
- **Density.** If too sparse → ask for a small chart (sparkline of last 7 daily spend rows). If too busy → cut the recent-activity strip.
- **Aspect ratio.** The TV displays at ~2:1. 16:9 PNGs fit fine but can be cropped to 2:1 if needed.

Re-paste the iterated prompt + re-attach the same CSV; NotebookLM keeps the notebook context.

---

## When to update this file

- A NEW field gets added to `infographic_data.csv` and the prompt should reference it.
- The aesthetic posture changes (e.g., move to a "warm dawn" palette for a major redesign).
- A successful iteration produces a stable prompt — overwrite the draft above with the proven version.

The auditor + the front-end don't depend on this file's exact wording; this is purely the operator-facing curated prompt.
