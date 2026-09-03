import { describe, expect, it, vi } from "vitest";
import type { AdminApi } from "./admin-api";
import {
  dashboardQuery,
  downloadDashboardCsv,
  shanghaiLocalToUtc,
  utcToShanghaiLocal,
} from "./admin-time";

describe("admin time and CSV", () => {
  it("converts Asia/Shanghai wall time to UTC and back", () => {
    expect(shanghaiLocalToUtc("2026-09-02T18:30")).toBe("2026-09-02T10:30:00.000Z");
    expect(utcToShanghaiLocal("2026-09-02T10:30:00.000Z")).toBe("2026-09-02T18:30");
  });

  it("validates a strict [from, to) interval and preserves the selected feed", () => {
    expect(
      dashboardQuery({
        feedType: "popular",
        fromLocal: "2026-09-02T10:00",
        toLocal: "2026-09-02T11:00",
      }),
    ).toEqual({
      feedType: "popular",
      fromUtc: "2026-09-02T02:00:00.000Z",
      toUtc: "2026-09-02T03:00:00.000Z",
    });
    expect(() =>
      dashboardQuery({
        feedType: "all",
        fromLocal: "2026-09-02T11:00",
        toLocal: "2026-09-02T11:00",
      }),
    ).toThrow("[from, to)");
  });

  it("downloads with the exact applied query and always revokes the object URL", async () => {
    const query = {
      feedType: "explore" as const,
      fromUtc: "2026-09-02T02:00:00.000Z",
      toUtc: "2026-09-02T03:00:00.000Z",
    };
    const exportDashboardCsv = vi.fn().mockResolvedValue(new Blob(["csv"]));
    const revokeObjectURL = vi.fn();
    const trigger = vi.fn(() => {
      throw new Error("download blocked");
    });

    await expect(
      downloadDashboardCsv(
        { exportDashboardCsv } as unknown as AdminApi,
        query,
        {
          createObjectURL: () => "blob:dashboard",
          revokeObjectURL,
          trigger,
        },
      ),
    ).rejects.toThrow("download blocked");

    expect(exportDashboardCsv).toHaveBeenCalledWith(query);
    expect(trigger).toHaveBeenCalledWith("blob:dashboard", "microlens-dashboard-2026-09-02.csv");
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:dashboard");
  });
});
