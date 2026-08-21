import { describe, expect, it } from "vitest";

import { formatChipLabel, parseKaraokeSegments } from "./karaokeRender";


describe("key-decision citation locators", () => {
  it("parses canonical H:MM:SS and legacy flat-minute MM:SS", () => {
    const segments = parseKaraokeSegments(
      "First [at 3:24:38], legacy [at 204:38], short [at 8:15].",
    );
    const citations = segments.filter(segment => segment.kind === "cite");

    expect(citations).toEqual([
      { kind: "cite", mm: 204, ss: 38, raw: "[at 3:24:38]" },
      { kind: "cite", mm: 204, ss: 38, raw: "[at 204:38]" },
      { kind: "cite", mm: 8, ss: 15, raw: "[at 8:15]" },
    ]);
  });

  it("does not turn malformed locators into seek chips", () => {
    const segments = parseKaraokeSegments(
      "[at 01:02:03] [at 1:2:03] [at 1:60:00] [at 8:99]",
    );

    expect(segments).toEqual([
      { kind: "text", value: "[at 01:02:03] [at 1:2:03] [at 1:60:00] [at 8:99]" },
    ]);
  });

  it("renders readable labels past one hour", () => {
    expect(formatChipLabel(204, 38)).toBe("3:24:38");
    expect(formatChipLabel(8, 15)).toBe("8:15");
  });
});
