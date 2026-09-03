import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ClientEventType, EventItemResult } from "../../api/generated";
import { ProfileRail } from "../profile/ProfileRail";
import { profileApi, type ProfileApi } from "../profile/profile-api";
import { useProfileController } from "../profile/useProfileController";
import {
  useDwellTracker,
  type DwellMeasurement,
  type UseDwellTrackerOptions,
} from "./dwell";
import { EventQueue, type EventQueueRecord, useEventQueue } from "./event-queue";
import { feedApi, type FeedApi } from "./feed-api";
import { FeedWorkspace, type FeedEntry } from "./FeedWorkspace";
import { useFeedController } from "./useFeedController";
import { useLoadMoreSentinel } from "./useLoadMoreSentinel";

export interface FeedExperienceProps {
  dwellOptions?: Omit<UseDwellTrackerOptions, "onDwell">;
  eventCapacity?: number;
  feedApi?: FeedApi;
  idFactory?: () => string;
  now?: () => Date;
  profileApi?: ProfileApi;
}

function successful(result: EventItemResult | null): boolean {
  return result?.status === "accepted" || result?.status === "duplicate";
}

interface EventDelivery {
  eventType: ClientEventType;
  result: EventItemResult | null;
}

export function EventFeedback({
  error,
  onRetry,
  onRetryBatch,
  records,
}: {
  error?: string | null;
  onRetry(eventId: string): void;
  onRetryBatch(): void;
  records: readonly EventQueueRecord[];
}) {
  if (records.length === 0 && !error) return null;
  const visible = [...records].reverse();
  const failed = records.some((record) => record.status === "failed" && record.retryable);
  return (
    <section className="ml-events" aria-labelledby="ml-events-title">
      <div className="ml-events__header">
        <h2 id="ml-events-title">Interaction delivery ({records.length})</h2>
        {failed ? (
          <button onClick={onRetryBatch} type="button">
            Retry failed batch
          </button>
        ) : null}
      </div>
      {error ? (
        <p className="ml-events__error" role="alert">
          {error}
        </p>
      ) : null}
      <ul aria-live="polite">
        {visible.map((record) => (
          <li data-status={record.status} key={record.payload.event_id}>
            <span>{record.payload.event_type.replaceAll("_", " ")}</span>
            <code>{record.payload.event_id}</code>
            <strong>{record.status}</strong>
            {record.error ? <small>{record.error}</small> : null}
            {record.status === "failed" && record.retryable ? (
              <button onClick={() => onRetry(record.payload.event_id)} type="button">
                Retry
              </button>
            ) : null}
          </li>
        ))}
      </ul>
    </section>
  );
}

export function FeedExperience({
  dwellOptions,
  eventCapacity,
  feedApi: providedFeedApi = feedApi,
  idFactory,
  now,
  profileApi: providedProfileApi = profileApi,
}: FeedExperienceProps) {
  const feed = useFeedController({ api: providedFeedApi });
  const profile = useProfileController({ api: providedProfileApi });
  const queue = useMemo(
    () =>
      new EventQueue({
        api: providedFeedApi,
        capacity: eventCapacity,
        idFactory,
        now,
      }),
    [eventCapacity, idFactory, now, providedFeedApi],
  );
  const events = useEventQueue(queue);
  const [dispatchError, setDispatchError] = useState<string | null>(null);
  const mountedRef = useRef(false);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const refreshAfterSuccess = useCallback(
    (deliveries: readonly EventDelivery[]) => {
      const succeeded = deliveries.filter(({ result }) => successful(result));
      if (!mountedRef.current || succeeded.length === 0) return;
      profile.refresh();
      if (
        feed.feedType === "personalized" &&
        succeeded.some(({ eventType }) => eventType !== "dwell")
      ) {
        feed.refresh();
      }
    },
    [feed.feedType, feed.refresh, profile.refresh],
  );

  const send = useCallback(
    async (eventType: ClientEventType, entry: FeedEntry, durationMs?: number) => {
      if (mountedRef.current) setDispatchError(null);
      try {
        const result = await events.enqueueAndSend({
          durationMs,
          eventType,
          itemId: entry.item.item_id,
          position: entry.item.position,
          requestId: entry.requestId,
        });
        refreshAfterSuccess([{ eventType, result }]);
      } catch (error) {
        if (mountedRef.current) {
          setDispatchError(error instanceof Error ? error.message : "The event could not be queued.");
        }
      }
    },
    [events.enqueueAndSend, refreshAfterSuccess],
  );

  const handleDwell = useCallback(
    ({ durationMs, entry }: DwellMeasurement) => {
      void send("dwell", entry, durationMs);
    },
    [send],
  );
  const cardRef = useDwellTracker({ ...dwellOptions, onDwell: handleDwell });
  const sentinelRef = useLoadMoreSentinel({
    enabled: feed.hasMore && !feed.isLoadingMore,
    onLoadMore: feed.loadMore,
  });
  const sending = events.records.some((record) => record.status === "sending");

  const retry = useCallback(
    async (eventId: string) => {
      const eventType = events.records.find(
        (record) => record.payload.event_id === eventId,
      )?.payload.event_type;
      const result = await events.retry(eventId);
      if (eventType) refreshAfterSuccess([{ eventType, result }]);
    },
    [events.records, events.retry, refreshAfterSuccess],
  );
  const retryBatch = useCallback(async () => {
    const eventTypes = new Map(
      events.records.map((record) => [record.payload.event_id, record.payload.event_type]),
    );
    const results = await events.retryFailedBatch();
    refreshAfterSuccess(
      results.flatMap((result) => {
        const eventType = eventTypes.get(result.event_id);
        return eventType ? [{ eventType, result }] : [];
      }),
    );
  }, [events.records, events.retryFailedBatch, refreshAfterSuccess]);

  return (
    <FeedWorkspace
      actionPending={sending}
      error={feed.error}
      eventFeedback={
        <EventFeedback
          error={dispatchError}
          onRetry={(eventId) => void retry(eventId)}
          onRetryBatch={() => void retryBatch()}
          records={events.records}
        />
      }
      feedType={feed.feedType}
      hasMore={feed.hasMore}
      isLoadingMore={feed.isLoadingMore}
      items={feed.items}
      onAction={(eventType, entry) => void send(eventType, entry)}
      onCardElement={cardRef}
      onFeedTypeChange={feed.setFeedType}
      onLoadMore={feed.loadMore}
      onOpen={(entry) => void send("click", entry)}
      onRetry={feed.retry}
      profileRail={
        <ProfileRail
          error={profile.error}
          onRetry={profile.refresh}
          profile={profile.profile}
          state={profile.state}
        />
      }
      sentinelRef={sentinelRef}
      state={feed.state}
    />
  );
}
