import { useCallback, useEffect, useRef, useState } from "react";
import type { FeedPage, FeedType } from "../../api/generated";
import { ApiError } from "../../api/http";
import type { FeedEntry, FeedViewError, FeedViewState } from "./FeedWorkspace";
import { feedApi, type FeedApi } from "./feed-api";

const DEFAULT_PAGE_SIZE = 20;

class FeedSnapshotMismatchError extends Error {
  constructor() {
    super("The next page did not belong to the active feed snapshot.");
    this.name = "FeedSnapshotMismatchError";
  }
}

interface FeedData {
  items: FeedEntry[];
  modelVersion: string | null;
  nextCursor: string | null;
  snapshotId: string | null;
}

export interface FeedControllerOptions {
  api?: Pick<FeedApi, "getPage">;
  initialFeedType?: FeedType;
  limit?: number;
}

export interface FeedController {
  error?: FeedViewError;
  feedType: FeedType;
  hasMore: boolean;
  isLoadingMore: boolean;
  isRefreshing: boolean;
  items: readonly FeedEntry[];
  modelVersion: string | null;
  refresh(): void;
  retry(): void;
  snapshotId: string | null;
  state: FeedViewState;
  loadMore(): void;
  setFeedType(feedType: FeedType): void;
}

function emptyData(): FeedData {
  return { items: [], modelVersion: null, nextCursor: null, snapshotId: null };
}

function errorView(error: unknown): { error: FeedViewError; state: FeedViewState } {
  if (error instanceof FeedSnapshotMismatchError) {
    return {
      error: { message: error.message, title: "Feed snapshot changed" },
      state: "error",
    };
  }
  if (error instanceof ApiError) {
    const title =
      error.kind === "unauthorized"
        ? "Sign in required"
        : error.kind === "forbidden"
          ? "Feed access denied"
          : error.status === 409
            ? "Feed snapshot changed"
            : error.status === 422
              ? "Invalid feed request"
              : undefined;
    return {
      error: { message: error.message, requestId: error.requestId, title },
      state: error.kind === "network" ? "offline" : "error",
    };
  }
  return {
    error: { message: error instanceof Error ? error.message : "The feed request failed." },
    state: "error",
  };
}

function invalidatesCursor(error: unknown): boolean {
  return (
    error instanceof FeedSnapshotMismatchError ||
    (error instanceof ApiError && (error.status === 409 || error.status === 422))
  );
}

function entriesFor(page: FeedPage): FeedEntry[] {
  return page.items.map((item) => ({ item, requestId: page.request_id }));
}

function appendUnique(current: readonly FeedEntry[], page: FeedPage): FeedEntry[] {
  const seen = new Set(current.map((entry) => entry.item.item_id));
  return [
    ...current,
    ...entriesFor(page).filter((entry) => {
      if (seen.has(entry.item.item_id)) return false;
      seen.add(entry.item.item_id);
      return true;
    }),
  ];
}

export function useFeedController(options: FeedControllerOptions = {}): FeedController {
  const api = options.api ?? feedApi;
  const limit = Math.min(100, Math.max(1, options.limit ?? DEFAULT_PAGE_SIZE));
  const [feedType, setFeedTypeState] = useState<FeedType>(
    options.initialFeedType ?? "personalized",
  );
  const [data, setData] = useState<FeedData>(emptyData);
  const [state, setState] = useState<FeedViewState>("loading");
  const [error, setError] = useState<FeedViewError>();
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const dataRef = useRef(data);
  const feedTypeRef = useRef(feedType);
  const generationRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const loadingMoreRef = useRef(false);
  const failedOperationRef = useRef<"first" | "more">("first");

  useEffect(() => {
    dataRef.current = data;
  }, [data]);

  const requestFirst = useCallback(
    (target: FeedType, retain: boolean) => {
      const generation = ++generationRef.current;
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      loadingMoreRef.current = false;
      setIsLoadingMore(false);
      setError(undefined);
      if (!retain) {
        dataRef.current = emptyData();
        setData(dataRef.current);
      }
      const hasRetained = retain && dataRef.current.items.length > 0;
      setIsRefreshing(hasRetained);
      setState(hasRetained ? "ready" : "loading");

      void api
        .getPage({ feedType: target, limit, signal: controller.signal })
        .then((page) => {
          if (generation !== generationRef.current || target !== feedTypeRef.current) return;
          const next: FeedData = {
            items: entriesFor(page),
            modelVersion: page.model_version,
            nextCursor: page.next_cursor,
            snapshotId: page.snapshot_id,
          };
          dataRef.current = next;
          setData(next);
          setState(next.items.length === 0 ? "empty" : "ready");
          setError(undefined);
        })
        .catch((caught: unknown) => {
          if (controller.signal.aborted || generation !== generationRef.current) return;
          failedOperationRef.current = "first";
          const view = errorView(caught);
          setError(view.error);
          setState(view.state);
        })
        .finally(() => {
          if (generation === generationRef.current) setIsRefreshing(false);
        });
    },
    [api, limit],
  );

  useEffect(() => {
    requestFirst(feedTypeRef.current, false);
    return () => {
      generationRef.current += 1;
      abortRef.current?.abort();
    };
  }, [requestFirst]);

  const setFeedType = useCallback(
    (next: FeedType) => {
      if (next === feedTypeRef.current) return;
      feedTypeRef.current = next;
      setFeedTypeState(next);
      requestFirst(next, false);
    },
    [requestFirst],
  );

  const refresh = useCallback(() => {
    requestFirst(feedTypeRef.current, true);
  }, [requestFirst]);

  const loadMore = useCallback(() => {
    const current = dataRef.current;
    if (!current.nextCursor || loadingMoreRef.current) return;
    const generation = generationRef.current;
    const target = feedTypeRef.current;
    const cursor = current.nextCursor;
    loadingMoreRef.current = true;
    setIsLoadingMore(true);
    setError(undefined);
    void api
      .getPage({ cursor, feedType: target, limit })
      .then((page) => {
        if (generation !== generationRef.current || target !== feedTypeRef.current) return;
        const latest = dataRef.current;
        if (latest.snapshotId && page.snapshot_id !== latest.snapshotId) {
          throw new FeedSnapshotMismatchError();
        }
        const next: FeedData = {
          items: appendUnique(latest.items, page),
          modelVersion: latest.modelVersion ?? page.model_version,
          nextCursor: page.next_cursor,
          snapshotId: latest.snapshotId ?? page.snapshot_id,
        };
        dataRef.current = next;
        setData(next);
        setState(next.items.length === 0 ? "empty" : "ready");
      })
      .catch((caught: unknown) => {
        if (generation !== generationRef.current) return;
        const cursorInvalid = invalidatesCursor(caught);
        failedOperationRef.current = cursorInvalid ? "first" : "more";
        if (cursorInvalid) {
          const retained = { ...dataRef.current, nextCursor: null };
          dataRef.current = retained;
          setData(retained);
        }
        const view = errorView(caught);
        setError(view.error);
        setState(view.state);
      })
      .finally(() => {
        if (generation === generationRef.current) {
          loadingMoreRef.current = false;
          setIsLoadingMore(false);
        }
      });
  }, [api, limit]);

  const retry = useCallback(() => {
    if (failedOperationRef.current === "more") loadMore();
    else refresh();
  }, [loadMore, refresh]);

  return {
    error,
    feedType,
    hasMore: data.nextCursor !== null,
    isLoadingMore,
    isRefreshing,
    items: data.items,
    loadMore,
    modelVersion: data.modelVersion,
    refresh,
    retry,
    setFeedType,
    snapshotId: data.snapshotId,
    state,
  };
}
