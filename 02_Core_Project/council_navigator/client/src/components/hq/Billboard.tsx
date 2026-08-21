import { useMemo } from "react";
import type { CSSProperties } from "react";
import type { BillboardSlide } from "@/utils/hqBillboards";
import type { Rect } from "./hqHelpers";

// Opaque LED panel that fully covers the baked-in billboard art on the image.
// Slides duplicate once so the -50% translate loop is seamless.
export default function Billboard({
  rect,
  slides,
  accent = "neon",
  durationSec = 38,
}: {
  rect: Rect;
  slides: BillboardSlide[];
  accent?: "neon" | "amber";
  durationSec?: number;
}) {
  const items = useMemo(() => [...slides, ...slides], [slides]);
  const colorClass = accent === "amber" ? "amber" : "";
  const style = {
    top: `${rect.top}%`,
    left: `${rect.left}%`,
    width: `${rect.width}%`,
    height: `${rect.height}%`,
    "--dur": `${durationSec}s`,
  } as CSSProperties;
  return (
    <div className="ov ticker" style={style} aria-label="Live billboard ticker">
      <div className="ticker-track">
        {items.map((s, i) => (
          <div key={`${s.id}-${i}`} className={`ticker-item ${colorClass}`}>
            <span className="pip" />
            <span>{s.caption}</span>
            <span className="sep">◆</span>
          </div>
        ))}
      </div>
    </div>
  );
}
