import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  proxyToBackend,
  resolveAccessEmail,
  verifyAccessJWT,
  _resetJwksCacheForTests,
} from "./edgeProxy";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("edge proxy client-IP forwarding", () => {
  it("replaces spoofable IP headers with CF-Connecting-IP", async () => {
    let proxied: Request | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (request: Request) => {
        proxied = request;
        return new Response("{}", { status: 200 });
      })
    );
    const request = new Request("https://zspan.org/public-api/health", {
      headers: {
        "cf-connecting-ip": "203.0.113.20",
        "x-forwarded-for": "198.51.100.77",
        "x-zspan-client-ip": "198.51.100.88",
        "x-zspan-edge-token": "client-forged",
      },
    });

    await proxyToBackend(
      request,
      {
        BACKEND_URL: "https://origin.example",
        ZSPAN_EDGE_TOKEN: "edge-secret",
      },
      "/public-api/health",
      true
    );

    expect(proxied).toBeDefined();
    expect(proxied!.headers.get("x-zspan-client-ip")).toBe("203.0.113.20");
    expect(proxied!.headers.get("x-forwarded-for")).toBeNull();
    expect(proxied!.headers.get("cf-connecting-ip")).toBeNull();
    expect(proxied!.headers.get("x-zspan-edge-token")).toBe("edge-secret");
  });

  it("drops a spoofed trusted header when CF metadata is absent", async () => {
    let proxied: Request | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (request: Request) => {
        proxied = request;
        return new Response("{}", { status: 200 });
      })
    );
    const request = new Request("https://zspan.org/public-api/health", {
      headers: { "x-zspan-client-ip": "198.51.100.88" },
    });

    await proxyToBackend(
      request,
      {
        BACKEND_URL: "https://origin.example",
        ZSPAN_EDGE_TOKEN: "edge-secret",
      },
      "/public-api/health",
      true
    );

    expect(proxied).toBeDefined();
    expect(proxied!.headers.get("x-zspan-client-ip")).toBeNull();
  });
});

// ── Session-103 paired test (sol Round-1 recommendation) ──────────────
// The origin's compute_redirect_uri picks the OAuth callback by inspecting
// X-Forwarded-Host, and Flask's _forwarded_host_url() reads BOTH
// X-Forwarded-Host + X-Forwarded-Proto. If the Pages Function doesn't
// synthesize these from the trusted Request.url, Flask sees the Railway
// internal host and falls through to localhost:3000 — which broke public
// sign-in after we removed the ZSPAN_OAUTH_REDIRECT_URI hardcode.
describe("edge proxy X-Forwarded-Host synthesis (sign-in bug diagnostic)", () => {
  it("synthesizes X-Forwarded-Host: zspan.org from a public request URL", async () => {
    let proxied: Request | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (request: Request) => {
        proxied = request;
        return new Response("{}", { status: 200 });
      })
    );
    const request = new Request(
      "https://zspan.org/api/auth/google/login?next=%2F",
      { method: "GET" }
    );

    await proxyToBackend(
      request,
      { BACKEND_URL: "https://origin.example" },
      "/api/auth/google/login?next=%2F",
      false
    );

    expect(proxied).toBeDefined();
    expect(proxied!.headers.get("x-forwarded-host")).toBe("zspan.org");
    expect(proxied!.headers.get("x-forwarded-proto")).toBe("https");
  });

  it("synthesizes X-Forwarded-Host: operator.zspan.org from an operator URL", async () => {
    let proxied: Request | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (request: Request) => {
        proxied = request;
        return new Response("{}", { status: 200 });
      })
    );
    const request = new Request(
      "https://operator.zspan.org/api/auth/google/login",
      { method: "GET" }
    );

    await proxyToBackend(
      request,
      { BACKEND_URL: "https://origin.example" },
      "/api/auth/google/login",
      false
    );

    expect(proxied).toBeDefined();
    expect(proxied!.headers.get("x-forwarded-host")).toBe("operator.zspan.org");
    expect(proxied!.headers.get("x-forwarded-proto")).toBe("https");
  });

  it("overrides caller-supplied X-Forwarded-Host with the trusted synthesized value", async () => {
    let proxied: Request | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (request: Request) => {
        proxied = request;
        return new Response("{}", { status: 200 });
      })
    );
    // Attacker-controlled headers on the incoming request — must be
    // dropped and replaced with values derived from Request.url so a
    // downstream compute_redirect_uri can never be steered by client input.
    const request = new Request("https://zspan.org/api/auth/google/login", {
      method: "GET",
      headers: {
        "x-forwarded-host": "malicious.example",
        "x-forwarded-proto": "http",
      },
    });

    await proxyToBackend(
      request,
      { BACKEND_URL: "https://origin.example" },
      "/api/auth/google/login",
      false
    );

    expect(proxied).toBeDefined();
    expect(proxied!.headers.get("x-forwarded-host")).toBe("zspan.org");
    expect(proxied!.headers.get("x-forwarded-proto")).toBe("https");
  });

  it("preserves port in the synthesized host when the request URL has one", async () => {
    let proxied: Request | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (request: Request) => {
        proxied = request;
        return new Response("{}", { status: 200 });
      })
    );
    const request = new Request(
      "http://localhost:3000/api/auth/google/login",
      { method: "GET" }
    );

    await proxyToBackend(
      request,
      { BACKEND_URL: "https://origin.example" },
      "/api/auth/google/login",
      false
    );

    expect(proxied).toBeDefined();
    // URL.host includes the port when non-default; URL.protocol strips the
    // trailing colon per the fix. Local-dev callers should reach Flask with
    // enough context to resolve the dev-fallback redirect_uri correctly.
    expect(proxied!.headers.get("x-forwarded-host")).toBe("localhost:3000");
    expect(proxied!.headers.get("x-forwarded-proto")).toBe("http");
  });
});

