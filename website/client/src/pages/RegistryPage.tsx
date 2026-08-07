/**
 * RR-5 — the public registry policy page +coverage listing.
 *
 * Policy copy is rendered VERBATIM from
 * 01_Project_Overview/REGISTRY_POLICY.md/: this publishes
 * as a SITE page at the flip, not a repo doc) so the operator's tonal
 * review target is exactly the shipped surface. The coverage listing IS
 * the policy's "the map stays visible" commitment made real: every
 * registered city, an honest plain-word status, and content freshness —
 * fed by GET /api/coverage (live DB truth wins over the static index;
 * the flagship city must never read "assessment pending" on its own
 * coverage page).
 */
import { useEffect, useMemo, useState } from "react";
import { fetchForPlane } from "../lib/planeFetch";

// Showcase edition (VITE_ZSPAN_EDITION=showcase): the static bake gates off
// the corrections surface, so the "corrections door" link below (which
// would dead-link) is dropped too. Inline import.meta.env compare so the
// bundler folds + DCEs it. Flagship leaves the var unset → link stays.
const IS_SHOWCASE = import.meta.env.VITE_ZSPAN_EDITION === "showcase";

type CoverageRow = {
 city: string;
 county: string | null;
 state: string;
 status: string;
 published_count: number;
 latest_published_date: string | null;
};

const STATUS_STYLE: Record<string, string> = {
 covered: "border-[var(--success-green)]/40 text-[var(--success-green)]",
 monitored: "border-white/25 text-gray-300",
 "needs repair": "border-[#F5A524]/40 text-[#F5A524]",
 postponed: "border-white/20 text-gray-400",
 "no video source": "border-white/20 text-gray-400",
 "assessment pending": "border-white/10 text-gray-500",
};

const STATE_NAMES: Record<string, string> = {
 AZ: "Arizona",
 NV: "Nevada",
 UT: "Utah",
 VA: "Virginia",
};

