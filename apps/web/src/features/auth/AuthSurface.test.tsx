import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AuthSurface } from "./AuthSurface";

describe("AuthSurface", () => {
  it("is composable inside a route-owned main landmark", () => {
    const { container } = render(<AuthSurface mode="login" onSubmit={vi.fn()} />);

    expect(container.querySelector("main")).toBeNull();
    expect(container.querySelector(".ml-auth")?.tagName).toBe("DIV");
    expect(screen.getByRole("heading", { level: 1, name: "MicroLens" })).toBeTruthy();
    expect(screen.getByText("Recommendation workspace")).toBeTruthy();
    expect(screen.queryByText("Signals in. Better choices out.")).toBeNull();
  });

  it("submits only username and password for registration", () => {
    const onSubmit = vi.fn();
    render(<AuthSurface mode="register" onSubmit={onSubmit} />);

    expect(screen.queryByLabelText("Role")).toBeNull();
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "new-user" } });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "secure-password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));

    expect(onSubmit).toHaveBeenCalledWith({
      password: "secure-password",
      username: "new-user",
    });
  });

  it("exposes offline and responsive surface semantics", () => {
    const { container } = render(
      <AuthSurface mode="login" onSubmit={vi.fn()} status="offline" />,
    );

    const root = container.querySelector(".ml-auth");
    expect(root?.getAttribute("data-mode")).toBe("login");
    expect(root?.getAttribute("data-status")).toBe("offline");
    expect(screen.getByRole("status").textContent).toContain("offline");
    expect(screen.getByRole("button", { name: "Log in" }).hasAttribute("disabled")).toBe(true);
  });

  it("shows a request-linked error and changes mode without routing", () => {
    const onModeChange = vi.fn();
    render(
      <AuthSurface
        error={{ message: "Too many attempts", requestId: "req-auth", title: "Try later" }}
        mode="register"
        onModeChange={onModeChange}
        onSubmit={vi.fn()}
        status="error"
      />,
    );

    expect(screen.getByRole("alert").textContent).toContain("Request req-auth");
    fireEvent.click(screen.getByRole("button", { name: "Log in" }));
    expect(onModeChange).toHaveBeenCalledWith("login");
  });
});
