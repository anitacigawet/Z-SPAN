import type { ComponentProps } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("../hooks/useCurrentUser", () => ({
  useCurrentUser: () => ({
    user: { user_id: 1, is_owner: true },
    loading: false,
  }),
}));

import { InfographDownloadButton } from "./InfographDownloadButton";

const baseProps: ComponentProps<typeof InfographDownloadButton> = {
  city: "Parity City",
  date: "2026-07-22",
  title: "Regular City Council Meeting",
  tagline: "A meeting headline",
  keyDecisions: ["The council adopted the fiscal-year budget."],
  publicId: "m_ABC123XYZ",
  publicUrl: "https://zspan.org/?view=broadcast&publicId=m_ABC123XYZ",
  disclaimerAcknowledged: true,
  isPubliclyServed: true,
  isPeek: false,
};

describe("InfographDownloadButton publication gate", () => {
  it("does not render in an auto-acknowledged owner peek", () => {
    const markup = renderToStaticMarkup(
      <InfographDownloadButton {...baseProps} isPeek />
    );

    expect(markup).toBe("");
  });

  it("does not render for a draft broadcast", () => {
    const markup = renderToStaticMarkup(
      <InfographDownloadButton {...baseProps} isPubliclyServed={false} />
    );

    expect(markup).toBe("");
  });

  it("renders for an acknowledged, publicly served, non-peek broadcast", () => {
    const markup = renderToStaticMarkup(
      <InfographDownloadButton {...baseProps} />
    );

    expect(markup).toContain("Download infographic");
  });
});