// ── Session-104 companion pair (Railway-ingress-survivable) ──────────
// Railway ingress rewrites X-Forwarded-Host between CF Pages Function
// and Express to the internal Railway hostname. X-ZSPAN-Origin-Host is
// the non-standard companion Railway leaves untouched — Express prefers
// it in proxyAuthToFlask so Flask sees the true browser-facing host.
describe("edge proxy X-ZSPAN-Origin-Host synthesis (Railway-ingress survivor)", () => {
  it("synthesizes X-ZSPAN-Origin-Host: zspan.org from a public request URL", async () => {
    let proxied: Request | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (request: Request) => {
        proxied = request;
        return new Response("{}", { status: 200 });
      })
    );
    const request = new Request(
      "https://zspan.org/api/auth/google/login?next=%2F",
      { method: "GET" }
    );

    await proxyToBackend(
      request,
      { BACKEND_URL: "https://origin.example" },
      "/api/auth/google/login?next=%2F",
      false
    );

    expect(proxied).toBeDefined();
    expect(proxied!.headers.get("x-zspan-origin-host")).toBe("zspan.org");
    expect(proxied!.headers.get("x-zspan-origin-proto")).toBe("https");
  });

  it("synthesizes X-ZSPAN-Origin-Host: operator.zspan.org from an operator URL", async () => {
    let proxied: Request | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (request: Request) => {
        proxied = request;
        return new Response("{}", { status: 200 });
      })
    );
    const request = new Request(
      "https://operator.zspan.org/api/auth/google/login",
      { method: "GET" }
    );

    await proxyToBackend(
      request,
      { BACKEND_URL: "https://origin.example" },
      "/api/auth/google/login",
      false
    );

    expect(proxied).toBeDefined();
    expect(proxied!.headers.get("x-zspan-origin-host")).toBe("operator.zspan.org");
    expect(proxied!.headers.get("x-zspan-origin-proto")).toBe("https");
  });

  it("overrides caller-supplied X-ZSPAN-Origin-Host with the trusted synthesized value", async () => {
    let proxied: Request | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (request: Request) => {
        proxied = request;
        return new Response("{}", { status: 200 });
      })
    );
    const request = new Request("https://zspan.org/api/auth/google/login", {
      method: "GET",
      headers: {
        "x-zspan-origin-host": "malicious.example",
        "x-zspan-origin-proto": "http",
      },
    });

    await proxyToBackend(
      request,
      { BACKEND_URL: "https://origin.example" },
      "/api/auth/google/login",
      false
    );

    expect(proxied).toBeDefined();
    expect(proxied!.headers.get("x-zspan-origin-host")).toBe("zspan.org");
    expect(proxied!.headers.get("x-zspan-origin-proto")).toBe("https");
  });
});

// ── CF Access JWT verification tests (sol pen-test Finding #10) ──────────
//
// Real RSA signing + WebCrypto verification. We generate a fresh RSA
// keypair in beforeEach, export the public JWK, and mock the JWKS fetch
// to return it. JWTs are signed with the private key. This gives real
// crypto coverage — not stubbed verify() calls.

const TEAM_DOMAIN = "zspan.cloudflareaccess.com";
const APP_AUD = "test-app-audience-12345";
const OWNER_EMAIL = "owner@zspan.example";
const JWKS_URL = `https://${TEAM_DOMAIN}/cdn-cgi/access/certs`;

