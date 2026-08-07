import {
 useCallback,
 useEffect,
 useMemo,
 useState,
 type ChangeEvent,
} from "react";

export type LibrarianTuningKey =
 | "librarian_daily_query_cap"
 | "librarian_reject_burst_threshold"
 | "librarian_reject_burst_window_seconds"
 | "librarian_reject_cooldown_seconds"
 | "librarian_reject_autoban_strike_threshold"
 | "librarian_reject_autoban_window_seconds";

interface TuningSetting {
 value: number;
 default: number;
 min: number;
 max: number | null;
 unit: string;
}

export interface LibrarianTuningResponse {
 settings: Record<LibrarianTuningKey, TuningSetting>;
 group_fallback_active: boolean;
 cross_field_rule: string;
 stats: {
 granted_accounts: number;
 requested_pending: number;
 cooldowns_active: number;
 auto_bans_last_7d: number;
 accepted_queries_last_24h: number;
 };
}

interface TuningRow {
 key: LibrarianTuningKey;
 label: string;
}

export const LIBRARIAN_TUNING_ROWS: readonly TuningRow[] = [
 {
 key: "librarian_daily_query_cap",
 label: "Accepted queries per rolling 24 hours",
 },
 {
 key: "librarian_reject_burst_threshold",
 label: "Rejected queries before cooldown",
 },
 {
 key: "librarian_reject_burst_window_seconds",
 label: "Rejected-query burst window",
 },
 {
 key: "librarian_reject_cooldown_seconds",
 label: "Cooldown length",
 },
 {
 key: "librarian_reject_autoban_strike_threshold",
 label: "Cooldowns before automatic ban",
 },
 {
 key: "librarian_reject_autoban_window_seconds",
 label: "Automatic-ban review window",
 },
];

type Drafts = Record<LibrarianTuningKey, string>;
type RowErrors = Partial<Record<LibrarianTuningKey, string>>;

export class LibrarianTuningRequestError extends Error {
 invalidKey: LibrarianTuningKey | null;

 constructor(message: string, invalidKey: LibrarianTuningKey | null = null) {
 super(message);
 this.name = "LibrarianTuningRequestError";
 this.invalidKey = invalidKey;
 }
}

function isTuningKey(value: unknown): value is LibrarianTuningKey {
 return (
 typeof value === "string" &&
 LIBRARIAN_TUNING_ROWS.some(row => row.key === value)
 );
}

function draftsFromResponse(response: LibrarianTuningResponse): Drafts {
 return Object.fromEntries(
 LIBRARIAN_TUNING_ROWS.map(({ key }) => [
 key,
 String(response.settings[key].value),
 ])
 ) as Drafts;
}

export function buildLibrarianTuningPatch(
 response: LibrarianTuningResponse,
 drafts: Drafts
): Partial<Record<LibrarianTuningKey, number>> {
 return Object.fromEntries(
 LIBRARIAN_TUNING_ROWS.filter(
 ({ key }) => drafts[key] !== String(response.settings[key].value)
 ).map(({ key }) => [key, Number(drafts[key])])
 );
}

async function readTuningResponse(
 response: Response
): Promise<LibrarianTuningResponse> {
 const body = (await response
 .json()
 .catch(() => ({}))) as Partial<LibrarianTuningResponse> & {
 error?: string;
 invalid_key?: unknown;
 };
 if (!response.ok || !body.settings || !body.stats) {
 throw new LibrarianTuningRequestError(
 body.error || "Couldn't load the current settings.",
 isTuningKey(body.invalid_key) ? body.invalid_key : null
 );
 }
 return body as LibrarianTuningResponse;
}

export async function fetchLibrarianTuning(): Promise<LibrarianTuningResponse> {
 const response = await fetch("/api/librarian/tuning", {
 credentials: "include",
 });
 return readTuningResponse(response);
}

export async function patchLibrarianTuning(
 changes: Partial<Record<LibrarianTuningKey, number>>
): Promise<LibrarianTuningResponse> {
 const response = await fetch("/api/librarian/tuning", {
 method: "PATCH",
 credentials: "include",
 headers: { "Content-Type": "application/json" },
 body: JSON.stringify(changes),
 });
 return readTuningResponse(response);
}

