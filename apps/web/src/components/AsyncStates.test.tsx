import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { EmptyState, ErrorState, ForbiddenState, LoadingState } from "./AsyncStates";

describe("accessible async states", () => {
  it("announces loading politely with busy state and visible text", () => {
    render(<LoadingState label="Loading recommendations" />);
    const status = screen.getByRole("status");
    expect(status.getAttribute("aria-live")).toBe("polite");
    expect(status.getAttribute("aria-busy")).toBe("true");
    expect(screen.getByText("Loading recommendations")).toBeTruthy();
  });

  it("does not present an empty result as an alert", () => {
    render(<EmptyState title="Nothing here" description="Try another filter." />);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("heading", { name: "Nothing here" })).toBeTruthy();
  });

  it("announces actionable errors and exposes a safe retry", () => {
    const retry = vi.fn();
    render(<ErrorState message="Request failed" onRetry={retry} requestId="req-1" />);
    expect(screen.getByRole("alert")).toBeTruthy();
    expect(screen.getByText("Request ID: req-1")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(retry).toHaveBeenCalledOnce();
  });

  it("shows forbidden as a stable page state instead of an urgent alert", () => {
    render(<ForbiddenState />);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByText("403 · Forbidden")).toBeTruthy();
  });
});
