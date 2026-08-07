// HQ lobby data seam.
//
// The HQ page and its overlays fetch GET /api/hq/status through this module.
// If the request fails or the payload is invalid, components render honest
// empty states; there is no bundled mock data.

import { useEffect, useState } from "react";

export type DepartmentKind = "agent" | "pipeline";
export type DepartmentState = "running" | "idle" | "escalated" | "offline";
export type ServiceState = "up" | "down" | "degraded";
export type BuildingStatus = "operational" | "degraded" | "maintenance";
export type AgentTaskStatus =
 | "in-progress"
 | "escalated"
 | "awaiting-review"
 | "queued"
 | "done";

// One independent agent run inside a department — rendered CLI-style in the
// hover callout (status glyph + a one-line human objective). This is the
// "see the active heads at work" surface/.
export interface AgentTask {
 id: string;
 model: string; // the engine running this worker — honest, not a fake handle: Codex / your API key / Whisper / Parsers
 status: AgentTaskStatus;
 objective: string; // brief one-line objective (the default roster line)
 detail: string; // in-depth view surfaced when the worker line is hovered
}

export interface Department {
 id: string; // stable key, never rendered
 name: string; // human label rendered in callouts
 short: string; // compact tag rendered on the building facade
 kind: DepartmentKind;
 state: DepartmentState;
 currentObjective: string | null; // human sentence when running/escalated
 recentSummary: string | null; // human sentence for idle/offline
 activeAgentCount: number;
 lastActiveAt: string | null; // ISO
 escalationCount: number;
 // Individual agent runs currently on this department's floor. Empty when the
 // department is idle/offline (we show its last accomplishment instead — idle
 // honesty). Excludes finished runs since the floor is a real-time view.
 agents: AgentTask[];
}

export interface ServiceStatus {
 id: string;
 label: string;
 status: ServiceState;
 isCore: boolean; // a core service down → whole-building maintenance tint
}

export interface InfrastructureStatus {
 services: ServiceStatus[];
}

export interface FundingStatus {
 balanceUsd: number;
 monthlyBurnUsd: number;
 runwayMonths: number;
 lastUpdated: string | null; // ISO (null when unconfigured or owner-only)
 source: string;
 restricted?: boolean; // true → balance/burn/runway are owner-only; render "—"
}

export interface HQData {
 building: { overallStatus: BuildingStatus };
 departments: Department[];
 infrastructure: InfrastructureStatus;
 funding: FundingStatus;
}

const EMPTY_HQ_DATA: HQData = {
 building: { overallStatus: "operational" },
 departments: [],
 infrastructure: { services: [] },
 funding: {
 balanceUsd: 0,
 monthlyBurnUsd: 0,
 runwayMonths: 0,
 lastUpdated: null,
 source: "unavailable",
 },
};

// Normalize the server payload to the HQData shape the frontend renders. The
// /api/hq/status response is a superset (carries `orchestrator`, `governor`,
// `badges`, `parsers`, `escalations`) — the existing components do not consume
// those fields, so this seam keeps them off the strict HQData type.
function normalizeHQResponse(raw: unknown): HQData | null {
 if (!raw || typeof raw !== "object") return null;
 const r = raw as Record<string, unknown>;
 const building = r.building as { overallStatus?: BuildingStatus } | undefined;
 const departments = Array.isArray(r.departments)
 ? (r.departments as Department[])
 : null;
 const infrastructure = r.infrastructure as InfrastructureStatus | undefined;
 const funding = r.funding as FundingStatus | undefined;
 if (!building || !departments || !infrastructure || !funding) {
 return null;
 }
 return {
 building: { overallStatus: building.overallStatus || "operational" },
 departments,
 infrastructure,
 funding,
 };
}

export interface HQDataState {
 data: HQData;
 isLoading: boolean;
 isFallback: boolean;
 error: string | null;
}

export function useHQDataState(): HQDataState {
 const [data, setData] = useState<HQData>(EMPTY_HQ_DATA);
 const [isLoading, setLoading] = useState(true);
 const [isFallback, setIsFallback] = useState(true);
 const [error, setError] = useState<string | null>(null);

 useEffect(() => {
 let cancelled = false;
 setLoading(true);
 fetch("/api/hq/status")
 .then((r) =>
 r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)),
 )
 .then((payload) => {
 if (cancelled) return;
 const normalized = normalizeHQResponse(payload);
 if (normalized) {
 setData(normalized);
 setIsFallback(false);
 setError(null);
 } else {
 setError("Unexpected payload shape from /api/hq/status");
 }
 })
 .catch((e: Error) => {
 if (cancelled) return;
 setError(e.message || "Could not load HQ status");
 })
 .finally(() => {
 if (!cancelled) setLoading(false);
 });
 return () => {
 cancelled = true;
 };
 }, []);

 return { data, isLoading, isFallback, error };
}
