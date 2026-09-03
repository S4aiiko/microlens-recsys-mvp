import type { ReactNode } from "react";
import type { ApiError } from "../../api/http";

export function AdminError({ error, title }: { error: ApiError; title?: string }) {
  const resolvedTitle =
    title ??
    (error.status === 401
      ? "Session expired"
      : error.status === 403
        ? "Server denied this action"
        : error.status === 409
          ? "The operation conflicts with current state"
          : error.status === 422
            ? "The request was not accepted"
            : error.kind === "network"
              ? "The service is unreachable"
              : "The request failed");
  return (
    <div className="admin-alert admin-alert--error" role="alert">
      <strong>{resolvedTitle}</strong>
      <span>{error.message}</span>
      {error.requestId ? <code>Request {error.requestId}</code> : null}
    </div>
  );
}

export function AdminNotice({ children }: { children: ReactNode }) {
  return (
    <div className="admin-alert" role="status">
      {children}
    </div>
  );
}

export function Metric({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="admin-metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

export function StatusBadge({ value }: { value: string }) {
  return <span className={`admin-status admin-status--${value.toLowerCase()}`}>{value}</span>;
}

export function JsonValue({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <span>Not available</span>;
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return <span>{String(value)}</span>;
  }
  return <code className="admin-json">{JSON.stringify(value, null, 2)}</code>;
}

export function formatCount(value: number): string {
  return new Intl.NumberFormat("en-US").format(value);
}

export function formatPercent(value: number): string {
  return new Intl.NumberFormat("en-US", {
    maximumFractionDigits: 2,
    style: "percent",
  }).format(value);
}

export function formatDuration(value: number): string {
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${(value / 1000).toFixed(1)} s`;
}
