import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { FeedPage } from "../../api/generated";
import { ApiError } from "../../api/http";
import type { FeedApi } from "./feed-api";
import { useFeedController } from "./useFeedController";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((ok, fail) => {
    resolve = ok;
    reject = fail;
  });
  return { promise, reject, resolve };
}

function page(
  requestId: string,
  itemIds: readonly string[],
  nextCursor: string | null = null,
  snapshotId = "snapshot-1",
): FeedPage {
  return {
    items: itemIds.map((itemId, position) => ({
      cover: null,
      item_id: itemId,
      model_version: "model-1",
      position,
      reason: `reason-${itemId}`,
      score: 1 - position / 10,
      source: "dssm",
      title: `Title ${itemId}`,
    })),
    model_version: "model-1",
    next_cursor: nextCursor,
    request_id: requestId,
    snapshot_id: snapshotId,
  };
}

describe("useFeedController", () => {
  it("ignores a stale initial response after a rapid feed switch", async () => {
    const first = deferred<FeedPage>();
    const second = deferred<FeedPage>();
    const getPage = vi
      .fn<FeedApi["getPage"]>()
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise);
    const api = { getPage };
    const { result } = renderHook(() => useFeedController({ api }));
    await waitFor(() => expect(getPage).toHaveBeenCalledTimes(1));

    act(() => result.current.setFeedType("popular"));
    await waitFor(() => expect(getPage).toHaveBeenCalledTimes(2));
    await act(async () => second.resolve(page("request-popular", ["popular"]))) ;
    await act(async () => first.resolve(page("request-old", ["old"]))) ;

    expect(result.current.feedType).toBe("popular");
    expect(result.current.items.map((entry) => entry.item.item_id)).toEqual(["popular"]);
    expect(result.current.items[0]?.requestId).toBe("request-popular");
  });

  it("single-flights load more, keeps opaque cursor and deduplicates by server item id", async () => {
    const next = deferred<FeedPage>();
    const getPage = vi
      .fn<FeedApi["getPage"]>()
      .mockResolvedValueOnce(page("request-1", ["a", "b"], "opaque::cursor"))
      .mockImplementationOnce(() => next.promise);
    const api = { getPage };
    const { result } = renderHook(() => useFeedController({ api, limit: 12 }));
    await waitFor(() => expect(result.current.state).toBe("ready"));

    act(() => {
      result.current.loadMore();
      result.current.loadMore();
    });
    expect(getPage).toHaveBeenCalledTimes(2);
    expect(getPage.mock.calls[1]?.[0]).toMatchObject({ cursor: "opaque::cursor", limit: 12 });
    await act(async () => next.resolve(page("request-2", ["b", "c"], null)));

    expect(result.current.items.map((entry) => entry.item.item_id)).toEqual(["a", "b", "c"]);
    expect(result.current.items[2]?.requestId).toBe("request-2");
    expect(result.current.items[2]?.item.position).toBe(1);
    expect(result.current.hasMore).toBe(false);
  });

  it("retains loaded items and maps network page failure to offline", async () => {
    const getPage = vi
      .fn<FeedApi["getPage"]>()
      .mockResolvedValueOnce(page("request-1", ["a"], "next"))
      .mockRejectedValueOnce(
        new ApiError("connection lost", { code: "NETWORK_ERROR", kind: "network" }),
      )
      .mockResolvedValueOnce(page("request-2", ["b"], null));
    const api = { getPage };
    const { result } = renderHook(() => useFeedController({ api }));
    await waitFor(() => expect(result.current.state).toBe("ready"));
    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.state).toBe("offline"));

    expect(result.current.items).toHaveLength(1);
    expect(result.current.error?.message).toBe("connection lost");
    act(() => result.current.retry());
    await waitFor(() => expect(result.current.items).toHaveLength(2));
    expect(getPage.mock.calls[2]?.[0].cursor).toBe("next");
  });

  it("retains items but recovers a 409 cursor conflict with a cursor-free refresh", async () => {
    const getPage = vi
      .fn<FeedApi["getPage"]>()
      .mockResolvedValueOnce(page("request-1", ["a"], "expired"))
      .mockRejectedValueOnce(
        new ApiError("cursor conflict", { code: "CURSOR_CONFLICT", kind: "api", status: 409 }),
      )
      .mockResolvedValueOnce(page("request-new", ["new"], null, "snapshot-new"));
    const api = { getPage };
    const { result } = renderHook(() => useFeedController({ api }));
    await waitFor(() => expect(result.current.state).toBe("ready"));
    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.error?.title).toBe("Feed snapshot changed"));
    expect(result.current.items[0]?.item.item_id).toBe("a");
    expect(result.current.hasMore).toBe(false);
    act(() => result.current.retry());
    await waitFor(() => expect(result.current.items[0]?.item.item_id).toBe("new"));
    expect(getPage.mock.calls[2]?.[0].cursor).toBeUndefined();
  });

  it("recovers a client-detected snapshot mismatch without resending the old cursor", async () => {
    const getPage = vi
      .fn<FeedApi["getPage"]>()
      .mockResolvedValueOnce(page("request-1", ["a"], "next", "snapshot-old"))
      .mockResolvedValueOnce(page("request-wrong", ["b"], null, "snapshot-wrong"))
      .mockResolvedValueOnce(page("request-new", ["c"], null, "snapshot-new"));
    const api = { getPage };
    const { result } = renderHook(() => useFeedController({ api }));
    await waitFor(() => expect(result.current.state).toBe("ready"));
    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.state).toBe("error"));
    expect(result.current.error?.title).toBe("Feed snapshot changed");
    expect(result.current.items.map((entry) => entry.item.item_id)).toEqual(["a"]);
    expect(result.current.hasMore).toBe(false);
    act(() => result.current.retry());
    await waitFor(() => expect(result.current.items[0]?.item.item_id).toBe("c"));
    expect(getPage.mock.calls[2]?.[0].cursor).toBeUndefined();
  });

  it("treats a load-more 422 as an invalid cursor and retries from a new first page", async () => {
    const getPage = vi
      .fn<FeedApi["getPage"]>()
      .mockResolvedValueOnce(page("request-1", ["a"], "invalid-cursor"))
      .mockRejectedValueOnce(
        new ApiError("invalid cursor", { code: "CURSOR_INVALID", kind: "api", status: 422 }),
      )
      .mockResolvedValueOnce(page("request-new", ["new"], null, "snapshot-new"));
    const api = { getPage };
    const { result } = renderHook(() => useFeedController({ api }));
    await waitFor(() => expect(result.current.state).toBe("ready"));
    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.error?.title).toBe("Invalid feed request"));
    expect(result.current.items.map((entry) => entry.item.item_id)).toEqual(["a"]);
    expect(result.current.hasMore).toBe(false);
    act(() => result.current.retry());
    await waitFor(() => expect(result.current.items[0]?.item.item_id).toBe("new"));
    expect(getPage.mock.calls[2]?.[0].cursor).toBeUndefined();
  });

  it.each([
    [401, "unauthorized", "Sign in required"],
    [403, "forbidden", "Feed access denied"],
    [409, "api", "Feed snapshot changed"],
    [422, "api", "Invalid feed request"],
  ] as const)("maps status %s to a complete feed error state", async (status, kind, title) => {
    const getPage = vi.fn<FeedApi["getPage"]>().mockRejectedValue(
      new ApiError("failed", { code: "FAILED", kind, status }),
    );
    const api = { getPage };
    const { result } = renderHook(() => useFeedController({ api }));
    await waitFor(() => expect(result.current.state).toBe("error"));
    expect(result.current.error?.title).toBe(title);
  });
});
