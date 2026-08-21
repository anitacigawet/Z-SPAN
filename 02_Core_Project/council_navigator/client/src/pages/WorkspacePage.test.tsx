import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("../hooks/useCurrentUser", () => ({
  useCurrentUser: () => ({
    user: {
      user_id: 42,
      email: "reader@example.com",
      display_name: "Reader",
      is_owner: false,
    },
  }),
}));

vi.mock("../components/ByokSetupModal", () => ({
  ByokSetupModal: () => null,
}));

vi.mock("../lib/byok", () => ({
  getByokMetadata: () => null,
}));

import WorkspacePage from "./WorkspacePage";

describe("WorkspacePage", () => {
  it("explains the local-first browser and CLI storage boundary", () => {
    const markup = renderToStaticMarkup(<WorkspacePage onNavigate={vi.fn()} />);
    expect(markup).toContain("Your workspace");
    expect(markup).toContain("Browser workspace");
    expect(markup).toContain("Complete local workspace");
    expect(markup).toContain("~/.zspan/workspace.db");
    expect(markup).toContain("API keys are memory-only");
    expect(markup).toContain("Received by Z-SPAN");
  });
});
