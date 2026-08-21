import { describe, expect, it } from "vitest";

import {
  deriveCountyStatus,
  deriveStateStatus,
  directoryStateStatus,
  countyDirectoryName,
  countyDirectoryTerminology,
  countyPublicBodyDisplayName,
  publicBodyDefinition,
  countyGovernmentSectionLabel,
  externalDirectorySelectionIntent,
  isDirectoryStateUnlocked,
  emptyShelfPresentation,
  placeDisplayName,
  regionalDirectoryLabel,
} from "./ChannelsPage";
import { nationalCivicsCatalogStateUrl } from "../lib/projectMeta";

describe("channel status rollups", () => {
  it("keeps a roster-only county amber and browsable", () => {
    expect(deriveCountyStatus([{ status: "scaffold" }])).toBe("cached");
  });

  it("rolls any live city up to green", () => {
    expect(
      deriveCountyStatus([{ status: "scaffold" }, { status: "live" }])
    ).toBe("live");
  });

  it("includes a separately-rendered county source in the county rollup", () => {
    expect(
      deriveCountyStatus(
        [{ status: "scaffold" }],
        [{ status: "live" }],
      ),
    ).toBe("live");
  });

  it("makes a rostered state amber until a city is live", () => {
    expect(
      deriveStateStatus([
        { cities: [{ status: "scaffold" }, { status: "postponed" }] },
      ])
    ).toBe("cached");
  });

  it("leaves a state with no roster gray", () => {
    expect(deriveStateStatus([])).toBeUndefined();
  });

  it("includes statewide and regional sources without treating them as counties", () => {
    expect(
      deriveStateStatus([], [{ status: "scaffold" }], [{ status: "live" }]),
    ).toBe("live");
  });

  it("uses only the civic levels a state actually contains", () => {
    expect(regionalDirectoryLabel([{ place_type: "tribal_jurisdiction" }])).toBe(
      "Tribal governments",
    );
    expect(regionalDirectoryLabel([{ place_type: "regional_authority" }])).toBe(
      "Regional governments",
    );
    expect(
      regionalDirectoryLabel([
        { place_type: "tribal_jurisdiction" },
        { place_type: "regional_authority" },
      ]),
    ).toBe("Regional and Tribal governments");
  });

  it("does not repeat County beneath the Counties heading", () => {
    expect(countyDirectoryName("Mohave County")).toBe("Mohave");
    expect(countyDirectoryName("District of Columbia")).toBe(
      "District of Columbia",
    );
    expect(countyDirectoryName("Washington Dc County", "DC")).toBe(
      "District of Columbia",
    );
    expect(countyDirectoryName("Terrebone County", "LA")).toBe("Terrebonne");
  });

  it("uses state-specific county-equivalent terminology without renaming data", () => {
    expect(countyDirectoryTerminology("AK")).toEqual({
      directoryLabel: "Boroughs and census areas",
    });
    expect(countyDirectoryTerminology("LA")).toEqual({
      directoryLabel: "Parishes",
    });
    expect(countyDirectoryTerminology("AZ")).toEqual({
      directoryLabel: "Counties",
    });
    expect(countyDirectoryTerminology("DC")).toEqual({
      directoryLabel: "District-wide government",
    });
  });

  it("uses the catalog's actual local-government name in county-equivalent views", () => {
    expect(countyPublicBodyDisplayName("LA", "Acadia County", [
      { name: "Acadia Parish", place_type: "county" },
      { name: "Crowley", place_type: "municipality" },
    ])).toBe("Acadia Parish");
    expect(countyPublicBodyDisplayName("AK", "Aleutians West Census Area", [])).toBe(
      "Aleutians West Census Area",
    );
    expect(countyPublicBodyDisplayName("AK", "Anchorage Municipality County", [])).toBe(
      "Anchorage Municipality",
    );
    expect(countyPublicBodyDisplayName("DC", "Washington Dc County", [])).toBe(
      "District of Columbia",
    );
    expect(countyPublicBodyDisplayName("LA", "Orleans County", [])).toBe(
      "Orleans Parish",
    );
    expect(countyPublicBodyDisplayName("LA", "Terrebone County", [])).toBe(
      "Terrebonne Parish",
    );
  });

  it("describes only the body types actually available in a state", () => {
    expect(publicBodyDefinition("AR", [])).toBe(
      "A county government or a local body with a public meeting source or a catalog slot waiting to be completed.",
    );
    expect(publicBodyDefinition("AZ", [{ place_type: "tribal_jurisdiction" }])).toBe(
      "A county government, a local body, or a Tribal government with a public meeting source or a catalog slot waiting to be completed.",
    );
    expect(publicBodyDefinition("LA", [])).toContain("parish government");
    expect(publicBodyDefinition("AK", [])).toContain("borough government");
    expect(publicBodyDefinition("DC", [])).toContain("district-wide");
  });

  it("derives a government section label from the actual public body", () => {
    expect(countyGovernmentSectionLabel("LA", "Acadia Parish")).toBe(
      "Parish government",
    );
    expect(countyGovernmentSectionLabel("AK", "Aleutians East Borough")).toBe(
      "Borough government",
    );
    expect(countyGovernmentSectionLabel("AK", "Juneau City and Borough")).toBe(
      "Borough government",
    );
    expect(countyGovernmentSectionLabel("DC", "District of Columbia")).toBe(
      "District-wide government",
    );
    expect(countyGovernmentSectionLabel("AZ", "Mohave County")).toBe(
      "County government",
    );
  });

  it("clears a stale county when browser navigation removes directory params", () => {
    const selected = externalDirectorySelectionIntent(null, {
      state: "AZ",
      county: "Mohave County",
      city: "Kingman",
    });
    expect(selected.action).toBe("select");
    expect(selected.stateCode).toBe("AZ");
    const returnedHome = externalDirectorySelectionIntent(selected.signature, {});
    expect(returnedHome.action).toBe("clear");
    expect(returnedHome.stateCode).toBe("AZ");
    expect(externalDirectorySelectionIntent(null, {}).action).toBe("ignore");
    expect(externalDirectorySelectionIntent(null, { state: "xx" }).stateCode).toBe(
      "AZ",
    );
  });

  it("keeps out-of-state deep links behind the V3 state gate", () => {
    const locked = externalDirectorySelectionIntent(null, {
      state: "NY",
      county: "Albany County",
      city: "Albany",
    });

    expect(locked.action).toBe("clear");
    expect(locked.stateCode).toBe("AZ");
    expect(isDirectoryStateUnlocked("AZ")).toBe(true);
    expect(isDirectoryStateUnlocked("ny")).toBe(false);
  });

  it("keeps the raw catalog status amber even while the state rail is locked", () => {
    expect(directoryStateStatus(undefined)).toBe("scaffold");
    expect(directoryStateStatus([])).toBe("scaffold");
  });

  it("links an empty shelf to the matching National Civics Catalog folder", () => {
    expect(nationalCivicsCatalogStateUrl("NY")).toBe(
      "https://github.com/anitacigawet/national-civics-catalog/tree/main/data/states/ny",
    );
  });

  it("uses the cat, guarantee card, and neutral shelf for distinct lifecycle stages", () => {
    expect(emptyShelfPresentation("needs_source", "")).toBe("contribute");
    expect(emptyShelfPresentation("working", "")).toBe("guarantee");
    expect(emptyShelfPresentation("blocked", "")).toBe("source_update");
    expect(emptyShelfPresentation("blocked", "existing-route")).toBe("source_update");
    expect(emptyShelfPresentation("moved", "existing-route")).toBe("source_update");
    expect(emptyShelfPresentation("working", "kingman")).toBe("neutral");
    expect(emptyShelfPresentation(undefined, "legacy-route")).toBe("neutral");
    expect(emptyShelfPresentation(undefined, "")).toBe("contribute");
  });

  it("disambiguates same-name governments without changing unique place names", () => {
    const siblings = [
      { name: "Colonie", place_type: "municipality" },
      { name: "Colonie", place_type: "township" },
      { name: "Cohoes", place_type: "municipality" },
    ];
    expect(placeDisplayName(siblings[0], siblings)).toBe("Colonie (Municipality)");
    expect(placeDisplayName(siblings[1], siblings)).toBe("Colonie (Township)");
    expect(placeDisplayName(siblings[2], siblings)).toBe("Cohoes");
  });
});
