import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import LoginPage, { googleLoginUrl, safeLoginNext } from "./LoginPage";


describe("LoginPage", () => {
  it("offers both authentication methods without treating roles as logins", () => {
    const markup = renderToStaticMarkup(
      <LoginPage onNavigate={vi.fn()} />,
    );
    expect(markup).toContain("Log in");
    expect(markup).toContain("Continue with Google");
    expect(markup).toContain("Email");
    expect(markup).toContain("Password");
    expect(markup).toContain("Have an invitation code");
    expect(markup).not.toContain("Log in as creator");
    expect(markup).not.toContain("username");
  });

  it("prefills invitation registration and explains the free role boundary", () => {
    const markup = renderToStaticMarkup(
      <LoginPage
        invitationToken={"A".repeat(32)}
        nextPath={`/i#invite=${"A".repeat(32)}`}
        onNavigate={vi.fn()}
      />,
    );
    expect(markup).toContain("Invitation recognized");
    expect(markup).toContain("Create account and enter");
    expect(markup).toContain("Invited access is free");
    expect(markup).toContain("Creator and verification roles are assigned separately");
  });
});

describe("login redirect helpers", () => {
  it("keeps only same-origin relative return paths", () => {
    expect(safeLoginNext("/following?tab=city")).toBe("/following?tab=city");
    expect(safeLoginNext("//attacker.example/path")).toBe("/");
    expect(safeLoginNext("https://attacker.example/path")).toBe("/");
    expect(safeLoginNext("/\\attacker.example")).toBe("/");
  });

  it("returns from Google to the selected local path", () => {
    expect(googleLoginUrl("/i#resume=invitation")).toBe(
      `/api/auth/google/login?next=${encodeURIComponent("/i#resume=invitation")}`,
    );
  });
});
