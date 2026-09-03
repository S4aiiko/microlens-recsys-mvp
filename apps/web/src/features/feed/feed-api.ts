import {
  createEvent,
  createEventBatch,
  getFeedPage,
  type EventBatchResponse,
  type EventItemResult,
  type EventRequest,
  type FeedPage,
  type FeedType,
} from "../../api/generated";
import type { Client } from "../../api/generated/client";
import { apiClient, toApiError } from "../../api/http";

interface ApiResult<T> {
  data?: T;
  error?: unknown;
  response?: Response;
}

function unwrap<T>(result: ApiResult<T>): T {
  if (result.error !== undefined) throw toApiError(result.error, result.response);
  if (result.data === undefined) {
    throw toApiError(new Error("The API returned no response body."), result.response);
  }
  return result.data;
}

export interface FeedPageRequest {
  cursor?: string | null;
  feedType: FeedType;
  limit?: number;
  signal?: AbortSignal;
}

export interface FeedApi {
  getPage(input: FeedPageRequest): Promise<FeedPage>;
  sendBatch(batchId: string, events: readonly EventRequest[]): Promise<EventBatchResponse>;
  sendEvent(event: EventRequest): Promise<EventItemResult>;
}

export function createFeedApi(client: Client = apiClient): FeedApi {
  return {
    async getPage({ cursor, feedType, limit, signal }) {
      return unwrap(
        await getFeedPage({
          client,
          path: { feed_type: feedType },
          query: { cursor, limit },
          signal,
        }),
      );
    },
    async sendBatch(batchId, events) {
      return unwrap(
        await createEventBatch({
          body: { batch_id: batchId, events: [...events] },
          client,
        }),
      );
    },
    async sendEvent(event) {
      return unwrap(await createEvent({ body: event, client }));
    },
  };
}

export const feedApi = createFeedApi();
