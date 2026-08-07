import { useEffect, useState } from "react";

// Shared layout types for the HQ overlay components. The actual coordinate
// values (COORDS / DEPT_ZONES) live in HQPage — these are just the shapes.
export interface Rect {
 top: number;
 left: number;
 width: number;
 height: number;
}

export type Side = "top" | "bottom" | "left" | "right";

export interface DeptZoneSpec extends Rect {
 deptId: string;
 side: Side; // which side of the zone the hover callout opens toward
 grid: [number, number]; // [cols, rows] for the live-state window grid
}

export function relativeFromNow(iso: string | null): string {
 if (!iso) return "—";
 const d = (Date.now() - new Date(iso).getTime()) / 1000;
 if (d < 60) return `${Math.round(d)}s ago`;
 if (d < 3600) return `${Math.round(d / 60)}m ago`;
 if (d < 86400) return `${Math.round(d / 3600)}h ago`;
 return `${Math.round(d / 86400)}d ago`;
}

export function useClock(): string {
 const [t, setT] = useState<Date>(() => new Date());
 useEffect(() => {
 const i = setInterval(() => setT(new Date()), 1000);
 return () => clearInterval(i);
 }, []);
 const hh = String(t.getHours()).padStart(2, "0");
 const mm = String(t.getMinutes()).padStart(2, "0");
 const ss = String(t.getSeconds()).padStart(2, "0");
 return `${hh}:${mm}:${ss} LOCAL`;
}

// Deterministic FNV-1a hash so each department's window pattern is stable
// across renders (rather than reshuffling on every paint).
export function hashStr(s: string): number {
 let h = 2166136261;
 for (let i = 0; i < s.length; i++) {
 h ^= s.charCodeAt(i);
 h = (h * 16777619) >>> 0;
 }
 return h >>> 0;
}
