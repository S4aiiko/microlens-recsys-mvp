import { useCallback, useEffect, useState } from "react";
import type {
  ClientEventType,
  EventItemResult,
  EventRequest,
} from "../../api/generated";
import { ApiError } from "../../api/http";
import type { FeedApi } from "./feed-api";

export const DEFAULT_EVENT_QUEUE_CAPACITY = 200;
export const MAX_EVENT_BATCH_SIZE = 100;
export const MAX_DWELL_MS = 86_400_000;

export type EventQueueStatus =
  | "queued"
  | "sending"
  | "accepted"
  | "duplicate"
  | "rejected"
  | "failed";

export interface EventQueueRecord {
  attempts: number;
  error: string | null;
  payload: EventRequest;
  result: EventItemResult | null;
  retryable: boolean;
  status: EventQueueStatus;
}

export interface EventDraft {
  durationMs?: number;
  eventType: ClientEventType;
  itemId: string;
  payload?: Record<string, unknown>;
  position: number;
  requestId: string;
}

export interface EventQueueOptions {
  api: Pick<FeedApi, "sendBatch" | "sendEvent">;
  capacity?: number;
  idFactory?: () => string;
  now?: () => Date;
}

type Listener = () => void;

function defaultIdFactory(): string {
  return crypto.randomUUID();
}

function isSettled(status: EventQueueStatus): boolean {
  return status === "accepted" || status === "duplicate" || status === "rejected";
}

function isEvictable(record: EventQueueRecord): boolean {
  return isSettled(record.status) || (record.status === "failed" && !record.retryable);
}

function errorMessage(error: unknown): string {
  return error instanceof Error && error.message ? error.message : "The event request failed.";
}

function isRetryableTransportError(error: unknown): boolean {
  if (!(error instanceof ApiError)) return true;
  return (
    error.kind === "network" ||
    error.kind === "csrf" ||
    (error.status !== null && error.status >= 500)
  );
}

export class EventQueue {
  private readonly api: EventQueueOptions["api"];
  private readonly capacity: number;
  private readonly idFactory: () => string;
  private readonly listeners = new Set<Listener>();
  private readonly now: () => Date;
  private records: EventQueueRecord[] = [];
  private batchIds = new Map<string, string>();

  constructor(options: EventQueueOptions) {
    this.api = options.api;
    this.capacity = Math.max(1, options.capacity ?? DEFAULT_EVENT_QUEUE_CAPACITY);
    this.idFactory = options.idFactory ?? defaultIdFactory;
    this.now = options.now ?? (() => new Date());
  }

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getSnapshot = (): readonly EventQueueRecord[] => this.records;

  enqueue(draft: EventDraft): EventQueueRecord {
    if (this.records.length >= this.capacity) {
      const removable = this.records.findIndex(isEvictable);
      if (removable < 0) throw new Error("The event queue is full. Retry pending events first.");
      this.records = this.records.filter((_, index) => index !== removable);
    }

    const duration =
      draft.eventType === "dwell"
        ? Math.min(MAX_DWELL_MS, Math.max(0, Math.round(draft.durationMs ?? 0)))
        : undefined;
    const record: EventQueueRecord = {
      attempts: 0,
      error: null,
      payload: {
        client_timestamp: this.now().toISOString(),
        duration_ms: duration,
        event_id: this.idFactory(),
        event_type: draft.eventType,
        item_id: draft.itemId,
        payload: draft.payload,
        position: draft.position,
        request_id: draft.requestId,
      },
      result: null,
      retryable: true,
      status: "queued",
    };
    this.records = [...this.records, record];
    this.emit();
    return record;
  }

