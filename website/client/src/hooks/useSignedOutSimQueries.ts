import { useCallback, useEffect, useState, useSyncExternalStore } from "react";

import { useCurrentUser } from "./useCurrentUser";
import { isPublicPlane } from "../lib/trustPlane";

export interface PublicSimQuery {
 question: string;
 answer: string;
 generated_at: string;
 model_id: string;
}

type ReadyResult = Readonly<{
 kind: "ready";
 publicId: string;
 simQueries: readonly [PublicSimQuery, PublicSimQuery, PublicSimQuery];
}>;

type EmptyResult = Readonly<{
 kind: "not_generated";
 publicId: string;
 simQueries: readonly [];
}>;

type SettledResult = ReadyResult | EmptyResult;

export type SignedOutSimQueryState =
 | Readonly<{ kind: "hidden"; simQueries: readonly [] }>
 | Readonly<{ kind: "loading"; simQueries: readonly [] }>
 | Readonly<{ kind: "unavailable"; simQueries: readonly [] }>
 | SettledResult;

type InflightEntry = {
 controller: AbortController;
 consumers: number;
 promise: Promise<SettledResult>;
};

// Dedupe only concurrent desktop/mobile consumers. Settled responses are not
// cached: a later SPA remount must observe a late local-generate/sync or a
// successful --force replacement.
const inflightByPublicId = new Map<string, InflightEntry>();

type SelectionEntry = {
 selectedIndex: number | null;
 listeners: Set<() => void>;
};

// Both responsive bodies remain mounted at once. Keep their picker/answer
// state synchronized while at least one consumer exists, then discard it so
// meeting/auth transitions and later remounts always start at the picker.
const selectionByPublicId = new Map<string, SelectionEntry>();

function selectionEntry(publicId: string): SelectionEntry {
 let entry = selectionByPublicId.get(publicId);
 if (!entry) {
 entry = { selectedIndex: null, listeners: new Set() };
 selectionByPublicId.set(publicId, entry);
 }
 return entry;
}

function useSharedSelection(publicId: string | undefined, enabled: boolean) {
 const selectionKey = enabled ? publicId : undefined;
 const subscribe = useCallback(
 (listener: () => void) => {
 if (!selectionKey) return () => {};
 const entry = selectionEntry(selectionKey);
 entry.listeners.add(listener);
 return () => {
 entry.listeners.delete(listener);
 if (
 entry.listeners.size === 0 &&
 selectionByPublicId.get(selectionKey) === entry
 ) {
 selectionByPublicId.delete(selectionKey);
 }
 };
 },
 [selectionKey]
 );
 const getSnapshot = useCallback(
 () =>
 selectionKey
 ? (selectionByPublicId.get(selectionKey)?.selectedIndex ?? null)
 : null,
 [selectionKey]
 );
 const selectedIndex = useSyncExternalStore(
 subscribe,
 getSnapshot,
 getSnapshot
 );
 const setSelectedIndex = useCallback(
 (nextIndex: number | null) => {
 if (!selectionKey) return;
 const entry = selectionEntry(selectionKey);
 if (entry.selectedIndex === nextIndex) return;
 entry.selectedIndex = nextIndex;
 entry.listeners.forEach(listener => listener());
 },
 [selectionKey]
 );
 return { selectedIndex, setSelectedIndex } as const;
}

function isExactSimQuery(value: unknown): value is PublicSimQuery {
 if (!value || typeof value !== "object" || Array.isArray(value)) return false;
 const record = value as Record<string, unknown>;
 const keys = Object.keys(record).sort();
 if (
 keys.length !== 4 ||
 keys[0] !== "answer" ||
 keys[1] !== "generated_at" ||
 keys[2] !== "model_id" ||
 keys[3] !== "question"
 ) {
 return false;
 }
 return (
 typeof record.question === "string" &&
 record.question.trim().length > 0 &&
 typeof record.answer === "string" &&
 record.answer.trim().length > 0 &&
 typeof record.generated_at === "string" &&
 record.generated_at.trim().length > 0 &&
 typeof record.model_id === "string" &&
 record.model_id.trim().length > 0
 );
}

