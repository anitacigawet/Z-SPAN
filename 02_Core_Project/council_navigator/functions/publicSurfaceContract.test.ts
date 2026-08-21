import { readFileSync, readdirSync } from "node:fs";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  onRequest as onApiRequest,
  PUBLIC_ACCOUNT_ROUTES,
  PUBLIC_AUTH_ROUTES,
  PUBLIC_CLI_ROUTES,
  PUBLIC_UNSUBSCRIBE_ROUTES,
} from "./api/[[catchall]]";
import {
  FUNCTION_ENTRYPOINT_TRUST_PLANE,
  PUBLIC_EDGE_ROUTES,
  matchPublicRoute,
} from "./publicSurfaceContract";
import { onRequest as onPublicApiRequest } from "./public-api/[[catchall]]";

const FUNCTIONS_DIR = dirname(fileURLToPath(import.meta.url));

const PUBLIC_AUTH_EXAMPLES: ReadonlyArray<
  readonly [string, string, string]
> = [
  [
    "GET",
    "/api/auth/google/login?next=%2Fguide",
    "/api/auth/google/login?next=%2Fguide",
  ],
  [
    "GET",
    "/api/auth/google/callback?code=provider-code&state=opaque&scope=openid&authuser=0&prompt=none&hd=example.org",
    "/api/auth/google/callback?code=provider-code&state=opaque&scope=openid&authuser=0&prompt=none&hd=example.org",
  ],
  ["GET", "/api/auth/me", "/api/auth/me"],
  ["POST", "/api/auth/logout", "/api/auth/logout"],
  ["POST", "/api/auth/password/register", "/api/auth/password/register"],
  ["POST", "/api/auth/password/login", "/api/auth/password/login"],
  ["POST", "/api/auth/password/forgot", "/api/auth/password/forgot"],
  ["POST", "/api/auth/password/reset", "/api/auth/password/reset"],
];

const PUBLIC_ACCOUNT_EXAMPLES: ReadonlyArray<
  readonly [string, string, string]
> = [
  [
    "POST",
    "/api/librarian/request-access?source=broadcast",
    "/api/librarian/request-access?source=broadcast",
  ],
  ["POST", "/api/invitations/status", "/api/invitations/status"],
  ["POST", "/api/invitations/redeem", "/api/invitations/redeem"],
  ["POST", "/api/byok/validate-key", "/api/byok/validate-key"],
  ["POST", "/api/byok/relay", "/api/byok/relay"],
  ["POST", "/api/byok/relay-stream", "/api/byok/relay-stream"],
  // Session-103 (product-slice2) — follow-a-city admits GET/POST/DELETE
  // on the public plane; Flask _require_user() scopes rows by session
  // cookie.
  ["GET", "/api/follows", "/api/follows"],
  ["POST", "/api/follows", "/api/follows"],
  ["DELETE", "/api/follows", "/api/follows"],
  ["GET", "/api/workspace/receipts", "/api/workspace/receipts"],
];

const PUBLIC_CLI_EXAMPLES: ReadonlyArray<
  readonly [string, string, string]
> = [
  [
    "GET",
    "/api/auth/cli/start?port=43123&state=opaque&challenge=opaque",
    "/api/auth/cli/start?port=43123&state=opaque&challenge=opaque",
  ],
  ["GET", "/api/auth/cli/finish", "/api/auth/cli/finish"],
  ["POST", "/api/auth/cli/finish", "/api/auth/cli/finish"],
  ["GET", "/api/auth/cli/cancel", "/api/auth/cli/cancel"],
  ["POST", "/api/auth/cli/exchange", "/api/auth/cli/exchange"],
  ["POST", "/api/auth/cli/revoke", "/api/auth/cli/revoke"],
  ["GET", "/api/auth/cli/me", "/api/auth/cli/me"],
  ["POST", "/api/generations/register", "/api/generations/register"],
  ["POST", "/api/contributions/submit", "/api/contributions/submit"],
];

const PUBLIC_UNSUBSCRIBE_EXAMPLES: ReadonlyArray<
  readonly [string, string, string]
> = [
  [
    "GET",
    "/api/unsubscribe?token=opaque.signature",
    "/api/unsubscribe?token=opaque.signature",
  ],
  ["POST", "/api/unsubscribe", "/api/unsubscribe"],
];

