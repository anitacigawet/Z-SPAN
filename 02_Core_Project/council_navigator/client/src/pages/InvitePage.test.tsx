import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import {
  InvitePanel,
  invitationEmailUrl,
  invitationSignInUrl,
} from "./InvitePage";

const handlers = {
  onAccept: vi.fn(),
  onSignIn: vi.fn(),
  onEmail: vi.fn(),
  onEnter: vi.fn(),
  onRetry: vi.fn(),
};

describe("InvitePanel", () => {
  it("welcomes an anonymous invited visitor without payment language", () => {
    const markup = renderToStaticMarkup(
      <InvitePanel
        mode="available"
        signInEnabled
        {...handlers}
      />,
    );

    expect(markup).toContain("Welcome");
    expect(markup).toContain("free, invitation-only access to Z-SPAN");
    expect(markup).toContain("Continue with Google");
    expect(markup).toContain("Use email and password");
    expect(markup).toContain("No payment or subscription is required");
    expect(markup).not.toContain("trial");
    expect(markup).not.toContain("billing");
  });

  it("asks the signed-in recipient to accept the single invitation", () => {
    const markup = renderToStaticMarkup(
      <InvitePanel
        mode="available"
        signedInName="Civic Reader"
        signInEnabled
        {...handlers}
      />,
    );

    expect(markup).toContain("Signed in as Civic Reader");
    expect(markup).toContain("Accept invitation");
    expect(markup).not.toContain("Continue with Google");
  });

  it("gives an admitted reader a direct library entrance", () => {
    const markup = renderToStaticMarkup(
      <InvitePanel
        mode="granted"
        signedInName="Civic Reader"
        signInEnabled
        {...handlers}
      />,
    );

    expect(markup).toContain("You’re in");
    expect(markup).toContain("Enter the library");
  });

  it("does not distinguish revoked, used, and unknown cards", () => {
    const markup = renderToStaticMarkup(
      <InvitePanel
        mode="unavailable"
        signInEnabled
        {...handlers}
      />,
    );

    expect(markup).toContain("This invitation is not available");
    expect(markup).not.toContain("revoked");
    expect(markup).not.toContain("redeemed");
  });
});

describe("invitationSignInUrl", () => {
  it("returns through a token-free resume route after Google OAuth", () => {
    expect(invitationSignInUrl()).toBe(
      `/api/auth/google/login?next=${encodeURIComponent("/i#resume=invitation")}`,
    );
  });

  it("keeps the email-registration bearer in the browser-only fragment", () => {
    const token = "Abc_123-xyz".repeat(3);
    const url = new URL(invitationEmailUrl(token), "https://zspan.org");
    expect(url.pathname).toBe("/login");
    expect(url.search).toBe("");
    expect(new URLSearchParams(url.hash.slice(1)).get("invite")).toBe(token);
    expect(url.href).not.toContain("?invite=");
  });
});
