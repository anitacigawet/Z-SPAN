import { DEPT_RECTS, rectToStyle } from "./BuildingCoords";
import type { ServiceStatus } from "@/utils/hqData";

// Service → dept-window mapping. When a service is down/degraded the
// corresponding dept zone's windows visibly dim — per REPROMPT_01:
// "when an infrastructure service is down, the windows it powers go
// dark / 'under maintenance' on the building." The org's degradation
// reads on the building face instead of in a separate dashboard.
//
// Core services (api, ingestion) trigger whole-building maintenance tint
// via building.overallStatus (handled elsewhere). Non-core services
// dim their specific dept zones here.
const SERVICE_TO_DEPTS: Record<string, (keyof typeof DEPT_RECTS)[]> = {
 api: [],
 ingestion: ["ingestion"],
 worker: ["contentScout", "transcription", "parserCustodian"],
 verification: ["verification"],
};

export default function ServiceWindowDimmer({
 services,
}: {
 services: ServiceStatus[];
}) {
 return (
 <>
 {services.flatMap((s) => {
 if (s.status === "up") return [];
 const deptKeys = SERVICE_TO_DEPTS[s.id] ?? [];
 return deptKeys.map((deptKey) => (
 <div
 key={`${s.id}-${deptKey}`}
 className={`window-dimmer is-${s.status}`}
 style={rectToStyle(DEPT_RECTS[deptKey])}
 aria-label={`${s.label} ${s.status}`}
 />
 ));
 })}
 </>
 );
}
