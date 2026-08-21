import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import PublishConfirmModal, { canPublishBroadcast } from "./PublishConfirmModal";
import type { EpisodeAuditSummary } from "./EpisodeAuditCard";

const meeting = {
  id: 127696,
  title: "Citation-era council meeting",
  date: "2026-07-15",
  city: "Test City",
};

const auditSummary = (
  verdict: EpisodeAuditSummary["verdict"],
): EpisodeAuditSummary => ({
  verdict,
  run_status: verdict === "incomplete" ? "runtime_failed" : "complete",
  findings_count: verdict === "flags" ? 2 : 0,
  open_findings_count: 0,
  suggestions_count: 0,
  deterministic_flags_count: verdict === "flags" ? 1 : 0,
  created_at: "2026-07-28T12:00:00Z",
});

describe("PublishConfirmModal publication gate", () => {
  it("allows citation-era meetings with zero hero quotes after the checklist", () => {
    expect(canPublishBroadcast(0, 0, false)).toBe(false);
    expect(canPublishBroadcast(0, 0, true)).toBe(true);

    const markup = renderToStaticMarkup(
      <PublishConfirmModal
        open
        meeting={meeting}
        heroCount={0}
        verifiedCount={0}
        quoteCountsLoaded
        onPublish={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    expect(markup).toContain("No legacy hero quotes");
    expect(markup).toContain("Citation-backed decisions are validated server-side");
    expect(markup).not.toContain("0 of 0 quotes still need verification");
  });

  it("keeps the verified-all gate for legacy quote-bearing meetings", () => {
    expect(canPublishBroadcast(2, 1, true)).toBe(false);
    expect(canPublishBroadcast(2, 2, true)).toBe(true);
    expect(canPublishBroadcast(2, 2, false)).toBe(false);

    const markup = renderToStaticMarkup(
      <PublishConfirmModal
        open
        meeting={meeting}
        heroCount={2}
        verifiedCount={1}
        quoteCountsLoaded
        onPublish={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    expect(markup).toContain("1 of 2 quotes still need verification");
    expect(markup).toContain("Cannot publish — 1 of 2 quotes still need verification");
  });

  it("does not mistake an unavailable quote response for a zero-quote meeting", () => {
    expect(canPublishBroadcast(0, 0, true, false)).toBe(false);

    const markup = renderToStaticMarkup(
      <PublishConfirmModal
        open
        meeting={meeting}
        heroCount={0}
        verifiedCount={0}
        quoteCountsLoaded={false}
        onPublish={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    expect(markup).toContain("Checking legacy hero quotes");
    expect(markup).toContain("Cannot publish — quote verification status has not loaded");
  });

  it("certifies only surfaces the broadcast page currently renders", () => {
    const markup = renderToStaticMarkup(
      <PublishConfirmModal
        open
        meeting={meeting}
        heroCount={0}
        verifiedCount={0}
        quoteCountsLoaded
        previewHref="/broadcast/127696"
        onPublish={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    for (const surface of [
      "tagline",
      "key decisions",
      "citation evidence",
      "community calls to action",
      "video player",
    ]) {
      expect(markup.toLowerCase()).toContain(surface);
    }
    expect(markup.toLowerCase()).not.toContain("synopsis");
    expect(markup.toLowerCase()).not.toContain("audio overview");
    expect(markup.toLowerCase()).not.toContain("infographic");

    expect(markup).toContain("Nothing from public commenters slipped into them");
    expect(markup).toContain("including the verbatim citation excerpts");
    expect(markup).toContain("No editorial language");
  });

  it("shows each audit verdict as quiet, non-gating context", () => {
    const renderWithAudit = (summary?: EpisodeAuditSummary) =>
      renderToStaticMarkup(
        <PublishConfirmModal
          open
          meeting={meeting}
          heroCount={0}
          verifiedCount={0}
          quoteCountsLoaded
          auditSummary={summary}
          onPublish={vi.fn()}
          onCancel={vi.fn()}
        />,
      );

    expect(renderWithAudit(auditSummary("no_catches"))).toContain(
      "The episode auditor found no catches on its last pass.",
    );
    expect(renderWithAudit(auditSummary("flags"))).toContain(
      "The episode auditor flagged 3 item(s) on its last pass",
    );
    expect(renderWithAudit(auditSummary("incomplete"))).toContain(
      "The last audit pass didn&#x27;t complete.",
    );

    const withoutAudit = renderWithAudit();
    expect(withoutAudit).not.toContain("episode auditor");
    expect(withoutAudit).not.toContain("last audit pass");
  });
});
