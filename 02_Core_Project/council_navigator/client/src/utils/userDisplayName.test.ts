import { describe, expect, it } from "vitest";

import { firstNameForChip } from "./userDisplayName";

describe("firstNameForChip", () => {
  it("uses only the first whitespace-delimited Google display-name token", () => {
    expect(firstNameForChip("James Jones", "james@example.com")).toBe("James");
    expect(firstNameForChip("  James   Jones  ", "james@example.com")).toBe("James");
  });

  it("falls back to email when the display name is empty", () => {
    expect(firstNameForChip("", "james@example.com")).toBe("james@example.com");
    expect(firstNameForChip(null, "james@example.com")).toBe("james@example.com");
  });

  it("uses a stable placeholder only when both identity fields are empty", () => {
    expect(firstNameForChip("   ", "   ")).toBe("?");
  });
});
