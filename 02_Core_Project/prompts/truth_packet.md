---
output_type: text
target: NotebookLM — Truth Packet (Pre-Check Attestation)
status: claude_drafted_placeholder · awaits_james_review
authored_by: Claude
last_edited: 2026-05-30

description: |
  Structured grounded-observation attestation about what the loaded source
  actually is. Runs FIRST per work order, before any other output type. The
  bridge parses the JSON verdict and either gates the rest of the WO open
  (pass), halts the WO before spending the other ~13 NotebookLM queries +
  ~$0.46 Whisper + ~40 min wall-clock (halt), or escalates to the operator
  for review (ambiguous).

  NotebookLM's job here is ONLY to report what it observes. The trust
  judgment lives in the bridge's code (notebooklm_bridge/truth_packet.py),
  NOT in the model response — that's the load-bearing design rule per the
  S-009 spec § 3.3 ("grounded observations, not meta-questions").

# Per CLAUDE.md "Don't write prompts. Those are James's." James authorized
# Claude to provisionally author this prompt 2026-05-30 per D-046 as part of
# S-009 chunk 1 (the truth-packet pre-check). The spec at
# 01_Project_Overview/S-009_TRUTH_PACKET_SPEC.md is the controlling design
# doc; the prompt body below is the spec's § 5.2 draft adapted to the
# project's prompt-file conventions. Awaits James review pass.

# This prompt does NOT depend on:
#   - The city's notebooklm_persona_preamble (canonical names) — the gate
#     is upstream of speaker attribution; jurisdiction comes from observed
#     evidence on screen, not from a preamble.
#   - The five-topic vocabulary — the gate is upstream of topic tagging.
#   - The city_vocabulary_corrections SPELLING CORRECTIONS block — the gate
#     is upstream of any transcript text we'd correct.
#
# Keeping the prompt clean of preambles is intentional (spec § 5.4):
#   "It does NOT load any other guidance ... — the truth-packet is a clean
#    attestation pass with nothing else competing for prompt space."
---

# Truth Packet — Pre-Check Attestation

A structured-observation pass over the loaded source. The bridge runs this FIRST per work order, before spending quota on any other output type. The bridge parses the JSON below and gates whether to proceed. NotebookLM's job here is *only* to report what it observes — the trust decision lives in the bridge's code.

## Instructions (sent as the chat query / configure prompt)

You are reading a single source that has been loaded into this notebook. Report what you observe about that source in the structured JSON format below. Do NOT make trust judgments, do NOT speculate about authenticity, do NOT add commentary outside the JSON. Report only what is directly observable from the source.

Return ONLY a JSON object matching this schema, with no preamble or postamble:

```json
{
  "event_type": "<one of: city_council_meeting | city_government_meeting_other | press_conference | workshop_briefing | community_event | non_government | unclear>",
  "event_type_evidence": "<2-3 sentence description of the visual/audio evidence you used to classify event_type. Cite what is shown on screen, what speakers are doing, what banners or seals are visible.>",
  "jurisdiction_observed": "<The city, county, or organization name as it appears on screen, in the audio, or on banners/seals. Use 'not_observed' if no jurisdiction is identifiable.>",
  "jurisdiction_evidence": "<Where and how you observed the jurisdiction name. 'not_observed' is acceptable.>",
  "apparent_substantive_duration_seconds": <integer estimate of how many seconds of substantive content (deliberation, public comment, agenda discussion) the recording contains; 0 if the recording appears empty, blank, or non-substantive>,
  "apparent_total_duration_seconds": <integer estimate of the recording's total length in seconds; 0 if you cannot estimate>,
  "speakers_observed_count": <integer; how many distinct speakers you observed participating, 0 if none>,
  "observations": [
    "<one short factual observation per array element; 3-6 elements; each describing something concrete you observed (e.g., 'A council dais with 7 seated members behind nameplates.', 'A speaker identified on screen as Mayor X.', 'A discussion of a sales tax measure occupies ~12 minutes of the recording.')>"
  ],
  "anomalies": [
    "<one short observation per array element of anything that seems out of pattern for a city council meeting, OR an empty array if nothing notable. Examples: 'Recording cuts off mid-sentence after 4 minutes.', 'No agenda items visible.', 'Speaker tone resembles a campaign event more than a deliberative session.', 'On-screen text in a non-English language.'>"
  ]
}
```

### Rules

