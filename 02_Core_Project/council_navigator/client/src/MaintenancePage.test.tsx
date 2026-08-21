import { readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import MaintenancePage from "./MaintenancePage";

describe("site-wide maintenance screen", () => {
  it("renders only the maintenance message and sleeping cat", () => {
    const markup = renderToStaticMarkup(<MaintenancePage />);

    expect(markup).toContain("Z-SPAN is undergoing maintenance.");
    expect(markup).toContain("Please check back later. Thank you for your time.");
    expect(markup).toContain('src="/states/sleeping-cat.png"');
    expect(markup.match(/Z-SPAN/g)).toHaveLength(1);
    expect(markup).not.toContain("maintenance-wordmark");
    expect(markup).not.toContain("<a ");
    expect(markup).not.toContain("<button");
  });

  it("remains available behind the maintenance build setting", () => {
    const entry = readFileSync(new URL("./main.tsx", import.meta.url), "utf8");

    expect(entry).toContain('import App from "./App"');
    expect(entry).toContain('import MaintenancePage from "./MaintenancePage"');
    expect(entry).toContain("VITE_SITE_MODE");
    expect(entry).toContain('=== "maintenance"');
    expect(entry).toContain("<MaintenancePage />");
    expect(entry).toContain("<App />");
  });
});
