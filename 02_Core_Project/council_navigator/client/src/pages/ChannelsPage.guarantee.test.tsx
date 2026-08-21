import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { CatalogContributionExplainerBody } from "../components/CatalogContributionDialog";
import { EmptyChannelState } from "./ChannelsPage";

function renderPresentation(
  presentation: "contribute" | "guarantee" | "source_update" | "neutral"
) {
  return renderToStaticMarkup(
    <EmptyChannelState
      title="Lifecycle heading"
      message="Lifecycle message"
      variant="episodes"
      presentation={presentation}
      contributionUrl="https://example.gov/contribute"
    />
  );
}

describe("empty shelf lifecycle artwork", () => {
  it("shows the sleeping cat only while the source is missing", () => {
    const markup = renderPresentation("contribute");
    expect(markup).toContain("/states/sleeping-cat.png");
    expect(
      markup.match(/<img[^>]+src="\/states\/sleeping-cat\.png"/g),
    ).toHaveLength(1);
    expect(markup).toContain("Click to wake the cat");
    expect(markup).not.toContain("zspan-guarantee-card.svg");
    expect(markup).not.toContain('href="https://example.gov/contribute"');
    expect(markup).not.toContain("Lifecycle message");
    expect(markup).not.toContain("Other Channels");
    expect(markup).not.toContain(">Guide<");
  });

  it("replaces the cat with the guarantee card while the parser is underway", () => {
    const markup = renderPresentation("guarantee");
    expect(markup).toContain("/brand/zspan-guarantee-card.svg");
    expect(markup).not.toContain("/states/sleeping-cat.png");
    expect(markup).not.toContain("wake the cat");
  });

  it("uses no promotional artwork for source results or connected empty shelves", () => {
    for (const presentation of ["source_update", "neutral"] as const) {
      const markup = renderPresentation(presentation);
      expect(markup).not.toContain("zspan-guarantee-card.svg");
      expect(markup).not.toContain("/states/sleeping-cat.png");
    }
  });

  it("renders the lifecycle title as a real heading", () => {
    expect(renderPresentation("guarantee")).toContain(
      '<h3 class="mb-2 text-base font-semibold text-foreground/80">Lifecycle heading</h3>'
    );
  });

  it("explains the catalog and guarantee before offering the AI handoff", () => {
    const markup = renderToStaticMarkup(
      <CatalogContributionExplainerBody contributionUrl="/public-api/catalog/contribute/source-1.md?state=AZ" />
    );

    expect(markup).toContain("National Civics Catalog");
    expect(markup).toContain("zspan-guarantee-card.svg");
    expect(markup).toContain("within three days");
    expect(markup).toContain("reviewed and accepted");
    expect(markup).toContain("clear public update explaining what");
    expect(markup).toContain("You never have to edit JSON by hand");
    expect(markup).toContain("Take the guide to your AI");
    expect(markup).toContain(
      'href="/public-api/catalog/contribute/source-1.md?state=AZ"'
    );
  });

  it("uses an honest state-folder action when there is no listing-specific handoff", () => {
    const markup = renderToStaticMarkup(
      <CatalogContributionExplainerBody contributionUrl="https://github.com/anitacigawet/national-civics-catalog/tree/main/data/states/pr" />
    );

    expect(markup).toContain("Open this state’s catalog folder");
    expect(markup).not.toContain("Take the guide to your AI");
  });
});
