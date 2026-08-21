import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

class MemoryStorage implements Storage {
  private readonly values = new Map<string, string>();

  get length(): number {
    return this.values.size;
  }

  clear(): void {
    this.values.clear();
  }

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  key(index: number): string | null {
    return [...this.values.keys()][index] ?? null;
  }

  removeItem(key: string): void {
    this.values.delete(key);
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }
}

const STORAGE_KEY = "zspan_byok_v1";

describe("BYOK volatile key custody", () => {
  let storage: MemoryStorage;
  let pagehideListeners: EventListenerOrEventListenerObject[];

  beforeEach(() => {
    vi.resetModules();
    storage = new MemoryStorage();
    pagehideListeners = [];
    vi.stubGlobal("window", {
      localStorage: storage,
      addEventListener: (
        type: string,
        listener: EventListenerOrEventListenerObject,
      ) => {
        if (type === "pagehide") pagehideListeners.push(listener);
      },
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.resetModules();
  });

  it("does not restore a saved key after page unload and module remount", async () => {
    const mounted = await import("./byok");
    mounted.saveByokConfig({
      provider: "google-gemini-2.5-flash",
      key: "AIza-volatile-test-secret",
      fingerprint: "AIza...cret",
      validatedAt: "2026-07-31T12:00:00.000Z",
      modelCount: 3,
    });

    expect(mounted.getByokConfig()?.key).toBe("AIza-volatile-test-secret");
    expect(JSON.parse(storage.getItem(STORAGE_KEY) ?? "{}"))
      .not.toHaveProperty("key");

    for (const listener of pagehideListeners) {
      if (typeof listener === "function") listener(new Event("pagehide"));
      else listener.handleEvent(new Event("pagehide"));
    }
    expect(mounted.getByokConfig()).toBeNull();

    vi.resetModules();
    const remounted = await import("./byok");
    expect(remounted.getByokConfig()).toBeNull();
    expect(remounted.getByokMetadata()).toEqual({
      provider: "google-gemini-2.5-flash",
      fingerprint: "AIza...cret",
      validatedAt: "2026-07-31T12:00:00.000Z",
      modelCount: 3,
    });
  });

  it("removes a legacy persisted key on the first module load", async () => {
    storage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        provider: "openai-gpt-4o-mini",
        key: "sk-legacy-test-secret",
        fingerprint: "sk-l...cret",
        validatedAt: "2026-07-30T12:00:00.000Z",
      }),
    );

    const byok = await import("./byok");
    expect(JSON.parse(storage.getItem(STORAGE_KEY) ?? "{}"))
      .toEqual({
        provider: "openai-gpt-4o-mini",
        fingerprint: "sk-l...cret",
        validatedAt: "2026-07-30T12:00:00.000Z",
      });
    expect(byok.getByokConfig()).toBeNull();
    expect(byok.getByokMetadata()).toEqual({
      provider: "openai-gpt-4o-mini",
      fingerprint: "sk-l...cret",
      validatedAt: "2026-07-30T12:00:00.000Z",
    });
  });

  it("deletes a legacy key even when the surrounding record is malformed", async () => {
    storage.setItem(
      STORAGE_KEY,
      JSON.stringify({ key: "sk-malformed-test-secret", provider: 42 }),
    );

    const byok = await import("./byok");
    expect(byok.getByokMetadata()).toBeNull();
    expect(storage.getItem(STORAGE_KEY)).toBeNull();
  });
});
