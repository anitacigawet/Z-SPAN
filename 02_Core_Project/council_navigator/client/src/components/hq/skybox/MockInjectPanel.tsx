import { useCallback, useState } from "react";

/**
 * Owner-only mock-traffic panel — operator-terminal aesthetic (V2).
 *
 * Reskinned 2026-05-29 to match the HQ's monospace / neon-on-dark palette
 * rather than vanilla pill buttons. Tight 2px corners, mono labels,
 * terminal-style `›` button prefixes, green PIP that lights briefly on
 * fire, running "SENT" counter at the bottom. Same behavior as V1:
 * POSTs to /api/hq/traffic-events/inject, which broadcasts mock events
 * through the SSE bus → StarField spawns a star (white for normal, red
 * for bad/bought, with red bouncing off the right wall).
 *
 * Wrapped in <OwnerOnly> by the caller — this component itself just
 * renders the panel.
 */
type InjectKind = "normal" | "bad";

type EventShape = {
  status: number;
  path_class: "broadcast" | "guide" | "api" | "static" | "admin" | "other";
  bot_classification: "human" | "verified_bot" | "likely_bot" | "unknown";
};

function makeEvent(kind: InjectKind): EventShape {
  if (kind === "normal") {
    return {
      status: 200,
      path_class: "broadcast",
      bot_classification: "human",
    };
  }
  // "bad" cycles randomly so the red rule exercises both branches:
  // the bot-flagged branch AND the 4xx branch.
  if (Math.random() < 0.5) {
    return {
      status: 200,
      path_class: "broadcast",
      bot_classification: "likely_bot",
    };
  }
  return {
    status: 403,
    path_class: "api",
    bot_classification: "unknown",
  };
}

async function injectBatch(kind: InjectKind, count: number): Promise<number> {
  const event = makeEvent(kind);
  try {
    const res = await fetch("/api/hq/traffic-events/inject", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ events: [event], count }),
    });
    if (!res.ok) {
      // eslint-disable-next-line no-console
      console.error("[MockInjectPanel] inject failed:", res.status);
      return 0;
    }
    const body = (await res.json()) as { injected?: number };
    return typeof body.injected === "number" ? body.injected : 0;
  } catch (err) {
    // eslint-disable-next-line no-console
    console.error("[MockInjectPanel] inject error:", err);
    return 0;
  }
}

/**
 * Spread injection over time so a high-volume burst (Storm · 100) flows
 * across the sky as a meteor-shower stream instead of clustering all
 * at one X position on the left edge. Chunks of CHUNK_SIZE go out at
 * regular intervals over spreadMs total.
 */
async function injectSpread(
  kind: InjectKind,
  count: number,
  spreadMs: number,
): Promise<number> {
  const CHUNK_SIZE = 5;
  const numChunks = Math.ceil(count / CHUNK_SIZE);
  const interval = spreadMs / numChunks;
  let total = 0;
  const pending: Array<Promise<void>> = [];

  for (let i = 0; i < numChunks; i++) {
    const thisCount = Math.min(CHUNK_SIZE, count - i * CHUNK_SIZE);
    // Fire chunk WITHOUT awaiting — network calls overlap; the spacing
    // is enforced by the setTimeout below, not by fetch latency.
    pending.push(
      injectBatch(kind, thisCount).then((n) => {
        total += n;
      }),
    );
    if (i < numChunks - 1) {
      await new Promise<void>((r) => window.setTimeout(r, interval));
    }
  }
  // Settle the in-flight chunks so the final count is accurate.
  await Promise.all(pending);
  return total;
}

async function inject(
  kind: InjectKind,
  count: number,
  spreadMs?: number,
): Promise<number> {
  if (spreadMs && count > 10) {
    return injectSpread(kind, count, spreadMs);
  }
  return injectBatch(kind, count);
}

export default function MockInjectPanel() {
  const [busy, setBusy] = useState<string | null>(null);
  const [totalSent, setTotalSent] = useState(0);
  const [pipLit, setPipLit] = useState(false);

  const fire = useCallback(
    async (
      label: string,
      kind: InjectKind,
      count: number,
      spreadMs?: number,
    ) => {
      setBusy(label);
      try {
        const injected = await inject(kind, count, spreadMs);
        if (injected > 0) {
          setTotalSent((prev) => prev + injected);
          setPipLit(true);
          // Decay the pip back to dark — the briefness IS the signal.
          window.setTimeout(() => setPipLit(false), 600);
        }
      } finally {
        setBusy(null);
      }
    },
    [],
  );

  const disabled = busy !== null;

  return (
    <div
      className="mock-inject-panel"
      aria-label="Mock traffic injection (owner only)"
    >
      <div className="mock-inject-head">
        <span>Mock Inject</span>
        <span
          className={`mock-inject-pip ${pipLit ? "lit" : ""}`}
          aria-hidden="true"
        />
      </div>

      <div className="mock-inject-row">
        <button
          type="button"
          data-kind="normal"
          onClick={() => fire("v1", "normal", 1)}
          disabled={disabled}
        >
          One visitor
        </button>
        <button
          type="button"
          data-kind="normal"
          onClick={() => fire("vN", "normal", 10)}
          disabled={disabled}
        >
          Burst &middot; 10
        </button>
      </div>

      <div className="mock-inject-row">
        <button
          type="button"
          data-kind="bad"
          onClick={() => fire("b1", "bad", 1)}
          disabled={disabled}
        >
          One bot
        </button>
        <button
          type="button"
          data-kind="bad"
          onClick={() => fire("bN", "bad", 10)}
          disabled={disabled}
        >
          Wave &middot; 10
        </button>
      </div>

      <div className="mock-inject-row">
        <button
          type="button"
          data-kind="normal"
          onClick={() => fire("storm", "normal", 100, 3000)}
          disabled={disabled}
        >
          Storm &middot; 100
        </button>
      </div>

      <div className="mock-inject-foot">
        <span>Sent</span>
        <span className="v">{totalSent.toLocaleString()}</span>
      </div>
    </div>
  );
}
