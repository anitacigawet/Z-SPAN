import { describe, expect, it } from "vitest";
import { workspaceToggleDestination } from "./TopBar";

describe("workspace/public mode switch", () => {
  it("offers every signed-in public reader their workspace", () => {
    expect(workspaceToggleDestination("home", false)).toEqual({
      view: "workspace",
      label: "View workspace",
    });
  });

  it("returns a member in the workspace to the public library", () => {
    expect(workspaceToggleDestination("workspace", false)).toEqual({
      view: "home",
      label: "View public",
    });
  });

  it("offers an owner on an operator surface the public view", () => {
    expect(workspaceToggleDestination("terminal", true)).toEqual({
      view: "home",
      label: "View public",
    });
  });

  it("does not mistake a public meeting surface for operator mode", () => {
    expect(workspaceToggleDestination("city", true)).toEqual({
      view: "workspace",
      label: "View workspace",
    });
  });
});
