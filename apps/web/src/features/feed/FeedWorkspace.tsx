import { useState, type ReactNode } from "react";
import type { ClientEventType, FeedItem, FeedType } from "../../api/generated";
import "./feed.css";

export interface FeedEntry {
  item: FeedItem;
  requestId: string;
}

export type FeedViewState = "ready" | "loading" | "empty" | "error" | "offline";

export interface FeedViewError {
  message: string;
  requestId?: string | null;
  title?: string;
}

export interface FeedWorkspaceProps {
  actionPending?: boolean;
  error?: FeedViewError;
  eventFeedback?: ReactNode;
  fallbackMessage?: string | null;
  feedType: FeedType;
  hasMore?: boolean;
  isLoadingMore?: boolean;
  items: readonly FeedEntry[];
  onAction?: (eventType: ClientEventType, entry: FeedEntry) => void;
  onCardElement?: (entry: FeedEntry, element: Element | null) => void;
  onFeedTypeChange: (feedType: FeedType) => void;
  onLoadMore?: () => void;
  onOpen?: (entry: FeedEntry) => void;
  onRetry?: () => void;
  profileRail?: ReactNode;
  sentinelRef?: (element: Element | null) => void;
  state: FeedViewState;
}

const FEED_LABELS: ReadonlyArray<{ label: string; value: FeedType }> = [
  { label: "For you", value: "personalized" },
  { label: "Popular", value: "popular" },
  { label: "Explore", value: "explore" },
];

const ACTIONS: ReadonlyArray<{ eventType: ClientEventType; label: string }> = [
  { eventType: "click", label: "Open" },
  { eventType: "like", label: "Like" },
  { eventType: "not_interested", label: "Not interested" },
  { eventType: "revisit", label: "Revisit" },
  { eventType: "share", label: "Share" },
];

function FeedSkeleton() {
  return (
    <div className="ml-feed__skeleton-list" aria-hidden="true">
      {[0, 1, 2].map((key) => (
        <div className="ml-feed__skeleton" key={key}>
          <span className="ml-feed__skeleton-cover" />
          <span className="ml-feed__skeleton-copy">
            <i />
            <i />
            <i />
          </span>
        </div>
      ))}
    </div>
  );
}

function FeedState({
  error,
  onRetry,
  state,
}: Pick<FeedWorkspaceProps, "error" | "onRetry" | "state">) {
  if (state === "loading") {
    return (
      <section className="ml-feed__state" aria-busy="true" aria-live="polite" role="status">
        <span>Loading recommendations</span>
        <FeedSkeleton />
      </section>
    );
  }

  if (state === "empty") {
    return (
      <section className="ml-feed__state ml-feed__state--empty">
        <strong>No recommendations in this view</strong>
        <p>Try another feed or refresh to request a new snapshot.</p>
      </section>
    );
  }

  if (state === "error" || state === "offline") {
    return (
      <section className="ml-feed__state ml-feed__state--error" role="alert">
        <strong>{state === "offline" ? "You are offline" : (error?.title ?? "Feed unavailable")}</strong>
        <p>
          {state === "offline"
            ? "Loaded recommendations stay visible when available. Reconnect to request more."
            : (error?.message ?? "The recommendation request could not be completed.")}
        </p>
        {error?.requestId ? <code>Request {error.requestId}</code> : null}
        {onRetry ? (
          <button onClick={onRetry} type="button">
            Try again
          </button>
        ) : null}
      </section>
    );
  }

  return null;
}

