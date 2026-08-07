import type { BuildingStatus } from "@/utils/hqData";
import { useClock } from "./hqHelpers";

export default function TopChrome({
 buildingStatus,
}: {
 buildingStatus: BuildingStatus | null;
}) {
 const clock = useClock();
 const statusClass =
 buildingStatus === null
 ? "no-feed"
 : buildingStatus === "operational"
 ? ""
 : buildingStatus === "degraded"
 ? "degraded"
 : "maintenance";
 const label =
 buildingStatus === null
 ? "No feed"
 : buildingStatus === "operational"
 ? "On Air"
 : buildingStatus === "degraded"
 ? "Degraded"
 : "Maintenance";
 return (
 <>
 <div className={`chrome ${statusClass}`}>
 <span className="dot" />
 <span>Z-SPAN</span>
 <span className="sep">/</span>
 <span className="key">HQ</span>
 <span className="sep">·</span>
 <span>{label}</span>
 </div>
 <div className="clock">{clock}</div>
 </>
 );
}
