import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchForPlane, planePath } from "./planeFetch";

function mockWindow(hostname: string, search = "") {
 vi.stubGlobal("window", {
 location: { hostname, search },
 localStorage: { getItem: vi.fn(() => null) },
 });
}

afterEach(() => {
 vi.unstubAllGlobals();
});

describe("plane fetch selection", () => {
 const paths = {
 publicPath: "/public-api/example",
 operatorPath: "/api/example",
 };

 it("selects the public DTO on the public plane", () => {
 mockWindow("zspan.org");
 expect(planePath(paths)).toBe("/public-api/example");
 });

 it("preserves the operator path on operator and dev planes", () => {
 mockWindow("operator.zspan.org");
 expect(planePath(paths)).toBe("/api/example");

 mockWindow("localhost");
 expect(planePath(paths)).toBe("/api/example");
 });

 it("honors the localhost public-plane override", async () => {
 mockWindow("localhost", "?__plane=public");
 const fetchMock = vi.fn(() => Promise.resolve(new Response())) as any;
 vi.stubGlobal("fetch", fetchMock);

 await fetchForPlane(paths);

 expect(fetchMock).toHaveBeenCalledWith("/public-api/example", undefined);
 });
});
