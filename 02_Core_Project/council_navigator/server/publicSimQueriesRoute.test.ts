import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { flaskProxyHeaders } from "./originTrust";

const SERVER_SOURCE = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), "index.ts"),
  "utf8"
);

describe("signed-out sim-query Express admission", () => {
  it("uses the anonymous status-preserving proxy and encodes the public id", () => {
    const route = SERVER_SOURCE.match(
      /app\.get\("\/public-api\/broadcasts\/:public_id\/sim-queries"[\s\S]*?\n  \}\);/
    )?.[0];

    expect(route).toBeDefined();
    expect(route).toContain("proxyJsonAnonymous(");
    expect(route).toContain("encodeURIComponent(req.params.public_id)");
    expect(route).not.toContain("proxyJsonAuth(");
    expect(route).toContain("Object.keys(req.query).length > 0");
    expect(route).toContain(
      'res.status(404).json({ success: false, error: "not found" })'
    );

    const anonymousProxy = SERVER_SOURCE.match(
      /async function proxyJsonAnonymous\([\s\S]*?\n  }\n\n  \/\/ ============================================/
    )?.[0];
    expect(anonymousProxy).toContain("res.status(upstream.status)");
    expect(anonymousProxy).toContain("res.send(await upstream.text())");
    expect(anonymousProxy).toContain('"retry-after"');
  });

  it("forwards no cookie, authorization, owner, or agent credential headers", () => {
    const headers = flaskProxyHeaders({
      headers: {
        authorization: "Bearer private",
        cookie: "zspan_session=private",
        "agent-role": "owner",
        "x-zspan-agent-role": "owner",
      },
      ip: "127.0.0.1",
    });

    expect(Object.keys(headers).map(key => key.toLowerCase())).not.toEqual(
      expect.arrayContaining([
        "authorization",
        "cookie",
        "agent-role",
        "x-zspan-agent-role",
      ])
    );
  });
});
