import { afterEach, describe, expect, it, vi } from "vitest";

import {
 clientIpForFlask,
 flaskProxyHeaders,
 originGateAllows,
 pickAuthOriginHost,
 requireEdgeToken,
 ZSPAN_CLIENT_IP_HEADER,
} from "./originTrust";

const ORIGINAL_EDGE_TOKEN = process.env.ZSPAN_EDGE_TOKEN;
const ORIGINAL_ALLOW_INSECURE =
 process.env.ZSPAN_ALLOW_INSECURE_NO_EDGE_TOKEN;
const ORIGINAL_NODE_ENV = process.env.NODE_ENV;

afterEach(() => {
 vi.restoreAllMocks();
 if (ORIGINAL_EDGE_TOKEN === undefined) {
 delete process.env.ZSPAN_EDGE_TOKEN;
 } else {
 process.env.ZSPAN_EDGE_TOKEN = ORIGINAL_EDGE_TOKEN;
 }
 if (ORIGINAL_ALLOW_INSECURE === undefined) {
 delete process.env.ZSPAN_ALLOW_INSECURE_NO_EDGE_TOKEN;
 } else {
 process.env.ZSPAN_ALLOW_INSECURE_NO_EDGE_TOKEN = ORIGINAL_ALLOW_INSECURE;
 }
 if (ORIGINAL_NODE_ENV === undefined) {
 delete process.env.NODE_ENV;
 } else {
 process.env.NODE_ENV = ORIGINAL_NODE_ENV;
 }
});

describe("origin edge trust", () => {
 it("enforces a configured edge token even when the insecure flag is set", () => {
 process.env.ZSPAN_EDGE_TOKEN = "edge-secret";
 process.env.ZSPAN_ALLOW_INSECURE_NO_EDGE_TOKEN = "1";
 expect(requireEdgeToken(process.env.ZSPAN_EDGE_TOKEN)).toBe("edge-secret");
 expect(
 originGateAllows("/api/calendar", undefined, "edge-secret")
 ).toBe(false);
 expect(
 originGateAllows("/api/calendar", "edge-secret", "edge-secret")
 ).toBe(true);
 });

 it("allows token-free local development only with the explicit flag", () => {
 process.env.NODE_ENV = "development";
 const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

 for (const flagValue of ["1", "true"]) {
 process.env.ZSPAN_ALLOW_INSECURE_NO_EDGE_TOKEN = flagValue;
 const edgeToken = requireEdgeToken(undefined);
 expect(originGateAllows("/api/calendar", undefined, edgeToken)).toBe(
 true
 );
 }
 expect(warn).toHaveBeenCalledOnce();
 expect(warn.mock.calls[0][0]).toMatch(/ORIGIN GATE IS DISABLED/);
 });

 it("fails closed when both the token and opt-in flag are absent", () => {
 process.env.NODE_ENV = "development";
 delete process.env.ZSPAN_ALLOW_INSECURE_NO_EDGE_TOKEN;
 expect(() => requireEdgeToken(undefined)).toThrow(/ZSPAN_EDGE_TOKEN/);
 expect(() => requireEdgeToken("")).toThrow(/fails closed/);
 });

 it("ignores the insecure flag in production and fails closed", () => {
 process.env.NODE_ENV = "production";
 process.env.ZSPAN_ALLOW_INSECURE_NO_EDGE_TOKEN = "1";
 expect(() => requireEdgeToken(undefined)).toThrow(/ZSPAN_EDGE_TOKEN/);
 });

 it("permits only valid tokens outside the reviewed exemptions", () => {
 expect(originGateAllows("/api/calendar", undefined, "edge-secret")).toBe(
 false
 );
 expect(
 originGateAllows("/api/calendar", "wrong-token", "edge-secret")
 ).toBe(false);
 expect(
 originGateAllows("/api/calendar", "edge-secret", "edge-secret")
 ).toBe(true);
 expect(originGateAllows("/healthz", undefined, "edge-secret")).toBe(true);
 // /media/* used to bypass here — closed per sol pen-test Finding #4.
 // Raw Railway-hostname requests to /media without the CF-injected
 // edge token now 403; browser flow via Cloudflare still works because
 // Pages/Workers add X-Zspan-Edge-Token on every proxied request.
 expect(originGateAllows("/media/clip.mp4", undefined, "edge-secret")).toBe(
 false
 );
 expect(
 originGateAllows("/media/1234/audio_overview.mp4", undefined, "edge-secret")
 ).toBe(false);
 expect(
 originGateAllows("/media/1234/audio_overview.mp4", "edge-secret", "edge-secret")
 ).toBe(true);
 expect(
 originGateAllows("/media-private/file", undefined, "edge-secret")
 ).toBe(false);
 });

 it("uses only the edge-authenticated trusted client-IP header", () => {
 process.env.ZSPAN_EDGE_TOKEN = "edge-secret";
 expect(
 clientIpForFlask({
 headers: {
 "x-zspan-edge-token": "edge-secret",
 "x-zspan-client-ip": "203.0.113.8",
 "x-forwarded-for": "198.51.100.99, 203.0.113.8",
 },
 socket: { remoteAddress: "127.0.0.1" },
 })
 ).toBe("203.0.113.8");

 expect(
 clientIpForFlask({
 headers: {
 "x-zspan-edge-token": "wrong-token",
 "x-zspan-client-ip": "203.0.113.8",
 "x-forwarded-for": "198.51.100.99",
 },
 socket: { remoteAddress: "127.0.0.1" },
 })
 ).toBe("127.0.0.1");
 });

 it("overwrites an inbound trusted-header lookalike", () => {
 process.env.ZSPAN_EDGE_TOKEN = "edge-secret";
 const headers = flaskProxyHeaders(
 {
 headers: {
 "x-zspan-edge-token": "edge-secret",
 "x-zspan-client-ip": "203.0.113.9",
 },
 socket: { remoteAddress: "127.0.0.1" },
 },
 { [ZSPAN_CLIENT_IP_HEADER]: "198.51.100.1" }
 );

 expect(headers[ZSPAN_CLIENT_IP_HEADER]).toBe("203.0.113.9");
 });
});

