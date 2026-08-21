import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { SourceCitationDisclosure } from "./CastMemberPanel";

const COMPONENT_DIR = dirname(fileURLToPath(import.meta.url));
const disclaimerSource = readFileSync(
  resolve(COMPONENT_DIR, "PublicDataDisclaimerGate.tsx"),
  "utf8",
);

describe("public data disclaimer navigation behavior", () => {
  it("resets acknowledgment on every defined scope change while preserving autoAck", () => {
    expect(disclaimerSource).not.toContain("hasAckedThisSessionRef");
    expect(disclaimerSource).toContain(
      "if (autoAck) {\n      setAcked(true);\n      setModalOpenFor(null);\n      return;\n    }",
    );
    expect(disclaimerSource).toContain(
      "setAcked(false);\n    if (scopeKey !== undefined) {\n      setModalOpenFor(\"__page_load__\");",
    );
    expect(disclaimerSource).toContain("}, [scopeKey, autoAck]);");
  });

  it("omits the apologetic supplementary sentence", () => {
    expect(disclaimerSource).not.toContain(
      "If not, I completely understand, and I would ask for your patience",
    );
    // D-182 (2026-07-25): the contribute-via-GitHub paragraph came down
    // with the open-sourcing postponement — the repo is private, so the
    // affordance was a dead link. The gate must NOT direct visitors to
    // the repo while the pause holds; restore the positive assertion
    // from git history at the reopening.
    expect(disclaimerSource).not.toContain(
      "If you want to contribute or collaborate, please find the project’s GitHub",
    );
  });
});

describe("cast member source citation", () => {
  it("renders the source URL as selectable text without a hyperlink", () => {
    const sourceUrl = "https://example.gov/seat.pdf?record=1&version=2";
    const markup = renderToStaticMarkup(
      <SourceCitationDisclosure sourceUrl={sourceUrl} />,
    );

    expect(markup).toContain("Where this seat was verified");
    expect(markup).toContain("<details");
    expect(markup).toContain("<code");
    expect(markup).toContain("select-text");
    expect(markup).toContain("https://example.gov/seat.pdf?record=1&amp;version=2");
    expect(markup).not.toContain("<a");
    expect(markup).not.toContain("href=");
  });
});
