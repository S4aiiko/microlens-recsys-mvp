import { useCallback, useEffect, useRef, useState } from "react";
import type {
  DashboardBucket,
  DashboardFeedDiagnostics,
  DashboardOverview,
  HotItem,
} from "../../api/generated";
import { toApiError, type ApiError } from "../../api/http";
import type { AdminApi, DashboardQuery } from "./admin-api";
import { adminApi } from "./admin-api";
import {
  dashboardQuery,
  defaultDashboardFilters,
  downloadDashboardCsv,
  type DashboardFilters,
  type DownloadEnvironment,
} from "./admin-time";

export interface DashboardData {
  feedDiagnostics: DashboardFeedDiagnostics;
  hotItems: HotItem[];
  overview: DashboardOverview;
  timeseries: DashboardBucket[];
}

type DashboardStatus = "empty" | "error" | "loading" | "ready" | "refreshing";

interface DashboardState {
  data: DashboardData | null;
  error: ApiError | null;
  query: DashboardQuery | null;
  status: DashboardStatus;
}

function isEmpty(data: DashboardData): boolean {
  return (
    data.timeseries.length === 0 &&
    data.hotItems.length === 0 &&
    data.overview.requests === 0 &&
    data.overview.exposures === 0 &&
    data.overview.clicks === 0 &&
    data.overview.likes === 0
  );
}

export function useDashboardController(api: AdminApi = adminApi) {
  const [filters, setFilters] = useState<DashboardFilters>(() => defaultDashboardFilters());
  const [state, setState] = useState<DashboardState>({
    data: null,
    error: null,
    query: null,
    status: "loading",
  });
  const [filterError, setFilterError] = useState<string | null>(null);
  const [downloadError, setDownloadError] = useState<ApiError | null>(null);
  const [downloading, setDownloading] = useState(false);
  const generation = useRef(0);
  const activeController = useRef<AbortController | null>(null);

  const load = useCallback(
    async (nextFilters: DashboardFilters) => {
      let query: DashboardQuery;
      try {
        query = dashboardQuery(nextFilters);
      } catch (error) {
        setFilterError(error instanceof Error ? error.message : "The time range is invalid.");
        return;
      }
      setFilterError(null);
      const currentGeneration = ++generation.current;
      activeController.current?.abort();
      const controller = new AbortController();
      activeController.current = controller;
      setState((current) => ({
        ...current,
        error: null,
        status: current.data ? "refreshing" : "loading",
      }));
      try {
        const [overview, timeseries, feedDiagnostics, hotItems] = await Promise.all([
          api.getOverview(query, controller.signal),
          api.getTimeseries(query, controller.signal),
          api.getFeedDiagnostics(query, controller.signal),
          api.getHotItems(query, controller.signal),
        ]);
        if (generation.current !== currentGeneration || controller.signal.aborted) return;
        const data = { feedDiagnostics, hotItems, overview, timeseries };
        setState({
          data,
          error: null,
          query,
          status: isEmpty(data) ? "empty" : "ready",
        });
      } catch (error) {
        if (generation.current !== currentGeneration || controller.signal.aborted) return;
        setState((current) => ({
          ...current,
          error: toApiError(error),
          status: "error",
        }));
      }
    },
    [api],
  );

  useEffect(() => {
    void load(filters);
    return () => activeController.current?.abort();
    // Initial filters are intentionally captured once; later changes require Apply.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load]);

  const exportCsv = useCallback(
    async (environment?: DownloadEnvironment) => {
      if (!state.query) return;
      setDownloadError(null);
      setDownloading(true);
      try {
        await downloadDashboardCsv(api, state.query, environment);
      } catch (error) {
        setDownloadError(toApiError(error));
      } finally {
        setDownloading(false);
      }
    },
    [api, state.query],
  );

  return {
    applyFilters: () => load(filters),
    downloadError,
    downloading,
    exportCsv,
    filterError,
    filters,
    refresh: () => load(filters),
    setFilters,
    state,
  };
}
