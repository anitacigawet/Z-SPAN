import { describe, expect, it, vi } from "vitest";

// App imports the Guide and parser-map surfaces, whose Leaflet dependency
// assumes a browser at module load. These tests exercise only the pure URL
// helpers, so keep that unrelated rendering dependency out of the node test.
vi.mock("leaflet", () => ({ default: {} }));

import {
  buildUrlForNavigation,
  parseNavigationFromUrlParts,
} from "./App";

describe("home directory URL navigation", () => {
  it("round-trips the signed-in workspace view", () => {
    const url = buildUrlForNavigation({ view: "workspace" });
    const parsedUrl = new URL(url, "https://zspan.org");
    expect(parseNavigationFromUrlParts(parsedUrl.search, parsedUrl.pathname)).toEqual({
      view: "workspace",
      params: undefined,
    });
  });
  it("round-trips state, county, and city without an explicit home view", () => {
    const url = buildUrlForNavigation({
      view: "home",
      params: {
        state: "LA",
        countyName: "Acadia County",
        cityName: "Crowley",
      },
    });
    const parsedUrl = new URL(url, "https://zspan.org");

    expect(parsedUrl.searchParams.has("view")).toBe(false);
    expect(
      parseNavigationFromUrlParts(parsedUrl.search, parsedUrl.pathname),
    ).toEqual({
      view: "home",
      params: {
        state: "LA",
        countyName: "Acadia County",
        cityName: "Crowley",
      },
    });
  });

  it("does not turn an unrelated query string into a directory deep link", () => {
    expect(parseNavigationFromUrlParts("?drafts=false", "/")).toBeNull();
  });

  it("keeps invitation and reset bearers in the browser-only fragment", () => {
    const token = "A".repeat(32);
    expect(parseNavigationFromUrlParts("", "/i", `#invite=${token}`)).toEqual({
      view: "invite",
      params: { inviteToken: token },
    });

    const inviteUrl = buildUrlForNavigation({
      view: "invite",
      params: { inviteToken: token },
    });
    expect(inviteUrl).toBe(`/i#invite=${token}`);

    const reset = "B".repeat(43);
    expect(
      parseNavigationFromUrlParts("", "/login", `#reset=${reset}`),
    ).toEqual({
      view: "login",
      params: { resetToken: reset },
    });
    expect(buildUrlForNavigation({
      view: "login",
      params: { resetToken: reset },
    })).toBe(`/login#reset=${reset}`);
  });
});
