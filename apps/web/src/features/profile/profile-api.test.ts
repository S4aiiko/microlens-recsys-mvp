import { describe, expect, it, vi } from "vitest";
import type { UserProfileResponse } from "../../api/generated";
import type { Client } from "../../api/generated/client";
import { createProfileApi } from "./profile-api";

const PROFILE: UserProfileResponse = {
  dwell_summary: {},
  negative_summary: {},
  positive_summary: {},
  profile_version: 1,
  recent_interactions: [],
  revisit_summary: {},
  share_summary: {},
  title_preferences: {},
  updated_at: "2026-09-02T10:00:00Z",
  user_id: "user-1",
};

describe("createProfileApi", () => {
  it("uses the injected client and returns the generated profile type", async () => {
    const get = vi.fn().mockResolvedValue({ data: PROFILE });
    const api = createProfileApi({ get } as unknown as Client);
    await expect(api.getMyProfile()).resolves.toEqual(PROFILE);
    expect(get).toHaveBeenCalledWith(expect.objectContaining({ url: "/api/profile/me" }));
  });
});