function countPhrase(count: number, singular: string, plural = `${singular}s`) {
 return `${count} ${count === 1 ? singular : plural}`;
}

export function LibrarianTuningPanelBody({
 response,
 drafts,
 rowErrors,
 loadError,
 loading,
 saving,
 onDraftChange,
 onRetry,
 onSave,
}: {
 response: LibrarianTuningResponse | null;
 drafts: Drafts | null;
 rowErrors: RowErrors;
 loadError: string;
 loading: boolean;
 saving: boolean;
 onDraftChange: (key: LibrarianTuningKey, value: string) => void;
 onRetry: () => void;
 onSave: () => void;
}) {
 if (loadError && !response) {
 return (
 <div className="flex items-center gap-3 text-[12px] text-red-300/90">
 <span>Couldn't load the current settings.</span>
 <button
 type="button"
 onClick={onRetry}
 className="text-[11px] uppercase tracking-widest text-amber-100/80 hover:text-amber-100 border border-amber-400/30 hover:border-amber-400/60 px-2.5 py-1"
 >
 Retry
 </button>
 </div>
 );
 }

 if (loading || !response || !drafts) {
 return (
 <p className="text-[12px] text-white/45">Loading Librarian controls…</p>
 );
 }

 const stats = response.stats;
 const dirty = LIBRARIAN_TUNING_ROWS.some(
 ({ key }) => drafts[key] !== String(response.settings[key].value)
 );

 return (
 <>
 <p className="overflow-x-auto whitespace-nowrap text-[12px] text-white/55">
 {countPhrase(stats.granted_accounts, "granted account")} ·{" "}
 {countPhrase(stats.requested_pending, "pending request")} ·{" "}
 {countPhrase(
 stats.cooldowns_active,
 "cooldown active",
 "cooldowns active"
 )}{" "}
 · {countPhrase(stats.auto_bans_last_7d, "auto-ban")} this week ·{" "}
 {countPhrase(
 stats.accepted_queries_last_24h,
 "accepted query",
 "accepted queries"
 )}{" "}
 in the last 24h
 </p>

 {response.group_fallback_active && (
 <p className="mt-2 text-[11px] text-amber-200/85">
 ⚠️ Some threshold settings had invalid values and were rolled back to
 defaults — see the current values below.
 </p>
 )}

 {loadError && (
 <p className="mt-2 text-[11px] text-red-300/90">{loadError}</p>
 )}

 <div className="mt-2 overflow-x-auto">
 <table className="w-full text-left text-[11px]">
 <thead className="text-white/40 uppercase tracking-wider">
 <tr className="border-b border-white/10">
 <th className="py-1.5 pr-4 font-medium">Control</th>
 <th className="py-1.5 pr-3 font-medium">Current</th>
 <th className="py-1.5 pr-3 font-medium">Unit</th>
 <th className="py-1.5 pr-3 font-medium">Default</th>
 <th className="py-1.5 pr-3 font-medium">Min</th>
 <th className="py-1.5 font-medium">Max</th>
 </tr>
 </thead>
 <tbody>
 {LIBRARIAN_TUNING_ROWS.map(({ key, label }) => {
 const setting = response.settings[key];
 const errorId = `${key}-error`;
 return (
 <tr key={key} className="border-b border-white/5 align-top">
 <td className="py-2 pr-4 text-white/80">
 <span>{label}</span>
 <code className="ml-2 text-[9px] text-white/30">{key}</code>
 </td>
 <td className="py-1.5 pr-3">
 <input
 type="number"
 value={drafts[key]}
 min={setting.min}
 max={setting.max ?? undefined}
 step={1}
 aria-label={label}
 aria-invalid={Boolean(rowErrors[key])}
 aria-describedby={rowErrors[key] ? errorId : undefined}
 onChange={(event: ChangeEvent<HTMLInputElement>) =>
 onDraftChange(key, event.target.value)
 }
 className="w-24 border border-white/20 bg-black/20 px-2 py-1 text-white/85 outline-none focus:border-amber-300/60"
 />
 {rowErrors[key] && (
 <p
 id={errorId}
 className="mt-1 max-w-56 text-[10px] text-red-300/90"
 >
 {rowErrors[key]}
 </p>
 )}
 </td>
 <td className="py-2 pr-3 text-white/45">{setting.unit}</td>
 <td className="py-2 pr-3 text-white/45">{setting.default}</td>
 <td className="py-2 pr-3 text-white/45">{setting.min}</td>
 <td className="py-2 text-white/45">
 {setting.max ?? "No maximum"}
 </td>
 </tr>
 );
 })}
 </tbody>
 </table>
 </div>

 <div className="mt-2 flex justify-end">
 <button
 type="button"
 onClick={onSave}
 disabled={!dirty || saving}
 className="text-[11px] uppercase tracking-widest text-amber-100/80 hover:text-amber-100 border border-amber-400/30 hover:border-amber-400/60 px-3 py-1.5 disabled:opacity-40 disabled:cursor-not-allowed"
 >
 {saving ? "Saving…" : "Save changes"}
 </button>
 </div>
 </>
 );
}

