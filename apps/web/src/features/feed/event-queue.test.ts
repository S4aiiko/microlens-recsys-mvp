import { describe, expect, it, vi } from "vitest";
import type { EventBatchResponse, EventItemResult, EventRequest } from "../../api/generated";
import { ApiError } from "../../api/http";
import { EventQueue, MAX_DWELL_MS, MAX_EVENT_BATCH_SIZE } from "./event-queue";

function draft(index = 1) {
  return {
    eventType: "like" as const,
    itemId: `item-${index}`,
    position: index,
    requestId: `request-${index}`,
  };
}

describe("EventQueue", () => {
  it("retries one event with the exact stable payload and event id", async () => {
    const sent: EventRequest[] = [];
    const sendEvent = vi.fn(async (event: EventRequest): Promise<EventItemResult> => {
      sent.push(event);
      if (sent.length === 1) throw new Error("offline");
      return { event_id: event.event_id, status: "duplicate" };
    });
    const queue = new EventQueue({
      api: { sendBatch: vi.fn(), sendEvent },
      idFactory: () => "event-stable",
      now: () => new Date("2026-09-02T10:00:00Z"),
    });
    const record = queue.enqueue(draft());

    expect(await queue.send(record.payload.event_id)).toBeNull();
    expect(queue.getSnapshot()[0]?.status).toBe("failed");
    expect((await queue.send(record.payload.event_id))?.status).toBe("duplicate");
    expect(sent).toHaveLength(2);
    expect(sent[1]).toEqual(sent[0]);
    expect(sent[0]?.event_id).toBe("event-stable");
  });

  it("retries a failed batch with the same batch id and caps batches at 100", async () => {
    let call = 0;
    const calls: Array<{ batchId: string; events: readonly EventRequest[] }> = [];
    const sendBatch = vi.fn(
      async (batchId: string, events: readonly EventRequest[]): Promise<EventBatchResponse> => {
        calls.push({ batchId, events });
        call += 1;
        if (call === 1) throw new Error("offline");
        return {
          accepted: events.length,
          batch_id: batchId,
          duplicate: 0,
          rejected: 0,
          results: events.map((event) => ({ event_id: event.event_id, status: "accepted" })),
        };
      },
    );
    let id = 0;
    const queue = new EventQueue({
      api: { sendBatch, sendEvent: vi.fn() },
      capacity: 120,
      idFactory: () => `uuid-${++id}`,
    });
    for (let index = 0; index < 105; index += 1) queue.enqueue(draft(index));

    expect(await queue.sendBatch()).toEqual([]);
    await queue.sendBatch();

    expect(calls[0]?.events).toHaveLength(MAX_EVENT_BATCH_SIZE);
    expect(calls[1]?.batchId).toBe(calls[0]?.batchId);
    expect(calls[1]?.events).toEqual(calls[0]?.events);
    expect(queue.getSnapshot().filter((record) => record.status === "accepted")).toHaveLength(100);
    expect(queue.getSnapshot().filter((record) => record.status === "queued")).toHaveLength(5);
  });

  it("keeps rejected results visible and evicts only settled records at capacity", async () => {
    const queue = new EventQueue({
      api: {
        sendBatch: vi.fn(),
        sendEvent: async (event) => ({
          error_code: "INVALID_EXPOSURE",
          event_id: event.event_id,
          message: "request/item mismatch",
          status: "rejected",
        }),
      },
      capacity: 2,
      idFactory: (() => {
        let id = 0;
        return () => `event-${++id}`;
      })(),
    });
    const rejected = queue.enqueue(draft(1));
    await queue.send(rejected.payload.event_id);
    queue.enqueue(draft(2));
    queue.enqueue(draft(3));

    expect(queue.getSnapshot()).toHaveLength(2);
    expect(queue.getSnapshot().some((record) => record.payload.item_id === "item-1")).toBe(false);
    expect(queue.getSnapshot().map((record) => record.status)).toEqual(["queued", "queued"]);
  });

  it("applies accepted, duplicate and rejected batch results per item", async () => {
    let id = 0;
    const queue = new EventQueue({
      api: {
        sendBatch: async (batchId, events) => ({
          accepted: 1,
          batch_id: batchId,
          duplicate: 1,
          rejected: 1,
          results: [
            { event_id: events[0]!.event_id, status: "accepted" },
            { event_id: events[1]!.event_id, status: "duplicate" },
            {
              error_code: "INVALID_EXPOSURE",
              event_id: events[2]!.event_id,
              message: "mismatch",
              status: "rejected",
            },
          ],
        }),
        sendEvent: vi.fn(),
      },
      idFactory: () => `uuid-${++id}`,
    });
    queue.enqueue(draft(1));
    queue.enqueue(draft(2));
    queue.enqueue(draft(3));
    await queue.sendBatch();
    expect(queue.getSnapshot().map((record) => record.status)).toEqual([
      "accepted",
      "duplicate",
      "rejected",
    ]);
    expect(queue.getSnapshot()[2]?.error).toBe("mismatch");
  });

  it("retries network failures but not forbidden single-event failures", async () => {
    const networkSend = vi
      .fn()
      .mockRejectedValue(new ApiError("offline", { code: "NETWORK", kind: "network" }));
    const networkQueue = new EventQueue({
      api: { sendBatch: vi.fn(), sendEvent: networkSend },
      idFactory: () => "network-event",
    });
    const network = networkQueue.enqueue(draft(1));
    await networkQueue.send(network.payload.event_id);
    await networkQueue.send(network.payload.event_id);
    expect(networkSend).toHaveBeenCalledTimes(2);
    expect(networkQueue.getSnapshot()[0]?.retryable).toBe(true);

    const forbiddenSend = vi.fn().mockRejectedValue(
      new ApiError("forbidden", { code: "FORBIDDEN", kind: "forbidden", status: 403 }),
    );
    const forbiddenQueue = new EventQueue({
      api: { sendBatch: vi.fn(), sendEvent: forbiddenSend },
      idFactory: () => "forbidden-event",
    });
    const forbidden = forbiddenQueue.enqueue(draft(2));
    await forbiddenQueue.send(forbidden.payload.event_id);
    await forbiddenQueue.send(forbidden.payload.event_id);
    expect(forbiddenSend).toHaveBeenCalledOnce();
    expect(forbiddenQueue.getSnapshot()[0]).toMatchObject({ retryable: false, status: "failed" });
  });

  it("does not blindly retry a validation-failed batch", async () => {
    const sendBatch = vi.fn().mockRejectedValue(
      new ApiError("invalid", { code: "VALIDATION", kind: "api", status: 422 }),
    );
    const queue = new EventQueue({
      api: { sendBatch, sendEvent: vi.fn() },
      idFactory: (() => {
        let id = 0;
        return () => `id-${++id}`;
      })(),
    });
    queue.enqueue(draft(1));
    await queue.sendBatch();
    await queue.sendBatch();
    expect(sendBatch).toHaveBeenCalledOnce();
    expect(queue.getSnapshot()[0]).toMatchObject({ retryable: false, status: "failed" });
  });

  it("evicts terminal non-retryable failures instead of permanently filling capacity", async () => {
    const queue = new EventQueue({
      api: {
        sendBatch: vi.fn(),
        sendEvent: vi.fn().mockRejectedValue(
          new ApiError("forbidden", { code: "FORBIDDEN", kind: "forbidden", status: 403 }),
        ),
      },
      capacity: 1,
      idFactory: (() => {
        let id = 0;
        return () => `event-${++id}`;
      })(),
    });
    const first = queue.enqueue(draft(1));
    await queue.send(first.payload.event_id);
    queue.enqueue(draft(2));
    expect(queue.getSnapshot()).toHaveLength(1);
    expect(queue.getSnapshot()[0]?.payload.item_id).toBe("item-2");
  });

  it("emits exactly the six client types, never impression, with duration only on dwell", () => {
    const clientTypes = [
      "click",
      "like",
      "not_interested",
      "dwell",
      "revisit",
      "share",
    ] as const;
    let id = 0;
    const queue = new EventQueue({
      api: { sendBatch: vi.fn(), sendEvent: vi.fn() },
      idFactory: () => `event-${++id}`,
    });
    clientTypes.forEach((eventType, index) =>
      queue.enqueue({ ...draft(index), durationMs: MAX_DWELL_MS + 123, eventType }),
    );
    const payloads = queue.getSnapshot().map((record) => record.payload);
    expect(payloads.map((payload) => payload.event_type)).toEqual(clientTypes);
    expect(payloads.some((payload) => payload.event_type === ("impression" as never))).toBe(false);
    for (const payload of payloads) {
      expect(payload.duration_ms).toBe(payload.event_type === "dwell" ? MAX_DWELL_MS : undefined);
    }
  });
});
