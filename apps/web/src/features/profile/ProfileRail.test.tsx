import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { UserProfileResponse } from "../../api/generated";
import { ProfileRail } from "./ProfileRail";

const PROFILE: UserProfileResponse = {
  dwell_summary: { average_ms: 4200, nested: { ignored: "safely" } },
  negative_summary: {},
  positive_summary: { likes: 3 },
  profile_version: 7,
  recent_interactions: [{ event_type: "like", item_id: "item-42" }],
  revisit_summary: { revisits: 1 },
  share_summary: { shares: 2 },
  title_preferences: { topics: ["science", "design"] },
  updated_at: "2026-09-02T08:00:00Z",
  user_id: "user-42",
};

describe("ProfileRail", () => {
  it("renders typed profile sections and safe open-dictionary values", () => {
    const { container } = render(<ProfileRail profile={PROFILE} state="ready" />);

    expect(container.querySelector(".ml-profile")?.getAttribute("data-state")).toBe("ready");
    expect(screen.getByRole("heading", { name: "Your profile" })).toBeTruthy();
    expect(screen.getByText("v7")).toBeTruthy();
    expect(screen.getByText("likes")).toBeTruthy();
    expect(screen.getByText("science, design")).toBeTruthy();
    expect(container.textContent).not.toContain("[object Object]");
  });

  it("announces loading without fabricating profile data", () => {
    render(<ProfileRail state="loading" />);
    const status = screen.getByRole("status");
    expect(status.getAttribute("aria-busy")).toBe("true");
    expect(status.textContent).toContain("Loading profile");
    expect(screen.queryByText("user-42")).toBeNull();
  });

  it("keeps a stale profile visible beside a retryable error", () => {
    const retry = vi.fn();
    render(
      <ProfileRail
        error={{ message: "Refresh failed", requestId: "request-profile" }}
        onRetry={retry}
        profile={PROFILE}
        state="error"
      />,
    );

    expect(screen.getByText("user-42")).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toContain("request-profile");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(retry).toHaveBeenCalledOnce();
  });
});