export default function LibrarianTuningPanel() {
 const [response, setResponse] = useState<LibrarianTuningResponse | null>(
 null
 );
 const [drafts, setDrafts] = useState<Drafts | null>(null);
 const [rowErrors, setRowErrors] = useState<RowErrors>({});
 const [loadError, setLoadError] = useState("");
 const [loading, setLoading] = useState(true);
 const [saving, setSaving] = useState(false);

 const load = useCallback(async () => {
 setLoading(true);
 setLoadError("");
 try {
 const next = await fetchLibrarianTuning();
 setResponse(next);
 setDrafts(draftsFromResponse(next));
 setRowErrors({});
 } catch {
 setResponse(null);
 setDrafts(null);
 setLoadError("Couldn't load the current settings.");
 } finally {
 setLoading(false);
 }
 }, []);

 useEffect(() => {
 void load();
 }, [load]);

 const dirtyPatch = useMemo(
 () =>
 response && drafts ? buildLibrarianTuningPatch(response, drafts) : {},
 [response, drafts]
 );

 const changeDraft = useCallback((key: LibrarianTuningKey, value: string) => {
 setDrafts(current => (current ? { ...current, [key]: value } : current));
 setRowErrors(current => ({ ...current, [key]: undefined }));
 }, []);

 const save = useCallback(async () => {
 if (!response || !drafts || Object.keys(dirtyPatch).length === 0) {
 return;
 }
 setSaving(true);
 setLoadError("");
 try {
 const next = await patchLibrarianTuning(dirtyPatch);
 setResponse(next);
 setDrafts(draftsFromResponse(next));
 setRowErrors({});
 } catch (caught) {
 if (caught instanceof LibrarianTuningRequestError && caught.invalidKey) {
 const invalidKey = caught.invalidKey;
 setDrafts(current =>
 current
 ? {
 ...current,
 [invalidKey]: String(response.settings[invalidKey].value),
 }
 : current
 );
 setRowErrors(current => ({
 ...current,
 [invalidKey]: caught.message,
 }));
 } else {
 setLoadError(
 caught instanceof Error
 ? caught.message
 : "Couldn't save the current settings."
 );
 }
 } finally {
 setSaving(false);
 }
 }, [dirtyPatch, drafts, response]);

 return (
 <section
 className="flex-none px-8 py-3 border-b border-amber-400/20 bg-amber-400/[0.03]"
 aria-label="Librarian tuning"
 >
 <header className="mb-2">
 <span className="text-[10px] uppercase tracking-[0.18em] text-amber-200/80">
 Librarian controls
 </span>
 </header>
 <LibrarianTuningPanelBody
 response={response}
 drafts={drafts}
 rowErrors={rowErrors}
 loadError={loadError}
 loading={loading}
 saving={saving}
 onDraftChange={changeDraft}
 onRetry={() => void load()}
 onSave={() => void save()}
 />
 </section>
 );
}
