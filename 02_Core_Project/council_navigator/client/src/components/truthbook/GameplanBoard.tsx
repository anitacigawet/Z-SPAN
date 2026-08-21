/**
 * The Tracking Board — the Truth Book's lead visualization, v2 (2026-07-03).
 *
 * v1 (same day) organized the record as Madden "situations" by topic;
 * James redirected: the member is a BASEBALL PLAYER CARD — career
 * statistics up top, then the two tracking fields side by side — with
 * the Madden play-diagram grammar kept for HOW the fields draw. His
 * framing: one individual person carrying career statistics like a
 * baseball player — a flake detector built from exactly two tracked
 * dimensions (commitments, and overt on-record positioning/stances —
 * nothing else).
 *
 * The two dimensions, two different ways of checking:
 *   1. COMMITMENTS — tracked through updates until they resolve:
 *      fulfilled or broken (withdrawn/unclear as the honest edges).
 *      The field shows the ACTIVE group — the cluster currently being
 *      tracked — as routes toward the outcome zone. Resolved ones live
 *      in the stat line and the lists below.
 *   2. OVERT STANCES — a position a member explicitly declares, beholden
 *      *until* it isn't: each update is a reaffirmation of the stance or
 *      the divergence that breaks it. Held stances run as continuing
 *      lines with reaffirmation ticks; a divergence bends the line down.
 *      (No stance data exists yet — the extraction pass + table are a
 *      queued build, and the extraction prompt is the operator's to
 *      author per the prompts rule. The field renders its structure +
 *      an honest empty state meanwhile. OVERT-only is what keeps this
 *      D-006-clean: the declaration, reaffirmation, and divergence are
 *      all factual events, never inferred positions.)
 *
 * Neutrality guardrail: the board displays counts and factual outcomes.
 * It never computes an aggregate "flake score" — the reader concludes,
 * the record shows.
 *
 * Data: the existing TruthBookResponse. Clicking a route emits the
 * page's TimelineSelection so the chunk-4 drill-down ("the tape") opens
 * unchanged. No backend change.
 */
import { useMemo } from "react";
import type {
  TruthBookResponse,
  TruthBookQuoteEntry,
  TruthBookClaimEntry,
} from "../../utils/truthBook";
import {
  TRACKED_CLAIM_STATUS_DISPLAY,
  TRACKED_CLAIM_TYPE_DISPLAY,
  formatTimeHorizon,
  type TrackedClaimStatus,
} from "../../utils/trackedClaims";

export type GameplanSelection =
  | { kind: "quotes"; label: string; date: string; items: TruthBookQuoteEntry[] }
  | { kind: "claim"; claim: TruthBookClaimEntry };

function shortDate(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso.length === 10 ? `${iso}T12:00:00` : iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d
    .toLocaleDateString("en-US", { month: "short", day: "numeric" })
    .toUpperCase();
}

