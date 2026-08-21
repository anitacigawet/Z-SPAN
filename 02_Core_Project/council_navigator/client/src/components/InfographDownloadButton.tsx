/**
 * InfographDownloadButton — signed-in-only per-meeting share-card generator.
 *
 * Sol Round-1 (session-103) recommended a client-side Canvas 2D render
 * (no server pipeline, no new dependency beyond the already-installed
 * `qrcode`). This button owns the render → download loop; the pure
 * render lives at `lib/renderInfograph.ts` so it's testable independent
 * of the button UI.
 *
 * Gating shape:
 *   - Anonymous viewer → renders nothing (they see the sign-in nudge from
 *     the TopBar's SignInPill, per SignInBenefitsToast).
 *   - Draft / owner-peek broadcast → renders nothing, even when the
 *     disclaimer provider auto-acknowledges peek mode.
 *   - Signed-in viewer on a publicly served broadcast → visible button;
 *     click triggers render + download.
 *   - The public data disclaimer gate (which governs the key-decision
 *     display) also governs this button — if the viewer hasn't
 *     acknowledged the disclaimer, we hide until they do, so the image
 *     download isn't a bypass around the on-page warning.
 */
import { useState, type ReactElement } from "react";

import { useCurrentUser } from "../hooks/useCurrentUser";
import {
  downloadBlob,
  infographFilename,
  renderInfograph,
  type InfographInput,
} from "../lib/renderInfograph";

interface Props {
  city: string;
  date: string;
  title: string;
  tagline: string | null;
  keyDecisions: string[];
  publicId: string;
  /**
   * Canonical public URL of this broadcast — feeds the QR + the printed
   * URL line. The caller builds it from the server-validated public_id;
   * ambient browser location state is never accepted here.
   */
  publicUrl: string;
  /**
   * True once the viewer has acknowledged the public-data disclaimer
   * that governs the key-decision display. When false the button is
   * hidden — the infograph carries the same key decisions, so an
   * un-gated PNG would bypass the on-page warning.
   */
  disclaimerAcknowledged: boolean;
  /**
   * Mirrors the server's public-serving predicate: published meeting plus
   * an approved work order. False for drafts and operator-only previews.
   */
  isPubliclyServed: boolean;
  /** True only for the owner's `?peek=1` review surface. */
  isPeek: boolean;
}

type ButtonState = "idle" | "rendering" | "error";

export function InfographDownloadButton({
  city,
  date,
  title,
  tagline,
  keyDecisions,
  publicId,
  publicUrl,
  disclaimerAcknowledged,
  isPubliclyServed,
  isPeek,
}: Props): ReactElement | null {
  const { user, loading } = useCurrentUser();
  const [state, setState] = useState<ButtonState>("idle");
  const [errorText, setErrorText] = useState<string>("");

  // Anonymous / still-loading viewer → nothing. The SignInPill nudge
  // handles the sign-in prompt separately.
  if (loading || !user) return null;
  if (!disclaimerAcknowledged || !isPubliclyServed || isPeek) return null;

  const onClick = async () => {
    setState("rendering");
    setErrorText("");
    try {
      const input: InfographInput = {
        city,
        date,
        title,
        tagline,
        keyDecisions,
        publicUrl,
      };
      const blob = await renderInfograph(input);
      if (!blob) {
        setState("error");
        setErrorText("Your browser couldn't render the image.");
        return;
      }
      downloadBlob(blob, infographFilename(city, date, publicId));
      setState("idle");
    } catch (err) {
      setState("error");
      setErrorText(err instanceof Error ? err.message : "Download failed.");
    }
  };

  const label =
    state === "rendering"
      ? "Rendering…"
      : state === "error"
        ? "Try again"
        : "Download infographic";

  return (
    <div className="inline-flex items-center gap-2">
      <button
        type="button"
        onClick={onClick}
        disabled={state === "rendering"}
        aria-label="Download a Z-SPAN share card for this meeting"
        className="inline-flex items-center gap-1.5 rounded-md border border-[var(--line)] bg-[var(--surface-2)] px-3 py-1.5 text-[12px] font-medium text-foreground/85 transition hover:border-[var(--line-strong)] hover:text-white disabled:opacity-50"
      >
        <svg
          width="14"
          height="14"
          viewBox="0 0 16 16"
          fill="none"
          xmlns="http://www.w3.org/2000/svg"
          aria-hidden="true"
        >
          <path
            d="M8 2v9m0 0 3-3m-3 3-3-3M3 13.5h10"
            stroke="currentColor"
            strokeWidth="1.4"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
        <span>{label}</span>
      </button>
      {state === "error" && errorText && (
        <span className="text-[11px] text-rose-300/80" role="alert">
          {errorText}
        </span>
      )}
    </div>
  );
}
