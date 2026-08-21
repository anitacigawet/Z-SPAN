import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const reactMock = vi.hoisted(() => ({
  getSnapshot: null as null | (() => boolean),
  subscribe: null as null | ((callback: () => void) => () => void),
}));

vi.mock("react", () => ({
  useSyncExternalStore: (
    subscribe: (callback: () => void) => () => void,
    getSnapshot: () => boolean
  ) => {
    reactMock.subscribe = subscribe;
    reactMock.getSnapshot = getSnapshot;
    return getSnapshot();
  },
}));

import {
  filterVisibleEpisodes,
  HIDE_PLACEHOLDERS_STORAGE_KEY,
  useHidePlaceholders,
} from "./useHidePlaceholders";

class MemoryStorage {
  private values = new Map<string, string>();

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }
}

let localStorage: MemoryStorage;
let storageListener: ((event: StorageEvent) => void) | null;

beforeEach(() => {
  localStorage = new MemoryStorage();
  storageListener = null;
  reactMock.getSnapshot = null;
  reactMock.subscribe = null;
  vi.stubGlobal("window", {
    localStorage,
    addEventListener: vi.fn(
      (type: string, listener: (event: StorageEvent) => void) => {
        if (type === "storage") storageListener = listener;
      }
    ),
    removeEventListener: vi.fn(),
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("useHidePlaceholders", () => {
  it("defaults to hiding placeholder cards when no preference is stored", () => {
    const [hidePlaceholders] = useHidePlaceholders();

    expect(hidePlaceholders).toBe(true);
  });

  it("reads an explicit reveal preference from localStorage", () => {
    localStorage.setItem(HIDE_PLACEHOLDERS_STORAGE_KEY, "0");

    const [hidePlaceholders] = useHidePlaceholders();

    expect(hidePlaceholders).toBe(false);
  });

  it("notifies the hook when the preference changes in another tab", () => {
    useHidePlaceholders();
    const notify = vi.fn();
    const unsubscribe = reactMock.subscribe!(notify);
    localStorage.setItem(HIDE_PLACEHOLDERS_STORAGE_KEY, "0");

    storageListener?.({
      key: HIDE_PLACEHOLDERS_STORAGE_KEY,
      newValue: "0",
    } as StorageEvent);

    expect(notify).toHaveBeenCalledOnce();
    expect(reactMock.getSnapshot!()).toBe(false);
    unsubscribe();
  });
});

describe("filterVisibleEpisodes", () => {
  const publishedEpisode = {
    meeting_date: "2026-07-07",
    availability: "published",
  };
  const legacyPublishedEpisode = {
    meeting_date: "2026-07-14",
  };
  const placeholderEpisode = {
    meeting_date: "2026-06-16",
    availability: "coming_soon",
  };

  it("removes placeholders while retaining published and legacy episodes", () => {
    expect(
      filterVisibleEpisodes(
        [publishedEpisode, placeholderEpisode, legacyPublishedEpisode],
        true
      )
    ).toEqual([publishedEpisode, legacyPublishedEpisode]);
  });

  it("returns an empty list when every episode is a hidden placeholder", () => {
    expect(filterVisibleEpisodes([placeholderEpisode], true)).toEqual([]);
  });

  it("removes placeholder-only weeks before calendar grouping", () => {
    const anotherPlaceholderWeek = {
      meeting_date: "2026-06-23",
      availability: "facts_only",
    };

    expect(
      filterVisibleEpisodes(
        [publishedEpisode, placeholderEpisode, anotherPlaceholderWeek],
        true
      ).map(episode => episode.meeting_date)
    ).toEqual(["2026-07-07"]);
  });

  it("preserves placeholders when the preference is disabled", () => {
    const episodes = [publishedEpisode, placeholderEpisode];

    expect(filterVisibleEpisodes(episodes, false)).toBe(episodes);
  });
});
