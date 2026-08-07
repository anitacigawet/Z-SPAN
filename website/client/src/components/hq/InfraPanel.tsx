import { useState } from "react";
import type { ServiceStatus } from "@/utils/hqData";
import type { Rect } from "./hqHelpers";

// Generator infrastructure annotation. Replaces the prior floating
// description card (per James 2026-06-02). The five LEDs sit baked onto
// the V1 photo's housing block — always visible, status-colored. Hovering
// the generator outbuilding slides a speech-bubble annotation into the
// sky above, with a dashed connector line tying it back to the LEDs.
//
// LED coords are hand-placed against the V1 painted photo's housing top
// face — the horizontal band just below the roof line, above the garage
// door, where the V1 art already paints a small green/red indicator.
// The 5 LEDs sit on this same horizontal line so they read as integrated
// status hardware on the housing.
const LED_POSITIONS = [
 { top: 83.6, left: 86.7 },
 { top: 83.6, left: 89.2 },
 { top: 83.6, left: 91.7 },
 { top: 83.6, left: 94.2 },
 { top: 83.6, left: 96.7 },
];

export default function InfraPanel({
 rect,
 services,
}: {
 rect: Rect;
 services: ServiceStatus[];
}) {
 const [hovered, setHovered] = useState(false);
 if (services.length === 0) return null;

 const hasDown = services.some((s) => s.status === "down");
 const hasDegraded = services.some((s) => s.status === "degraded");
 const titleClass = hasDown ? "warn" : hasDegraded ? "degraded" : "";
 const titleLbl = hasDown ? "Attention" : hasDegraded ? "Degraded" : "Nominal";

 const ledServices = services.slice(0, LED_POSITIONS.length);

 return (
 <>
 {/* Invisible hover-trigger over the generator outbuilding. */}
 <div
 className="infra-trigger"
 style={{
 top: `${rect.top}%`,
 left: `${rect.left}%`,
 width: `${rect.width}%`,
 height: `${rect.height}%`,
 }}
 onMouseEnter={() => setHovered(true)}
 onMouseLeave={() => setHovered(false)}
 aria-label="Infrastructure status"
 />

 {/* Five LEDs baked onto the housing block — always visible. */}
 {ledServices.map((s, i) => (
 <div
 key={s.id}
 className="infra-led-bake"
 data-s={s.status}
 style={{
 top: `${LED_POSITIONS[i].top}%`,
 left: `${LED_POSITIONS[i].left}%`,
 }}
 aria-label={`${s.label} ${s.status}`}
 />
 ))}

 {/* Hover-revealed speech-bubble annotation. */}
 <div
 className={`infra-bubble ${hovered ? "is-on" : ""} ${titleClass}`}
 aria-hidden={!hovered}
 >
 <div className="infra-bubble-title">
 <span>PWR · NET</span>
 <span className="infra-bubble-status">{titleLbl}</span>
 </div>
 {services.map((s) => (
 <div className="infra-bubble-row" key={s.id}>
 <span className="infra-bubble-led" data-s={s.status} />
 <span className="infra-bubble-lbl">{s.label}</span>
 </div>
 ))}
 </div>

 {/* Dashed connector line from bubble to LEDs. */}
 <div className={`infra-connector ${hovered ? "is-on" : ""}`} aria-hidden>
 <svg viewBox="0 0 10 100" preserveAspectRatio="none">
 <line
 x1="2"
 y1="0"
 x2="9"
 y2="100"
 stroke="rgba(81, 209, 246, 0.55)"
 strokeWidth="1.2"
 strokeDasharray="3 3"
 vectorEffect="non-scaling-stroke"
 />
 </svg>
 </div>
 </>
 );
}
