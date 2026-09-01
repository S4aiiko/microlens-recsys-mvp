import { useState } from "react";
import {
  Link,
  Navigate,
  NavLink,
  Outlet,
  useLocation,
  useNavigate,
  useSearchParams,
} from "react-router";
import type { Capability } from "../auth/capabilities";
import { hasCapability } from "../auth/capabilities";
import { useSession } from "../auth/session";
import { ErrorState, ForbiddenState, LoadingState } from "../components/AsyncStates";
import { getSafeIntendedPath } from "./intended-path";

export function AnonymousLayout() {
  const { refresh, state } = useSession();
  const [searchParams] = useSearchParams();

  if (state.status === "hydrating") return <LoadingState label="Checking your session" />;
  if (state.status === "error") {
    return (
      <main className="centered-shell">
        <ErrorState
          message={state.error.message}
          onRetry={() => void refresh()}
          requestId={state.error.requestId}
          title="Session check failed"
        />
      </main>
    );
  }
  if (state.status === "authenticated") {
    return <Navigate replace to={getSafeIntendedPath(searchParams.get("from"))} />;
  }

  return (
    <main className="auth-shell">
      <section className="auth-intro" aria-labelledby="product-name">
        <Link className="brand" id="product-name" to="/login">
          MicroLens
        </Link>
        <p className="eyebrow">Recommendation MVP</p>
        <h1>One secure session for discovery and operations.</h1>
        <p>
          The browser carries the HttpOnly session cookie. Credentials are never copied into web
          storage.
        </p>
      </section>
      <section className="auth-card">
        <Outlet />
      </section>
    </main>
  );
}

export function AuthenticatedLayout() {
  const location = useLocation();
  const { refresh, state } = useSession();

  if (state.status === "hydrating") return <LoadingState label="Loading your workspace" />;
  if (state.status === "error") {
    return (
      <main className="centered-shell">
        <ErrorState
          message={state.error.message}
          onRetry={() => void refresh()}
          requestId={state.error.requestId}
          title="Workspace unavailable"
        />
      </main>
    );
  }
  if (state.status === "anonymous") {
    const intendedPath = `${location.pathname}${location.search}${location.hash}`;
    return <Navigate replace to={`/login?from=${encodeURIComponent(intendedPath)}`} />;
  }

  return <Outlet />;
}

export function UserLayout() {
  const navigate = useNavigate();
  const { logout, state } = useSession();
  const [logoutError, setLogoutError] = useState<string | null>(null);

  if (state.status !== "authenticated") return null;
  const { user } = state;

  async function handleLogout() {
    setLogoutError(null);
    try {
      await logout();
      navigate("/login", { replace: true });
    } catch (error) {
      setLogoutError(error instanceof Error ? error.message : "Logout failed.");
    }
  }

  return (
    <div className="app-shell">
      <header className="app-header">
        <Link className="brand" to="/">
          MicroLens
        </Link>
        <nav aria-label="Primary navigation" className="primary-nav">
          <NavLink end to="/">
            Feed
          </NavLink>
          {hasCapability(user.role, "dashboardRead") ? (
            <NavLink to="/dashboard">Dashboard</NavLink>
          ) : null}
          {hasCapability(user.role, "operationsWrite") ? (
            <NavLink to="/operations">Operations</NavLink>
          ) : null}
          {hasCapability(user.role, "roleManagement") ? (
            <NavLink to="/admin/users">Roles</NavLink>
          ) : null}
        </nav>
        <div className="session-summary">
          <span>
            <strong>{user.username}</strong>
            <small>{user.role}</small>
          </span>
          <button className="button button--ghost" onClick={() => void handleLogout()} type="button">
            Log out
          </button>
        </div>
      </header>
      {logoutError ? <p className="inline-error" role="alert">{logoutError}</p> : null}
      <main className="app-main">
        <Outlet />
      </main>
    </div>
  );
}

export function RequireCapability({ capability }: { capability: Capability }) {
  const { state } = useSession();
  if (state.status !== "authenticated") return null;
  return hasCapability(state.user.role, capability) ? <Outlet /> : <ForbiddenState />;
}

export function OperationsLayout() {
  return (
    <section aria-labelledby="operations-title" className="workspace-stack">
      <div className="page-heading">
        <p className="eyebrow">Protected workspace</p>
        <h1 id="operations-title">Operations</h1>
        <p>Write controls require an operator or administrator role and server authorization.</p>
      </div>
      <Outlet />
    </section>
  );
}