/** Short label for a route chip — first few words of the outcome/claim. */
function routeLabel(c: TruthBookClaimEntry, max = 22): string {
  const src = (c.expected_outcome || c.claim_text || "commitment").trim();
  const words = src.replace(/["""'']/g, "").split(/\s+/);
  let out = "";
  for (const w of words) {
    if ((out + " " + w).trim().length > max) break;
    out = (out + " " + w).trim();
  }
  return (out || src.slice(0, max)).toUpperCase();
}

function statusOf(c: TruthBookClaimEntry): TrackedClaimStatus {
  return (c.status ?? "active") as TrackedClaimStatus;
}

/** One big stat: numeral + small-caps label, baseball-card style. */
function Stat({
  n,
  label,
  color,
  dim,
}: {
  n: number;
  label: string;
  color?: string;
  dim?: boolean;
}) {
  return (
    <div className={`px-4 py-2 text-center ${dim ? "opacity-50" : ""}`}>
      <div
        className="font-mono text-2xl font-bold leading-none"
        style={{ color: color ?? "rgba(255,255,255,0.9)" }}
      >
        {n}
      </div>
      <div className="mt-1 text-[9.5px] uppercase tracking-[0.16em] text-white/45">
        {label}
      </div>
    </div>
  );
}

/** Shared field chrome for both tracking fields. */
function FieldChrome({ w, h, endZoneLabel }: { w: number; h: number; endZoneLabel: string }) {
  return (
    <>
      <rect x="0" y="0" width={w} height={h} rx="6" fill="#0C1512" />
      <rect
        x="0.75"
        y="0.75"
        width={w - 1.5}
        height={h - 1.5}
        rx="5.5"
        fill="none"
        stroke="rgba(255,255,255,0.07)"
        strokeWidth="1.5"
      />
      {[0.25, 0.45, 0.65].map(f => (
        <line
          key={f}
          x1="10"
          x2={w - 10}
          y1={h * f}
          y2={h * f}
          stroke="rgba(148, 210, 172, 0.06)"
          strokeWidth="1"
        />
      ))}
      {/* outcome / horizon zone at the top */}
      <line
        x1="10"
        x2={w - 10}
        y1="26"
        y2="26"
        stroke="rgba(148, 210, 172, 0.22)"
        strokeWidth="1"
        strokeDasharray="5 4"
      />
      <text
        x={w / 2}
        y="17"
        textAnchor="middle"
        fill="rgba(255,255,255,0.4)"
        fontSize="8"
        fontFamily="var(--mono, monospace)"
        letterSpacing="0.14em"
      >
        {endZoneLabel}
      </text>
      {/* the said-it baseline */}
      <line
        x1="10"
        x2={w - 10}
        y1={h - 34}
        y2={h - 34}
        stroke="rgba(148, 210, 172, 0.28)"
        strokeWidth="1.5"
      />
    </>
  );
}

const FIELD_W = 430;
const FIELD_H = 240;
const BASE_Y = FIELD_H - 34;

/** The commitments field: the ACTIVE cluster as routes toward the
 *  outcome zone, each clickable to the tape. */
function CommitmentsField({
  active,
  resolvedCount,
  onSelect,
}: {
  active: TruthBookClaimEntry[];
  resolvedCount: number;
  onSelect: (sel: GameplanSelection) => void;
}) {
  const shown = active.slice(0, 6);
  const extra = active.length - shown.length;
  const n = shown.length;
  return (
    <svg
      viewBox={`0 0 ${FIELD_W} ${FIELD_H}`}
      className="block h-auto w-full"
      role="group"
      aria-label={`Commitments being tracked: ${active.length} active`}
    >
      <FieldChrome w={FIELD_W} h={FIELD_H} endZoneLabel="EXPECTED OUTCOMES" />
      {n === 0 ? (
        <>
          <text
            x={FIELD_W / 2}
            y={FIELD_H / 2 - 6}
            textAnchor="middle"
            fill="rgba(255,255,255,0.35)"
            fontSize="11"
            fontStyle="italic"
          >
            Nothing being tracked right now.
          </text>
          {resolvedCount > 0 && (
            <text
              x={FIELD_W / 2}
              y={FIELD_H / 2 + 12}
              textAnchor="middle"
              fill="rgba(255,255,255,0.3)"
              fontSize="9.5"
            >
              {resolvedCount} resolved commitment{resolvedCount === 1 ? "" : "s"} in the
              stat line + lists below.
            </text>
          )}
        </>
      ) : (
        shown.map((c, i) => {
          const x0 = 46 + (i * (FIELD_W - 96)) / Math.max(n - 1, 1);
          // routes converge gently toward the upper zone, fanning by index
          const x1 = 60 + (i * (FIELD_W - 130)) / Math.max(n - 1, 1);
          const midX = (x0 + x1) / 2 + (i % 2 === 0 ? 14 : -14);
          const col = TRACKED_CLAIM_STATUS_DISPLAY.active.fg;
          const horizon =
            c.time_horizon_months != null
              ? formatTimeHorizon(c.time_horizon_months).toUpperCase()
              : null;
          return (
            <g
              key={c.claim_id}
              role="button"
              tabIndex={0}
              aria-label={`Open the tape: ${TRACKED_CLAIM_TYPE_DISPLAY[c.claim_type ?? ""] ?? "commitment"} from ${shortDate(c.meeting_date)}: ${routeLabel(c)}`}
              className="cursor-pointer focus:outline-none"
              onClick={() => onSelect({ kind: "claim", claim: c })}
              onKeyDown={e => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onSelect({ kind: "claim", claim: c });
                }
              }}
            >
              {/* generous invisible hit area */}
              <rect
                x={Math.min(x0, x1) - 18}
                y={30}
                width={Math.abs(x1 - x0) + 36}
                height={BASE_Y - 18}
                fill="transparent"
              />
              <path
                d={`M ${x0} ${BASE_Y} Q ${midX} ${(BASE_Y + 40) / 2} ${x1} 40`}
                fill="none"
                stroke={col}
                strokeWidth="2.2"
                strokeDasharray="7 6"
                strokeLinecap="round"
                opacity="0.92"
              />
              <path
                d={`M ${x1} 40 l -8.5 2.5 l 3 -8`}
                fill="none"
                stroke={col}
                strokeWidth="2.2"
                strokeLinecap="round"
              />
              <circle cx={x0} cy={BASE_Y} r="5.5" fill={col} />
              <circle cx={x0} cy={BASE_Y} r="8.5" fill="none" stroke={col} strokeWidth="1" opacity="0.35" />
              <text
                x={x0}
                y={BASE_Y + 16}
                textAnchor="middle"
                fill="rgba(255,255,255,0.55)"
                fontSize="8"
                fontFamily="var(--mono, monospace)"
                letterSpacing="0.08em"
              >
                {shortDate(c.meeting_date)}
              </text>
              {horizon && (
                <text
                  x={midX}
                  y={(BASE_Y + 40) / 2 - 6}
                  textAnchor="middle"
                  fill="rgba(255,255,255,0.42)"
                  fontSize="7.5"
                  fontFamily="var(--mono, monospace)"
                >
                  {horizon}
                </text>
              )}
              <text
                x={x1}
                y="34"
                textAnchor="middle"
                fill="rgba(255,255,255,0.6)"
                fontSize="7.5"
                fontFamily="var(--mono, monospace)"
                letterSpacing="0.04em"
              >
                {routeLabel(c, 18)}
              </text>
            </g>
          );
        })
      )}
      {extra > 0 && (
        <text
          x={FIELD_W - 14}
          y={BASE_Y - 6}
          textAnchor="end"
          fill="rgba(255,255,255,0.5)"
          fontSize="10"
          fontFamily="var(--mono, monospace)"
        >
          +{extra} more below
        </text>
      )}
    </svg>
  );
}

/** The stances field: overt positions held-until-diverged. No stance
 *  extraction exists yet — the field renders its structure + an honest
 *  empty state so the second dimension is visible and defined. */
function StancesField() {
  const gray = "rgba(255,255,255,0.35)";
  return (
    <svg
      viewBox={`0 0 ${FIELD_W} ${FIELD_H}`}
      className="block h-auto w-full"
      role="group"
      aria-label="Overt stances: none on record yet"
    >
      <FieldChrome w={FIELD_W} h={FIELD_H} endZoneLabel="HELD · UNTIL IT ISN'T" />
      {/* ghost example of the grammar, clearly marked */}
      <g opacity="0.32" aria-hidden>
        <circle cx="60" cy={BASE_Y} r="5" fill={gray} />
        <line x1="60" y1={BASE_Y} x2="300" y2={BASE_Y - 92} stroke={gray} strokeWidth="2" strokeLinecap="round" />
        {[130, 200].map(x => (
          <g key={x}>
            <circle cx={x} cy={BASE_Y - ((x - 60) / 240) * 92} r="4" fill="none" stroke={gray} strokeWidth="1.6" />
            <path
              d={`M ${x - 2.5} ${BASE_Y - ((x - 60) / 240) * 92} l 2 2.5 l 3.5 -5`}
              fill="none"
              stroke={gray}
              strokeWidth="1.4"
            />
          </g>
        ))}
        <path d={`M 300 ${BASE_Y - 92} l -8.5 1 l 3.5 -7.5`} fill="none" stroke={gray} strokeWidth="2" strokeLinecap="round" />
        <text x="188" y={BASE_Y + 16} textAnchor="middle" fill={gray} fontSize="8" fontFamily="var(--mono, monospace)" letterSpacing="0.1em">
          TAKEN → REAFFIRMED ✓ ✓ → STILL HELD
        </text>
      </g>
      <text
        x={FIELD_W / 2}
        y="72"
        textAnchor="middle"
        fill="rgba(255,255,255,0.42)"
        fontSize="10.5"
        fontStyle="italic"
      >
        No overt stances on the record yet.
      </text>
      <text x={FIELD_W / 2} y="88" textAnchor="middle" fill="rgba(255,255,255,0.32)" fontSize="8.5">
        Z-SPAN will track only positions a member explicitly declares —
      </text>
      <text x={FIELD_W / 2} y="100" textAnchor="middle" fill="rgba(255,255,255,0.32)" fontSize="8.5">
        each update either reaffirms the stance or is the divergence that breaks it.
      </text>
    </svg>
  );
}

export default function GameplanBoard({
  data,
  onSelect,
}: {
  data: TruthBookResponse;
  onSelect: (sel: GameplanSelection) => void;
}) {
  const stats = useMemo(() => {
    const by = (s: TrackedClaimStatus) =>
      data.claims.filter(c => statusOf(c) === s);
    const active = by("active");
    return {
      active,
      fulfilled: by("fulfilled").length,
      broken: by("broken").length,
      withdrawn: by("withdrawn").length,
      unclear: by("unclear").length,
      quotes: data.lanes.reduce((s, l) => s + l.entries.length, 0),
    };
  }, [data]);

  const resolvedCount =
    stats.fulfilled + stats.broken + stats.withdrawn + stats.unclear;
  const S = TRACKED_CLAIM_STATUS_DISPLAY;

  return (
    <section
      className="mb-12 overflow-hidden rounded-xl border border-[var(--line)] bg-[#0F1117]"
      aria-label="The tracking board — commitments and overt stances"
    >
      {/* Title band */}
      <div className="border-b border-white/10 bg-gradient-to-b from-[#22262e] via-[#171a20] to-[#101318] px-5 py-3 text-center">
        <h2 className="text-xl font-bold uppercase tracking-[0.22em] text-white/90">
          Tracking Board
        </h2>
        <p className="mt-0.5 text-[11px] text-white/45">
          two ways of checking the record — commitments that resolve, and overt
          stances held until they aren{"'"}t
        </p>
      </div>

      {/* The stat line — baseball-card career numbers. Counts are facts;
       *  Z-SPAN never scores a person. */}
      <div
        className="flex flex-wrap items-stretch justify-center divide-x divide-white/8 border-b border-white/8 bg-[#12151b] py-2"
        role="group"
        aria-label="Career record statistics"
      >
        <Stat n={stats.active.length} label="Tracking" color={S.active.fg} />
        <Stat n={stats.fulfilled} label="Fulfilled" color={S.fulfilled.fg} dim={stats.fulfilled === 0} />
        <Stat n={stats.broken} label="Broken" color={S.broken.fg} dim={stats.broken === 0} />
        {stats.withdrawn > 0 && (
          <Stat n={stats.withdrawn} label="Withdrawn" color={S.withdrawn.fg} />
        )}
        {stats.unclear > 0 && (
          <Stat n={stats.unclear} label="Unclear" color={S.unclear.fg} />
        )}
        <Stat n={0} label="Stances held" dim />
        <Stat n={0} label="Diverged" dim />
        <Stat n={stats.quotes} label="On the record" color="#60A5FA" />
      </div>

      {/* The two tracking fields, side by side */}
      <div className="grid grid-cols-1 gap-4 px-5 py-5 lg:grid-cols-2">
        <div className="rounded-lg border border-white/10 bg-[#141820]">
          <div className="flex items-baseline justify-between border-b border-white/8 px-3 py-2">
            <span className="text-[12px] font-bold uppercase tracking-wide text-white/85">
              Commitments — being tracked
            </span>
            <span
              className="rounded-full border px-2 py-0.5 text-[10px] font-medium"
              style={{ color: S.active.fg, borderColor: S.active.border }}
            >
              {stats.active.length} active
            </span>
          </div>
          <div className="p-2">
            <CommitmentsField
              active={stats.active}
              resolvedCount={resolvedCount}
              onSelect={onSelect}
            />
          </div>
          <div className="px-3 pb-2 text-[10px] text-white/40">
            Each route is one commitment on the record — click it to open the
            tape.
          </div>
        </div>

        <div className="rounded-lg border border-white/10 bg-[#141820]">
          <div className="flex items-baseline justify-between border-b border-white/8 px-3 py-2">
            <span className="text-[12px] font-bold uppercase tracking-wide text-white/85">
              Overt stances — held positions
            </span>
            <span className="rounded-full border border-white/15 px-2 py-0.5 text-[10px] font-medium text-white/45">
              tracking begins soon
            </span>
          </div>
          <div className="p-2">
            <StancesField />
          </div>
          <div className="px-3 pb-2 text-[10px] text-white/40">
            Explicitly declared positions only — never inferred.
          </div>
        </div>
      </div>

      {/* HOW TO READ — the help band */}
      <div className="border-t border-white/10 bg-[#0C0E13] px-5 py-4">
        <div className="mb-2 text-center text-[11px] font-semibold uppercase tracking-[0.2em] text-white/50">
          How to read this board
        </div>
        <div className="grid grid-cols-1 gap-4 text-[11.5px] leading-relaxed text-white/55 md:grid-cols-3">
          <div>
            <div className="mb-1 font-semibold uppercase tracking-wide text-white/75">
              Commitments resolve
            </div>
            A commitment starts on the baseline the day it was said and routes
            toward its expected outcome.{" "}
            <span style={{ color: S.active.fg }}>Dashed amber</span> is being
            tracked; updates land it{" "}
            <span style={{ color: S.fulfilled.fg }}>fulfilled</span> or{" "}
            <span style={{ color: S.broken.fg }}>broken</span> — those move to
            the stat line.
          </div>
          <div>
            <div className="mb-1 font-semibold uppercase tracking-wide text-white/75">
              Stances hold until they don{"'"}t
            </div>
            An overt stance is beholden from the day it{"'"}s declared. Each
            update is a ✓ reaffirmation — or the divergence that{" "}
            <span style={{ color: S.broken.fg }}>breaks</span> it. A stance
            that keeps running is a stance kept.
          </div>
          <div>
            <div className="mb-1 font-semibold uppercase tracking-wide text-white/75">
              Facts, then the tape
            </div>
            Every number is a count and every status a factual outcome —
            Z-SPAN never scores the person. Click anything to open the source:
            the verified quote with synced audio and its citation.
          </div>
        </div>
      </div>
    </section>
  );
}
