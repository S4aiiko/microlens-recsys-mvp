import { describe, expect, it, vi } from "vitest";
import {
  ApiError,
  createConfiguredApiClient,
  getCookieValue,
  protectCsrfRequest,
  toApiError,
} from "./http";

const csrfOptions = {
  apiOrigin: "https://api.microlens.test",
  cookieSource: () => "theme=dark; microlens_csrf=token%20value",
  csrfCookieName: "microlens_csrf",
};

describe("CSRF request protection", () => {
  it("does not add the CSRF header to safe methods", () => {
    const request = new Request("https://api.microlens.test/api/auth/me", { method: "GET" });
    const protectedRequest = protectCsrfRequest(request, true, csrfOptions);
    expect(protectedRequest.headers.get("X-CSRF-Token")).toBeNull();
  });

  it("does not require CSRF for public unsafe operations", () => {
    const request = new Request("https://api.microlens.test/api/auth/login", { method: "POST" });
    const protectedRequest = protectCsrfRequest(request, false, csrfOptions);
    expect(protectedRequest.headers.get("X-CSRF-Token")).toBeNull();
  });

  it("adds the readable CSRF cookie only to protected unsafe API requests", () => {
    const request = new Request("https://api.microlens.test/api/auth/logout", { method: "POST" });
    const protectedRequest = protectCsrfRequest(request, true, csrfOptions);
    expect(protectedRequest.headers.get("X-CSRF-Token")).toBe("token value");
    expect(protectedRequest.headers.has("Cookie")).toBe(false);
  });

  it("fails closed without leaking CSRF to a different origin", () => {
    const request = new Request("https://attacker.test/collect", { method: "POST" });
    expect(() => protectCsrfRequest(request, true, csrfOptions)).toThrowError(
      expect.objectContaining({ code: "CSRF_ORIGIN_MISMATCH" }),
    );
    expect(request.headers.get("X-CSRF-Token")).toBeNull();
  });

  it("fails closed when a protected unsafe request has no token", () => {
    const request = new Request("https://api.microlens.test/api/events", { method: "POST" });
    expect(() =>
      protectCsrfRequest(request, true, { ...csrfOptions, cookieSource: () => "" }),
    ).toThrowError(expect.objectContaining({ code: "CSRF_TOKEN_MISSING" }));
  });

  it("parses only the exact named cookie and safely rejects malformed encoding", () => {
    expect(getCookieValue("csrf_extra=no; csrf=yes", "csrf")).toBe("yes");
    expect(getCookieValue("csrf=%E0%A4%A", "csrf")).toBeUndefined();
  });
});

describe("configured generated client", () => {
  it("uses credentialed fetch while leaving the HttpOnly Cookie header browser-managed", async () => {
    let captured: Request | undefined;
    const fetchMock = vi.fn(async (request: RequestInfo | URL) => {
      captured = request as Request;
      return new Response(JSON.stringify({ status: "ok" }), {
        headers: { "Content-Type": "application/json" },
        status: 200,
      });
    }) as typeof fetch;
    const client = createConfiguredApiClient({
      baseUrl: "https://api.microlens.test",
      fetch: fetchMock,
    });

    await client.get({ url: "/health" });

    expect(fetchMock).toHaveBeenCalledOnce();
    expect(captured?.credentials).toBe("include");
    expect(captured?.headers.has("Cookie")).toBe(false);
  });
});

describe("typed API errors", () => {
  const envelope = {
    code: "ACCESS_DENIED",
    details: null,
    message: "No access",
    request_id: "req-42",
  };

  it("keeps 401 and 403 semantically distinct", () => {
    const unauthorized = toApiError(envelope, new Response(null, { status: 401 }));
    const forbidden = toApiError(envelope, new Response(null, { status: 403 }));

    expect(unauthorized).toMatchObject({ kind: "unauthorized", requestId: "req-42", status: 401 });
    expect(forbidden).toMatchObject({ kind: "forbidden", requestId: "req-42", status: 403 });
  });

  it("preserves local CSRF failures", () => {
    const error = new ApiError("missing", { code: "CSRF_TOKEN_MISSING", kind: "csrf" });
    expect(toApiError(error)).toBe(error);
  });
});
