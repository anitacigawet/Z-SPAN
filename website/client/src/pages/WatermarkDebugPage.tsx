/**
 *visual showcase page — `?view=watermark-debug`.
 *
 * Renders three real (or sample) Z-SPAN outputs with their provenance
 * ribbons + a single-line verdict beside each. Mobile-first; no
 * developer schema-dump or CLI instructions on the surface (those
 * stayed only in source comments + dev docs).
 */
import { useEffect, useState } from "react";
import { WatermarkRibbon } from "../components/WatermarkRibbon";

type Sample = {
 label: string;
 meeting_id: number;
 output_type: string;
 text: string;
};

const SAMPLES: Sample[] = [
 {
 label: "Bullhead Synopsis",
 meeting_id: 103225,
 output_type: "synopsis",
 text:
 "The Bullhead City Council convened on May 19, 2026, working through resolutions on city-center improvements, water infrastructure, and Desert Rose / Chaparral Terrace development. Public comment ran nearly forty minutes, with eleven residents speaking on related items.",
 },
 {
 label: "Bullhead Key Decision",
 meeting_id: 103225,
 output_type: "key_decisions",
 text:
 "The Council voted six in favor to approve resolution 2026R-16 authorizing the city manager to execute the contract for $262,611.31 with City Center contingent on completion of the funding mechanism.",
 },
 {
 label: "Community Call to Action",
 meeting_id: 104714,
 output_type: "community_calls_to_action",
 text:
 "We need every resident who cares about the future of Bullhead City to show up at the public hearing on Tuesday at 6 PM — the rezoning decision will be made that night.",
 },
];

type LookupResult = {
 exists: boolean;
 meeting_title?: string;
 city_name?: string;
 note?: string;
};

type SampleRow = Sample & {
 token: string | null;
 registrationState: "registered" | "pending" | null;
 lookup: LookupResult | null;
};

export default function WatermarkDebugPage() {
 const [rows, setRows] = useState<SampleRow[]>(
 SAMPLES.map((s) => ({
 ...s,
 token: null,
 registrationState: null,
 lookup: null,
 })),
 );

 useEffect(() => {
 let cancelled = false;
 (async () => {
 const resolved = await Promise.all(
 SAMPLES.map(async (s) => {
 let token: string | null = null;
 let registrationState: "registered" | "pending" | null = null;
 let lookup: LookupResult | null = null;
 try {
 const notebookResp = await fetch(`/api/notebook/${s.meeting_id}`);
 const notebook = await notebookResp.json();
 const output = notebook?.outputs?.[s.output_type];
 token = output?.ribbon_token || null;
 registrationState = output?.registration_state || null;
 if (token) {
 const resp = await fetch(`/api/watermark-lookup/${token}`);
 lookup = await resp.json();
 }
 } catch (err) {
 lookup = { exists: false, note: String(err) };
 }
 return { ...s, token, registrationState, lookup };
 }),
 );
 if (!cancelled) setRows(resolved);
 })();
 return () => { cancelled = true; };
 }, []);

 return (
 <div className="min-h-screen bg-[#0A0A0A] text-white">
 <div className="max-w-md mx-auto px-5 py-6">
 <div className="text-[10px] font-semibold uppercase tracking-[0.18em] text-[#6B6B72] mb-2">
 Z-SPAN · Provenance Samples
 </div>
 <p className="text-[13px] text-gray-400 leading-relaxed mb-6">
 Each ribbon below is unique to that output. Tap one to verify it.
 </p>

 <div className="space-y-7">
 {rows.map((row, i) => (
 <section key={i}>
 <div className="flex items-center justify-between gap-2 mb-2">
 <div className="text-[11px] font-semibold uppercase tracking-[0.18em] text-gray-500">
 {row.label}
 </div>
 <WatermarkRibbon
 meetingId={row.meeting_id}
 outputType={row.output_type}
 ribbonToken={row.token}
 registrationState={row.registrationState}
 />
 </div>
 <p className="text-[14px] leading-relaxed text-gray-200 mb-2">
 {row.text}
 </p>
 {row.lookup?.exists && (
 <div
 className="text-[12px] border-l-2 pl-3 py-1 text-gray-300"
 style={{
 borderColor: "var(--success-green)",
 background: "rgba(34,197,94,0.05)",
 }}
 >
 ✅ <span className="text-white">{row.lookup.city_name}</span>
 {row.lookup.meeting_title && (
 <span className="text-gray-400"> · {row.lookup.meeting_title}</span>
 )}
 </div>
 )}
 </section>
 ))}
 </div>
 </div>
 </div>
 );
}
