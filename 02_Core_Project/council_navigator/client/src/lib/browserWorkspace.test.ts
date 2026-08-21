import { afterEach, describe, expect, it, vi } from "vitest";

import {
  listBrowserWorkspaceEntries,
  saveBrowserWorkspaceEntry,
} from "./browserWorkspace";

describe("browser workspace storage", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("fails honestly when device-local browser storage is unavailable", async () => {
    vi.stubGlobal("indexedDB", undefined);

    await expect(listBrowserWorkspaceEntries(42)).rejects.toThrow(
      "Local browser storage is unavailable.",
    );
    await expect(saveBrowserWorkspaceEntry({
      id: "run-1",
      userId: 42,
      meetingId: 100,
      query: "What happened?",
      answer: "A cited answer.",
      provider: "google-gemini-2.5-flash",
      model: "gemini-2.5-flash",
      inputTokens: 10,
      outputTokens: 20,
      costUsd: 0,
      runId: "run-1",
      createdAt: "2026-08-14T12:00:00.000Z",
    })).rejects.toThrow("Local browser storage is unavailable.");
  });
});
