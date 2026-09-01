import { type FormEvent, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import { useSession } from "../auth/session";
import { type ApiError, toApiError } from "../api/http";
import { EmptyState, ErrorState } from "../components/AsyncStates";
import { getSafeIntendedPath } from "./intended-path";

function formError(error: unknown): ApiError {
  return toApiError(error);
}

export function LoginPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { login } = useSession();
  const [error, setError] = useState<ApiError | null>(null);
  const [pending, setPending] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setPending(true);
    const form = new FormData(event.currentTarget);
    try {
      await login({
        password: String(form.get("password") ?? ""),
        username: String(form.get("username") ?? ""),
      });
      navigate(getSafeIntendedPath(searchParams.get("from")), { replace: true });
    } catch (caught) {
      setError(formError(caught));
    } finally {
      setPending(false);
    }
  }

  return (
    <>
      <div className="page-heading">
        <p className="eyebrow">Welcome back</p>
        <h2>Log in</h2>
        <p>Use your MicroLens account to continue.</p>
      </div>
      {searchParams.get("registered") === "1" ? (
        <p className="success-message" role="status">
          Account created. Log in with your new credentials.
        </p>
      ) : null}
      {error ? (
        <ErrorState message={error.message} requestId={error.requestId} title="Login failed" />
      ) : null}
      <form className="auth-form" onSubmit={(event) => void handleSubmit(event)}>
        <label>
          Username
          <input autoComplete="username" name="username" required type="text" />
        </label>
        <label>
          Password
          <input autoComplete="current-password" name="password" required type="password" />
        </label>
        <button className="button" disabled={pending} type="submit">
          {pending ? "Logging in…" : "Log in"}
        </button>
      </form>
      <p className="auth-switch">
        New here? <Link to="/register">Create a user account</Link>
      </p>
    </>
  );
}

export function RegisterPage() {
  const navigate = useNavigate();
  const { register } = useSession();
  const [error, setError] = useState<ApiError | null>(null);
  const [pending, setPending] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setPending(true);
    const form = new FormData(event.currentTarget);
    try {
      await register({
        password: String(form.get("password") ?? ""),
        username: String(form.get("username") ?? ""),
      });
      navigate("/login?registered=1", { replace: true });
    } catch (caught) {
      setError(formError(caught));
    } finally {
      setPending(false);
    }
  }

  return (
    <>
      <div className="page-heading">
        <p className="eyebrow">New account</p>
        <h2>Register</h2>
        <p>Registration always creates a standard user. Elevated roles are admin-managed.</p>
      </div>
      {error ? (
        <ErrorState message={error.message} requestId={error.requestId} title="Registration failed" />
      ) : null}
      <form className="auth-form" onSubmit={(event) => void handleSubmit(event)}>
        <label>
          Username
          <input
            autoComplete="username"
            minLength={3}
            maxLength={64}
            name="username"
            required
            type="text"
          />
        </label>
        <label>
          Password
          <input
            autoComplete="new-password"
            minLength={12}
            maxLength={256}
            name="password"
            required
            type="password"
          />
        </label>
        <button className="button" disabled={pending} type="submit">
          {pending ? "Creating account…" : "Create account"}
        </button>
      </form>
      <p className="auth-switch">
        Already registered? <Link to="/login">Log in</Link>
      </p>
    </>
  );
}

export function FeedFoundationPage() {
  return (
    <section className="workspace-stack" aria-labelledby="feed-title">
      <div className="page-heading">
        <p className="eyebrow">Authenticated workspace</p>
        <h1 id="feed-title">Your feed</h1>
        <p>Recommendation pages will consume the generated public API client in a later phase.</p>
      </div>
      <EmptyState
        description="No recommendation request has been made. This foundation never falls back to fabricated items."
        title="Feed integration is pending"
      />
    </section>
  );
}

export function DashboardFoundationPage() {
  return (
    <section className="workspace-stack" aria-labelledby="dashboard-title">
      <div className="page-heading">
        <p className="eyebrow">Read-only operations insight</p>
        <h1 id="dashboard-title">Dashboard</h1>
        <p>Metrics must come from database-backed API responses.</p>
      </div>
      <EmptyState
        description="No dashboard query has been run. Production code contains no hardcoded metric fallback."
        title="Awaiting dashboard integration"
      />
    </section>
  );
}

export function OperationsFoundationPage() {
  return (
    <EmptyState
      description="Promotion, offline and restore controls will be connected after their API implementation is integrated."
      title="No operation selected"
    />
  );
}

export function RoleManagementFoundationPage() {
  return (
    <section className="workspace-stack" aria-labelledby="roles-title">
      <div className="page-heading">
        <p className="eyebrow">Administrator only</p>
        <h1 id="roles-title">Role management</h1>
      </div>
      <EmptyState
        description="User roles will be loaded from the admin API. The browser does not grant or infer privileges."
        title="No user list loaded"
      />
    </section>
  );
}