export default function RegistryPage() {
 const [rows, setRows] = useState<CoverageRow[] | null>(null);
 const [loadError, setLoadError] = useState(false);

 useEffect(() => {
 let cancelled = false;
 fetchForPlane({
 publicPath: "/public-api/coverage",
 operatorPath: "/api/coverage",
 })
 .then((r) => r.json())
 .then((data) => {
 if (cancelled) return;
 if (data?.success) setRows(data.cities ?? []);
 else setLoadError(true);
 })
 .catch(() => {
 if (!cancelled) setLoadError(true);
 });
 return () => {
 cancelled = true;
 };
 }, []);

 const grouped = useMemo(() => {
 if (!rows) return [];
 const byState = new Map<string, CoverageRow[]>();
 for (const row of rows) {
 const key = row.state || "—";
 if (!byState.has(key)) byState.set(key, []);
 byState.get(key)!.push(row);
 }
 return Array.from(byState.entries());
 }, [rows]);

 const coveredCount = rows?.filter((r) => r.status === "covered").length ?? 0;

 return (
 <div className="min-h-screen bg-[#0A0A0A] text-white">
 <div className="max-w-2xl mx-auto px-5 py-10">
 <div className="text-[10px] font-semibold uppercase tracking-[0.18em] text-[#6B6B72] mb-4">
 Z-SPAN · The Parser Registry
 </div>
 <h1 className="text-[24px] font-semibold leading-tight mb-4">
 The Parser Registry — what's open, what's sealed, and why
 </h1>

 {/* ── Policy copy — verbatim from REGISTRY_POLICY.md ── */}
 <p className="text-[14px] text-gray-300 leading-relaxed mb-4">
 Z-SPAN is open source. The runtime, the pipeline, the vendor
 adapters, the schemas, the verification harness, the user
 interface — the machinery that does the work — is public under
 AGPL-3.0. You can read it, audit it, and fork it.
 </p>
 <p className="text-[14px] text-gray-300 leading-relaxed mb-8">
 One part is different, and this page explains it honestly.
 </p>

 <h2 className="text-[16px] font-semibold mb-3">The sealed registry</h2>
 <p className="text-[14px] text-gray-300 leading-relaxed mb-4">
 Each city Z-SPAN covers has a small "recipe": where that city
 publishes its meeting calendar, which vendor platform it uses,
 and the quirks its pages have. One recipe is mundane.{" "}
 <span className="text-white font-medium">
 Hundreds of recipes together are a turnkey system for
 bulk-collecting municipal data nationwide
 </span>{" "}
 — and handing that out as plaintext would mostly benefit exactly
 the kind of operation this project exists to counterbalance:
 engagement-farms and data brokers who want civic content at
 industrial scale without caring what breaks.
 </p>
 <p className="text-[14px] text-gray-300 leading-relaxed mb-8">
 So the recipes distribute as a{" "}
 <span className="text-white font-medium">sealed registry</span>:
 encrypted bundles, with a public signed manifest anyone can
 check. The manifest shows the registry's size, its change
 history, and the verification record of every repair — you can
 see the registry <em>exists and evolves</em> without the recipes
 themselves being one <code className="text-[12px] bg-white/5 px-1 rounded">git clone</code> away.
 </p>

 <h2 className="text-[16px] font-semibold mb-3">What sealing is not</h2>
 <p className="text-[14px] text-gray-300 leading-relaxed mb-3">
 We want to be precise about the claims, because precision is the
 whole brand:
 </p>
 <ul className="text-[14px] text-gray-300 leading-relaxed mb-4 space-y-3">
 <li>
 <span className="text-white font-medium">It is not secrecy.</span>{" "}
 Every record Z-SPAN publishes links to its public source — the
 city's own recording, the city's own calendar. Anyone can check
 our output against the source for their own city without
 reading a line of parser code. The endpoints aren't hidden; the{" "}
 <em>machinery for harvesting all of them at once</em> is what's
 sealed.
 </li>
 <li>
 <span className="text-white font-medium">
 It is not protection against determined actors.
 </span>{" "}
 A well-resourced adversary can rebuild recipes from scratch
 with modern AI tooling in days. Sealing doesn't stop them, and
 we won't pretend it does. It stops the casual, automated, bulk
 case — which is most of what actually shows up.
 </li>
 <li>
 <span className="text-white font-medium">It is not a business moat.</span>{" "}
 The processed civic record itself is headed for an open license
 (attribution required). We seal the acquisition machinery out
 of responsibility, not to sell it.
 </li>
 </ul>
 <p className="text-[14px] text-gray-300 leading-relaxed mb-8">
 The honest one-line version:{" "}
 <span className="text-white font-medium">
 responsible friction against bulk acquisition, honestly labeled.
 </span>
 </p>

 <h2 className="text-[16px] font-semibold mb-3">
 How the recipes stay maintained — the volunteer channel
 </h2>
 <p className="text-[14px] text-gray-300 leading-relaxed mb-3">
 City websites change; recipes break. Verified volunteers keep
 them repaired, one at a time:
 </p>
 <ol className="text-[14px] text-gray-300 leading-relaxed mb-4 list-decimal list-inside space-y-2">
 <li>
 You enroll (a real conversation with the maintainer — no ID
 documents, just a track record and an ethical-use agreement).
 </li>
 <li>
 You get assigned <span className="text-white font-medium">one</span>{" "}
 broken recipe as a work order, decrypted just for you.
 </li>
 <li>
 You fix it with whatever tools you like — your own AI
 assistant, your own compute, plain hands.
 </li>
 <li>
 You submit the fix with evidence. The project independently
 re-runs everything in an isolated environment before accepting
 — contributor evidence is a claim to test, never a trusted
 attestation.
 </li>
 <li>
 Accepted repairs are re-sealed, credited to you in the public
 manifest, and merged by the maintainer (no contributor has
 merge rights — that's a standing governance rule, not a
 slight).
 </li>
 </ol>
 <p className="text-[14px] text-gray-300 leading-relaxed mb-8">
 If your community isn't covered at all, you don't need the
 registry: the open scaffolding is everything required to build a
 new recipe from scratch and submit it through the same channel.
 </p>

 <h2 className="text-[16px] font-semibold mb-3">
 Coverage stays publicly visible
 </h2>
 <p className="text-[14px] text-gray-300 leading-relaxed mb-4">
 Sealing the machinery does not mean hiding the map. The listing
 below shows every city in the registry with its live status and
 how fresh its data is. If a recipe rots, that's visible to
 everyone, not just to us.{" "}
 <span className="text-gray-500 text-[13px]">
 (The visual coverage map is planned; the commitment holds from
 day one via this listing.)
 </span>
 </p>

 {/* ── The live coverage listing ── */}
 <div className="border border-white/10 rounded-lg p-5 mb-8 bg-[#141416]">
 <div className="flex items-baseline justify-between gap-3 mb-3 flex-wrap">
 <div className="text-[10px] font-semibold uppercase tracking-[0.18em] text-[#6B6B72]">
 Coverage listing
 </div>
 {rows && (
 <div className="text-[11px] text-gray-500">
 {rows.length} cities in the registry · {coveredCount} with
 published broadcasts
 </div>
 )}
 </div>
 <p className="text-[12px] text-gray-500 leading-relaxed mb-4">
 The label on each city tells you where it stands:{" "}
 <span className="text-[var(--success-green)]">covered</span> means
 published broadcasts exist;{" "}
 <span className="text-gray-300">monitored</span> means we're
 tracking its calendar but nothing's published yet;{" "}
 <span className="text-[#F5A524]">needs repair</span> means the
 city changed its site and the recipe is waiting on a fix; and{" "}
 <span className="text-gray-400">postponed</span>,{" "}
 <span className="text-gray-400">no video source</span>, and{" "}
 <span className="text-gray-400">assessment pending</span> are
 honest not-yet states.
 </p>
 {loadError && (
 <p className="text-[13px] text-gray-500">
 The listing couldn't load just now — refresh to retry.
 </p>
 )}
 {!loadError && rows === null && (
 <p className="text-[13px] text-gray-500">Loading…</p>
 )}
 {!loadError &&
 grouped.map(([state, stateRows]) => (
 <div key={state} className="mb-5 last:mb-0">
 <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-gray-400 mb-2">
 {STATE_NAMES[state] ?? state}{" "}
 <span className="text-gray-600 normal-case tracking-normal">
 · {stateRows.length} cities
 </span>
 </div>
 <ul className="space-y-1.5">
 {stateRows.map((row) => (
 <li
 key={`${row.state}-${row.city}`}
 className="flex items-center gap-2 flex-wrap text-[13px]"
 >
 <span
 className={`inline-block border rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide ${
 STATUS_STYLE[row.status] ?? STATUS_STYLE["assessment pending"]
 }`}
 >
 {row.status}
 </span>
 <span className="text-white">{row.city}</span>
 <span className="text-gray-600 text-[11px]">
 {row.county}
 </span>
 {row.published_count > 0 && (
 <span className="text-gray-500 text-[11px]">
 {row.published_count} broadcast
 {row.published_count === 1 ? "" : "s"}
 {row.latest_published_date
 ? ` · latest ${row.latest_published_date}`
 : ""}
 </span>
 )}
 </li>
 ))}
 </ul>
 </div>
 ))}
 </div>

 <h2 className="text-[16px] font-semibold mb-3">
 If the steward disappears
 </h2>
 <p className="text-[14px] text-gray-300 leading-relaxed mb-8">
 The registry keys are held by the project's legal entity, not by
 any individual. If the maintainer can no longer operate the
 project, a successor inherits them. If no successor takes up the
 role within a defined window,{" "}
 <span className="text-white font-medium">
 the registry unseals publicly
 </span>{" "}
 — the archive opens rather than dying locked. The open framework
 and scaffolding mean anyone could also rebuild coverage
 independently at any time; the seal is a distribution choice,
 never a single point of failure for the mission.
 </p>

 <h2 className="text-[16px] font-semibold mb-3">
 The licensing picture, in one breath
 </h2>
 <div className="border border-white/10 rounded-lg overflow-hidden mb-8">
 <table className="w-full text-[13px]">
 <tbody>
 {(
 [
 ["Framework (everything that does work)", "AGPL-3.0 — fully open"],
 ["Jurisdiction recipes (the sealed registry)", "Controlled-access license for verified volunteers"],
 ["The processed civic record (transcripts, summaries)", "Open with attribution (CC-BY 4.0), formalized at public launch"],
 ["The underlying government records", "Public. Always were. Not ours to license."],
 ] as [string, string][]
 ).map(([layer, license]) => (
 <tr key={layer} className="border-b border-white/5 last:border-b-0">
 <td className="px-3 py-2.5 text-gray-300 align-top w-1/2">{layer}</td>
 <td className="px-3 py-2.5 text-gray-400 align-top">{license}</td>
 </tr>
 ))}
 </tbody>
 </table>
 </div>

 {!IS_SHOWCASE && (
 <p className="text-[12px] text-gray-500 leading-relaxed">
 Questions about any of this are welcome — write to us through{" "}
 <a
 href="/?view=corrections"
 className="text-[#F5A524] hover:underline underline-offset-4"
 >
 the corrections door
 </a>{" "}
 or the repo's issue tracker, and you'll get a plain answer from
 the maintainer.
 </p>
 )}
 </div>
 </div>
 );
}
