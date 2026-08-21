import { useCallback, useEffect, useState, type ReactElement } from "react";

import { useCurrentUser } from "../hooks/useCurrentUser";
import "./invite-page.css";

type InviteMode =
  | "checking"
  | "available"
  | "redeeming"
  | "granted"
  | "unavailable"
  | "error";

interface InvitePageProps {
  token: string;
  onNavigate: (view: string, params?: unknown) => void;
}

interface InvitePanelProps {
  mode: InviteMode;
  signedInName?: string | null;
  signInEnabled: boolean;
  onAccept: () => void;
  onSignIn: () => void;
  onEmail: () => void;
  onEnter: () => void;
  onRetry: () => void;
}

export const PENDING_INVITATION_STORAGE_KEY = "zspan.pending-invitation";

export function invitationSignInUrl(): string {
  const next = "/i#resume=invitation";
  return `/api/auth/google/login?next=${encodeURIComponent(next)}`;
}

export function invitationEmailUrl(token: string): string {
  const fragment = new URLSearchParams({
    invite: token,
    next: `/i#invite=${encodeURIComponent(token)}`,
  });
  return `/login#${fragment.toString()}`;
}

export function InvitePanel({
  mode,
  signedInName,
  signInEnabled,
  onAccept,
  onSignIn,
  onEmail,
  onEnter,
  onRetry,
}: InvitePanelProps): ReactElement {
  const isBusy = mode === "checking" || mode === "redeeming";

  return (
    <main className="invite-page">
      <div className="invite-shell">
        <section className="invite-brand" aria-label="Z-SPAN invitation">
          <svg
            className="invite-sun"
            viewBox="0 0 640 760"
            aria-hidden="true"
          >
            <circle cx="420" cy="610" r="300" />
            <line x1="42" y1="610" x2="112" y2="610" />
            <line x1="150" y1="340" x2="200" y2="390" />
            <line x1="420" y1="232" x2="420" y2="302" />
          </svg>

          <div className="invite-sign" aria-label="Z-SPAN">
            Z-SPAN
          </div>

          <div className="invite-brand-copy">
            <p>Invitation only</p>
            <h2>A virtual library<br />for local politics.</h2>
          </div>
        </section>

        <section className="invite-entry" aria-live="polite" aria-busy={isBusy}>
          <div className="invite-entry-inner">
            <p className="invite-kicker">A personal invitation</p>
            <h1>Welcome</h1>
            <p className="invite-lede">
              This invitation unlocks free, invitation-only access to Z-SPAN.
            </p>

            <div className="invite-rule" aria-hidden="true" />

            {mode === "checking" && (
              <div className="invite-message">
                <span className="invite-pulse" aria-hidden="true" />
                Checking your invitation…
              </div>
            )}

            {mode === "available" && signedInName && (
              <>
                <p className="invite-message">
                  Signed in as {signedInName}. Your card is ready to accept.
                </p>
                <button className="invite-primary" type="button" onClick={onAccept}>
                  Accept invitation
                </button>
              </>
            )}

            {mode === "available" && !signedInName && signInEnabled && (
              <>
                <p className="invite-message">
                  Choose Google, or use an email and password. Either way, this
                  invitation will belong to one account.
                </p>
                <div className="invite-actions">
                  <button className="invite-primary" type="button" onClick={onSignIn}>
                    Continue with Google
                  </button>
                  <button className="invite-secondary" type="button" onClick={onEmail}>
                    Use email and password
                  </button>
                </div>
              </>
            )}

            {mode === "available" && !signedInName && !signInEnabled && (
              <>
                <p className="invite-message">
                  Invitation entry is temporarily unavailable. Your card will remain valid.
                </p>
                <button className="invite-secondary" type="button" onClick={onRetry}>
                  Try again
                </button>
              </>
            )}

            {mode === "redeeming" && (
              <div className="invite-message">
                <span className="invite-pulse" aria-hidden="true" />
                Opening the library…
              </div>
            )}

            {mode === "granted" && (
              <>
                <p className="invite-success">You’re in.</p>
                <p className="invite-message">
                  Your free invitation access is now active.
                </p>
                <button className="invite-primary" type="button" onClick={onEnter}>
                  Enter the library
                </button>
              </>
            )}

            {mode === "unavailable" && (
              <>
                <p className="invite-unavailable">This invitation is not available.</p>
                <p className="invite-message">
                  If you received this card personally and believe this is a mistake,
                  use the contact address printed on your card.
                </p>
              </>
            )}

            {mode === "error" && (
              <>
                <p className="invite-unavailable">
                  We couldn’t check this invitation just now.
                </p>
                <button className="invite-secondary" type="button" onClick={onRetry}>
                  Try again
                </button>
              </>
            )}

            <p className="invite-footnote">No payment or subscription is required.</p>
          </div>
        </section>
      </div>
    </main>
  );
}

export default function InvitePage({ token, onNavigate }: InvitePageProps): ReactElement {
  const currentUser = useCurrentUser();
  const [mode, setMode] = useState<InviteMode>("checking");

  const checkInvitation = useCallback(async () => {
    setMode("checking");
    try {
      const response = await fetch("/api/invitations/status", {
        method: "POST",
        credentials: "include",
        cache: "no-store",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
      const body = await response.json().catch(() => null);
      if (!response.ok || !body?.success) {
        setMode("error");
        return;
      }
      setMode(body.available ? "available" : "unavailable");
    } catch {
      setMode("error");
    }
  }, [token]);

  useEffect(() => {
    void checkInvitation();
  }, [checkInvitation]);

  const acceptInvitation = useCallback(async () => {
    if (!currentUser.user) return;
    setMode("redeeming");
    try {
      const response = await fetch("/api/invitations/redeem", {
        method: "POST",
        credentials: "include",
        cache: "no-store",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
      const body = await response.json().catch(() => null);
      if (response.ok && body?.success && body?.status === "granted") {
        currentUser.refresh();
        setMode("granted");
        return;
      }
      if (response.status === 409) {
        setMode("unavailable");
        return;
      }
      setMode("error");
    } catch {
      setMode("error");
    }
  }, [currentUser, token]);

  const signedInName = currentUser.user
    ? currentUser.user.display_name || currentUser.user.email
    : null;
  const displayMode = currentUser.user?.librarian_access === "granted"
    ? "granted"
    : currentUser.loading && (mode === "available" || mode === "unavailable")
      ? "checking"
      : mode;

  return (
    <InvitePanel
      mode={displayMode}
      signedInName={signedInName}
      signInEnabled={currentUser.signInEnabled}
      onAccept={() => void acceptInvitation()}
      onSignIn={() => {
        try {
          window.sessionStorage.setItem(PENDING_INVITATION_STORAGE_KEY, token);
          window.location.assign(invitationSignInUrl());
        } catch {
          // A browser that blocks session storage cannot safely carry a
          // bearer through the cross-origin OAuth round trip. Keep the token
          // browser-only and offer the same invitation via local account setup.
          window.location.assign(invitationEmailUrl(token));
        }
      }}
      onEmail={() => {
        window.location.assign(invitationEmailUrl(token));
      }}
      onEnter={() => onNavigate("home")}
      onRetry={() => void checkInvitation()}
    />
  );
}