function b64url(input: Uint8Array | string): string {
  const bytes = typeof input === "string" ? new TextEncoder().encode(input) : input;
  let raw = "";
  for (let i = 0; i < bytes.length; i++) raw += String.fromCharCode(bytes[i]);
  return btoa(raw).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
}

async function makeKeypair(): Promise<CryptoKeyPair> {
  return crypto.subtle.generateKey(
    {
      name: "RSASSA-PKCS1-v1_5",
      modulusLength: 2048,
      publicExponent: new Uint8Array([1, 0, 1]),
      hash: "SHA-256",
    },
    true,
    ["sign", "verify"],
  ) as Promise<CryptoKeyPair>;
}

async function exportJwk(key: CryptoKey, kid: string): Promise<JsonWebKey> {
  const jwk = (await crypto.subtle.exportKey("jwk", key)) as JsonWebKey & { kid?: string };
  jwk.kid = kid;
  jwk.alg = "RS256";
  jwk.use = "sig";
  return jwk;
}

async function signJWT(
  privateKey: CryptoKey,
  kid: string,
  payload: Record<string, unknown>,
): Promise<string> {
  const header = { alg: "RS256", kid, typ: "JWT" };
  const headerB64 = b64url(JSON.stringify(header));
  const payloadB64 = b64url(JSON.stringify(payload));
  const signingInput = new TextEncoder().encode(`${headerB64}.${payloadB64}`);
  const signature = await crypto.subtle.sign(
    "RSASSA-PKCS1-v1_5",
    privateKey,
    signingInput,
  );
  const sigB64 = b64url(new Uint8Array(signature));
  return `${headerB64}.${payloadB64}.${sigB64}`;
}

function baseClaims(overrides: Partial<Record<string, unknown>> = {}): Record<string, unknown> {
  const now = Math.floor(Date.now() / 1000);
  return {
    iss: `https://${TEAM_DOMAIN}`,
    aud: APP_AUD,
    exp: now + 3600,
    nbf: now - 60,
    email: OWNER_EMAIL,
    sub: "test-subject",
    identity_nonce: "test-nonce",
    ...overrides,
  };
}