describe("pickAuthOriginHost — OAuth origin resolution", () => {
 const defaultHost = "localhost:3000";

 it("prefers X-ZSPAN-Origin-Host over an incorrect X-Forwarded-Host", () => {
 // The observed failure mode: CF Pages sends the true
 // hostname, Railway ingress rewrites X-Forwarded-Host to the
 // internal Railway host. The companion pair rescues the true value.
 const { host, proto } = pickAuthOriginHost(
 {
 headers: {
 "x-zspan-origin-host": "zspan.org",
 "x-zspan-origin-proto": "https",
 "x-forwarded-host": "z-span-production.up.railway.app",
 "x-forwarded-proto": "https",
 host: "z-span-production.up.railway.app",
 },
 },
 defaultHost,
 );
 expect(host).toBe("zspan.org");
 expect(proto).toBe("https");
 });

 it("rejects an unallowlisted X-ZSPAN-Origin-Host and falls back to X-Forwarded-Host", () => {
 // Defense-in-depth: if Railway ever mutates or an attacker ever
 // injects the companion header with an unknown host, ignore it and
 // fall back to the standard chain.
 const { host } = pickAuthOriginHost(
 {
 headers: {
 "x-zspan-origin-host": "malicious.example",
 "x-forwarded-host": "zspan.org",
 },
 },
 defaultHost,
 );
 expect(host).toBe("zspan.org");
 });

 it("normalizes allowlisted X-ZSPAN-Origin-Host values case-insensitively", () => {
 const { host } = pickAuthOriginHost(
 { headers: { "x-zspan-origin-host": "ZSPAN.ORG" } },
 defaultHost,
 );
 expect(host).toBe("zspan.org");
 });

 it("allowlists operator.zspan.org and lab.zspan.org", () => {
 const operator = pickAuthOriginHost(
 { headers: { "x-zspan-origin-host": "operator.zspan.org" } },
 defaultHost,
 );
 expect(operator.host).toBe("operator.zspan.org");

 const lab = pickAuthOriginHost(
 { headers: { "x-zspan-origin-host": "lab.zspan.org" } },
 defaultHost,
 );
 expect(lab.host).toBe("lab.zspan.org");
 });

 it("falls back to X-Forwarded-Host when the companion is absent", () => {
 const { host, proto } = pickAuthOriginHost(
 {
 headers: {
 "x-forwarded-host": "zspan.org",
 "x-forwarded-proto": "https",
 },
 },
 defaultHost,
 );
 expect(host).toBe("zspan.org");
 expect(proto).toBe("https");
 });

 it("returns https for Flask cookie security from a valid forwarded pair", () => {
 const { proto } = pickAuthOriginHost(
 {
 headers: {
 "x-forwarded-host": "zspan.org",
 "x-forwarded-proto": "https",
 },
 },
 defaultHost,
 "http",
 );
 expect(proto).toBe("https");
 });

 it("rejects an unallowlisted X-Forwarded-Host and falls back to the default", () => {
 const { host } = pickAuthOriginHost(
 {
 headers: {
 "x-forwarded-host": "attacker.example",
 host: "attacker.example",
 },
 },
 defaultHost,
 );
 expect(host).toBe(defaultHost);
 });

 it("falls back to req.headers.host when both forwarded chains are absent", () => {
 const { host } = pickAuthOriginHost(
 { headers: { host: "127.0.0.1:3000" } },
 defaultHost,
 );
 expect(host).toBe("127.0.0.1:3000");
 });

 it("falls back to the caller-supplied default when nothing is set", () => {
 const { host, proto } = pickAuthOriginHost(
 { headers: {} },
 defaultHost,
 "http",
 );
 expect(host).toBe(defaultHost);
 expect(proto).toBe("http");
 });

 it("rejects a non-http/https X-ZSPAN-Origin-Proto and falls through", () => {
 const { proto } = pickAuthOriginHost(
 {
 headers: {
 "x-zspan-origin-host": "zspan.org",
 "x-zspan-origin-proto": "javascript",
 "x-forwarded-host": "zspan.org",
 "x-forwarded-proto": "https",
 },
 },
 defaultHost,
 );
 expect(proto).toBe("https");
 });

 it("rejects the companion proto atomically when its host is untrusted", () => {
 const { host, proto } = pickAuthOriginHost(
 {
 headers: {
 "x-zspan-origin-host": "attacker.example",
 "x-zspan-origin-proto": "https",
 "x-forwarded-host": "zspan.org",
 },
 },
 defaultHost,
 "http",
 );
 expect(host).toBe("zspan.org");
 expect(proto).toBe("http");
 });

 it("rejects X-Forwarded-Proto atomically when its host is untrusted", () => {
 const { host, proto } = pickAuthOriginHost(
 {
 headers: {
 "x-forwarded-host": "attacker.example",
 "x-forwarded-proto": "https",
 host: "zspan.org",
 },
 },
 defaultHost,
 "http",
 );
 expect(host).toBe("zspan.org");
 expect(proto).toBe("http");
 });

 it("handles array-valued headers by using the first entry", () => {
 // Node's http request can hand back arrays for some headers; the
 // helper should defend against that shape without crashing.
 const { host } = pickAuthOriginHost(
 {
 headers: {
 "x-zspan-origin-host": ["zspan.org", "malicious.example"] as any,
 },
 },
 defaultHost,
 );
 expect(host).toBe("zspan.org");
 });
});
