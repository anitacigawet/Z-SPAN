import { afterEach, describe, expect, it, vi } from "vitest";
import {
 getApiBase,
 getTrustPlane,
 isOperatorSurfaceAllowed,
 isPublicPlane,
 type TrustPlane,
} from "./trustPlane";

function mockWindow(
 hostname: string,
 options: { search?: string; storedOverride?: string | null } = {},
) {
 const getItem = vi.fn((key: string) =>
 key === "zspanPlaneOverride" ? (options.storedOverride ?? null) : null,
 );

 vi.stubGlobal("window", {
 location: {
 hostname,
 search: options.search ?? "",
 },
 localStorage: { getItem },
 });

 return { getItem };
}

afterEach(() => {
 vi.unstubAllGlobals();
});

describe("getTrustPlane", () => {
 it.each<[string, TrustPlane]>([
 ["zspan.org", "public"],
 ["www.zspan.org", "public"],
 ["operator.zspan.org", "operator"],
 ["localhost", "dev"],
 ["127.0.0.1", "dev"],
 ["studio.local", "dev"],
 ["unknown-host.example", "public"],
 ["z-span.pages.dev", "public"],
 ])("maps %s to %s", (hostname, expected) => {
 mockWindow(hostname);
 expect(getTrustPlane()).toBe(expected);
 });

 it("fails closed when window is unavailable", () => {
 expect(getTrustPlane()).toBe("public");
 });
});

describe("plane predicates", () => {
 it.each<[string, boolean, boolean]>([
 ["zspan.org", true, false],
 ["operator.zspan.org", false, true],
 ["localhost", false, true],
 ])(
 "reports predicates for %s",
 (hostname, expectedPublic, expectedOperatorAllowed) => {
 mockWindow(hostname);
 expect(isPublicPlane()).toBe(expectedPublic);
 expect(isOperatorSurfaceAllowed()).toBe(expectedOperatorAllowed);
 },
 );

 it("maps API bases without deciding endpoint-specific /v1 routes", () => {
 expect(getApiBase("public")).toBe("/public-api");
 expect(getApiBase("operator")).toBe("/api");
 expect(getApiBase("dev")).toBe("/api");
 });
});

describe("dev public-plane override", () => {
 it("accepts the query override on localhost", () => {
 mockWindow("localhost", { search: "?__plane=public" });
 expect(getTrustPlane()).toBe("public");
 expect(isOperatorSurfaceAllowed()).toBe(false);
 });

 it("accepts the localStorage override on localhost", () => {
 mockWindow("localhost", { storedOverride: "public" });
 expect(getTrustPlane()).toBe("public");
 });

 it("ignores override inputs on zspan.org", () => {
 const { getItem } = mockWindow("zspan.org", {
 search: "?__plane=public",
 storedOverride: "public",
 });
 expect(getTrustPlane()).toBe("public");
 expect(getItem).not.toHaveBeenCalled();
 });

 it("cannot force the public plane on the operator hostname", () => {
 mockWindow("operator.zspan.org", {
 search: "?__plane=public",
 storedOverride: "public",
 });
 expect(getTrustPlane()).toBe("operator");
 });
});
