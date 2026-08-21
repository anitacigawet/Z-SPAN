import { readFileSync, readdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { WatermarkRibbon } from "./WatermarkRibbon";

const COMPONENT_DIR = dirname(fileURLToPath(import.meta.url));
const CLIENT_SRC_DIR = resolve(COMPONENT_DIR, "..");

function productionClientSource(directory: string): string {
  return readdirSync(directory, { withFileTypes: true })
    .flatMap(entry => {
      const path = resolve(directory, entry.name);
      if (entry.isDirectory()) return productionClientSource(path);
      if (!/\.(ts|tsx)$/.test(entry.name) || /\.(test|spec)\.(ts|tsx)$/.test(entry.name))
        return [];
      return [readFileSync(path, "utf8")];
    })
    .join("\n");
}

describe("operator live-review regressions", () => {
  it("renders no invented badge while a ribbon token is pending", () => {
    expect(renderToStaticMarkup(
      <WatermarkRibbon
        meetingId={127795}
        outputType="community_calls_to_action"
        registrationState="pending"
      />,
    )).toBe("");
  });

  it("keeps a valid registered ribbon visible and verifiable", () => {
    const markup = renderToStaticMarkup(
      <WatermarkRibbon
        meetingId={127696}
        outputType="community_calls_to_action"
        ribbonToken="ABCDEFGH"
        registrationState="registered"
      />,
    );
    expect(markup).toContain("watermark-verify");
    expect(markup).toContain('data-registration-state="registered"');
    expect(markup).toContain("<svg");
  });

  it("uses neutral CCTA fallback copy and removes the public dispute affordance", () => {
    const source = productionClientSource(CLIENT_SRC_DIR);
    expect(source).toContain("Approximate source marker:");
    expect(source).not.toMatch(/said around/i);
    expect(source).not.toMatch(/dispute (?:this|the) record/i);
  });

  it("routes the constrained account pill through the first-name helper", () => {
    const source = readFileSync(resolve(COMPONENT_DIR, "SignInPill.tsx"), "utf8");
    expect(source).toContain(
      "const chipName = firstNameForChip(user.display_name, user.email);",
    );
    expect(source).not.toContain("{user.display_name || user.email}");
  });
});
