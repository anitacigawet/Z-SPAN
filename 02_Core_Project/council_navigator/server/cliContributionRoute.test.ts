import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const SERVER_SOURCE = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), "index.ts"),
  "utf8",
);

describe("official client contribution proxy", () => {
  it("admits the exact contribution route through the bearer proxy", () => {
    expect(SERVER_SOURCE).toContain(
      'app.post("/api/contributions/submit", (req, res) =>',
    );
    expect(SERVER_SOURCE).toContain(
      'proxyJsonBearer(req, res, "/api/contributions/submit")',
    );
  });

  it("applies the same 25 MiB ceiling as Flask before the global parser", () => {
    expect(SERVER_SOURCE).toContain(
      'const contributionJsonParser = express.json({ limit: "25mb" });',
    );
    expect(SERVER_SOURCE).toContain(
      'req.path === "/api/contributions/submit"',
    );
  });
});