const APPROVED_EXAMPLES: ReadonlyArray<readonly [string, string]> = [
  ["/public-api/channels/tree", ""],
  [
    "/public-api/catalog/contribute/us-az-kingman-primary-meeting-source.md",
    "state=AZ",
  ],
  ["/public-api/cities/Kingman/years", ""],
  ["/public-api/cities/Kingman/meetings", "year=2026"],
  ["/public-api/calendar/county/Mohave/meetings", "state=Arizona"],
  ["/public-api/calendar/search", "q=water&limit=25&offset=0"],
  ["/public-api/calendar/stats", ""],
  ["/public-api/health", ""],
  ["/public-api/broadcasts/m_AAAAAAAAAAAAAAAAAAAAAA", ""],
  ["/public-api/broadcasts/m_AAAAAAAAAAAAAAAAAAAAAA/sim-queries", ""],
  ["/public-api/broadcasts/m_AAAAAAAAAAAAAAAAAAAAAA/sidecars/quotes", ""],
  ["/public-api/broadcasts/m_AAAAAAAAAAAAAAAAAAAAAA/citation", ""],
  ["/public-api/cast/Kingman", ""],
  ["/public-api/cast/Kingman/mayor", ""],
  ["/public-api/guide", ""],
  ["/public-api/travelers", ""],
  ["/public-api/youtube/embed-check", "video_id=dQw4w9WgXcQ"],
  ["/v1/catalog/jurisdictions", ""],
  ["/v1/catalog/meetings", "state=AZ&county=Mohave&city=Kingman&year=2026"],
  ["/v1/catalog/meetings/m_AAAAAAAAAAAAAAAAAAAAAA", ""],
];