describe("CF Access JWT verification (Finding #10)", () => {
  let keypair: CryptoKeyPair;
  const kid = "test-key-1";

  beforeEach(async () => {
    _resetJwksCacheForTests();
    keypair = await makeKeypair();
    const publicJwk = await exportJwk(keypair.publicKey, kid);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string | URL | Request) => {
        const target = typeof url === "string" ? url : url instanceof URL ? url.toString() : url.url;
        if (target === JWKS_URL) {
          return new Response(JSON.stringify({ keys: [publicJwk] }), { status: 200 });
        }
        return new Response("not found", { status: 404 });
      }),
    );
  });

  it("returns the email for a valid signed JWT with correct iss + aud + exp", async () => {
    const jwt = await signJWT(keypair.privateKey, kid, baseClaims());
    const email = await verifyAccessJWT(jwt, {
      ACCESS_TEAM_DOMAIN: TEAM_DOMAIN,
      ACCESS_APP_AUD: APP_AUD,
    });
    expect(email).toBe(OWNER_EMAIL);
  });

  it("resolveAccessEmail returns null when Cf-Access-Jwt-Assertion header is missing", async () => {
    const headers = new Headers({ "cf-access-authenticated-user-email": OWNER_EMAIL });
    const email = await resolveAccessEmail(headers, {
      ACCESS_TEAM_DOMAIN: TEAM_DOMAIN,
      ACCESS_APP_AUD: APP_AUD,
    });
    expect(email).toBeNull();
  });

  it("returns null for a malformed JWT (wrong part count)", async () => {
    const email = await verifyAccessJWT("not.a.valid.jwt.at.all", {
      ACCESS_TEAM_DOMAIN: TEAM_DOMAIN,
      ACCESS_APP_AUD: APP_AUD,
    });
    expect(email).toBeNull();
  });

  it("returns null for a JWT with three parts but a garbage signature", async () => {
    const header = b64url(JSON.stringify({ alg: "RS256", kid, typ: "JWT" }));
    const payload = b64url(JSON.stringify(baseClaims()));
    const badSig = b64url("garbage-signature-bytes");
    const email = await verifyAccessJWT(`${header}.${payload}.${badSig}`, {
      ACCESS_TEAM_DOMAIN: TEAM_DOMAIN,
      ACCESS_APP_AUD: APP_AUD,
    });
    expect(email).toBeNull();
  });

  it("returns null for a JWT signed by a different key (wrong signature)", async () => {
    const otherKeypair = await makeKeypair();
    const jwt = await signJWT(otherKeypair.privateKey, kid, baseClaims());
    const email = await verifyAccessJWT(jwt, {
      ACCESS_TEAM_DOMAIN: TEAM_DOMAIN,
      ACCESS_APP_AUD: APP_AUD,
    });
    expect(email).toBeNull();
  });

  it("returns null for a wrong-audience JWT", async () => {
    const jwt = await signJWT(keypair.privateKey, kid, baseClaims({ aud: "some-other-app" }));
    const email = await verifyAccessJWT(jwt, {
      ACCESS_TEAM_DOMAIN: TEAM_DOMAIN,
      ACCESS_APP_AUD: APP_AUD,
    });
    expect(email).toBeNull();
  });

  it("accepts a JWT with aud as an array that includes the app AUD", async () => {
    const jwt = await signJWT(keypair.privateKey, kid, baseClaims({ aud: ["other", APP_AUD] }));
    const email = await verifyAccessJWT(jwt, {
      ACCESS_TEAM_DOMAIN: TEAM_DOMAIN,
      ACCESS_APP_AUD: APP_AUD,
    });
    expect(email).toBe(OWNER_EMAIL);
  });

  it("returns null for a wrong-issuer JWT", async () => {
    const jwt = await signJWT(keypair.privateKey, kid, baseClaims({ iss: "https://evil.example" }));
    const email = await verifyAccessJWT(jwt, {
      ACCESS_TEAM_DOMAIN: TEAM_DOMAIN,
      ACCESS_APP_AUD: APP_AUD,
    });
    expect(email).toBeNull();
  });

  it("returns null for an expired JWT (exp in the past)", async () => {
    const jwt = await signJWT(keypair.privateKey, kid, baseClaims({ exp: Math.floor(Date.now() / 1000) - 60 }));
    const email = await verifyAccessJWT(jwt, {
      ACCESS_TEAM_DOMAIN: TEAM_DOMAIN,
      ACCESS_APP_AUD: APP_AUD,
    });
    expect(email).toBeNull();
  });

  it("returns null for a not-yet-valid JWT (nbf in the future)", async () => {
    const jwt = await signJWT(keypair.privateKey, kid, baseClaims({ nbf: Math.floor(Date.now() / 1000) + 3600 }));
    const email = await verifyAccessJWT(jwt, {
      ACCESS_TEAM_DOMAIN: TEAM_DOMAIN,
      ACCESS_APP_AUD: APP_AUD,
    });
    expect(email).toBeNull();
  });

  it("fails closed when ACCESS_TEAM_DOMAIN is unset", async () => {
    const jwt = await signJWT(keypair.privateKey, kid, baseClaims());
    const email = await verifyAccessJWT(jwt, {
      ACCESS_TEAM_DOMAIN: "",
      ACCESS_APP_AUD: APP_AUD,
    });
    expect(email).toBeNull();
  });

  it("fails closed when ACCESS_APP_AUD is unset", async () => {
    const jwt = await signJWT(keypair.privateKey, kid, baseClaims());
    const email = await verifyAccessJWT(jwt, {
      ACCESS_TEAM_DOMAIN: TEAM_DOMAIN,
      ACCESS_APP_AUD: "",
    });
    expect(email).toBeNull();
  });

  it("resolveAccessEmail rejects the friendly cf-access-authenticated-user-email header without a JWT", async () => {
    const headers = new Headers({
      "cf-access-authenticated-user-email": OWNER_EMAIL,
    });
    const email = await resolveAccessEmail(headers, {
      ACCESS_TEAM_DOMAIN: TEAM_DOMAIN,
      ACCESS_APP_AUD: APP_AUD,
    });
    expect(email).toBeNull();
  });

  it("returns null when the JWT's kid isn't in the JWKS", async () => {
    const jwt = await signJWT(keypair.privateKey, "unknown-kid", baseClaims());
    const email = await verifyAccessJWT(jwt, {
      ACCESS_TEAM_DOMAIN: TEAM_DOMAIN,
      ACCESS_APP_AUD: APP_AUD,
    });
    expect(email).toBeNull();
  });

  it("returns null when the JWKS fetch fails", async () => {
    vi.unstubAllGlobals();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("server error", { status: 503 })),
    );
    _resetJwksCacheForTests();
    const jwt = await signJWT(keypair.privateKey, kid, baseClaims());
    const email = await verifyAccessJWT(jwt, {
      ACCESS_TEAM_DOMAIN: TEAM_DOMAIN,
      ACCESS_APP_AUD: APP_AUD,
    });
    expect(email).toBeNull();
  });
});
