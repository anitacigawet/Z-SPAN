import { useMemo, useState } from "react";
import type { CSSProperties } from "react";
import type { AgentTask, AgentTaskStatus, Department } from "@/utils/hqData";
import type { DeptZoneSpec, Side } from "./hqHelpers";
import { hashStr } from "./hqHelpers";

// Short status word shown on the hovered worker's detail panel.
const STATUS_WORD: Record<AgentTaskStatus, string> = {
  "in-progress": "running",
  escalated: "escalated",
  "awaiting-review": "needs review",
  queued: "queued",
  done: "done",
};

// Empty-state office windows: a grid of lit/dim panes shown when a department
// has no agents on the floor (idle/offline). Deterministic per dept id so it
// doesn't reshuffle on every render. Offline panes are darkened via CSS.
function DeptWindowsGrid({
  dept,
  cols,
  rows,
}: {
  dept: Department;
  cols: number;
  rows: number;
}) {
  const cells = useMemo(() => {
    const total = cols * rows;
    const seed = hashStr(dept.id);
    const arr: string[] = [];
    for (let i = 0; i < total; i++) {
      const r = ((seed * (i + 1)) >>> 0) % 100;
      arr.push(dept.state === "idle" && r < 22 ? "lit" : "");
    }
    return arr;
  }, [dept.id, dept.state, cols, rows]);
  const style = { "--cols": cols, "--rows": rows } as CSSProperties;
  return (
    <div className="dept-windows" style={style}>
      {cells.map((cls, i) => (
        <div key={i} className={`w ${cls}`} />
      ))}
    </div>
  );
}

// One worker line in the in-window roster. Hovering (or focusing) it tells the
// parent to surface that worker's in-depth detail panel — it adds detail, it
// never replaces the roster.
function AgentLine({
  agent,
  onHover,
}: {
  agent: AgentTask;
  onHover: (id: string | null) => void;
}) {
  return (
    <li
      className="win-agent"
      data-astatus={agent.status}
      onMouseEnter={() => onHover(agent.id)}
      onMouseLeave={() => onHover(null)}
      onFocus={() => onHover(agent.id)}
      onBlur={() => onHover(null)}
      tabIndex={0}
    >
      <span className="win-agent-glyph" />
      <span className="win-agent-model">{agent.model}</span>
      <span className="win-agent-obj">{agent.objective}</span>
    </li>
  );
}

// The in-depth panel for the hovered worker — opens outward from the window
// (Ctrl+O-style expansion of what that worker is doing).
function AgentDetail({
  agent,
  style,
}: {
  agent: AgentTask;
  style: CSSProperties;
}) {
  return (
    <div className="callout agent-detail" style={style}>
      <div className="head">
        <div className="name">{agent.model}</div>
        <div className="badge" data-state={agent.status}>
          {STATUS_WORD[agent.status]}
        </div>
      </div>
      <div className="obj">{agent.objective}</div>
      <div className="detail">{agent.detail}</div>
    </div>
  );
}

// Where the per-worker detail panel opens relative to the window.
const SIDE_POS: Record<Side, CSSProperties> = {
  right: { left: "calc(100% + 12px)", top: "0%" },
  left: { right: "calc(100% + 12px)", top: "0%" },
  bottom: { top: "calc(100% + 12px)", left: "50%", transform: "translateX(-50%)" },
  top: { bottom: "calc(100% + 12px)", left: "50%", transform: "translateX(-50%)" },
};

export default function DeptZone({
  zone,
  dept,
}: {
  zone: DeptZoneSpec;
  dept: Department | undefined;
}) {
  const [hoveredAgentId, setHoveredAgentId] = useState<string | null>(null);
  if (!dept) return null;
  const [cols, rows] = zone.grid;
  const hasAgents = dept.agents.length > 0;
  const hoveredAgent = hasAgents
    ? (dept.agents.find((a) => a.id === hoveredAgentId) ?? null)
    : null;
  // Every dept keeps a fixed block height so the placeholder pane grid
  // renders behind whatever content is on top — operator flagged 2026-07-04
  // session-33 that "hover the windows" points at nothing on depts that
  // used to hide the grid the moment an agent showed up.
  const boxStyle: CSSProperties = {
    top: `${zone.top}%`,
    left: `${zone.left}%`,
    width: `${zone.width}%`,
    height: `${zone.height}%`,
  };
  return (
    <div
      className={`ov dept${hasAgents ? " is-open" : ""}`}
      data-state={dept.state}
      style={boxStyle}
      aria-label={`${dept.name}, ${dept.state}`}
    >
      <DeptWindowsGrid dept={dept} cols={cols} rows={rows} />
      <div className="dept-tag">
        <span className="led" />
        <span>{dept.short}</span>
        {hasAgents && <span className="dept-count">·{dept.agents.length}</span>}
      </div>
      {hasAgents && (
        <ul className="win-roster">
          {dept.agents.map((a) => (
            <AgentLine key={a.id} agent={a} onHover={setHoveredAgentId} />
          ))}
        </ul>
      )}
      {hoveredAgent && (
        <AgentDetail agent={hoveredAgent} style={SIDE_POS[zone.side]} />
      )}
    </div>
  );
}
