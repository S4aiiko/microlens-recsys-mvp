import { describe, expect, it, vi } from "vitest";
import type { FeedPage } from "../../api/generated";
import type { Client } from "../../api/generated/client";
import { ApiError } from "../../api/http";
import { createFeedApi } from "./feed-api";

const PAGE: FeedPage = {
  items: [],
  model_version: "model-1",
  next_cursor: "next",
  request_id: "request-1",
  snapshot_id: "snapshot-1",
};

describe("createFeedApi", () => {
  it("injects the configured client and forwards opaque feed parameters", async () => {
    const get = vi.fn().mockResolvedValue({ data: PAGE });
    const post = vi
      .fn()
      .mockResolvedValueOnce({ data: { event_id: "event-1", status: "accepted" } })
      .mockResolvedValueOnce({
        data: {
          accepted: 1,
          batch_id: "batch-1",
          duplicate: 0,
          rejected: 0,
          results: [{ event_id: "event-1", status: "accepted" }],
        },
      });
    const api = createFeedApi({ get, post } as unknown as Client);
    await api.getPage({ cursor: "opaque::cursor", feedType: "explore", limit: 17 });
    expect(get).toHaveBeenCalledWith(
      expect.objectContaining({
        path: { feed_type: "explore" },
        query: { cursor: "opaque::cursor", limit: 17 },
        url: "/api/feeds/{feed_type}",
      }),
    );
    const event = {
      client_timestamp: "2026-09-02T10:00:00Z",
      event_id: "event-1",
      event_type: "like" as const,
      item_id: "item-1",
      position: 0,
      request_id: "request-1",
    };
    await api.sendEvent(event);
    await api.sendBatch("batch-1", [event]);
    expect(post.mock.calls[0]?.[0]).toMatchObject({ body: event, url: "/api/events" });
    expect(post.mock.calls[1]?.[0]).toMatchObject({
      body: { batch_id: "batch-1", events: [event] },
      url: "/api/events/batch",
    });
  });

  it("normalizes generated error envelopes through the shared ApiError contract", async () => {
    const get = vi.fn().mockResolvedValue({
      error: { code: "CURSOR_INVALID", details: null, message: "bad cursor", request_id: "api-1" },
      response: new Response(null, { status: 422 }),
    });
    const api = createFeedApi({ get } as unknown as Client);
    await expect(api.getPage({ feedType: "personalized" })).rejects.toMatchObject({
      code: "CURSOR_INVALID",
      requestId: "api-1",
      status: 422,
    } satisfies Partial<ApiError>);
  });
});
