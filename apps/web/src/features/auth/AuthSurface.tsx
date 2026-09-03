import { type FormEvent, useId } from "react";
import type { LoginRequest, RegisterRequest } from "../../api/generated";
import "./auth.css";

export type AuthMode = "login" | "register";
export type AuthViewStatus = "idle" | "submitting" | "success" | "error" | "offline";

export interface AuthSurfaceError {
  message: string;
  requestId?: string | null;
  title: string;
}

export interface AuthSurfaceProps {
  error?: AuthSurfaceError;
  mode: AuthMode;
  onModeChange?: (mode: AuthMode) => void;
  onSubmit: (credentials: LoginRequest | RegisterRequest) => void;
  status?: AuthViewStatus;
}

const CONTENT = {
  login: {
    eyebrow: "Welcome back",
    heading: "Log in",
    intro: "Return to a recommendation workspace shaped by your recent activity.",
    passwordAutocomplete: "current-password",
    submit: "Log in",
    submitting: "Logging in...",
    switchAction: "Create a user account",
    switchCopy: "New to MicroLens?",
  },
  register: {
    eyebrow: "New account",
    heading: "Create your account",
    intro: "Registration creates a standard user account. Elevated roles stay admin-managed.",
    passwordAutocomplete: "new-password",
    submit: "Create account",
    submitting: "Creating account...",
    switchAction: "Log in",
    switchCopy: "Already registered?",
  },
} as const;

export function AuthSurface({
  error,
  mode,
  onModeChange,
  onSubmit,
  status = "idle",
}: AuthSurfaceProps) {
  const usernameId = useId();
  const passwordId = useId();
  const content = CONTENT[mode];
  const isSubmitting = status === "submitting";

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (isSubmitting) return;
    const form = new FormData(event.currentTarget);
    onSubmit({
      password: String(form.get("password") ?? ""),
      username: String(form.get("username") ?? ""),
    });
  }

  return (
    <div className="ml-auth" data-mode={mode} data-status={status}>
      <section className="ml-auth__surface" aria-labelledby="ml-auth-product">
        <header className="ml-auth__intro">
          <p className="ml-auth__kicker">Recommendation workspace</p>
          <h1 id="ml-auth-product">MicroLens</h1>
          <p>Sign in or create a standard account to continue.</p>
        </header>

        <div className="ml-auth__form-pane">
          <div className="ml-auth__heading">
            <p>{content.eyebrow}</p>
            <h2 id="ml-auth-title">{content.heading}</h2>
            <span>{content.intro}</span>
          </div>

          {status === "success" ? (
            <p className="ml-auth__notice ml-auth__notice--success" role="status">
              Account created. Log in with your new credentials.
            </p>
          ) : null}
          {status === "offline" ? (
            <p className="ml-auth__notice ml-auth__notice--offline" role="status">
              You appear to be offline. Check your connection before trying again.
            </p>
          ) : null}
          {status === "error" && error ? (
            <div className="ml-auth__notice ml-auth__notice--error" role="alert">
              <strong>{error.title}</strong>
              <span>{error.message}</span>
              {error.requestId ? <code>Request {error.requestId}</code> : null}
            </div>
          ) : null}

          <form className="ml-auth__form" onSubmit={handleSubmit}>
            <label htmlFor={usernameId}>Username</label>
            <input
              autoComplete="username"
              id={usernameId}
              maxLength={64}
              minLength={3}
              name="username"
              required
              type="text"
            />

            <div className="ml-auth__label-row">
              <label htmlFor={passwordId}>Password</label>
              {mode === "register" ? <span>12 characters minimum</span> : null}
            </div>
            <input
              autoComplete={content.passwordAutocomplete}
              id={passwordId}
              maxLength={256}
              minLength={mode === "register" ? 12 : undefined}
              name="password"
              required
              type="password"
            />

            <button disabled={isSubmitting || status === "offline"} type="submit">
              {isSubmitting ? content.submitting : content.submit}
            </button>
          </form>

          {onModeChange ? (
            <p className="ml-auth__switch">
              {content.switchCopy}{" "}
              <button
                onClick={() => onModeChange(mode === "login" ? "register" : "login")}
                type="button"
              >
                {content.switchAction}
              </button>
            </p>
          ) : null}
        </div>
      </section>
    </div>
  );
}
