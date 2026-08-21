import { useMemo, useState, type FormEvent, type ReactElement } from "react";

import { useCurrentUser } from "../hooks/useCurrentUser";
import {
  invitationSignInUrl,
  PENDING_INVITATION_STORAGE_KEY,
} from "./InvitePage";
import "./invite-page.css";

type LoginMode = "login" | "register" | "forgot" | "forgot-sent" | "reset";

interface LoginPageProps {
  invitationToken?: string;
  resetToken?: string;
  nextPath?: string;
  onNavigate: (view: string, params?: unknown) => void;
}

interface ApiBody {
  success?: boolean;
  message?: string;
}

export function safeLoginNext(nextPath?: string): string {
  if (
    !nextPath
    || !nextPath.startsWith("/")
    || nextPath.startsWith("//")
    || nextPath.includes("\\")
    || nextPath.includes("://")
  ) {
    return "/";
  }
  return nextPath;
}

export function googleLoginUrl(nextPath?: string): string {
  return `/api/auth/google/login?next=${encodeURIComponent(safeLoginNext(nextPath))}`;
}

async function postAccountForm(path: string, body: object): Promise<ApiBody> {
  const response = await fetch(path, {
    method: "POST",
    credentials: "include",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({})) as ApiBody;
  if (!response.ok) {
    throw new Error(payload.message || "That didn’t work. Please try again.");
  }
  return payload;
}

function AccountBrand(): ReactElement {
  return (
    <section className="invite-brand login-brand" aria-label="Z-SPAN">
      <svg className="invite-sun" viewBox="0 0 640 760" aria-hidden="true">
        <circle cx="420" cy="610" r="300" />
        <line x1="42" y1="610" x2="112" y2="610" />
        <line x1="150" y1="340" x2="200" y2="390" />
        <line x1="420" y1="232" x2="420" y2="302" />
      </svg>
      <div className="invite-sign" aria-label="Z-SPAN">Z-SPAN</div>
      <div className="invite-brand-copy">
        <p>Your library account</p>
        <h2>A virtual library<br />for local politics.</h2>
      </div>
    </section>
  );
}

export default function LoginPage({
  invitationToken,
  resetToken,
  nextPath,
  onNavigate,
}: LoginPageProps): ReactElement {
  const currentUser = useCurrentUser();
  const [mode, setMode] = useState<LoginMode>(
    resetToken ? "reset" : invitationToken ? "register" : "login",
  );
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [manualInvitation, setManualInvitation] = useState(invitationToken || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const returnPath = useMemo(
    () => invitationToken
      ? `/i#invite=${encodeURIComponent(invitationToken)}`
      : safeLoginNext(nextPath),
    [invitationToken, nextPath],
  );

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);
    if ((mode === "register" || mode === "reset") && password !== confirmPassword) {
      setError("The two passwords do not match.");
      return;
    }
    setBusy(true);
    try {
      if (mode === "register") {
        await postAccountForm("/api/auth/password/register", {
          email,
          display_name: displayName,
          password,
          invitation_token: invitationToken,
        });
        window.location.assign(returnPath);
        return;
      }
      if (mode === "login") {
        await postAccountForm("/api/auth/password/login", {
          email,
          password,
          invitation_token: invitationToken || undefined,
        });
        window.location.assign(returnPath);
        return;
      }
      if (mode === "forgot") {
        await postAccountForm("/api/auth/password/forgot", { email });
        setMode("forgot-sent");
        return;
      }
      if (mode === "reset") {
        await postAccountForm("/api/auth/password/reset", {
          token: resetToken,
          password,
        });
        window.location.assign("/");
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Please try again.");
    } finally {
      setBusy(false);
    }
  };

  const useManualInvitation = () => {
    const token = manualInvitation.trim();
    if (!/^[A-Za-z0-9_-]{24,64}$/.test(token)) {
      setError("Enter the invitation code exactly as it was given to you.");
      return;
    }
    const next = safeLoginNext(nextPath);
    const fragment = new URLSearchParams({ invite: token, next });
    window.location.assign(`/login#${fragment.toString()}`);
  };

  const preserveInvitationForGoogle = () => {
    if (!invitationToken) return true;
    try {
      window.sessionStorage.setItem(
        PENDING_INVITATION_STORAGE_KEY,
        invitationToken,
      );
      return true;
    } catch {
      setError(
        "This browser cannot preserve your invitation through Google login. "
        + "Use email and password instead.",
      );
      return false;
    }
  };

  let title = "Log in";
  let lede = "Use Google or your Z-SPAN email and password.";
  if (mode === "register") {
    title = "Welcome";
    lede = "Create the account that will hold your invitation-only access.";
  } else if (mode === "forgot" || mode === "forgot-sent") {
    title = "Reset your password";
    lede = "We’ll send a one-time link if that email has a password account.";
  } else if (mode === "reset") {
    title = "Choose a new password";
    lede = "Use a memorable phrase of at least 15 characters.";
  }

  return (
    <main className="invite-page login-page">
      <div className="invite-shell login-shell">
        <AccountBrand />
        <section className="invite-entry login-entry">
          <div className="invite-entry-inner login-entry-inner">
            <p className="invite-kicker">
              {invitationToken ? "Invitation recognized" : "One account, your choice"}
            </p>
            <h1>{title}</h1>
            <p className="invite-lede">{lede}</p>
            <div className="invite-rule" aria-hidden="true" />

            {!currentUser.loading && currentUser.user && mode !== "reset" ? (
              <div className="login-signed-in">
                <p className="invite-message">
                  You’re already logged in as {currentUser.user.display_name || currentUser.user.email}.
                </p>
                <button
                  className="invite-primary"
                  type="button"
                  onClick={() => window.location.assign(returnPath)}
                >
                  Continue
                </button>
              </div>
            ) : !currentUser.loading && !currentUser.signInEnabled ? (
              <p className="invite-message">
                Login is temporarily paused. Please try again later.
              </p>
            ) : (
              <>
                {(mode === "login" || mode === "register") && (
                  <>
                    <a
                      className="login-google"
                      href={invitationToken
                        ? invitationSignInUrl()
                        : googleLoginUrl(returnPath)}
                      onClick={(event) => {
                        if (!preserveInvitationForGoogle()) event.preventDefault();
                      }}
                    >
                      Continue with Google
                    </a>
                    <div className="login-divider"><span>or</span></div>
                  </>
                )}

                {mode === "forgot-sent" ? (
                  <>
                    <p className="invite-message">
                      If that email has a password account, a reset link is on its way.
                    </p>
                    <button
                      className="invite-secondary"
                      type="button"
                      onClick={() => setMode("login")}
                    >
                      Back to login
                    </button>
                  </>
                ) : (
                  <form className="login-form" onSubmit={(event) => void submit(event)}>
                    {mode === "register" && (
                      <label>
                        <span>Your name</span>
                        <input
                          autoComplete="name"
                          maxLength={80}
                          required
                          value={displayName}
                          onChange={(event) => setDisplayName(event.target.value)}
                        />
                      </label>
                    )}

                    {mode !== "reset" && (
                      <label>
                        <span>Email</span>
                        <input
                          type="email"
                          autoComplete="email"
                          maxLength={254}
                          required
                          value={email}
                          onChange={(event) => setEmail(event.target.value)}
                        />
                      </label>
                    )}

                    {mode !== "forgot" && (
                      <label>
                        <span>{mode === "reset" ? "New password" : "Password"}</span>
                        <input
                          type="password"
                          autoComplete={mode === "login" ? "current-password" : "new-password"}
                          minLength={mode === "login" ? undefined : 15}
                          maxLength={256}
                          required
                          value={password}
                          onChange={(event) => setPassword(event.target.value)}
                        />
                      </label>
                    )}

                    {(mode === "register" || mode === "reset") && (
                      <label>
                        <span>Confirm password</span>
                        <input
                          type="password"
                          autoComplete="new-password"
                          minLength={15}
                          maxLength={256}
                          required
                          value={confirmPassword}
                          onChange={(event) => setConfirmPassword(event.target.value)}
                        />
                      </label>
                    )}

                    {error && <p className="login-error" role="alert">{error}</p>}
                    <button className="invite-primary login-submit" type="submit" disabled={busy}>
                      {busy
                        ? "Please wait…"
                        : mode === "register"
                          ? "Create account and enter"
                          : mode === "forgot"
                            ? "Send reset link"
                            : mode === "reset"
                              ? "Save new password"
                              : "Log in"}
                    </button>
                  </form>
                )}

                {mode === "login" && (
                  <button className="login-text-button" type="button" onClick={() => setMode("forgot")}>
                    Forgot your password?
                  </button>
                )}
                {mode === "forgot" && (
                  <button className="login-text-button" type="button" onClick={() => setMode("login")}>
                    Back to login
                  </button>
                )}
                {mode === "register" && (
                  <button className="login-text-button" type="button" onClick={() => setMode("login")}>
                    Already have an account? Log in
                  </button>
                )}

                {mode === "login" && !invitationToken && (
                  <div className="login-invitation-entry">
                    <p>Have an invitation code?</p>
                    <div>
                      <input
                        aria-label="Invitation code"
                        autoComplete="off"
                        maxLength={64}
                        value={manualInvitation}
                        onChange={(event) => setManualInvitation(event.target.value)}
                      />
                      <button type="button" onClick={useManualInvitation}>Use code</button>
                    </div>
                  </div>
                )}
              </>
            )}

            <p className="invite-footnote">
              Invited access is free. Creator and verification roles are assigned separately.
            </p>
            <button className="login-home-link" type="button" onClick={() => onNavigate("home")}>
              Return to the library
            </button>
          </div>
        </section>
      </div>
    </main>
  );
}
