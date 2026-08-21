import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import { DirectoryStateTab } from "./ChannelsPage";

describe("V3 state rail gate", () => {
  it("keeps a locked state visibly in progress without making it clickable", () => {
    const onSelect = vi.fn();
    const markup = renderToStaticMarkup(
      <DirectoryStateTab
        code="NY"
        name="New York"
        selected={false}
        status="scaffold"
        onSelect={onSelect}
      />,
    );

    expect(markup).toContain("disabled");
    expect(markup).toContain("cursor-not-allowed");
    expect(markup).toContain("bg-amber-500/85");
    expect(markup).toContain("lucide-lock");
    expect(markup).toContain(">v3</span>");
    expect(markup).toContain("unlocks in version 3");
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("leaves Arizona open without a version lock", () => {
    const markup = renderToStaticMarkup(
      <DirectoryStateTab
        code="AZ"
        name="Arizona"
        selected
        status="live"
        onSelect={() => {}}
      />,
    );

    expect(markup).not.toContain("disabled");
    expect(markup).not.toContain("lucide-lock");
    expect(markup).not.toContain(">v3</span>");
    expect(markup).toContain("kg-dot-active");
    expect(markup).toContain("Browse Arizona");
  });
});
