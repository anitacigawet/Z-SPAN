import type { ReactElement } from "react";

import { useFollows, type FollowTargetType } from "../hooks/useFollows";
import { TOPIC_LABELS, TOPIC_TAG_IDS } from "../utils/topicTags";

interface FollowingListProps {
  onNavigate: (view: string, params?: any) => void;
}

const TYPE_LABELS: Record<FollowTargetType, string> = {
  city: "Cities",
  county: "Counties",
  meeting: "Meetings",
};

const TYPE_ORDER: FollowTargetType[] = ["city", "county", "meeting"];

export function FollowingList({
  onNavigate,
}: FollowingListProps): ReactElement {
  const { follows, cityTopics, loading, unfollow, setCityTopics } =
    useFollows();
  const grouped: Record<FollowTargetType, typeof follows> = {
    city: [],
    county: [],
    meeting: [],
  };

  for (const follow of follows) {
    if (follow.target_type in grouped) {
      grouped[follow.target_type as FollowTargetType].push(follow);
    }
  }

  const totalFollows = follows.length;

  return (
    <>
      <p className="mt-2 text-sm text-foreground/55">
        {totalFollows === 0
          ? loading
            ? "Loading your follows…"
            : "You haven't followed anything yet."
          : `${totalFollows} ${totalFollows === 1 ? "follow" : "follows"}`}
      </p>

      {totalFollows === 0 && !loading && (
        <div className="mt-10 rounded-xl border border-white/10 bg-white/5 px-6 py-10 text-center">
          <p className="text-sm text-foreground/65 mb-4">
            Follow a city, county, or meeting to see it here.
          </p>
          <button
            type="button"
            onClick={() => onNavigate("home")}
            className="inline-flex items-center gap-2 rounded-full border border-white/15 bg-white/0 px-4 py-1.5 text-xs font-medium text-white/85 hover:border-white/40 hover:bg-white/5 transition"
          >
            Browse Channels
          </button>
        </div>
      )}

      <div className="mt-10 space-y-10">
        {TYPE_ORDER.map(type => {
          const items = grouped[type];
          if (items.length === 0) return null;
          return (
            <section key={type}>
              <div className="text-[11px] uppercase tracking-[0.18em] text-foreground/45 mb-3">
                {TYPE_LABELS[type]} · {items.length}
              </div>
              {type === "city" && (
                <p className="text-[11px] text-foreground/45 mb-2">
                  When you enable a topic, matching meetings will highlight it
                  in the email.
                </p>
              )}
              <ul className="divide-y divide-white/5 rounded-xl border border-white/10 bg-white/[0.02]">
                {items.map(item => {
                  const enabledTopics =
                    type === "city"
                      ? cityTopics[item.target_key] ??
                        Object.entries(cityTopics).find(
                          ([cityKey]) =>
                            cityKey.toLowerCase() ===
                            item.target_key.toLowerCase()
                        )?.[1] ??
                        []
                      : [];
                  return (
                    <li
                      key={`${item.target_type}:${item.target_key}`}
                      className="flex flex-col gap-3 px-4 py-3"
                    >
                      <div className="flex items-center justify-between gap-4">
                        <div className="min-w-0">
                          <div className="text-sm text-white truncate">
                            {item.target_key}
                          </div>
                          <div className="text-[11px] text-foreground/40 mt-0.5">
                            Followed{" "}
                            {new Date(item.created_at).toLocaleDateString(
                              undefined,
                              {
                                year: "numeric",
                                month: "short",
                                day: "numeric",
                              }
                            )}
                          </div>
                        </div>
                        <div className="flex items-center gap-2 flex-shrink-0">
                          {type === "meeting" && (
                            <button
                              type="button"
                              onClick={() =>
                                onNavigate("broadcast", {
                                  meetingId: Number(item.target_key),
                                })
                              }
                              className="text-xs text-foreground/65 hover:text-white px-2 py-1 rounded transition"
                            >
                              Open
                            </button>
                          )}
                          <button
                            type="button"
                            onClick={() => {
                              void unfollow(
                                item.target_type,
                                item.target_key
                              );
                            }}
                            className="text-[11px] text-foreground/50 hover:text-rose-300 px-2 py-1 rounded transition"
                            aria-label={`Unfollow ${item.target_key}`}
                          >
                            Unfollow
                          </button>
                        </div>
                      </div>
                      {type === "city" && (
                        <div className="flex flex-wrap gap-x-4 gap-y-1.5 pl-0.5">
                          {TOPIC_TAG_IDS.map(tagId => {
                            const enabled = enabledTopics.includes(tagId);
                            return (
                              <label
                                key={tagId}
                                className="inline-flex items-center gap-1.5 text-[11px] text-foreground/60 hover:text-foreground/85 cursor-pointer transition"
                              >
                                <input
                                  type="checkbox"
                                  checked={enabled}
                                  onChange={() => {
                                    const next = enabled
                                      ? enabledTopics.filter(
                                          t => t !== tagId
                                        )
                                      : [...enabledTopics, tagId];
                                    void setCityTopics(
                                      item.target_key,
                                      next
                                    );
                                  }}
                                  className="h-3 w-3 accent-white/70"
                                  aria-label={`${TOPIC_LABELS[tagId]} tag for ${item.target_key}`}
                                />
                                {TOPIC_LABELS[tagId]}
                              </label>
                            );
                          })}
                        </div>
                      )}
                    </li>
                  );
                })}
              </ul>
            </section>
          );
        })}
      </div>
    </>
  );
}

export default FollowingList;