  async send(eventId: string): Promise<EventItemResult | null> {
    const record = this.find(eventId);
    if (
      !record ||
      record.status === "sending" ||
      isSettled(record.status) ||
      (record.status === "failed" && !record.retryable)
    ) {
      return record?.result ?? null;
    }
    this.patch(eventId, { attempts: record.attempts + 1, error: null, status: "sending" });
    try {
      const result = await this.api.sendEvent(record.payload);
      this.applyResult(result);
      return result;
    } catch (error) {
      this.patch(eventId, {
        error: errorMessage(error),
        retryable: isRetryableTransportError(error),
        status: "failed",
      });
      return null;
    }
  }

  async sendBatch(eventIds?: readonly string[]): Promise<readonly EventItemResult[]> {
    const selected = this.selectBatch(eventIds);
    if (selected.length === 0) return [];
    const signature = selected.map((record) => record.payload.event_id).join(":");
    const batchId = this.batchIds.get(signature) ?? this.idFactory();
    if (!this.batchIds.has(signature) && this.batchIds.size >= this.capacity) {
      const oldest = this.batchIds.keys().next().value;
      if (oldest !== undefined) this.batchIds.delete(oldest);
    }
    this.batchIds.set(signature, batchId);
    for (const record of selected) {
      this.patch(record.payload.event_id, {
        attempts: record.attempts + 1,
        error: null,
        status: "sending",
      });
    }
    try {
      const response = await this.api.sendBatch(
        batchId,
        selected.map((record) => record.payload),
      );
      for (const result of response.results) this.applyResult(result);
      const returned = new Set(response.results.map((result) => result.event_id));
      for (const record of selected) {
        if (!returned.has(record.payload.event_id)) {
          this.patch(record.payload.event_id, {
            error: "The batch response omitted this event.",
            status: "failed",
          });
        }
      }
      this.batchIds.delete(signature);
      return response.results;
    } catch (error) {
      for (const record of selected) {
        this.patch(record.payload.event_id, {
          error: errorMessage(error),
          retryable: isRetryableTransportError(error),
          status: "failed",
        });
      }
      return [];
    }
  }

  private applyResult(result: EventItemResult): void {
    this.patch(result.event_id, {
      error: result.status === "rejected" ? (result.message ?? result.error_code ?? "Rejected") : null,
      retryable: false,
      result,
      status: result.status,
    });
  }

  private emit(): void {
    for (const listener of this.listeners) listener();
  }

  private find(eventId: string): EventQueueRecord | undefined {
    return this.records.find((record) => record.payload.event_id === eventId);
  }

  private patch(eventId: string, patch: Partial<EventQueueRecord>): void {
    this.records = this.records.map((record) =>
      record.payload.event_id === eventId ? { ...record, ...patch } : record,
    );
    this.emit();
  }

  private selectBatch(eventIds?: readonly string[]): EventQueueRecord[] {
    const allowed = eventIds ? new Set(eventIds) : null;
    return this.records
      .filter(
        (record) =>
          (!allowed || allowed.has(record.payload.event_id)) &&
          (record.status === "queued" || (record.status === "failed" && record.retryable)),
      )
      .slice(0, MAX_EVENT_BATCH_SIZE);
  }
}

export interface UseEventQueueResult {
  enqueueAndSend(draft: EventDraft): Promise<EventItemResult | null>;
  records: readonly EventQueueRecord[];
  retry(eventId: string): Promise<EventItemResult | null>;
  retryFailedBatch(): Promise<readonly EventItemResult[]>;
}

export function useEventQueue(queue: EventQueue): UseEventQueueResult {
  const [records, setRecords] = useState(queue.getSnapshot());
  useEffect(() => queue.subscribe(() => setRecords(queue.getSnapshot())), [queue]);

  const enqueueAndSend = useCallback(
    async (draft: EventDraft) => {
      const record = queue.enqueue(draft);
      return queue.send(record.payload.event_id);
    },
    [queue],
  );

  return {
    enqueueAndSend,
    records,
    retry: useCallback((eventId: string) => queue.send(eventId), [queue]),
    retryFailedBatch: useCallback(() => queue.sendBatch(), [queue]),
  };
}
