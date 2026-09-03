import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type {
  DashboardFeedDiagnostics,
  DashboardOverview,
  HotItem,
} from "../../api/generated";
import type { AdminApi } from "./admin-api";
import { useDashboardController } from "./useDashboardController";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => {
    resolve = next;
  });
  return { promise, resolve };
}

function overview(requests: number): DashboardOverview {
  return {
    active_model_version: null,
    active_users: 0,
    clicks: 0,
    ctr: 0,
    dwell_ms_total: 0,
    exposures: requests,
    from_utc: "2026-09-01T00:00:00Z",
    likes: 0,
    offline_item_count: 0,
    requests,
    revisits: 0,
    shares: 0,
    to_utc: "2026-09-02T00:00:00Z",
    total_users: 0,
    zero_denominator: requests === 0,
  };
}

const FEEDS: DashboardFeedDiagnostics = {
  feed_share: {},
  feeds: [],
  from_utc: "2026-09-01T00:00:00Z",
  to_utc: "2026-09-02T00:00:00Z",
};

describe("useDashboardController", () => {
  it("ignores a stale parallel response after a refresh", async () => {
    const firstOverview = deferred<DashboardOverview>();
    const firstTimeseries = deferred<[]>();
    const firstFeeds = deferred<DashboardFeedDiagnostics>();
    const firstHot = deferred<HotItem[]>();
    const api = {
      getOverview: vi.fn().mockReturnValueOnce(firstOverview.promise).mockResolvedValueOnce(overview(8)),
      getTimeseries: vi.fn().mockReturnValueOnce(firstTimeseries.promise).mockResolvedValueOnce([]),
      getFeedDiagnostics: vi.fn().mockReturnValueOnce(firstFeeds.promise).mockResolvedValueOnce(FEEDS),
      getHotItems: vi.fn().mockReturnValueOnce(firstHot.promise).mockResolvedValueOnce([]),
    } as unknown as AdminApi;
    const { result } = renderHook(() => useDashboardController(api));

    await waitFor(() => expect(api.getOverview).toHaveBeenCalledOnce());
    await act(async () => {
      await result.current.refresh();
    });
    expect(result.current.state.data?.overview.requests).toBe(8);

    await act(async () => {
      firstOverview.resolve(overview(99));
      firstTimeseries.resolve([]);
      firstFeeds.resolve(FEEDS);
      firstHot.resolve([]);
      await Promise.resolve();
    });

    expect(result.current.state.data?.overview.requests).toBe(8);
  });

  it("exposes an honest empty state for a zero-activity aggregate", async () => {
    const api = {
      getOverview: vi.fn().mockResolvedValue(overview(0)),
      getTimeseries: vi.fn().mockResolvedValue([]),
      getFeedDiagnostics: vi.fn().mockResolvedValue(FEEDS),
      getHotItems: vi.fn().mockResolvedValue([]),
    } as unknown as AdminApi;
    const { result } = renderHook(() => useDashboardController(api));

    await waitFor(() => expect(result.current.state.status).toBe("empty"));
    expect(result.current.state.data?.overview.zero_denominator).toBe(true);
  });
});
