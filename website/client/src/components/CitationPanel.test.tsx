import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { CitationTrackedClaims } from "./CitationPanel";

describe("CitationPanel tracked-claims boundary", () => {
 it("never renders the tracked-claims label or counts for a non-owner", () => {
 const markup = renderToStaticMarkup(
 <CitationTrackedClaims
 isOwner={false}
 trackedClaims={{ total: 3, by_status: { active: 3 } }}
 />,
 );

 expect(markup).not.toContain("Tracked claims");
 expect(markup).not.toContain("3");
 expect(markup).not.toContain("active");
 });
});
