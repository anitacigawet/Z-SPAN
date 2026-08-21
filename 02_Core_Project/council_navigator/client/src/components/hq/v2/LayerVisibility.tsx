import { useCallback, useSyncExternalStore } from "react";

// V2 layer ids, ordered back-to-front per HQ_V2_BACKGROUND_REBUILD_PLAN § 2.
// Each layer's visibility lives in a single localStorage object so toggles
// persist across reloads. V2-5 through V2-14 each consume this via
// useLayerVisibility() — a layer renders `null` when its key is false.
export const LAYER_KEYS = [
  "mountains",
  "building",
  "sign",
  "billboards",
  "ground",
  "generator",
  "press",
  "plaza",
  "fog",
  "thankyou",
  "fireworks",
] as const;

export type LayerKey = (typeof LAYER_KEYS)[number];

const LAYER_LABELS: Record<LayerKey, string> = {
  mountains: "Mountains",
  building: "Building",
  sign: "Z-SPAN sign",
  billboards: "Billboards",
  ground: "Ground floor",
  generator: "Generator",
  press: "Press vignette",
  plaza: "Plaza",
  fog: "Fog band",
  thankyou: "Thank-you sky",
  fireworks: "July-4 fireworks",
};

export type LayerVisibility = Record<LayerKey, boolean>;

const DEFAULT_VISIBILITY: LayerVisibility = LAYER_KEYS.reduce(
  (acc, key) => {
    acc[key] = true;
    return acc;
  },
  {} as LayerVisibility,
);

const STORAGE_KEY = "zspan.hq.v2.layerVisibility";

function readStored(): LayerVisibility {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULT_VISIBILITY;
    const parsed = JSON.parse(raw) as Partial<LayerVisibility>;
    // Merge with defaults so a new layer added in a future chunk defaults to
    // visible without requiring a localStorage migration.
    return { ...DEFAULT_VISIBILITY, ...parsed };
  } catch {
    return DEFAULT_VISIBILITY;
  }
}

// Singleton store — one source of truth for V2 layer visibility, shared
// across every component that calls useLayerVisibility(). Before this
// pattern (V2-5 follow-up fix), each hook instance had its own React state
// and they desynced — the panel's toggle wrote localStorage but the layer
// components kept their stale local copies and didn't re-render. Now all
// subscribers see the same `currentVisibility` reference and `useSync-
// ExternalStore` re-runs every consumer on change.
let currentVisibility: LayerVisibility =
  typeof window === "undefined" ? DEFAULT_VISIBILITY : readStored();

const subscribers = new Set<() => void>();

function subscribe(cb: () => void): () => void {
  subscribers.add(cb);
  return () => {
    subscribers.delete(cb);
  };
}

function getSnapshot(): LayerVisibility {
  return currentVisibility;
}

function getServerSnapshot(): LayerVisibility {
  return DEFAULT_VISIBILITY;
}

function commit(next: LayerVisibility) {
  currentVisibility = next;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  } catch {
    // Quota / private browsing — in-memory state still works for this session.
  }
  subscribers.forEach((cb) => cb());
}

// Hook contract: returns the current visibility map, a per-key toggle, and
// a reset-to-defaults helper. Future V2 layer components import this hook
// to gate their own render (e.g. `if (!visibility.mountains) return null`).
export function useLayerVisibility() {
  const visibility = useSyncExternalStore(
    subscribe,
    getSnapshot,
    getServerSnapshot,
  );

  const toggle = useCallback((key: LayerKey) => {
    commit({ ...currentVisibility, [key]: !currentVisibility[key] });
  }, []);

  const reset = useCallback(() => {
    commit(DEFAULT_VISIBILITY);
  }, []);

  return { visibility, toggle, reset };
}

// The dev-mode panel — 8-checkbox group anchored to the bottom-left of the
// V2 pane. Each row toggles its layer's visibility; the "show all" link
// appears only when at least one layer is hidden, so the chrome stays
// minimal in the common case.
export function LayerVisibilityPanel() {
  const { visibility, toggle, reset } = useLayerVisibility();
  const allVisible = LAYER_KEYS.every((k) => visibility[k]);

  return (
    <div className="hq-v2-layers" role="group" aria-label="V2 layer visibility">
      <div className="hq-v2-layers__head">
        <span className="hq-v2-layers__title">Layers</span>
        {!allVisible && (
          <button
            type="button"
            className="hq-v2-layers__reset"
            onClick={reset}
            aria-label="Show all layers"
          >
            show all
          </button>
        )}
      </div>
      <div className="hq-v2-layers__grid">
        {LAYER_KEYS.map((key) => (
          <label key={key} className="hq-v2-layers__row">
            <input
              type="checkbox"
              checked={visibility[key]}
              onChange={() => toggle(key)}
            />
            <span>{LAYER_LABELS[key]}</span>
          </label>
        ))}
      </div>
      <p className="hq-v2-layers__foot">
        Layers ship V2-5 → V2-14. Toggles persist; new layers default to
        visible.
      </p>
    </div>
  );
}