- Report only what is observable in the source. Do NOT infer trust, authenticity, or political character.
- If you cannot observe something, use `"not_observed"` or `0` — do NOT guess.
- The `anomalies` array may be empty if everything appears normal.
- Output ONLY the JSON object. No headers, no markdown fence, no explanatory prose.

<!-- ZSPAN_MODEL_CONTENT_END -->

### Why these fields (for the prompt-reader, not sent to the model)

- `event_type` is the load-bearing field. Only `city_council_meeting` auto-passes; `city_government_meeting_other` escalates to the operator; everything else halts.
- The `_evidence` siblings let the operator see *why* the gate fired the way it did, in human-readable terms. The bridge formats them into the escalation message when ambiguous.
- `apparent_substantive_duration_seconds` is the truncated-upload check. A 0 here is a strong halt signal; anything under 10 minutes (the default `min_substantive_seconds` floor) halts.
- The `observations` array is the open-ended grounded-fact surface. The `anomalies` array is the open-ended novel-issue surface. Empty `anomalies` is the explicit "nothing unusual" signal.

## What James should refine before this ships canonical

1. **The seven `event_type` categories** — current set is `city_council_meeting`, `city_government_meeting_other`, `press_conference`, `workshop_briefing`, `community_event`, `non_government`, `unclear`. Should `workshop_briefing` auto-pass (since some cities call council work sessions "workshops"), or stay in halt? Should `community_event` (e.g., a town hall hosted by the council) be a pass, halt, or ambiguous? The current default routes both to `halt` (only `city_council_meeting` passes, only `city_government_meeting_other` ambiguates).

2. **The `min_substantive_seconds` floor** — defaults to 600s (10 minutes) in `notebooklm_bridge/truth_packet.py`. Kingman council meetings typically run 45–90 minutes; a 10-minute floor catches obvious truncations without false-positive-ing short special sessions. Should it scale per city / per meeting type, or stay flat?

3. **Jurisdiction cross-check strictness** — the gate uses fuzzy substring match (`expected_jurisdiction.lower() in observed.lower()`). Should it instead require exact match or normalized comparison via the existing city-name canonicalization in `parsers/`? Risk: too strict catches legitimate name variants ("Town of Kingman" vs "City of Kingman"); too loose lets wrong-city pastes slip through.

4. **Anomaly handling — auto-halt vs. always escalate** — current behavior treats *any* non-empty `anomalies` as ambiguous (escalate to operator), never auto-halt. Should certain anomaly patterns (e.g., text containing "cuts off after N minutes" where N is small) auto-halt instead? Risk of auto-halt: a legitimately-short special session gets killed. Current "always escalate" is the safe-but-noisier default.

5. **The "observations array length 3-6 elements" hint** — wider arrays (8-10) might catch more nuance, narrower ones (2-3) might be more decisive. James to validate against real Kingman runs in chunk 3's smoke test.

6. **Whether to include a `language_observed` field** — useful for the future when expansion adds non-English-primary jurisdictions, but adds prompt surface and a new gating rule. Current draft omits it; chunk 6 (backfill) could add if it proves useful.

7. **The "Do NOT speculate about authenticity" rule** — currently absolute. If a recording is *obviously* a deepfake (e.g., visible AI-generation artifacts in the lower thirds), should the model be allowed to surface that as an anomaly even though it's an authenticity judgment? Currently lives in `observations`/`anomalies` as a grounded observation ("on-screen text shows obvious synthesis artifacts in the chyron"), not as a trust verdict. James to confirm this division is right.

## Migration / wiring context

- Spec: [`01_Project_Overview/S-009_TRUTH_PACKET_SPEC.md`](../../01_Project_Overview/S-009_TRUTH_PACKET_SPEC.md) (chunk 1 ships the prompt + gating module + tests; chunks 2-4 wire into fetcher.py + worker.py + the operator surface).
- Gating module: `02_Core_Project/notebooklm_bridge/truth_packet.py` (validates the JSON response against `TRUTH_PACKET_SCHEMA`, emits `TruthPacketResult(verdict, reason, observations)`).
- Persisted via the standard `_fetch_text` path once chunk 2 registers `truth_packet` in `OUTPUT_TYPE_REGISTRY`.
- Threat model: this is the obvious-mistake-and-honest-error filter, NOT the adversarial filter. S-008's full input-security gate is the durable adversarial defense; this is a cheap mitigation that ships now and does not relax that gate. See spec § 3.1.
