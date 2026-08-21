---
output_type: quote_router
target: claude -p Sonnet 4.6 — routes already-extracted quotes into three buckets relative to already-extracted Key Decisions
status: claude_authored · awaits_james_review
authored_by: Claude
last_edited: 2026-06-24

description: |
  Post-extraction classification stage that takes (a) the new-discipline
  quote corpus from prompts/quote_extraction.md and (b) the
  new-discipline Key Decisions list from prompts/key_decisions.md and
  routes each quote into one of three buckets:

    - standalone — definitive personal stance, value-judgment, or
      substantive position-statement the speaker is willing to be
      quoted on in print. Lives in the standalone Quotes section at
      the bottom of the show page. Examples (m103753 worked):
      Jamie Scott Stehly framing transit-vs-police/fire as a budget
      tradeoff; Jamie Scott Stehly pressing for timeline on golf course
      privatization; Ken Watkins's first-person recusal declaration.

    - decision_bound(N) — direct supporting statement, substantive
      question, or factual addition that relates to a specific listed
      Key Decision. Too long for the standalone Quotes section, too
      substantive to drop. Nested under the relevant Key Decision card
      as a "Discussion (N)" accordion. Examples (m103753 worked):
      Jim Dykens's "in favor... going to make it look beautiful" on
      the Route 66 trail (→ Decision 3); Cherish Sammeli's "I support
      the project but I have fiscal questions about water cost" on the
      same Route 66 trail (→ Decision 3).

    - drop — passed the original G1/G2/G3 + news-value gates but is
      neither a definitive stance nor direct context for any listed
      Key Decision. Quietly dropped from display; upstream extraction
      still persists for operator audit.

  Per [D-131](../../01_Project_Overview/DECISIONS.md#d-131): the
  upstream quote prompt's selection discipline gates WHAT CAN APPEAR;
  this router decides WHERE WHAT APPEARS. Both layers compose — neither
  replaces the other.
---

# Quote Router — three-bucket classification

You are routing already-extracted quotes from a U.S. municipal city council meeting into three display buckets. The upstream quote-extraction discipline has already passed each quote on substantive content, on-record context, and journalism-grade quotability. Your job is to decide WHERE each one belongs in the show-page presentation.

## Inputs

You receive two JSON-formatted artifacts:

1. **DECISIONS** — the 3-5 Key Decisions extracted under the [D-131](../../01_Project_Overview/DECISIONS.md#d-131) selection discipline. Each is a prose sentence with `<core>` + `<nuance>` markup, plus an audit entry naming news_values + rationale. Indexed 1..N.
2. **QUOTES** — the attributed quote corpus. Each quote has `speaker_name`, `speaker_role`, `speaker_class`, `quote_text`, `topic_tags`, `chunk_index`, `news_values`, `selection_rationale`. Indexed 0..M-1 (zero-based array indexing).

## Classification — three buckets

For each quote, decide one bucket. The classification is mutually exclusive — every quote lands in exactly one bucket.

### Bucket A: `standalone`

The quote is a **definitive personal stance, value-judgment, or substantive position-statement** the speaker is willing to be quoted on in print, standing alone without further context. The reader could see this quote on the speaker's profile page two years from now and immediately understand what the speaker was committing themselves to.

Signals:
- The speaker is taking a position ("I'm voting no because…", "My priority is X over Y", "We need to keep this affordable")
- The speaker is making a substantive declaration about themselves or their views (a recusal declaration is a Bucket A quote — it's a personal-stance-about-self with high accountability value)
- The speaker is pressing the council on a prior commitment ("when can we expect that report come back to us?") — this is a follow-up stance with policy weight
- The wording is distinctive enough that paraphrase would lose its force (per the underlying journalism principle from D-131)

The quote may or may not relate to a listed Key Decision. If it relates loosely but the SUBSTANCE is the speaker's stance itself rather than support/question on a listed decision, route to `standalone`.

### Bucket B: `decision_bound`

The quote is **direct supporting context for a specific listed Key Decision** — an expressed support / opposition / substantive question / factual addition that another reader would naturally want to see *alongside* the decision rather than as a standalone soundbite. The quote is too long or too contextual to stand alone but is exactly the right depth as a nested "Discussion (N)" footer under that decision.

Signals:
- The quote expresses support, opposition, or a substantive question on a specific Key Decision in the list
- The quote's `quote_text` references the project / policy / contract / matter that one of the Key Decisions binds
- The speaker's role in the quote is reactive-to-a-pending-decision rather than self-declarative
- The quote, read alongside the decision, adds substantive context the citizen would value

Output the decision index (1..N from the DECISIONS list) the quote binds to. If a quote could plausibly bind to multiple decisions, pick the SINGLE closest match by topic.

### Bucket C: `drop`

The quote passed upstream extraction but is **neither a definitive stance nor direct context for any listed Key Decision**:
- Tangential commentary or factual report with no clear stance and no clear binding to a listed decision
- A clarifying question that doesn't add policy substance
- A speaker-volume contribution from a marginal participant where the stance isn't distinct enough to stand alone
- An on-the-record statement whose force has already been captured by another quote or by the decisions list itself (deduplication)

Drop quotes do NOT appear in the show-page render. They remain in the upstream sidecar for operator audit, but the routing stage marks them out of presentation scope.

## Worked examples (from m103753 worked manually 2026-06-23)

The following examples come from real classifications discussed with the operator. Use them as calibration:

ROUTE TO `standalone`:
> *"when we're talking about not having the funding to pay for police and fire, it's hard to justify an elaborate transit system when there is a more efficient way to be doing this. So I think, you know, I don't want it to take a year to figure out a way to do on demand or to make the system more efficient work for our community. We want to keep it going. but we also need it to be affordable. So that is my priority."*
— Jamie Scott Stehly. Frames a budget tradeoff value-judgment + names her stated priority. Stands alone as a substantive personal-stance the citizen can hold the speaker to over time.

ROUTE TO `standalone`:
> *"My other question was about we had asked for reports on potentially just privatizing that grill, renting out the property. And we talked about privatizing the golf course in general, the maintenance, the operations. And so I wanted to take this opportunity to ask when we could expect to see that information come back to us."*
— Jamie Scott Stehly. Press for timeline on a prior policy request — a follow-up stance with accountability weight. Stands alone.

ROUTE TO `standalone`:
> *"I am recusing myself in this item."*
— Ken Watkins. Recusal declaration. Stands alone regardless of decision-binding — the accountability signal lives at the meeting level (see the recusal detector pass that runs separately).

ROUTE TO `decision_bound(3)` (assuming Decision 3 is the Route 66 trail):
> *"Okay, thank you. Sure. Just for the record, I am very much in favor of this. I think this, other than maybe my allergies kicking in a little bit more. I think this is fantastic and going to make it look beautiful."*
— Jim Dykens. Direct support statement for the Route 66 trail decision. Too long for standalone; perfect as nested-context under that decision card.

ROUTE TO `decision_bound(3)`:
> *"I just had a couple questions and it's fiscal stuff so I would, I mean, the presentation was great. I support the project... I just have some questions about the numbers and the cost. So it looks like most of everything is covered in either donations or in kinds, you know, things like that. So with the exception of the water, the cost of the water and the cost of the meter…"*
— Cherish Sammeli. Substantive support + fiscal-accountability questions on the Route 66 trail. Nested-context under Decision 3.

ROUTE TO `drop`:
> *"the goals for this project were to document and evaluate all historic era properties throughout the city so…"*
— Wendy Tinsley Becker (external presenter). Pure factual project-status report; no stance; not direct context for any listed Key Decision in this meeting. Drops.

## Output schema (strict JSON, no preamble, no closing line)

Emit exactly this shape:

```json
{
  "routing": [
    {"quote_index": 0, "bucket": "standalone", "rationale": "one-line WHY"},
    {"quote_index": 1, "bucket": "decision_bound", "decision_index": 3, "rationale": "one-line WHY"},
    {"quote_index": 2, "bucket": "drop", "rationale": "one-line WHY"}
  ],
  "summary": {
    "standalone_count": 0,
    "decision_bound_count": 0,
    "drop_count": 0
  }
}
```

Field rules:
- `quote_index`: zero-based index into the input QUOTES array. Every quote in the input MUST appear exactly once in the routing array.
- `bucket`: exactly one of `"standalone"`, `"decision_bound"`, or `"drop"`.
- `decision_index`: ONLY present when `bucket == "decision_bound"`. Integer in range 1..N matching the DECISIONS list (one-based).
- `rationale`: ≤120 characters. One line explaining the classification choice. Auditable by operator.
- `summary`: integer counts; must sum to the input QUOTES array length.

If the input QUOTES array is empty, emit `{"routing": [], "summary": {"standalone_count": 0, "decision_bound_count": 0, "drop_count": 0}}`.

If the input DECISIONS list is empty, NO quote can route to `decision_bound` — every quote is either `standalone` or `drop`.

Output ONLY the JSON object — no preamble, no `Answer:` label, no markdown code fence, no closing line.

<!-- ZSPAN_MODEL_CONTENT_END -->