function entryPointFiles(directory: string): string[] {
  const results: string[] = [];
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const absolute = resolve(directory, entry.name);
    if (entry.isDirectory()) {
      results.push(...entryPointFiles(absolute));
      continue;
    }
    if (!entry.name.endsWith(".ts") || entry.name.endsWith(".test.ts"))
      continue;
    const source = readFileSync(absolute, "utf8");
    if (
      /export\s+(?:const|async\s+function|function)\s+onRequest\b/.test(source)
    ) {
      results.push(relative(FUNCTIONS_DIR, absolute).replaceAll("\\", "/"));
    }
  }
  return results.sort();
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("D-179 public app-OAuth admission", () => {
  it.each(PUBLIC_AUTH_EXAMPLES)(
    "proxies %s %s on the public plane",
    async (method, requestPath, expectedPath) => {
      let proxied: Request | undefined;
      vi.stubGlobal(
        "fetch",
        vi.fn(async (request: Request) => {
          proxied = request;
          return new Response("{}", { status: 200 });
        }),
      );

      const request = new Request(`https://zspan.org${requestPath}`, {
        method,
      });
      const response = await onApiRequest({
        request,
        env: { BACKEND_URL: "https://origin.example" },
      } as Parameters<typeof onApiRequest>[0]);

      expect(PUBLIC_AUTH_ROUTES).toHaveLength(8);
      expect(response.status).toBe(200);
      expect(proxied?.url).toBe(`https://origin.example${expectedPath}`);
      expect(proxied?.method).toBe(method);
    },
  );

  it.each([
    ["GET", "/api/work-orders"],
    ["POST", "/api/auth/google/login"],
  ])("returns 404 for %s %s on the public plane", async (method, pathname) => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const request = new Request(`https://zspan.org${pathname}`, { method });
    const response = await onApiRequest({
      request,
      env: { BACKEND_URL: "https://origin.example" },
    } as Parameters<typeof onApiRequest>[0]);

    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({
      success: false,
      error: "not found",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("official client admission", () => {
  it.each(PUBLIC_CLI_EXAMPLES)(
    "proxies %s %s on the public plane",
    async (method, requestPath, expectedPath) => {
      let proxied: Request | undefined;
      vi.stubGlobal(
        "fetch",
        vi.fn(async (request: Request) => {
          proxied = request;
          return new Response("{}", { status: 200 });
        }),
      );

      const request = new Request(`https://zspan.org${requestPath}`, {
        method,
        headers: { authorization: "Bearer zspan_cli_test" },
      });
      const response = await onApiRequest({
        request,
        env: { BACKEND_URL: "https://origin.example" },
      } as Parameters<typeof onApiRequest>[0]);

      expect(PUBLIC_CLI_ROUTES).toHaveLength(9);
      expect(response.status).toBe(200);
      expect(proxied?.url).toBe(`https://origin.example${expectedPath}`);
      expect(proxied?.method).toBe(method);
      expect(proxied?.headers.get("authorization")).toBe("Bearer zspan_cli_test");
    },
  );

  it.each([
    ["GET", "/api/contributions/submit"],
    ["GET", "/api/generations/register"],
    ["POST", "/api/auth/cli/me"],
  ])("rejects the wrong method for %s %s", async (method, pathname) => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const response = await onApiRequest({
      request: new Request(`https://zspan.org${pathname}`, { method }),
      env: { BACKEND_URL: "https://origin.example" },
    } as Parameters<typeof onApiRequest>[0]);
    expect(response.status).toBe(404);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("2026-07-27 public signed-in account admission", () => {
  it.each(PUBLIC_ACCOUNT_EXAMPLES)(
    "proxies %s %s on the public plane",
    async (method, requestPath, expectedPath) => {
      let proxied: Request | undefined;
      vi.stubGlobal(
        "fetch",
        vi.fn(async (request: Request) => {
          proxied = request;
          return new Response("{}", { status: 200 });
        }),
      );

      const request = new Request(`https://zspan.org${requestPath}`, {
        method,
      });
      const response = await onApiRequest({
        request,
        env: { BACKEND_URL: "https://origin.example" },
      } as Parameters<typeof onApiRequest>[0]);

      expect(PUBLIC_ACCOUNT_ROUTES).toHaveLength(10);
      expect(response.status).toBe(200);
      expect(proxied?.url).toBe(`https://origin.example${expectedPath}`);
      expect(proxied?.method).toBe(method);
    },
  );

  it.each([
    ["GET", "/api/invitations/status"],
    ["GET", "/api/invitations"],
    ["POST", "/api/invitations/import"],
    ["POST", "/api/invitations/1/revoke"],
    ["GET", "/api/byok/relay"],
    ["GET", "/api/librarian/access-requests"],
  ])("returns 404 for %s %s on the public plane", async (method, pathname) => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const request = new Request(`https://zspan.org${pathname}`, { method });
    const response = await onApiRequest({
      request,
      env: { BACKEND_URL: "https://origin.example" },
    } as Parameters<typeof onApiRequest>[0]);

    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({
      success: false,
      error: "not found",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("2026-07-30 public notification unsubscribe admission", () => {
  it.each(PUBLIC_UNSUBSCRIBE_EXAMPLES)(
    "proxies %s %s on the public plane",
    async (method, requestPath, expectedPath) => {
      let proxied: Request | undefined;
      vi.stubGlobal(
        "fetch",
        vi.fn(async (request: Request) => {
          proxied = request;
          return new Response("{}", { status: 200 });
        }),
      );

      const request = new Request(`https://zspan.org${requestPath}`, {
        method,
      });
      const response = await onApiRequest({
        request,
        env: { BACKEND_URL: "https://origin.example" },
      } as Parameters<typeof onApiRequest>[0]);

      expect(PUBLIC_UNSUBSCRIBE_ROUTES).toHaveLength(2);
      expect(response.status).toBe(200);
      expect(proxied?.url).toBe(`https://origin.example${expectedPath}`);
      expect(proxied?.method).toBe(method);
    },
  );
});

describe("D-180 public edge admission", () => {
  it("freezes the explicit scope inventory", () => {
    expect(
      PUBLIC_EDGE_ROUTES.filter(route =>
        route.pathPattern.startsWith("/public-api/")
      )
    ).toHaveLength(17);
    expect(
      PUBLIC_EDGE_ROUTES.filter(route => route.pathPattern.startsWith("/v1/"))
    ).toHaveLength(3);
  });

  it("admits every explicitly approved route example", () => {
    expect(APPROVED_EXAMPLES).toHaveLength(PUBLIC_EDGE_ROUTES.length);
    APPROVED_EXAMPLES.forEach(([pathname, query], index) => {
      const matched = matchPublicRoute(
        "GET",
        pathname,
        new URLSearchParams(query)
      );
      expect(matched, `${pathname}?${query}`).toBe(PUBLIC_EDGE_ROUTES[index]);
    });
  });

  it.each([
    "/api/prompts/synopsis",
    "/api/work-orders",
    "/api/notebook/1",
    "/public-api/verify-run/1",
    "/public-api/watermark/lookup",
    "/public-api/ledger/Kingman",
    "/public-api/coverage",
    "/public-api/corrections",
    "/v1/not-catalog",
  ])("rejects unlisted path %s", pathname => {
    expect(matchPublicRoute("GET", pathname, new URLSearchParams())).toBeNull();
  });

  it("rejects non-GET methods", () => {
    expect(
      matchPublicRoute("POST", "/public-api/health", new URLSearchParams())
    ).toBeNull();
    expect(
      matchPublicRoute("get", "/public-api/health", new URLSearchParams())
    ).toBeNull();
    expect(
      matchPublicRoute(
        "POST",
        "/public-api/broadcasts/m_AAAAAAAAAAAAAAAAAAAAAA/sim-queries",
        new URLSearchParams(),
      ),
    ).toBeNull();
  });

  it.each([
    ["/public-api/cities/Kingman/meetings", "include_drafts=true"],
    [
      "/public-api/broadcasts/m_AAAAAAAAAAAAAAAAAAAAAA/citation",
      "audience=operator",
    ],
    [
      "/public-api/broadcasts/m_AAAAAAAAAAAAAAAAAAAAAA/sidecars/quotes",
      "include_all=true",
    ],
    [
      "/public-api/broadcasts/m_AAAAAAAAAAAAAAAAAAAAAA/sim-queries",
      "include_provenance=true",
    ],
  ])("rejects forbidden query switches on %s", (pathname, query) => {
    expect(
      matchPublicRoute("GET", pathname, new URLSearchParams(query))
    ).toBeNull();
  });

  it("classifies every Pages entry point into a reviewed trust plane", () => {
    expect(entryPointFiles(FUNCTIONS_DIR)).toEqual(
      Object.keys(FUNCTION_ENTRYPOINT_TRUST_PLANE).sort()
    );
    expect(new Set(Object.values(FUNCTION_ENTRYPOINT_TRUST_PLANE))).toEqual(
      new Set(["operator", "public", "shared"])
    );
  });

  it("proxies sim queries on the public host with credentials stripped and response preserved", async () => {
    let proxied: Request | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (request: Request) => {
        proxied = request;
        return new Response('{"error":"rate limited"}', {
          status: 429,
          headers: {
            "content-type": "application/json",
            "retry-after": "17",
          },
        });
      }),
    );

    const request = new Request(
      "https://zspan.org/public-api/broadcasts/m_AAAAAAAAAAAAAAAAAAAAAA/sim-queries",
      {
        method: "GET",
        headers: {
          authorization: "Bearer private",
          cookie: "zspan_session=private",
          "agent-role": "owner",
          "x-zspan-agent-role": "owner",
        },
      },
    );
    const response = await onPublicApiRequest({
      request,
      env: {
        BACKEND_URL: "https://origin.example",
        ZSPAN_EDGE_TOKEN: "edge-secret",
      },
    } as Parameters<typeof onPublicApiRequest>[0]);

    expect(proxied?.url).toBe(
      "https://origin.example/public-api/broadcasts/m_AAAAAAAAAAAAAAAAAAAAAA/sim-queries",
    );
    expect(proxied?.method).toBe("GET");
    expect(proxied?.headers.get("authorization")).toBeNull();
    expect(proxied?.headers.get("cookie")).toBeNull();
    expect(proxied?.headers.get("agent-role")).toBeNull();
    expect(proxied?.headers.get("x-zspan-agent-role")).toBeNull();
    expect(proxied?.headers.get("x-zspan-edge-token")).toBe("edge-secret");
    expect(response.status).toBe(429);
    expect(response.headers.get("retry-after")).toBe("17");
    expect(await response.text()).toBe('{"error":"rate limited"}');
  });
});
