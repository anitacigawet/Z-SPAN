import { VARIANTS } from "./StarField";

/**
 * Owner-only dev panel for A/B-testing the StarField fiber-optic variants.
 *
 * V4 design exploration (2026-05-31): the 4 variants live in StarField.ts
 * (Pure White Bloom / Cyan Bundle / Rainbow Strand / Cool Plasma). This
 * panel lets James cycle between them live in the HQ to pick the winner
 * before we delete the losers + the panel itself.
 *
 * Aesthetic matches MockInjectPanel — same terminal monospace neon-on-dark
 * palette, 2px corners, `>` button prefixes. Sits bottom-LEFT so it doesn't
 * collide with MockInjectPanel (bottom-right).
 *
 * Wrapped in <OwnerOnly> by the caller — this component renders the panel.
 */
export default function VariantSwitcherPanel({
  variantId,
  onVariantChange,
}: {
  variantId: string;
  onVariantChange: (id: string) => void;
}) {
  return (
    <div
      style={{
        position: "fixed",
        bottom: 16,
        left: 16,
        zIndex: 80,
        padding: "10px 12px",
        background: "rgba(8, 14, 20, 0.92)",
        border: "1px solid rgba(120, 200, 255, 0.35)",
        borderRadius: 2,
        fontFamily:
          "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', monospace",
        fontSize: 11,
        color: "rgba(180, 220, 255, 0.95)",
        boxShadow:
          "0 0 0 1px rgba(0, 0, 0, 0.4), 0 12px 28px rgba(0, 0, 0, 0.55)",
        minWidth: 220,
      }}
    >
      <div
        style={{
          fontSize: 9,
          letterSpacing: "0.12em",
          textTransform: "uppercase",
          opacity: 0.7,
          marginBottom: 8,
        }}
      >
        Fiber-optic variant
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {VARIANTS.map((v) => {
          const active = v.id === variantId;
          return (
            <button
              key={v.id}
              type="button"
              onClick={() => onVariantChange(v.id)}
              style={{
                appearance: "none",
                background: active
                  ? "rgba(120, 200, 255, 0.18)"
                  : "transparent",
                border: active
                  ? "1px solid rgba(120, 200, 255, 0.55)"
                  : "1px solid rgba(120, 200, 255, 0.18)",
                borderRadius: 2,
                color: active ? "rgba(220, 245, 255, 1)" : "rgba(180, 220, 255, 0.85)",
                cursor: "pointer",
                fontFamily: "inherit",
                fontSize: 12,
                padding: "5px 8px",
                textAlign: "left",
                lineHeight: 1.2,
              }}
            >
              <span style={{ opacity: 0.7, marginRight: 6 }}>
                {active ? "›" : " "}
              </span>
              {v.display_name}
            </button>
          );
        })}
      </div>
      <div
        style={{
          marginTop: 8,
          fontSize: 9,
          opacity: 0.55,
          lineHeight: 1.4,
        }}
      >
        Use MockInject (bottom-right) to fire test stars while you A/B.
        Delete this panel + the losing variants once you pick.
      </div>
    </div>
  );
}
