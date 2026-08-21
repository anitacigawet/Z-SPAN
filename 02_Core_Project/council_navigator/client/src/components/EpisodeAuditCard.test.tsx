import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  applyEpisodeAuditFix,
  EpisodeAuditCardBody,
  nextApprovalPhase,
  PublishedRecordConfirm,
  submitEpisodeAuditDisposition,
  type AuditResponse,
} from "./EpisodeAuditCard";

function proposalResponse(): AuditResponse {
  return {
    status: "ok",
    run: {
      run_id: "run-23",
      verdict: "flags",
      run_status: "complete",
      findings_count: 1,
      deterministic_flags_count: 0,
      report: {
        llm: {
          findings: [
            "**Incorrect councilmember name**\n\nThe newsletter differs from the transcript.",
          ],
          proposals: [
            {
              id: "proposal-1",
              finding_number: 1,
              target_output: "newsletter",
              before: "**Jon Smyth** opened the hearing.",
              after: "**John Smith** opened the hearing.",
              fix_rationale:
                "The transcript and key decisions both use **John Smith**.",
              validated: true,
              apply_gated: false,
              checks: {
                exact_match_in_newsletter: true,
                citations_intact: true,
                no_new_names: true,
              },
              parse_ok: true,
              delimiters_ok: true,
            },
          ],
          no_safe_proposals: [],
          open_findings: [],
          suggestions: [],
        },
        deterministic: {},
      },
    },
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("EpisodeAuditCard review content", () => {
  it("renders prose findings and human-readable deterministic checks", () => {
    const response: AuditResponse = {
      status: "ok",
      run: {
        verdict: "flags",
        run_status: "complete",
        findings_count: 1,
        deterministic_flags_count: 3,
        started_at_utc: "2026-07-28T12:00:00Z",
        duration_seconds: 12.4,
        report: {
          llm: {
            verdict_line: "**Four items deserve a look.**",
            findings: [
              "**Name conflict in the tagline**\n\nThe surname differs from the key decisions.",
            ],
            open_findings: [],
            suggestions: [],
          },
          deterministic: {
            entity_consistency: {
              status: "completed",
              variant_collisions: [
                {
                  kind: "FLAG",
                  spellings: ["Jon Smith", "John Smith"],
                  outputs: {
                    "Jon Smith": ["tagline"],
                    "John Smith": ["key_decisions"],
                  },
                },
              ],
            },
            locator_existence: {
              status: "completed",
              citations_checked: 3,
              out_of_range: [{ citation: "01:30:00" }],
            },
            quote_existence: {
              status: "completed",
              quotes_checked: [{ quote: "one" }, { quote: "two" }],
              llm_evidence_not_found: [{ quote: "two" }],
            },
            entropy: {
              status: "completed",
              low_entropy_window_count: 2,
              regions: [{ start: 1 }, { start: 2 }],
            },
            provenance: { status: "recorded" },
            valid_empty: {
              status: "completed",
              valid_empty: ["community_calls_to_action"],
            },
          },
        },
      },
    };

    const markup = renderToStaticMarkup(
      <EpisodeAuditCardBody response={response} />,
    );

    expect(markup).toContain("Episode audit — 4 flags");
    expect(markup).toContain("Four items deserve a look.");
    expect(markup).toContain("Name conflict in the tagline");
    expect(markup).toContain(
      "The surname differs from the key decisions.",
    );
    expect(markup).not.toContain("**");
    expect(markup).toContain(
      "Cross-output name check: 1 conflict — &quot;Jon Smith&quot; vs &quot;John Smith&quot; (tagline, key decisions).",
    );
    expect(markup).toContain(
      "Timecode citations: 3 checked; 1 point outside the meeting&#x27;s timeline.",
    );
    expect(markup).toContain(
      "Quoted evidence: 2 passages checked; 1 could not be matched verbatim.",
    );
    expect(markup).toContain(
      "Transcript machine-loop scan: 2 low-entropy regions noted.",
    );
    expect(markup).toContain("Generation provenance: recorded.");
    expect(markup).toContain(
      "Legitimately-empty outputs: community calls to action.",
    );
  });

  it("renders the normal unaudited state", () => {
    const markup = renderToStaticMarkup(
      <EpisodeAuditCardBody
        response={{ status: "none", meeting_id: 127696 }}
      />,
    );
    expect(markup).toContain("No audit has been run for this meeting yet.");
  });

  it("renders a proposal's before, after, rationale, and verified checks", () => {
    const markup = renderToStaticMarkup(
      <EpisodeAuditCardBody
        response={proposalResponse()}
        meetingId={127696}
      />,
    );

    expect(markup).toContain("Proposed fix · newsletter");
    expect(markup).toContain("Jon Smyth opened the hearing.");
    expect(markup).toContain("John Smith opened the hearing.");
    expect(markup).toContain(
      "The transcript and key decisions both use John Smith.",
    );
    expect(markup).toContain(
      "verified: exact match in newsletter · citations intact · no new names",
    );
    expect(markup).toContain("line-through");
    expect(markup).not.toContain("**");
  });

  it("shows APPROVE FIX only for validated, non-gated proposals", () => {
    const response = proposalResponse();
    if (response.status !== "ok") throw new Error("Expected audit run");
    response.run.report.llm.proposals?.push(
      {
        id: "proposal-gated",
        finding_number: 1,
        target_output: "key_decisions",
        before: "Old gated text",
        after: "New gated text",
        fix_rationale: "Validated but no write adapter exists.",
        validated: true,
        apply_gated: true,
        checks: ["citations_intact"],
        parse_ok: true,
      },
      {
        id: "proposal-unvalidated",
        finding_number: 1,
        target_output: "tagline",
        before: "Old tagline",
        after: "New tagline",
        fix_rationale: "The exact source could not be confirmed.",
        validated: false,
        apply_gated: false,
        checks: { exact_source_match: false },
        parse_ok: true,
      },
    );

    const markup = renderToStaticMarkup(
      <EpisodeAuditCardBody response={response} meetingId={127696} />,
    );

    expect((markup.match(/APPROVE FIX/g) ?? []).length).toBe(1);
    expect(markup).toContain(
      "Fix validated — applying to key decisions arrives with its adapter",
    );
    expect(markup).toContain(
      "Could not be machine-verified — review manually",
    );
    expect(markup).toContain("failed: exact source match");
  });

  it("does not submit a rejection with an empty reason", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const result = await submitEpisodeAuditDisposition({
      meetingId: 127696,
      runId: "run-23",
      proposalId: "proposal-1",
      disposition: "rejected",
      reason: "   ",
    });

    expect(result).toBe("blocked");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("shows the applied state after a successful apply request", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ status: "applied" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = await applyEpisodeAuditFix({
      meetingId: 127696,
      runId: "run-23",
      proposalId: "proposal-1",
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/episode-audit/127696/apply-fix",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          run_id: "run-23",
          proposal_id: "proposal-1",
        }),
      }),
    );
    expect(result).toEqual({ phase: "applied", failedChecks: [] });
  });

  it("confirms before applying a fix to a published record", () => {
    expect(nextApprovalPhase(true)).toBe("confirming");
    expect(nextApprovalPhase(false)).toBe("idle");

    const markup = renderToStaticMarkup(
      <PublishedRecordConfirm onApply={() => {}} onCancel={() => {}} />,
    );
    expect(markup).toContain("This edits a published public record.");
    expect(markup).toContain("Apply anyway");
    expect(markup).toContain("Cancel");
  });

  it("renders the matching NO_SAFE_PROPOSAL reason", () => {
    const response = proposalResponse();
    if (response.status !== "ok") throw new Error("Expected audit run");
    response.run.report.llm.no_safe_proposals = [
      {
        finding_number: 1,
        reason: "**The source evidence is ambiguous.**",
      },
    ];

    const markup = renderToStaticMarkup(
      <EpisodeAuditCardBody response={response} meetingId={127696} />,
    );

    expect(markup).toContain(
      "No safe automatic fix — The source evidence is ambiguous.",
    );
    expect(markup).not.toContain("**The source evidence");
  });
});
