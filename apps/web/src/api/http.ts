import type { ErrorEnvelope } from "./generated";
import { createClient, type Client, type ResolvedRequestOptions } from "./generated/client";

export const CSRF_HEADER_NAME = "X-CSRF-Token";
export const DEFAULT_CSRF_COOKIE_NAME = "microlens_csrf";

const UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export type ApiErrorKind =
  | "api"
  | "csrf"
  | "forbidden"
  | "network"
  | "unauthorized"
  | "unknown";

export class ApiError extends Error {
  readonly code: string;
  readonly details: ErrorEnvelope["details"];
  readonly kind: ApiErrorKind;
  readonly requestId: string | null;
  readonly status: number | null;

  constructor(
    message: string,
    options: {
      code: string;
      details?: ErrorEnvelope["details"];
      kind: ApiErrorKind;
      requestId?: string | null;
      status?: number | null;
    },
  ) {
    super(message);
    this.name = "ApiError";
    this.code = options.code;
    this.details = options.details ?? null;
    this.kind = options.kind;
    this.requestId = options.requestId ?? null;
    this.status = options.status ?? null;
  }
}

export interface ApiClientOptions {
  apiOrigin?: string;
  baseUrl?: string;
  cookieSource?: () => string;
  csrfCookieName?: string;
  fetch?: typeof fetch;
}

interface CsrfProtectionOptions {
  apiOrigin: string;
  cookieSource: () => string;
  csrfCookieName: string;
}

function browserOrigin(): string {
  return typeof window === "undefined" ? "http://localhost" : window.location.origin;
}

export function normalizeBaseUrl(value: string): string {
  const url = new URL(value, browserOrigin());
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new ApiError("API base URL must use HTTP or HTTPS.", {
      code: "INVALID_API_BASE_URL",
      kind: "unknown",
    });
  }
  url.hash = "";
  url.search = "";
  return url.toString().replace(/\/$/, "");
}

export function getApiBaseUrl(): string {
  return normalizeBaseUrl(import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000");
}

export function getCookieValue(cookieHeader: string, name: string): string | undefined {
  for (const part of cookieHeader.split(";")) {
    const separator = part.indexOf("=");
    if (separator < 0) continue;
    const key = part.slice(0, separator).trim();
    if (key !== name) continue;
    const rawValue = part.slice(separator + 1).trim();
    try {
      return decodeURIComponent(rawValue);
    } catch {
      return undefined;
    }
  }
  return undefined;
}

function requiresCsrf(options: ResolvedRequestOptions): boolean {
  return Boolean(
    options.security?.some(
      (security) =>
        security.type === "apiKey" &&
        security.in !== "cookie" &&
        security.name?.toLowerCase() === CSRF_HEADER_NAME.toLowerCase(),
    ),
  );
}

export function protectCsrfRequest(
  request: Request,
  csrfRequired: boolean,
  options: CsrfProtectionOptions,
): Request {
  if (!UNSAFE_METHODS.has(request.method.toUpperCase()) || !csrfRequired) return request;

  if (new URL(request.url).origin !== options.apiOrigin) {
    throw new ApiError("Refusing to send a CSRF-protected request to another origin.", {
      code: "CSRF_ORIGIN_MISMATCH",
      kind: "csrf",
    });
  }

  const token = getCookieValue(options.cookieSource(), options.csrfCookieName);
  if (!token) {
    throw new ApiError("The CSRF token is missing. Refresh the session and try again.", {
      code: "CSRF_TOKEN_MISSING",
      kind: "csrf",
    });
  }

  const headers = new Headers(request.headers);
  headers.set(CSRF_HEADER_NAME, token);
  return new Request(request, { headers });
}

export function createConfiguredApiClient(options: ApiClientOptions = {}): Client {
  const baseUrl = normalizeBaseUrl(options.baseUrl ?? getApiBaseUrl());
  const apiOrigin = new URL(options.apiOrigin ?? baseUrl, browserOrigin()).origin;
  const cookieSource =
    options.cookieSource ?? (() => (typeof document === "undefined" ? "" : document.cookie));
  const csrfCookieName =
    options.csrfCookieName ??
    import.meta.env.VITE_CSRF_COOKIE_NAME ??
    DEFAULT_CSRF_COOKIE_NAME;

  const configuredClient = createClient({
    auth: () => undefined,
    baseUrl,
    credentials: "include",
    fetch: options.fetch,
  });

  configuredClient.interceptors.request.use((request, requestOptions) =>
    protectCsrfRequest(request, requiresCsrf(requestOptions), {
      apiOrigin,
      cookieSource,
      csrfCookieName,
    }),
  );

  return configuredClient;
}

function isErrorEnvelope(value: unknown): value is ErrorEnvelope {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<ErrorEnvelope>;
  return (
    typeof candidate.code === "string" &&
    typeof candidate.message === "string" &&
    (typeof candidate.request_id === "string" || candidate.request_id === null) &&
    "details" in candidate
  );
}

function kindForStatus(status: number | null): ApiErrorKind {
  if (status === 401) return "unauthorized";
  if (status === 403) return "forbidden";
  return status === null ? "network" : "api";
}

export function toApiError(error: unknown, response?: Response): ApiError {
  if (error instanceof ApiError) return error;
  const status = response?.status ?? null;
  if (isErrorEnvelope(error)) {
    return new ApiError(error.message, {
      code: error.code,
      details: error.details,
      kind: kindForStatus(status),
      requestId: error.request_id,
      status,
    });
  }
  if (error instanceof Error) {
    return new ApiError(error.message || "The API request failed.", {
      code: status === null ? "NETWORK_ERROR" : "API_ERROR",
      kind: kindForStatus(status),
      status,
    });
  }
  return new ApiError("The API request failed.", {
    code: status === null ? "NETWORK_ERROR" : "API_ERROR",
    kind: kindForStatus(status),
    status,
  });
}

export const apiClient = createConfiguredApiClient();
