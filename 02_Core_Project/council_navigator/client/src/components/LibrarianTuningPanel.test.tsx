import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  buildLibrarianTuningPatch,
  LibrarianTuningPanelBody,
  LibrarianTuningRequestError,
  patchLibrarianTuning,
  type LibrarianTuningKey,
  type LibrarianTuningResponse,
} from "./LibrarianTuningPanel";

const values: Record<LibrarianTuningKey, number> = {
  librarian_daily_query_cap: 3,
  librarian_reject_burst_threshold: 8,
  librarian_reject_burst_window_seconds: 600,
  librarian_reject_cooldown_seconds: 1800,
  librarian_reject_autoban_strike_threshold: 3,
  librarian_reject_autoban_window_seconds: 86400,
};

function tuningResponse(groupFallbackActive = false): LibrarianTuningResponse {
  return {
    settings: {
      librarian_daily_query_cap: {
        value: 3,
        default: 3,
        min: 1,
        max: null,
        unit: "queries",
      },
      librarian_reject_burst_threshold: {
        value: 8,
        default: 8,
        min: 4,
        max: 64,
        unit: "rejects",
      },
      librarian_reject_burst_window_seconds: {
        value: 600,
        default: 600,
        min: 60,
        max: null,
        unit: "seconds",
      },
      librarian_reject_cooldown_seconds: {
        value: 1800,
        default: 1800,
        min: 300,
        max: null,
        unit: "seconds",
      },
      librarian_reject_autoban_strike_threshold: {
        value: 3,
        default: 3,
        min: 2,
        max: 32,
        unit: "cooldowns",
      },
      librarian_reject_autoban_window_seconds: {
        value: 86400,
        default: 86400,
        min: 3600,
        max: null,
        unit: "seconds",
      },
    },
    group_fallback_active: groupFallbackActive,
    cross_field_rule: "cooldown_seconds must be <= autoban_window_seconds",
    stats: {
      granted_accounts: 4,
      requested_pending: 1,
      cooldowns_active: 1,
      auto_bans_last_7d: 0,
      accepted_queries_last_24h: 12,
    },
  };
}

function drafts(overrides: Partial<Record<LibrarianTuningKey, string>> = {}) {
  return {
    ...Object.fromEntries(
      Object.entries(values).map(([key, value]) => [key, String(value)])
    ),
    ...overrides,
  } as Record<LibrarianTuningKey, string>;
}

function renderBody({
  response = tuningResponse(),
  currentDrafts = drafts(),
  rowErrors = {},
  loadError = "",
  loading = false,
}: {
  response?: LibrarianTuningResponse | null;
  currentDrafts?: Record<LibrarianTuningKey, string> | null;
  rowErrors?: Partial<Record<LibrarianTuningKey, string>>;
  loadError?: string;
  loading?: boolean;
} = {}) {
  return renderToStaticMarkup(
    <LibrarianTuningPanelBody
      response={response}
      drafts={currentDrafts}
      rowErrors={rowErrors}
      loadError={loadError}
      loading={loading}
      saving={false}
      onDraftChange={vi.fn()}
      onRetry={vi.fn()}
      onSave={vi.fn()}
    />
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("LibrarianTuningPanel", () => {
  it("renders all six human-labelled threshold rows and the stats strip", () => {
    const markup = renderBody();

    for (const label of [
      "Accepted queries per rolling 24 hours",
      "Rejected queries before cooldown",
      "Rejected-query burst window",
      "Cooldown length",
      "Cooldowns before automatic ban",
      "Automatic-ban review window",
    ]) {
      expect(markup).toContain(label);
    }
    expect((markup.match(/type="number"/g) ?? []).length).toBe(6);
    expect(markup).toContain(
      "4 granted accounts · 1 pending request · 1 cooldown active · 0 auto-bans this week · 12 accepted queries in the last 24h"
    );
  });

  it("sends only the dirty row in the PATCH request", async () => {
    const responseBody = tuningResponse();
    const fetchMock = vi
      .fn()
      .mockResolvedValue(Response.json(responseBody, { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const patch = buildLibrarianTuningPatch(
      responseBody,
      drafts({ librarian_daily_query_cap: "5" })
    );

    await patchLibrarianTuning(patch);

    expect(patch).toEqual({ librarian_daily_query_cap: 5 });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/librarian/tuning",
      expect.objectContaining({
        method: "PATCH",
        credentials: "include",
        body: JSON.stringify({ librarian_daily_query_cap: 5 }),
      })
    );
  });

  it("surfaces a rejected value's plain error on the offending row", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json(
          {
            success: false,
            error: "Enter a value no greater than 64.",
            invalid_key: "librarian_reject_burst_threshold",
          },
          { status: 400 }
        )
      )
    );

    let caught: LibrarianTuningRequestError | null = null;
    try {
      await patchLibrarianTuning({
        librarian_reject_burst_threshold: 65,
      });
    } catch (error) {
      caught = error as LibrarianTuningRequestError;
    }

    expect(caught).toBeInstanceOf(LibrarianTuningRequestError);
    expect(caught?.invalidKey).toBe("librarian_reject_burst_threshold");
    const markup = renderBody({
      rowErrors: {
        librarian_reject_burst_threshold: caught?.message ?? "",
      },
    });
    const rowStart = markup.indexOf("Rejected queries before cooldown");
    const error = markup.indexOf("Enter a value no greater than 64.");
    const nextRow = markup.indexOf("Rejected-query burst window");
    expect(rowStart).toBeGreaterThan(-1);
    expect(error).toBeGreaterThan(rowStart);
    expect(error).toBeLessThan(nextRow);
  });

  it("shows an honest fetch failure with a Retry button", () => {
    const markup = renderBody({
      response: null,
      currentDrafts: null,
      loadError: "network failed",
    });

    expect(markup).toContain("Couldn&#x27;t load the current settings.");
    expect(markup).toContain("Retry");
  });

  it("renders the grouped-fallback warning", () => {
    const markup = renderBody({
      response: tuningResponse(true),
    });

    expect(markup).toContain(
      "Some threshold settings had invalid values and were rolled back to defaults"
    );
  });
});
