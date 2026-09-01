import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import type { AuthApi } from "../api/auth-api";
import type { Role, User } from "../api/generated";
import { ApiError } from "../api/http";
import { AppRoutes } from "../App";
import { SessionProvider } from "./session";

function user(role: Role): User {
  return {
    created_at: "2026-09-01T00:00:00Z",
    id: `id-${role}`,
    role,
    status: "enabled",
    username: `${role}-demo`,
  };
}

function unauthorized(): ApiError {
  return new ApiError("Authentication required", {
    code: "AUTH_REQUIRED",
    kind: "unauthorized",
    status: 401,
  });
}

function mockAuthApi(currentUser: User | Error): AuthApi {
  return {
    getCurrentUser: vi.fn(async () => {
      if (currentUser instanceof Error) throw currentUser;
      return currentUser;
    }),
    login: vi.fn(async () => user("operator")),
    logout: vi.fn(async () => undefined),
    register: vi.fn(async () => user("user")),
  };
}

function renderRoute(path: string, api: AuthApi) {
  return render(
    <SessionProvider api={api}>
      <MemoryRouter initialEntries={[path]}>
        <AppRoutes />
      </MemoryRouter>
    </SessionProvider>,
  );
}

describe("session hydration and protected routing", () => {
  it("keeps protected content hidden until hydration resolves", async () => {
    let resolveUser: ((value: User) => void) | undefined;
    const pendingUser = new Promise<User>((resolve) => {
      resolveUser = resolve;
    });
    const api = mockAuthApi(user("user"));
    api.getCurrentUser = vi.fn(() => pendingUser);

    renderRoute("/", api);
    expect(screen.getByRole("status").textContent).toContain("Loading your workspace");

    await act(async () => resolveUser?.(user("user")));
    expect(await screen.findByRole("heading", { name: "Your feed" })).toBeTruthy();
  });

  it("redirects an anonymous protected request to login", async () => {
    renderRoute("/operations?tab=scheduled", mockAuthApi(unauthorized()));
    expect(await screen.findByRole("heading", { name: "Log in" })).toBeTruthy();
  });

  it.each([
    ["user", false],
    ["operator_readonly", false],
    ["operator", true],
    ["admin", true],
  ] as const)("enforces the operations route for %s", async (role, allowed) => {
    renderRoute("/operations", mockAuthApi(user(role)));
    if (allowed) {
      expect(await screen.findByRole("heading", { name: "Operations" })).toBeTruthy();
    } else {
      expect(
        await screen.findByRole("heading", { name: "This area is not available for your role" }),
      ).toBeTruthy();
    }
  });

  it("restores an internal intended path after login", async () => {
    const api = mockAuthApi(unauthorized());
    renderRoute("/login?from=%2Foperations%3Ftab%3Dscheduled", api);

    expect(await screen.findByRole("heading", { name: "Log in" })).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "operator-demo" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "secure-password" } });
    fireEvent.click(screen.getByRole("button", { name: "Log in" }));

    expect(await screen.findByRole("heading", { name: "Operations" })).toBeTruthy();
    expect(api.login).toHaveBeenCalledWith({
      password: "secure-password",
      username: "operator-demo",
    });
  });

  it("never restores an external intended destination", async () => {
    const api = mockAuthApi(unauthorized());
    renderRoute("/login?from=https%3A%2F%2Fattacker.test%2Fsteal", api);

    expect(await screen.findByRole("heading", { name: "Log in" })).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "operator-demo" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "secure-password" } });
    fireEvent.click(screen.getByRole("button", { name: "Log in" }));

    expect(await screen.findByRole("heading", { name: "Your feed" })).toBeTruthy();
  });

  it("registration submits only username and password, never a role", async () => {
    const api = mockAuthApi(unauthorized());
    renderRoute("/register", api);

    expect(await screen.findByRole("heading", { name: "Register" })).toBeTruthy();
    expect(screen.queryByLabelText("Role")).toBeNull();
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "new-user" } });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "secure-password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));

    expect(await screen.findByText("Account created. Log in with your new credentials.")).toBeTruthy();
    expect(api.register).toHaveBeenCalledWith({
      password: "secure-password",
      username: "new-user",
    });
  });
});
