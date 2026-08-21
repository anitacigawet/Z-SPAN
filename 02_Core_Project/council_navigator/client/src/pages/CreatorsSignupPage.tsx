/**
 * CreatorsSignupPage — Creator Network signup wizard (V0).
 *
 * Per ACCOUNT_SYSTEM_SPEC chunk 8 + D-095. Four steps, each with a
 * structural gate the user cannot skip:
 *
 *   1. TOS acceptance — scroll-to-bottom gate + "I agree" button.
 *      V0 ships with the three-clause structural draft from
 *      CREATOR_NETWORK_PLAYBOOK.md:85-95 (TOS_VERSION "v0-prelaunch-draft").
 *      The lawyer-reviewed final TOS lands before any creator account
 *      becomes effective for public use; the load-bearing structure
 *      (civic-engagement use / disclaimer acknowledgment / banning &
 *      revocation) survives lawyer revision per D-095 Hard Preconditions.
 *   2. Disclaimer narrated-karaoke acknowledgment — word-by-word
 *      highlight; "I acknowledge" button gated until the highlight
 *      reaches the last word. V0 ships with the D-095 verbatim canonical
 *      text (DISCLAIMER_VERSION "v0-canonical-d095"); the ElevenLabs
 *      audio render + word-level karaoke timing JSON are deferred to a
 *      follow-up chunk. Until that lands, an even-cadence timer
 *      (KARAOKE_MS_PER_WORD) advances the highlight to enforce a
 *      D-095-compliant ~50-60s dwell window over the ~140-word text.
 *   3. Signup form — display name, handle, optional creator-context
 *      note. Free-text fields go through parsers/input_moderation.py
 *      on the backend per S-008 chunk 3 (surface="creator_signup").
 *   4. Confirmation — review + final submit. POST /api/creators/promote
 *      fires; on success route to CreatorsLandingPage.
 *
 * Anonymous users enter through the provider-neutral login page.
 * before reaching here. Users with role !== 'light' get redirected
 * to CreatorsLandingPage by the App.tsx route guard.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactElement } from "react";

import { useCurrentUser, invalidateCurrentUserCache } from "../hooks/useCurrentUser";

interface CreatorsSignupPageProps {
  onNavigate: (view: string, params?: any) => void;
}

// V0 text constants. The launch-day pre-flight check
// (parsers/scripts/check_creator_placeholders.py) greps user-facing
// surfaces for known "placeholder X" trigger substrings; flagship-public
// access stays gated until that check exits clean. As of 2026-06-11:
// TOS body is the playbook v0-prelaunch-draft (lawyer-reviewed final
// still pending per D-095 Hard Preconditions); disclaimer body is
// D-095 verbatim canonical. The ElevenLabs audio render + word-timing
// JSON land in a follow-up chunk — until then StepTwo uses an
// even-cadence timer (KARAOKE_MS_PER_WORD) to enforce dwell time.

const TOS_TEXT_V0 =
  "Z-SPAN Creator Network — Terms of Service (v0 pre-launch draft).\n\n" +
  "This is a draft of the three load-bearing clauses that govern the " +
  "Creator Network. The full lawyer-reviewed Terms of Service lands " +
  "before any creator account becomes effective for public use; the " +
  "three clauses below are the canonical-substance load-bearing parts " +
  "that survive lawyer revision (per CREATOR_NETWORK_PLAYBOOK.md and " +
  "D-095 Hard Preconditions).\n\n" +
  "Clause 1 — Use for civic engagement. You agree to use the assets " +
  "you download from this repository to promote civic engagement, " +
  "civic awareness, or civic accountability. You agree NOT to use the " +
  "assets for raw, out-of-context partisan harassment of private " +
  "citizens, or for purposes that misrepresent the source meeting.\n\n" +
  "Clause 2 — Disclaimer acknowledgment. You acknowledge that before " +
  "each download you have been shown the human-consequence disclaimer, " +
  "and that you have either chosen to download in full awareness of " +
  "the disclaimer's content, or chosen \"I changed my mind\" and walked " +
  "away. The platform logs your disclaimer acknowledgment per download; " +
  "this log is part of the structural record of your use of the " +
  "repository.\n\n" +
  "Clause 3 — Banning and revocation. Documented violations of Clause 1 " +
  "may result in account suspension. Appeals are available via owner " +
  "review. Suspended accounts retain the ability to view their past " +
  "downloads but lose the ability to download new assets.\n\n" +
  "Additional clauses (data handling, indemnification, jurisdiction, " +
  "dispute resolution) will be added under lawyer guidance before " +
  "public launch.";

// D-095:610-612 verbatim. Do not paraphrase — any revision goes
// through a new D-entry per D-095. Two paragraphs separated by \n\n;
// StepTwo renders them with a visible break.
const DISCLAIMER_TEXT =
  "Before you download this clip, please remember that the person or " +
  "people in the clip are real people, just like you. They were " +
  "compelled to attend this meeting out of a voluntary commitment to " +
  "uplift their community through civic engagement. Whether it be the " +
  "role that a council member signed up for, or a community member " +
  "making their voice heard, they did not sign up to be on YouTube, " +
  "TikTok, or any other social media platforms. They came to the table " +
  "because they wanted to make a change.\n\n" +
  "Before you decide on a thumbnail, or pick a title, please pause and " +
  "ask yourself: are you working with the fragment of someone's lived " +
  "experience? or simply working with just some video/meme? If it is " +
  "the second, we would politely ask that you stop here. Remember, " +
  "honesty is the best policy.";

const DISCLAIMER_AUDIO_NOTE =
  "Voice: warm-female narrator (ElevenLabs Multilingual v2). " +
  "Audio render pending; V0 uses an even-cadence timer to enforce " +
  "dwell time until the disclaimer.mp3 and word-timing JSON land.";

const TOS_VERSION = "v0-prelaunch-draft";
const DISCLAIMER_VERSION = "v0-canonical-d095";

// Karaoke cadence — ms per word. With the D-095 canonical disclaimer
// (~140 words) 380 ms gives a total gate duration of ~53 seconds —
// inside D-095's "~45-60 seconds of narration" target. When the
// ElevenLabs render + word-timing JSON land, StepTwo switches to
// audio-element-driven advancement and this constant becomes obsolete.
const KARAOKE_MS_PER_WORD = 380;

const CONTEXT_MAX_CHARS = 500;
const HANDLE_MAX_CHARS = 50;
const DISPLAY_NAME_MAX_CHARS = 80;

function buildSignInHref(): string {
  if (typeof window === "undefined")
    return "/login?next=%2F%3Fview%3Dcreators";
  const next = "/?view=creators";
  return `/login?next=${encodeURIComponent(next)}`;
}

// Step 1 — TOS acceptance with scroll-to-bottom gate.
function StepOne({ onAgree }: { onAgree: () => void }): ReactElement {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [scrolledToBottom, setScrolledToBottom] = useState(false);

  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    // Within 8px of bottom counts as "reached" — small slack for
    // rendering rounding.
    const reached = el.scrollHeight - el.scrollTop - el.clientHeight < 8;
    if (reached) setScrolledToBottom(true);
  }, []);

  // If the content fits without scrolling (small viewport / large
  // font), enable the button immediately so the gate doesn't
  // permanently disable.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (el.scrollHeight <= el.clientHeight + 8) {
      setScrolledToBottom(true);
    }
  }, []);

  return (
    <section className="space-y-5">
      <div>
        <div className="text-[11px] uppercase tracking-[0.18em] text-foreground/45 mb-2">
          Step 1 of 4 · Terms of Service
        </div>
        <h2 className="text-xl font-light tracking-tight text-white">
          Read and accept the Terms of Service
        </h2>
      </div>
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        className="max-h-72 overflow-y-auto rounded-lg border border-white/10 bg-white/[0.02] px-5 py-4 text-sm leading-relaxed text-foreground/70 space-y-3"
      >
        {TOS_TEXT_V0.split(/\n\n+/).map((para, i) => (
          <p key={i}>{para}</p>
        ))}
      </div>
      <div className="flex items-center justify-between gap-4">
        <p className="text-[11px] text-foreground/45">
          {scrolledToBottom
            ? "You've reached the bottom. You can now agree."
            : "Scroll to the bottom of the text before agreeing."}
        </p>
        <button
          type="button"
          onClick={onAgree}
          disabled={!scrolledToBottom}
          className="inline-flex items-center gap-2 rounded-full border border-emerald-400/30 bg-emerald-400/10 px-4 py-1.5 text-xs font-medium text-emerald-100 transition disabled:opacity-30 disabled:cursor-not-allowed hover:border-emerald-400/60"
        >
          I agree
        </button>
      </div>
    </section>
  );
}

// Step 2 — Disclaimer narrated-karaoke acknowledgment.
function StepTwo({ onAcknowledge }: { onAcknowledge: () => void }): ReactElement {
  // Split DISCLAIMER_TEXT into paragraphs; each paragraph carries the
  // starting global-word-index so the karaoke highlight indexes
  // monotonically across all paragraphs while the render preserves the
  // visible paragraph break from D-095.
  const paragraphs = useMemo(() => {
    const result: { words: string[]; startIdx: number }[] = [];
    let offset = 0;
    for (const para of DISCLAIMER_TEXT.split(/\n\n+/)) {
      const w = para.split(/\s+/).filter(Boolean);
      if (w.length > 0) {
        result.push({ words: w, startIdx: offset });
        offset += w.length;
      }
    }
    return result;
  }, []);
  const totalWords = useMemo(
    () => paragraphs.reduce((n, p) => n + p.words.length, 0),
    [paragraphs],
  );
  const [currentIndex, setCurrentIndex] = useState(0);
  const [completed, setCompleted] = useState(false);

  useEffect(() => {
    if (completed) return;
    const id = window.setInterval(() => {
      setCurrentIndex((idx) => {
        if (idx >= totalWords - 1) {
          window.clearInterval(id);
          setCompleted(true);
          return totalWords - 1;
        }
        return idx + 1;
      });
    }, KARAOKE_MS_PER_WORD);
    return () => window.clearInterval(id);
  }, [completed, totalWords]);

  return (
    <section className="space-y-5">
      <div>
        <div className="text-[11px] uppercase tracking-[0.18em] text-foreground/45 mb-2">
          Step 2 of 4 · Disclaimer (narrated)
        </div>
        <h2 className="text-xl font-light tracking-tight text-white">
          Listen to the disclaimer
        </h2>
        <p className="mt-1 text-[11px] text-foreground/40">
          {DISCLAIMER_AUDIO_NOTE}
        </p>
      </div>
      <div className="rounded-lg border border-white/10 bg-white/[0.02] px-5 py-6 text-base leading-relaxed space-y-3">
        {paragraphs.map(({ words, startIdx }, pIdx) => (
          <p key={pIdx}>
            {words.map((w, i) => {
              const globalI = startIdx + i;
              return (
                <span
                  key={globalI}
                  className={
                    globalI < currentIndex
                      ? "text-foreground/55"
                      : globalI === currentIndex
                        ? "text-white bg-emerald-400/15 rounded px-0.5"
                        : "text-foreground/25"
                  }
                >
                  {w}
                  {i < words.length - 1 ? " " : ""}
                </span>
              );
            })}
          </p>
        ))}
      </div>
      <div className="flex items-center justify-between gap-4">
        <p className="text-[11px] text-foreground/45">
          {completed
            ? "Disclaimer finished. You can now acknowledge."
            : `Listening… ${currentIndex + 1} of ${totalWords}`}
        </p>
        <button
          type="button"
          onClick={onAcknowledge}
          disabled={!completed}
          className="inline-flex items-center gap-2 rounded-full border border-emerald-400/30 bg-emerald-400/10 px-4 py-1.5 text-xs font-medium text-emerald-100 transition disabled:opacity-30 disabled:cursor-not-allowed hover:border-emerald-400/60"
        >
          I acknowledge
        </button>
      </div>
    </section>
  );
}

interface SignupForm {
  display_name: string;
  handle: string;
  creator_context: string;
}

// Step 3 — Signup form.
function StepThree({
  initial,
  onNext,
}: {
  initial: SignupForm;
  onNext: (form: SignupForm) => void;
}): ReactElement {
  const [form, setForm] = useState<SignupForm>(initial);
  const update = (k: keyof SignupForm) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));
  const valid =
    form.display_name.trim().length > 0 &&
    form.display_name.length <= DISPLAY_NAME_MAX_CHARS &&
    form.handle.trim().length > 0 &&
    form.handle.length <= HANDLE_MAX_CHARS &&
    form.creator_context.length <= CONTEXT_MAX_CHARS;

  return (
    <section className="space-y-5">
      <div>
        <div className="text-[11px] uppercase tracking-[0.18em] text-foreground/45 mb-2">
          Step 3 of 4 · About you
        </div>
        <h2 className="text-xl font-light tracking-tight text-white">
          Tell us how you'd like to appear
        </h2>
      </div>
      <div className="space-y-4">
        <label className="block">
          <span className="text-xs text-foreground/55 mb-1 block">
            Display name
          </span>
          <input
            type="text"
            value={form.display_name}
            onChange={update("display_name")}
            maxLength={DISPLAY_NAME_MAX_CHARS}
            className="w-full rounded-md border border-white/10 bg-white/[0.03] px-3 py-2 text-sm text-white outline-none focus:border-white/40"
          />
        </label>
        <label className="block">
          <span className="text-xs text-foreground/55 mb-1 block">Handle</span>
          <input
            type="text"
            value={form.handle}
            onChange={update("handle")}
            maxLength={HANDLE_MAX_CHARS}
            placeholder="e.g. yourname"
            className="w-full rounded-md border border-white/10 bg-white/[0.03] px-3 py-2 text-sm text-white outline-none focus:border-white/40"
          />
        </label>
        <label className="block">
          <span className="text-xs text-foreground/55 mb-1 block">
            Tell us about your work
            <span className="text-foreground/35"> · optional · max {CONTEXT_MAX_CHARS} chars</span>
          </span>
          <textarea
            value={form.creator_context}
            onChange={update("creator_context")}
            maxLength={CONTEXT_MAX_CHARS}
            rows={3}
            className="w-full rounded-md border border-white/10 bg-white/[0.03] px-3 py-2 text-sm text-white outline-none focus:border-white/40"
          />
          <span className="text-[10px] text-foreground/35 mt-1 block">
            {form.creator_context.length} / {CONTEXT_MAX_CHARS}
          </span>
        </label>
      </div>
      <div className="flex justify-end">
        <button
          type="button"
          onClick={() => onNext(form)}
          disabled={!valid}
          className="inline-flex items-center gap-2 rounded-full border border-emerald-400/30 bg-emerald-400/10 px-4 py-1.5 text-xs font-medium text-emerald-100 transition disabled:opacity-30 disabled:cursor-not-allowed hover:border-emerald-400/60"
        >
          Continue
        </button>
      </div>
    </section>
  );
}

// Step 4 — Confirmation.
function StepFour({
  form,
  submitting,
  error,
  onSubmit,
  onBack,
}: {
  form: SignupForm;
  submitting: boolean;
  error: string | null;
  onSubmit: () => void;
  onBack: () => void;
}): ReactElement {
  return (
    <section className="space-y-5">
      <div>
        <div className="text-[11px] uppercase tracking-[0.18em] text-foreground/45 mb-2">
          Step 4 of 4 · Confirm
        </div>
        <h2 className="text-xl font-light tracking-tight text-white">
          Review and create your creator account
        </h2>
      </div>
      <div className="rounded-lg border border-white/10 bg-white/[0.02] px-5 py-4 space-y-3">
        <div>
          <div className="text-[10px] uppercase tracking-wider text-foreground/40">Display name</div>
          <div className="text-sm text-white">{form.display_name}</div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-foreground/40">Handle</div>
          <div className="text-sm text-white">{form.handle}</div>
        </div>
        {form.creator_context && (
          <div>
            <div className="text-[10px] uppercase tracking-wider text-foreground/40">About your work</div>
            <div className="text-sm text-foreground/80 whitespace-pre-wrap">{form.creator_context}</div>
          </div>
        )}
        <div className="pt-2 text-[11px] text-foreground/40 border-t border-white/5">
          By clicking Create, you confirm acceptance of the TOS (v{TOS_VERSION}) +
          the disclaimer (v{DISCLAIMER_VERSION}).
        </div>
      </div>
      {error && (
        <div className="rounded-md border border-rose-400/40 bg-rose-500/10 px-4 py-2 text-sm text-rose-200">
          {error}
        </div>
      )}
      <div className="flex items-center justify-between gap-4">
        <button
          type="button"
          onClick={onBack}
          disabled={submitting}
          className="text-xs text-foreground/55 hover:text-white transition disabled:opacity-30"
        >
          Back
        </button>
        <button
          type="button"
          onClick={onSubmit}
          disabled={submitting}
          className="inline-flex items-center gap-2 rounded-full border border-emerald-400/30 bg-emerald-400/15 px-4 py-1.5 text-xs font-medium text-emerald-100 transition disabled:opacity-30 disabled:cursor-not-allowed hover:border-emerald-400/60"
        >
          {submitting ? "Creating…" : "Create my creator account"}
        </button>
      </div>
    </section>
  );
}

export default function CreatorsSignupPage({ onNavigate }: CreatorsSignupPageProps): ReactElement {
  const { user, loading } = useCurrentUser();
  const [step, setStep] = useState<1 | 2 | 3 | 4>(1);
  const [form, setForm] = useState<SignupForm>({
    display_name: "",
    handle: "",
    creator_context: "",
  });
  const [disclaimerAckAt, setDisclaimerAckAt] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Hydrate display_name from Google profile on first mount.
  useEffect(() => {
    if (user && !form.display_name && user.display_name) {
      setForm((f) => ({ ...f, display_name: user.display_name || "" }));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user]);

  // Anonymous users — render the sign-in prompt.
  if (loading) {
    return (
      <div className="min-h-screen bg-background flex items-center justify-center">
        <div className="text-foreground/40 text-sm">Loading…</div>
      </div>
    );
  }
  if (!user) {
    return (
      <div className="min-h-screen bg-background px-6 py-16">
        <div className="mx-auto max-w-md text-center space-y-5">
          <div className="text-[11px] uppercase tracking-[0.18em] text-foreground/45">
            Creator Network · signup
          </div>
          <h1 className="text-2xl font-light tracking-tight text-white">
            Log in to begin
          </h1>
          <p className="text-sm text-foreground/55 leading-relaxed">
            The Creator Network signup flow uses your Z-SPAN account so the
            agreement stays connected to the account you use here.
          </p>
          <a
            href={buildSignInHref()}
            className="inline-flex items-center gap-2 rounded-full border border-white/15 bg-white/5 px-4 py-2 text-sm font-medium text-white hover:border-white/40 hover:bg-white/10 transition"
          >
            Log in
          </a>
        </div>
      </div>
    );
  }

  // Submit handler — fires on Step 4 final button.
  const handleSubmit = useCallback(async () => {
    setSubmitting(true);
    setError(null);
    try {
      const body = {
        tos_version: TOS_VERSION,
        disclaimer_version: DISCLAIMER_VERSION,
        disclaimer_acknowledged_at: disclaimerAckAt || new Date().toISOString(),
        signup_form: form,
      };
      const res = await fetch("/api/creators/promote", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const json = await res.json().catch(() => null);
      if (!res.ok || !json?.success) {
        const detail =
          json?.detail ||
          json?.reason ||
          json?.error ||
          `Server returned ${res.status}`;
        setError(detail);
        setSubmitting(false);
        return;
      }
      // Force a re-fetch of /api/auth/me so the upgraded role
      // surfaces everywhere via useCurrentUser.
      invalidateCurrentUserCache();
      onNavigate("creators");
    } catch (err: any) {
      setError(err?.message || "Network error");
      setSubmitting(false);
    }
  }, [disclaimerAckAt, form, onNavigate]);

  return (
    <div className="min-h-screen bg-background px-6 py-12">
      <div className="mx-auto max-w-2xl">
        <header className="mb-8">
          <div className="text-[11px] uppercase tracking-[0.18em] text-foreground/40 mb-1">
            Creator Network
          </div>
          <h1 className="text-2xl font-light tracking-tight text-white">
            Become a Z-SPAN creator
          </h1>
        </header>

        {/* Progress dots */}
        <div className="flex items-center gap-2 mb-10">
          {[1, 2, 3, 4].map((n) => (
            <div
              key={n}
              className={`h-1 flex-1 rounded-full transition-colors ${
                n < step
                  ? "bg-emerald-400/60"
                  : n === step
                    ? "bg-white/60"
                    : "bg-white/10"
              }`}
            />
          ))}
        </div>

        {step === 1 && <StepOne onAgree={() => setStep(2)} />}
        {step === 2 && (
          <StepTwo
            onAcknowledge={() => {
              setDisclaimerAckAt(new Date().toISOString());
              setStep(3);
            }}
          />
        )}
        {step === 3 && (
          <StepThree
            initial={form}
            onNext={(f) => {
              setForm(f);
              setStep(4);
            }}
          />
        )}
        {step === 4 && (
          <StepFour
            form={form}
            submitting={submitting}
            error={error}
            onSubmit={handleSubmit}
            onBack={() => setStep(3)}
          />
        )}
      </div>
    </div>
  );
}
