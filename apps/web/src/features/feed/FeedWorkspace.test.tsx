import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { FeedEntry } from "./FeedWorkspace";
import { FeedWorkspace } from "./FeedWorkspace";

const ENTRY: FeedEntry = {
  item: {
    cover: null,
    item_id: "item-42",
    model_version: "model-alpha",
    position: 0,
    reason: "Similar to titles you liked",
    score: 0.81234,
    source: "item_item_cf",
    title: "A typed recommendation",
  },
  requestId: "request-42",
};

describe("FeedWorkspace", () => {
  it("exposes a segmented feed control and responsive layout contract", () => {
    const onFeedTypeChange = vi.fn();
    const { container } = render(
      <FeedWorkspace
        feedType="personalized"
        items={[]}
        onFeedTypeChange={onFeedTypeChange}
        profileRail={<div>Profile rail</div>}
        state="empty"
      />,
    );

    const root = container.querySelector(".ml-feed-workspace");
    expect(root?.getAttribute("data-layout")).toBe("feed-profile");
    expect(root?.getAttribute("data-state")).toBe("empty");
    expect(screen.getByRole("button", { name: "For you" }).getAttribute("aria-pressed")).toBe(
      "true",
    );

    fireEvent.click(screen.getByRole("button", { name: "Explore" }));
    expect(onFeedTypeChange).toHaveBeenCalledWith("explore");
  });

  it("renders stable metadata, honest missing-cover state and typed actions", () => {
    const onAction = vi.fn();
    render(
      <FeedWorkspace
        feedType="personalized"
        items={[ENTRY]}
        onAction={onAction}
        onFeedTypeChange={vi.fn()}
        state="ready"
      />,
    );

    expect(screen.getByRole("img", { name: "Cover unavailable" })).toBeTruthy();
    expect(screen.getByText("model-alpha")).toBeTruthy();
    expect(screen.getByText("request-42")).toBeTruthy();
    expect(screen.getByText("0.8123")).toBeTruthy();

    const actions = screen.getByLabelText("Actions for A typed recommendation");
    expect(within(actions).getByRole("button", { name: "Open" })).toBeTruthy();
    fireEvent.click(within(actions).getByRole("button", { name: "Like" }));
    expect(onAction).toHaveBeenCalledWith("like", ENTRY);
  });

  it("keeps loaded content visible with an offline recovery state", () => {
    const onRetry = vi.fn();
    render(
      <FeedWorkspace
        feedType="popular"
        items={[ENTRY]}
        onFeedTypeChange={vi.fn()}
        onRetry={onRetry}
        state="offline"
      />,
    );

    expect(screen.getByText("A typed recommendation")).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toContain("offline");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it("uses skeleton, empty and fallback states without production fixtures", () => {
    const { rerender } = render(
      <FeedWorkspace
        feedType="explore"
        items={[]}
        onFeedTypeChange={vi.fn()}
        state="loading"
      />,
    );
    expect(screen.getByRole("status").getAttribute("aria-busy")).toBe("true");

    rerender(
      <FeedWorkspace
        fallbackMessage="Personalized candidates are unavailable; showing popular items."
        feedType="personalized"
        items={[]}
        onFeedTypeChange={vi.fn()}
        state="empty"
      />,
    );
    expect(screen.getByText("No recommendations in this view")).toBeTruthy();
    expect(screen.getByText("Fallback active")).toBeTruthy();
  });

  it("keeps an accessible manual load-more control beside the observer sentinel", () => {
    const loadMore = vi.fn();
    const sentinelRef = vi.fn();
    render(
      <FeedWorkspace
        feedType="personalized"
        hasMore
        items={[ENTRY]}
        onFeedTypeChange={vi.fn()}
        onLoadMore={loadMore}
        sentinelRef={sentinelRef}
        state="ready"
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(loadMore).toHaveBeenCalledOnce();
    expect(sentinelRef).toHaveBeenCalled();
  });

  it("falls back to the stable No cover placeholder when a non-null image fails", () => {
    const entry: FeedEntry = { ...ENTRY, item: { ...ENTRY.item, cover: "/broken-cover.jpg" } };
    const { container } = render(
      <FeedWorkspace
        feedType="personalized"
        items={[entry]}
        onFeedTypeChange={vi.fn()}
        state="ready"
      />,
    );
    const image = container.querySelector(".ml-feed-card__cover img");
    expect(image).toBeTruthy();
    fireEvent.error(image!);
    expect(container.querySelector(".ml-feed-card__cover img")).toBeNull();
    expect(screen.getByRole("img", { name: "Cover unavailable" })).toBeTruthy();
    expect(screen.getByText("No cover")).toBeTruthy();
  });
});