/** Fail closed unless the public endpoint returns its exact fixed DTO. */
export function parseSimQueryResponse(value: unknown): SettledResult | null {
 if (!value || typeof value !== "object" || Array.isArray(value)) return null;
 const record = value as Record<string, unknown>;
 const keys = Object.keys(record).sort();
 if (
 keys.length !== 3 ||
 keys[0] !== "public_id" ||
 keys[1] !== "sim_queries" ||
 keys[2] !== "status" ||
 typeof record.public_id !== "string" ||
 record.public_id.trim().length === 0 ||
 !Array.isArray(record.sim_queries)
 ) {
 return null;
 }

 if (record.status === "not_generated" && record.sim_queries.length === 0) {
 return {
 kind: "not_generated",
 publicId: record.public_id,
 simQueries: [],
 };
 }

 if (
 record.status !== "ready" ||
 record.sim_queries.length !== 3 ||
 !record.sim_queries.every(isExactSimQuery)
 ) {
 return null;
 }

 const [first, second, third] = record.sim_queries;
 return {
 kind: "ready",
 publicId: record.public_id,
 simQueries: [first, second, third],
 };
}

async function fetchSimQueries(
 publicId: string,
 signal: AbortSignal
): Promise<SettledResult> {
 const response = await fetch(
 `/public-api/broadcasts/${encodeURIComponent(publicId)}/sim-queries`,
 {
 method: "GET",
 credentials: "omit",
 signal,
 }
 );
 if (!response.ok) {
 throw new Error(`sim queries unavailable (${response.status})`);
 }
 const parsed = parseSimQueryResponse(await response.json());
 if (!parsed) throw new Error("sim queries returned an invalid public DTO");
 if (parsed.publicId !== publicId) {
 throw new Error("sim queries returned a different canonical public id");
 }
 return parsed;
}

function acquireRequest(publicId: string): {
 promise: Promise<SettledResult>;
 release: () => void;
} {
 let entry = inflightByPublicId.get(publicId);
 if (!entry) {
 const controller = new AbortController();
 entry = {
 controller,
 consumers: 0,
 promise: fetchSimQueries(publicId, controller.signal),
 };
 const ownedEntry = entry;
 void entry.promise
 .catch(() => undefined)
 .finally(() => {
 if (inflightByPublicId.get(publicId) === ownedEntry) {
 inflightByPublicId.delete(publicId);
 }
 });
 inflightByPublicId.set(publicId, entry);
 }

 entry.consumers += 1;
 let released = false;
 return {
 promise: entry.promise,
 release: () => {
 if (released) return;
 released = true;
 entry!.consumers -= 1;
 if (
 entry!.consumers === 0 &&
 inflightByPublicId.get(publicId) === entry
 ) {
 inflightByPublicId.delete(publicId);
 entry!.controller.abort();
 }
 },
 };
}

export function useSignedOutSimQueries(publicId: string | undefined) {
 const currentUser = useCurrentUser();
 const publicPlane = isPublicPlane();
 const enabled = Boolean(
 publicPlane && publicId && !currentUser.loading && currentUser.user === null
 );
 const [state, setState] = useState<SignedOutSimQueryState>({
 kind: "hidden",
 simQueries: [],
 });
 const [requestIdentity, setRequestIdentity] = useState<string | null>(null);
 const selection = useSharedSelection(publicId, enabled);

 useEffect(() => {
 if (!enabled || !publicId) {
 setRequestIdentity(null);
 setState({ kind: "hidden", simQueries: [] });
 return;
 }

 let active = true;
 setRequestIdentity(publicId);
 setState({ kind: "loading", simQueries: [] });
 const request = acquireRequest(publicId);
 request.promise.then(
 result => {
 if (active) setState(result);
 },
 () => {
 if (active) setState({ kind: "unavailable", simQueries: [] });
 }
 );

 return () => {
 active = false;
 request.release();
 };
 }, [enabled, publicId]);

 const visibleState: SignedOutSimQueryState = !enabled
 ? { kind: "hidden", simQueries: [] }
 : requestIdentity === publicId
 ? state
 : { kind: "loading", simQueries: [] };
 return { state: visibleState, enabled, ...selection } as const;
}

/** Test-only reset for module-level request deduplication state. */
export function resetSignedOutSimQueryCacheForTests(): void {
 inflightByPublicId.forEach(entry => entry.controller.abort());
 inflightByPublicId.clear();
 selectionByPublicId.clear();
}
