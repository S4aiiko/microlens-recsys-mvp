import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import type { AuthApi } from "../api/auth-api";
import type { Role, User } from "../api/generated";
import { ApiError } from "../api/http";
import { AppRoutes } from "../App";
import type { AdminApi } from "../features/admin";
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

function pendingAdminApi(): AdminApi {
  const pending = new Promise<never>(() => undefined);
  return {
    getFeedDiagnostics: vi.fn(() => pending),
    getHotItems: vi.fn(() => pending),
    getOverview: vi.fn(() => pending),
    getTimeseries: vi.fn(() => pending),
    listModels: vi.fn(() => pending),
    listOperations: vi.fn(() => pending),
    listTrainingJobs: vi.fn(() => pending),
    listUsers: vi.fn(() => pending),
    searchItems: vi.fn(() => pending),
  } as unknown as AdminApi;
}

function renderRoute(path: string, api: AuthApi, adminApi: AdminApi = pendingAdminApi()) {
  return render(
    <SessionProvider api={api}>
      <MemoryRouter initialEntries={[path]}>
        <AppRoutes adminApi={adminApi} />
      </MemoryRouter>
    </SessionProvider>,
  );
}

describe("session hydration and protected routing", () => {
  it.each(["/login", "/register"])(
    "renders one auth surface and one main landmark at %s",
    async (path) => {
      const { container } = renderRoute(path, mockAuthApi(unauthorized()));

      expect(await screen.findByRole("heading", { name: "MicroLens" })).toBeTruthy();
      expect(container.querySelectorAll("main")).toHaveLength(1);
      expect(container.querySelectorAll(".ml-auth__surface")).toHaveLength(1);
      expect(container.querySelector(".auth-shell")).toBeNull();
      expect(container.querySelector(".auth-intro")).toBeNull();
      expect(container.querySelector(".auth-card")).toBeNull();
    },
  );

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
    ["user", ["Feed"]],
    ["operator_readonly", ["Feed", "Dashboard", "Operations"]],
    ["operator", ["Feed", "Dashboard", "Operations"]],
    ["admin", ["Feed", "Dashboard", "Operations", "Roles"]],
  ] as const)("renders the exact navigation for %s", async (role, expectedLinks) => {
    renderRoute("/dashboard", mockAuthApi(user(role)));
    const navigation = await screen.findByRole("navigation", { name: "Primary navigation" });
    expect(within(navigation).getAllByRole("link").map((link) => link.textContent)).toEqual(
      expectedLinks,
    );
  });

  it.each([
    ["/dashboard", "Dashboard", "user", false],
    ["/dashboard", "Dashboard", "operator_readonly", true],
    ["/dashboard", "Dashboard", "operator", true],
    ["/dashboard", "Dashboard", "admin", true],
    ["/operations", "Operations", "user", false],
    ["/operations", "Operations", "operator_readonly", true],
    ["/operations", "Operations", "operator", true],
    ["/operations", "Operations", "admin", true],
    ["/admin/users", "Role management", "user", false],
    ["/admin/users", "Role management", "operator_readonly", false],
    ["/admin/users", "Role management", "operator", false],
    ["/admin/users", "Role management", "admin", true],
  ] as const)("guards direct %s access for %s", async (path, heading, role, allowed) => {
    renderRoute(path, mockAuthApi(user(role)));
    if (allowed) {
      expect(await screen.findByRole("heading", { name: heading })).toBeTruthy();
    } else {
      expect(
        await screen.findByRole("heading", { name: "This area is not available for your role" }),
      ).toBeTruthy();
    }
  });

  it.each([
    ["operator_readonly", false],
    ["operator", true],
    ["admin", true],
  ] as const)("exposes operations write controls correctly for %s", async (role, canWrite) => {
    renderRoute("/operations", mockAuthApi(user(role)));
    expect(await screen.findByRole("heading", { name: "Operations" })).toBeTruthy();
    if (canWrite) {
      expect(screen.getByRole("button", { name: "Submit operation" })).toBeTruthy();
      expect(screen.queryByText("Read-only operations view")).toBeNull();
    } else {
      expect(screen.queryByRole("button", { name: "Submit operation" })).toBeNull();
      expect(screen.getByText("Read-only operations view")).toBeTruthy();
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

    expect(await screen.findByRole("heading", { name: "Create your account" })).toBeTruthy();
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
