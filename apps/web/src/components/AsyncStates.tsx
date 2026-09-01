import type { ReactNode } from "react";

interface LoadingStateProps {
  label?: string;
}

export function LoadingState({ label = "Loading" }: LoadingStateProps) {
  return (
    <section className="state-panel" aria-busy="true" aria-live="polite" role="status">
      <span className="state-spinner" aria-hidden="true" />
      <p>{label}</p>
    </section>
  );
}

interface EmptyStateProps {
  action?: ReactNode;
  description: string;
  title: string;
}

export function EmptyState({ action, description, title }: EmptyStateProps) {
  return (
    <section className="state-panel state-panel--quiet">
      <h2>{title}</h2>
      <p>{description}</p>
      {action ? <div className="state-action">{action}</div> : null}
    </section>
  );
}

interface ErrorStateProps {
  message: string;
  onRetry?: () => void;
  requestId?: string | null;
  title?: string;
}

export function ErrorState({
  message,
  onRetry,
  requestId,
  title = "Something went wrong",
}: ErrorStateProps) {
  return (
    <section className="state-panel state-panel--error" role="alert">
      <h2>{title}</h2>
      <p>{message}</p>
      {requestId ? <p className="request-id">Request ID: {requestId}</p> : null}
      {onRetry ? (
        <button className="button button--secondary" onClick={onRetry} type="button">
          Try again
        </button>
      ) : null}
    </section>
  );
}

export function ForbiddenState() {
  return (
    <section className="state-panel state-panel--forbidden">
      <p className="eyebrow">403 · Forbidden</p>
      <h1>This area is not available for your role</h1>
      <p>Your session is valid, but the server remains authoritative for every protected action.</p>
    </section>
  );
}

export function NotFoundState() {
  return (
    <main className="centered-shell">
      <section className="state-panel">
        <p className="eyebrow">404 · Not found</p>
        <h1>The requested page does not exist</h1>
        <a className="text-link" href="/">
          Return home
        </a>
      </section>
    </main>
  );
}
