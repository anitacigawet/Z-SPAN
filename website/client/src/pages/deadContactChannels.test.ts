import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const PAGE_DIR = dirname(fileURLToPath(import.meta.url));
const CLIENT_DIR = resolve(PAGE_DIR, "../..");

const readClientFile = (path: string) =>
 readFileSync(resolve(CLIENT_DIR, path), "utf8");

describe("dead public contact channels", () => {
 it("does not advertise the retired corrections mailbox", () => {
 const visitorPages = [
 readClientFile("src/pages/BroadcastPage.tsx"),
 readClientFile("src/pages/CorrectionsPage.tsx"),
 ].join("\n");

 expect(visitorPages).not.toContain("corrections@zspan.org");
 expect(visitorPages).not.toContain("mailto:corrections");
 expect(visitorPages).toContain("Corrections intake is temporarily closed.");
 });

 it("does not publish the retired security mailbox or false policy URL", () => {
 const securityTxt = resolve(CLIENT_DIR, "public/.well-known/security.txt");
 const robotsTxt = readClientFile("public/robots.txt");

 expect(existsSync(securityTxt)).toBe(false);
 expect(robotsTxt).not.toContain("security@zspan.org");
 expect(robotsTxt).not.toContain("/security-policy");
 expect(robotsTxt).not.toContain("security.txt");
 });
});
