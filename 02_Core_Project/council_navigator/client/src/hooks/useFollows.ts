// useFollows — light-account follow/subscribe primitive.
//
// Reads + mutates /api/follows. Composes with useCurrentUser:
//   - Anonymous callers get empty state and no-op mutations.
//   - Signed-in callers hydrate from /api/auth/me, then refresh from
//     /api/follows for canonical state.
//   - Mutations update the UI optimistically and run in invocation order.
//
// Per ACCOUNT_SYSTEM_SPEC chunk 3.

import { useCallback, useEffect, useRef, useState } from "react";

import { useCurrentUser, type CurrentUserFollow } from "./useCurrentUser";

export type FollowTargetType = "city" | "county" | "meeting";
export type CityTopicsMap = Record<string, string[]>;

interface FollowsSnapshot {
  follows: CurrentUserFollow[];
  cityTopics: CityTopicsMap;
}

interface FollowsState extends FollowsSnapshot {
  loading: boolean;
}

interface FollowsAPI extends FollowsState {
  isFollowing: (type: FollowTargetType, key: string) => boolean;
  follow: (type: FollowTargetType, key: string) => Promise<boolean>;
  unfollow: (type: FollowTargetType, key: string) => Promise<boolean>;
  /** Replace one followed city's optional email-decoration topics. */
  setCityTopics: (cityKey: string, tagIds: string[]) => Promise<string[]>;
}

function cityToken(cityKey: string): string {
  return cityKey.trim().toLowerCase();
}

function matchesTarget(
  follow: CurrentUserFollow,
  type: FollowTargetType,
  key: string,
): boolean {
  if (follow.target_type !== type) return false;
  return type === "city"
    ? cityToken(follow.target_key) === cityToken(key)
    : follow.target_key === key;
}

function followToken(type: FollowTargetType, key: string): string {
  return `${type}:${type === "city" ? cityToken(key) : key}`;
}

function topicToken(cityKey: string): string {
  return `topic:${cityToken(cityKey)}`;
}

function bumpRevision(revisions: Map<string, number>, token: string): number {
  const revision = (revisions.get(token) ?? 0) + 1;
  revisions.set(token, revision);
  return revision;
}

function cityTopicsEntry(
  cityTopics: CityTopicsMap,
  cityKey: string,
): [string, string[]] | undefined {
  const token = cityToken(cityKey);
  return Object.entries(cityTopics).find(
    ([storedKey]) => cityToken(storedKey) === token,
  );
}

function cityTopicsFor(cityTopics: CityTopicsMap, cityKey: string): string[] {
  return cityTopicsEntry(cityTopics, cityKey)?.[1] ?? [];
}

function replaceCityTopics(
  cityTopics: CityTopicsMap,
  cityKey: string,
  tagIds: string[],
): CityTopicsMap {
  const token = cityToken(cityKey);
  const next = Object.fromEntries(
    Object.entries(cityTopics).filter(
      ([storedKey]) => cityToken(storedKey) !== token,
    ),
  );
  if (tagIds.length > 0) next[cityKey] = tagIds;
  return next;
}

function copyCityTopics(
  current: CityTopicsMap,
  canonical: CityTopicsMap,
  cityKey: string,
): CityTopicsMap {
  const entry = cityTopicsEntry(canonical, cityKey);
  return entry
    ? replaceCityTopics(current, entry[0], entry[1])
    : replaceCityTopics(current, cityKey, []);
}

function reconcileFollow(
  current: CurrentUserFollow[],
  canonical: CurrentUserFollow[],
  type: FollowTargetType,
  key: string,
): CurrentUserFollow[] {
  const withoutTarget = current.filter(
    (follow) => !matchesTarget(follow, type, key),
  );
  const serverTarget = canonical.find((follow) =>
    matchesTarget(follow, type, key),
  );
  return serverTarget ? [serverTarget, ...withoutTarget] : withoutTarget;
}

function readCityTopics(body: unknown): CityTopicsMap {
  if (!body || typeof body !== "object") return {};
  const raw = (body as { city_topics?: unknown }).city_topics;
  if (!raw || typeof raw !== "object") return {};
  const cityTopics: CityTopicsMap = {};
  for (const [cityKey, value] of Object.entries(raw)) {
    if (Array.isArray(value)) {
      cityTopics[cityKey] = value.filter(
        (tagId): tagId is string => typeof tagId === "string",
      );
    }
  }
  return cityTopics;
}

function snapshotFromBody(body: unknown): FollowsSnapshot | null {
  if (
    !body ||
    typeof body !== "object" ||
    (body as { success?: unknown }).success !== true ||
    !Array.isArray((body as { follows?: unknown }).follows)
  ) {
    return null;
  }
  return {
    follows: (body as { follows: CurrentUserFollow[] }).follows,
    cityTopics: readCityTopics(body),
  };
}

