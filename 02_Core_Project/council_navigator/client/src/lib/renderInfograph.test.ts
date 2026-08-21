import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const qrMocks = vi.hoisted(() => ({
  toDataURL: vi.fn(),
}));

vi.mock("qrcode", () => ({
  default: {
    toDataURL: qrMocks.toDataURL,
  },
}));

import {
  canonicalBroadcastUrl,
  infographFilename,
  prepareInfographKeyDecisions,
  renderInfograph,
  type InfographInput,
} from "./renderInfograph";

let fillText: ReturnType<typeof vi.fn>;

function infographInput(
  overrides: Partial<InfographInput> = {}
): InfographInput {
  return {
    city: "Parity City",
    date: "2026-07-22",
    title: "Regular City Council Meeting",
    tagline: "A meeting headline",
    keyDecisions: ["The council adopted the fiscal-year budget."],
    publicUrl: canonicalBroadcastUrl("m_ABC123XYZ"),
    ...overrides,
  };
}

beforeEach(() => {
  qrMocks.toDataURL.mockReset();
  qrMocks.toDataURL.mockResolvedValue("data:image/png;base64,cXI=");

  fillText = vi.fn();
  const context = {
    fillStyle: "",
    strokeStyle: "",
    font: "",
    lineWidth: 1,
    textBaseline: "alphabetic",
    textAlign: "start",
    fillRect: vi.fn(),
    fillText,
    measureText: vi.fn((text: string) => ({ width: text.length * 7 })),
    beginPath: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    quadraticCurveTo: vi.fn(),
    closePath: vi.fn(),
    fill: vi.fn(),
    stroke: vi.fn(),
    drawImage: vi.fn(),
  } as unknown as CanvasRenderingContext2D;
  const canvas = {
    width: 0,
    height: 0,
    getContext: vi.fn(() => context),
    toBlob: vi.fn((callback: BlobCallback) => {
      callback(new Blob(["png"], { type: "image/png" }));
    }),
  } as unknown as HTMLCanvasElement;

  vi.stubGlobal("document", {
    createElement: vi.fn(() => canvas),
  });
  vi.stubGlobal(
    "Image",
    class {
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;

      set src(_value: string) {
        this.onload?.();
      }
    }
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("renderInfograph", () => {
  it("uses the exact canonical public-id URL for both QR and printed provenance", async () => {
    const publicId = "m_ABC123XYZ";
    const expectedUrl =
      "https://zspan.org/?view=broadcast&publicId=m_ABC123XYZ";

    expect(canonicalBroadcastUrl(publicId)).toBe(expectedUrl);
    await renderInfograph(
      infographInput({ publicUrl: canonicalBroadcastUrl(publicId) })
    );

    expect(qrMocks.toDataURL).toHaveBeenCalledWith(
      expectedUrl,
      expect.any(Object)
    );
    expect(fillText.mock.calls.some(([text]) => text === expectedUrl)).toBe(
      true
    );
  });

  it("renders a conventional dark-on-light QR with a four-module margin", async () => {
    const publicUrl = canonicalBroadcastUrl("m_QR");

    await renderInfograph(infographInput({ publicUrl }));

    expect(qrMocks.toDataURL).toHaveBeenCalledWith(publicUrl, {
      width: 128,
      margin: 4,
      color: {
        dark: "#0b0d10",
        light: "#ffffff",
      },
      errorCorrectionLevel: "M",
    });
  });

  it("prefers sidecar decisions and sends only plain text into the card", async () => {
    const decisions = prepareInfographKeyDecisions(
      ["<core>fiscal budget</core> **adopted**"],
      ["Legacy decision"]
    );

    expect(decisions).toEqual(["fiscal budget adopted"]);
    await renderInfograph(infographInput({ keyDecisions: decisions }));

    expect(
      fillText.mock.calls.some(([text]) => text === "fiscal budget adopted")
    ).toBe(true);
    expect(
      fillText.mock.calls.some(([text]) => text === "Legacy decision")
    ).toBe(false);
  });

  it("uses neutral copy when no key-decision summary is available", async () => {
    await renderInfograph(infographInput({ keyDecisions: [] }));

    expect(
      fillText.mock.calls.some(
        ([text]) => text === "No key-decision summary available."
      )
    ).toBe(true);
    expect(
      fillText.mock.calls.some(
        ([text]) => text === "No binding decisions on record for this meeting."
      )
    ).toBe(false);
  });
});

describe("infographFilename", () => {
  it("slugifies city + date and preserves the public_id verbatim", () => {
    expect(
      infographFilename("Bullhead City", "2026-07-15", "m_ABC123XYZ")
    ).toBe("zspan-bullhead-city-2026-07-15-m_ABC123XYZ.png");
  });

  it("collapses runs of non-alphanumeric characters", () => {
    expect(
      infographFilename("St. John's — West", "Jul 15, 2026", "m_pub")
    ).toBe("zspan-st-john-s-west-jul-15-2026-m_pub.png");
  });

  it("strips leading and trailing slug hyphens", () => {
    expect(infographFilename("!!Kingman!!", "!!date!!", "id0")).toBe(
      "zspan-kingman-date-id0.png"
    );
  });
});