function FeedCard({
  actionPending,
  entry,
  onAction,
  onCardElement,
  onOpen,
}: Pick<FeedWorkspaceProps, "actionPending" | "onAction" | "onCardElement" | "onOpen"> & {
  entry: FeedEntry;
}) {
  const { item, requestId } = entry;
  const [coverFailed, setCoverFailed] = useState(false);

  return (
    <article
      className="ml-feed-card"
      data-source={item.source}
      ref={(element) => onCardElement?.(entry, element)}
    >
      <div className="ml-feed-card__cover">
        {item.cover && !coverFailed ? (
          <img alt="" loading="lazy" onError={() => setCoverFailed(true)} src={item.cover} />
        ) : (
          <div className="ml-feed-card__cover-missing" role="img" aria-label="Cover unavailable">
            <span aria-hidden="true">No cover</span>
          </div>
        )}
        <span className="ml-feed-card__position">#{item.position + 1}</span>
      </div>

      <div className="ml-feed-card__body">
        <div className="ml-feed-card__title-row">
          <button
            className="ml-feed-card__title"
            disabled={!onOpen}
            onClick={() => onOpen?.(entry)}
            type="button"
          >
            {item.title}
          </button>
          <span className="ml-feed-card__source">{item.source.replaceAll("_", " ")}</span>
        </div>

        <p className="ml-feed-card__reason">{item.reason}</p>

        <dl className="ml-feed-card__meta">
          <div>
            <dt>Score</dt>
            <dd>{item.score.toFixed(4)}</dd>
          </div>
          <div>
            <dt>Item</dt>
            <dd>{item.item_id}</dd>
          </div>
          <div>
            <dt>Model</dt>
            <dd>{item.model_version}</dd>
          </div>
          <div>
            <dt>Request</dt>
            <dd>{requestId}</dd>
          </div>
        </dl>

        <div className="ml-feed-card__actions" aria-label={`Actions for ${item.title}`}>
          {ACTIONS.map((action) => (
            <button
              disabled={actionPending || !onAction}
              key={action.eventType}
              onClick={() => onAction?.(action.eventType, entry)}
              type="button"
            >
              {action.label}
            </button>
          ))}
        </div>
      </div>
    </article>
  );
}

export function FeedWorkspace({
  actionPending = false,
  error,
  eventFeedback,
  fallbackMessage,
  feedType,
  hasMore = false,
  isLoadingMore = false,
  items,
  onAction,
  onCardElement,
  onFeedTypeChange,
  onLoadMore,
  onOpen,
  onRetry,
  profileRail,
  sentinelRef,
  state,
}: FeedWorkspaceProps) {
  const retainsItems = items.length > 0 && (state === "error" || state === "offline");
  const showList = state === "ready" || retainsItems;

  return (
    <section
      className="ml-feed-workspace"
      data-layout={profileRail ? "feed-profile" : "feed-only"}
      data-state={state}
      aria-labelledby="ml-feed-title"
    >
      <div className="ml-feed-workspace__main">
        <header className="ml-feed__header">
          <div>
            <p>Recommendation feed</p>
            <h1 id="ml-feed-title">Your feed</h1>
          </div>
          <span>{items.length} loaded</span>
        </header>

        <div className="ml-feed__segments" role="group" aria-label="Feed type">
          {FEED_LABELS.map((option) => (
            <button
              aria-pressed={feedType === option.value}
              key={option.value}
              onClick={() => onFeedTypeChange(option.value)}
              type="button"
            >
              {option.label}
            </button>
          ))}
        </div>

        {fallbackMessage ? (
          <p className="ml-feed__fallback" role="status">
            <strong>Fallback active</strong>
            <span>{fallbackMessage}</span>
          </p>
        ) : null}

        {retainsItems ? <FeedState error={error} onRetry={onRetry} state={state} /> : null}
        {!showList ? <FeedState error={error} onRetry={onRetry} state={state} /> : null}

        {showList ? (
          <div className="ml-feed__list">
            {items.map((entry) => (
              <FeedCard
                actionPending={actionPending}
                entry={entry}
                key={`${entry.requestId}:${entry.item.position}:${entry.item.item_id}`}
                onAction={onAction}
                onCardElement={onCardElement}
                onOpen={onOpen}
              />
            ))}
          </div>
        ) : null}

        {eventFeedback}

        {showList && hasMore ? (
          <div className="ml-feed__more">
            <div className="ml-feed__sentinel" aria-hidden="true" ref={sentinelRef} />
            <button disabled={isLoadingMore || !onLoadMore} onClick={onLoadMore} type="button">
              {isLoadingMore ? "Loading more..." : "Load more"}
            </button>
          </div>
        ) : null}
      </div>

      {profileRail ? <aside className="ml-feed-workspace__rail">{profileRail}</aside> : null}
    </section>
  );
}