function topicResultFromBody(
  body: unknown,
  fallbackCityKey: string,
): { cityKey: string; tagIds: string[] } | null {
  if (
    !body ||
    typeof body !== "object" ||
    (body as { success?: unknown }).success !== true ||
    !Array.isArray((body as { tag_ids?: unknown }).tag_ids)
  ) {
    return null;
  }
  const rawCityKey = (body as { city_key?: unknown }).city_key;
  return {
    cityKey: typeof rawCityKey === "string" ? rawCityKey : fallbackCityKey,
    tagIds: (body as { tag_ids: unknown[] }).tag_ids.filter(
      (tagId): tagId is string => typeof tagId === "string",
    ),
  };
}

async function fetchSnapshot(): Promise<FollowsSnapshot | null> {
  try {
    const response = await fetch("/api/follows", {
      credentials: "include",
      cache: "no-store",
    });
    if (!response.ok) return null;
    return snapshotFromBody(await response.json());
  } catch {
    return null;
  }
}

function enqueue<T>(
  tailRef: { current: Promise<void> },
  task: () => Promise<T>,
): Promise<T> {
  const result = tailRef.current.then(task, task);
  tailRef.current = result.then(
    () => undefined,
    () => undefined,
  );
  return result;
}

export function useFollows(): FollowsAPI {
  const { user, loading: userLoading } = useCurrentUser();
  const initialSnapshot: FollowsSnapshot = {
    follows: user?.follows ?? [],
    cityTopics: user?.city_topics ?? {},
  };
  const [state, setState] = useState<FollowsState>({
    ...initialSnapshot,
    loading: true,
  });
  const queueRef = useRef<Promise<void>>(Promise.resolve());
  const principalRef = useRef<number | null>(user?.user_id ?? null);
  const generationRef = useRef(0);
  const mutationRevisionRef = useRef(0);
  const settlementRevisionRef = useRef(0);
  const activeMutationsRef = useRef(0);
  const targetRevisionsRef = useRef(new Map<string, number>());
  const topicIntentsRef = useRef(new Map<string, "set" | "unfollow">());
  const confirmedRef = useRef<FollowsSnapshot>(initialSnapshot);

  const principalId = user?.user_id ?? null;
  const principalChanged = principalRef.current !== principalId;
  if (principalChanged) {
    principalRef.current = principalId;
    generationRef.current += 1;
    mutationRevisionRef.current += 1;
    settlementRevisionRef.current += 1;
    activeMutationsRef.current = 0;
    targetRevisionsRef.current.clear();
    topicIntentsRef.current.clear();
    queueRef.current = Promise.resolve();
    confirmedRef.current = initialSnapshot;
  }

  useEffect(() => {
    if (userLoading) return;
    if (!user) {
      setState({ follows: [], cityTopics: {}, loading: false });
      return;
    }

    const generation = generationRef.current;
    if (principalChanged) {
      setState({ ...confirmedRef.current, loading: true });
    } else {
      setState((current) => ({ ...current, loading: true }));
    }
    const mutationRevision = mutationRevisionRef.current;
    const settlementRevision = settlementRevisionRef.current;
    let active = true;
    void fetchSnapshot().then((snapshot) => {
      if (!active || generationRef.current !== generation) return;
      const isCurrent =
        mutationRevisionRef.current === mutationRevision &&
        settlementRevisionRef.current === settlementRevision &&
        activeMutationsRef.current === 0;
      if (snapshot && isCurrent) {
        confirmedRef.current = snapshot;
        setState({ ...snapshot, loading: false });
      } else {
        setState((current) => ({ ...current, loading: false }));
      }
    });
    return () => {
      active = false;
    };
  }, [user, userLoading]);

  const isFollowing = useCallback(
    (type: FollowTargetType, key: string): boolean =>
      state.follows.some((follow) => matchesTarget(follow, type, key)),
    [state.follows],
  );

  const mutateFollow = useCallback(
    async (
      method: "POST" | "DELETE",
      type: FollowTargetType,
      key: string,
    ): Promise<boolean> => {
      if (!user) return false;
      const generation = generationRef.current;
      const mutationRevision = ++mutationRevisionRef.current;
      const targetKey = followToken(type, key);
      const targetRevision = bumpRevision(
        targetRevisionsRef.current,
        targetKey,
      );
      const topicKey = topicToken(key);
      const topicRevision =
        method === "DELETE" && type === "city"
          ? bumpRevision(targetRevisionsRef.current, topicKey)
          : null;
      if (topicRevision !== null) {
        topicIntentsRef.current.set(topicKey, "unfollow");
      }
      activeMutationsRef.current += 1;

      const stamp = new Date().toISOString();
      setState((current) => ({
        ...current,
        follows:
          method === "POST"
            ? current.follows.some((follow) =>
                matchesTarget(follow, type, key),
              )
              ? current.follows
              : [
                  { target_type: type, target_key: key, created_at: stamp },
                  ...current.follows,
                ]
            : current.follows.filter(
                (follow) => !matchesTarget(follow, type, key),
              ),
        cityTopics:
          method === "DELETE" && type === "city"
            ? replaceCityTopics(current.cityTopics, key, [])
            : current.cityTopics,
      }));

      try {
        return await enqueue(queueRef, async () => {
          if (generationRef.current !== generation) return false;
          let snapshot: FollowsSnapshot | null = null;
          let changed = false;
          try {
            const response = await fetch("/api/follows", {
              method,
              credentials: "include",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ target_type: type, target_key: key }),
            });
            const body: unknown = await response.json().catch(() => null);
            snapshot = snapshotFromBody(body);
            if (!response.ok || !snapshot) {
              throw new Error("follow mutation was not successful");
            }
            changed =
              (body as { added?: unknown; removed?: unknown })[
                method === "POST" ? "added" : "removed"
              ] === true;
          } catch {
            snapshot = await fetchSnapshot();
          }
          if (generationRef.current !== generation) return false;
          if (snapshot) confirmedRef.current = snapshot;

          const confirmed = confirmedRef.current;
          const allCurrent =
            mutationRevisionRef.current === mutationRevision;
          const followIsCurrent =
            targetRevisionsRef.current.get(targetKey) === targetRevision;
          const topicIsCurrent =
            topicRevision !== null &&
            targetRevisionsRef.current.get(topicKey) === topicRevision;
          setState((current) =>
            allCurrent
              ? { ...confirmed, loading: current.loading }
              : {
                  ...current,
                  follows: followIsCurrent
                    ? reconcileFollow(
                        current.follows,
                        confirmed.follows,
                        type,
                        key,
                      )
                    : current.follows,
                  cityTopics: topicIsCurrent
                    ? copyCityTopics(
                        current.cityTopics,
                        confirmed.cityTopics,
                        key,
                      )
                    : current.cityTopics,
                },
          );
          return changed;
        });
      } finally {
        if (generationRef.current === generation) {
          activeMutationsRef.current = Math.max(
            0,
            activeMutationsRef.current - 1,
          );
          settlementRevisionRef.current += 1;
        }
      }
    },
    [user],
  );

  const follow = useCallback(
    (type: FollowTargetType, key: string) =>
      mutateFollow("POST", type, key),
    [mutateFollow],
  );

  const unfollow = useCallback(
    (type: FollowTargetType, key: string) =>
      mutateFollow("DELETE", type, key),
    [mutateFollow],
  );

  const setCityTopics = useCallback(
    async (cityKey: string, tagIds: string[]): Promise<string[]> => {
      if (!user) return [];
      const generation = generationRef.current;
      const mutationRevision = ++mutationRevisionRef.current;
      const targetKey = topicToken(cityKey);
      const targetRevision = bumpRevision(
        targetRevisionsRef.current,
        targetKey,
      );
      topicIntentsRef.current.set(targetKey, "set");
      activeMutationsRef.current += 1;
      const normalized = Array.from(
        new Set(tagIds.map((tagId) => tagId.toLowerCase())),
      ).sort();
      setState((current) => ({
        ...current,
        cityTopics: replaceCityTopics(
          current.cityTopics,
          cityKey,
          normalized,
        ),
      }));

      try {
        return await enqueue(queueRef, async () => {
          if (generationRef.current !== generation) return [];
          if (
            targetRevisionsRef.current.get(targetKey) !== targetRevision &&
            topicIntentsRef.current.get(targetKey) === "unfollow"
          ) {
            return cityTopicsFor(confirmedRef.current.cityTopics, cityKey);
          }
          let canonical: { cityKey: string; tagIds: string[] } | null = null;
          let snapshot: FollowsSnapshot | null = null;
          try {
            const response = await fetch(
              `/api/follows/city-topics/${encodeURIComponent(cityKey)}`,
              {
                method: "PUT",
                credentials: "include",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ tag_ids: tagIds }),
              },
            );
            const body: unknown = await response.json().catch(() => null);
            canonical = topicResultFromBody(body, cityKey);
            if (!response.ok || !canonical) {
              throw new Error("city-topic mutation was not successful");
            }
          } catch {
            snapshot = await fetchSnapshot();
          }
          if (generationRef.current !== generation) return [];
          if (snapshot) {
            confirmedRef.current = snapshot;
          } else if (canonical) {
            confirmedRef.current = {
              ...confirmedRef.current,
              cityTopics: replaceCityTopics(
                confirmedRef.current.cityTopics,
                canonical.cityKey,
                canonical.tagIds,
              ),
            };
          }

          const confirmed = confirmedRef.current;
          const allCurrent =
            mutationRevisionRef.current === mutationRevision;
          const targetIsCurrent =
            targetRevisionsRef.current.get(targetKey) === targetRevision;
          setState((current) =>
            allCurrent
              ? { ...confirmed, loading: current.loading }
              : targetIsCurrent
                ? {
                    ...current,
                    cityTopics: copyCityTopics(
                      current.cityTopics,
                      confirmed.cityTopics,
                      cityKey,
                    ),
                  }
                : current,
          );
          return canonical?.tagIds ?? cityTopicsFor(confirmed.cityTopics, cityKey);
        });
      } finally {
        if (generationRef.current === generation) {
          activeMutationsRef.current = Math.max(
            0,
            activeMutationsRef.current - 1,
          );
          settlementRevisionRef.current += 1;
        }
      }
    },
    [user],
  );

  return { ...state, isFollowing, follow, unfollow, setCityTopics };
}
