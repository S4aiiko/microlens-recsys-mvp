import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { UserProfileResponse } from "../../api/generated";
import { ApiError } from "../../api/http";
import type { ProfileApi } from "./profile-api";
import { useProfileController } from "./useProfileController";

function profile(version: number): UserProfileResponse {
  return {
    dwell_summary: {},
    negative_summary: {},
    positive_summary: { likes: version },
    profile_version: version,
    recent_interactions: [],
    revisit_summary: {},
    share_summary: {},
    title_preferences: {},
    updated_at: `2026-09-02T10:00:0${version}Z`,
    user_id: "user-1",
  };
}

describe("useProfileController", () => {
  it("retains the last profile when a refresh fails offline", async () => {
    const getMyProfile = vi
      .fn<ProfileApi["getMyProfile"]>()
      .mockResolvedValueOnce(profile(1))
      .mockRejectedValueOnce(
        new ApiError("offline", { code: "NETWORK_ERROR", kind: "network" }),
      );
    const api = { getMyProfile };
    const { result } = renderHook(() => useProfileController({ api }));
    await waitFor(() => expect(result.current.profile?.profile_version).toBe(1));
    act(() => result.current.refresh());
    await waitFor(() => expect(result.current.state).toBe("offline"));
    expect(result.current.profile?.profile_version).toBe(1);
  });

  it("ignores an older refresh response and keeps the latest profile", async () => {
    let resolveOlder!: (value: UserProfileResponse) => void;
    let resolveLatest!: (value: UserProfileResponse) => void;
    const older = new Promise<UserProfileResponse>((resolve) => (resolveOlder = resolve));
    const latest = new Promise<UserProfileResponse>((resolve) => (resolveLatest = resolve));
    const getMyProfile = vi
      .fn<ProfileApi["getMyProfile"]>()
      .mockResolvedValueOnce(profile(1))
      .mockImplementationOnce(() => older)
      .mockImplementationOnce(() => latest);
    const api = { getMyProfile };
    const { result } = renderHook(() => useProfileController({ api }));
    await waitFor(() => expect(result.current.profile?.profile_version).toBe(1));
    act(() => {
      result.current.refresh();
      result.current.refresh();
    });
    await act(async () => resolveLatest(profile(3)));
    await act(async () => resolveOlder(profile(2)));
    expect(result.current.profile?.profile_version).toBe(3);
  });

  it.each([
    ["unauthorized", "Sign in to load your profile."],
    ["forbidden", "This account cannot access the requested profile."],
  ] as const)("maps %s without discarding state", async (kind, message) => {
    const getMyProfile = vi.fn<ProfileApi["getMyProfile"]>().mockRejectedValue(
      new ApiError("raw", { code: "DENIED", kind }),
    );
    const api = { getMyProfile };
    const { result } = renderHook(() => useProfileController({ api }));
    await waitFor(() => expect(result.current.state).toBe("error"));
    expect(result.current.error?.message).toBe(message);
  });
});
