import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { EventItemResult, FeedPage, UserProfileResponse } from "../../api/generated";
import { ApiError } from "../../api/http";
import type { EventQueueRecord } from "./event-queue";
import type { FeedApi } from "./feed-api";
import { EventFeedback, FeedExperience } from "./FeedExperience";

const PAGE: FeedPage = {
  items: [
    {
      cover: null,
      item_id: "item-1",
      model_version: "model-1",
      position: 0,
      reason: "Title affinity",
      score: 0.9,
      source: "dssm",
      title: "First item",
    },
  ],
  model_version: "model-1",
  next_cursor: null,
  request_id: "request-1",
  snapshot_id: "snapshot-1",
};

const SECOND_ITEM: FeedPage["items"][number] = {
  cover: null,
  item_id: "item-2",
  model_version: "model-1",
  position: 1,
  reason: "Related history",
  score: 0.8,
  source: "profile_title",
  title: "Second item",
};

const PROFILE: UserProfileResponse = {
  dwell_summary: {},
  negative_summary: {},
  positive_summary: {},
  profile_version: 1,
  recent_interactions: [],
  revisit_summary: {},
  share_summary: {},
  title_preferences: {},
  updated_at: "2026-09-02T10:00:00Z",
  user_id: "user-1",
};

function setup(status: EventItemResult["status"]) {
  const getPage = vi.fn<FeedApi["getPage"]>().mockResolvedValue(PAGE);
  const sendEvent = vi.fn<FeedApi["sendEvent"]>().mockImplementation(async (event) => ({
    event_id: event.event_id,
    status,
  }));
  const feedApi: FeedApi = { getPage, sendBatch: vi.fn(), sendEvent };
  const getMyProfile = vi.fn().mockResolvedValue(PROFILE);
  let id = 0;
  render(
    <FeedExperience
      feedApi={feedApi}
      idFactory={() => `event-${++id}`}
      profileApi={{ getMyProfile }}
    />,
  );
  return { getMyProfile, getPage, sendEvent };
}

describe("FeedExperience behavior refresh", () => {
  it.each(["accepted", "duplicate"] as const)(
    "refreshes profile and requests a cursor-free personalized snapshot after %s",
    async (status) => {
      const { getMyProfile, getPage, sendEvent } = setup(status);
      const actions = await screen.findByLabelText("Actions for First item");
      await waitFor(() => expect(getMyProfile).toHaveBeenCalledTimes(1));
      fireEvent.click(within(actions).getByRole("button", { name: "Like" }));

      await waitFor(() => expect(sendEvent).toHaveBeenCalledTimes(1));
      await waitFor(() => expect(getPage).toHaveBeenCalledTimes(2));
      await waitFor(() => expect(getMyProfile).toHaveBeenCalledTimes(2));
      expect(getPage.mock.calls[1]?.[0].cursor).toBeUndefined();
      expect(sendEvent.mock.calls[0]?.[0]).toMatchObject({
        event_id: "event-1",
        event_type: "like",
        item_id: "item-1",
        position: 0,
        request_id: "request-1",
      });
      expect(screen.getByText(status)).toBeTruthy();
    },
  );

  it("keeps a rejected result visible without refreshing profile or feed", async () => {
    const { getMyProfile, getPage, sendEvent } = setup("rejected");
    const actions = await screen.findByLabelText("Actions for First item");
    fireEvent.click(within(actions).getByRole("button", { name: "Not interested" }));
    await waitFor(() => expect(sendEvent).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByText("rejected")).toBeTruthy());
    expect(getPage).toHaveBeenCalledTimes(1);
    expect(getMyProfile).toHaveBeenCalledTimes(1);
  });

  it.each(["accepted", "duplicate"] as const)(
    "refreshes the profile but not the personalized Feed after a %s dwell",
    async (status) => {
      let intersectionCallback: IntersectionObserverCallback | undefined;
      const observer = {
        disconnect: vi.fn(),
        observe: vi.fn(),
        unobserve: vi.fn(),
      } as unknown as IntersectionObserver;
      let now = 0;
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        get: () => "visible",
      });
      const firstPage = { ...PAGE, items: [...PAGE.items, SECOND_ITEM] };
      const replacementPage = {
        ...firstPage,
        request_id: "request-2",
        snapshot_id: "snapshot-2",
      };
      const getPage = vi
        .fn<FeedApi["getPage"]>()
        .mockResolvedValueOnce(firstPage)
        .mockResolvedValue(replacementPage);
      const sendEvent = vi.fn<FeedApi["sendEvent"]>().mockImplementation(async (event) => ({
        event_id: event.event_id,
        status,
      }));
      const getMyProfile = vi.fn().mockResolvedValue(PROFILE);
      let id = 0;
      const view = render(
        <FeedExperience
          dwellOptions={{
            document,
            now: () => now,
            observerFactory: (callback) => {
              intersectionCallback = callback;
              return observer;
            },
          }}
          feedApi={{ getPage, sendBatch: vi.fn(), sendEvent }}
          idFactory={() => `dwell-${++id}`}
          profileApi={{ getMyProfile }}
        />,
      );
      const first = (await screen.findByText("First item")).closest("article")!;
      const second = screen.getByText("Second item").closest("article")!;
      await waitFor(() => expect(getMyProfile).toHaveBeenCalledOnce());
      act(() => {
        intersectionCallback?.(
          [first, second].map(
            (target) =>
              ({
                intersectionRatio: 0.8,
                isIntersecting: true,
                target,
              }) as unknown as IntersectionObserverEntry,
          ),
          observer,
        );
      });
      now = 120;
      act(() => {
        intersectionCallback?.(
          [
            {
              intersectionRatio: 0,
              isIntersecting: false,
              target: first,
            } as unknown as IntersectionObserverEntry,
          ],
          observer,
        );
      });

      await waitFor(() => expect(sendEvent).toHaveBeenCalledOnce());
      await waitFor(() => expect(getMyProfile).toHaveBeenCalledTimes(2));
      await new Promise((resolve) => setTimeout(resolve, 20));
      expect(getPage).toHaveBeenCalledOnce();
      expect(sendEvent).toHaveBeenCalledOnce();

      view.unmount();
      await waitFor(() => expect(sendEvent).toHaveBeenCalledTimes(2));
      expect(getPage).toHaveBeenCalledOnce();
      expect(getMyProfile).toHaveBeenCalledTimes(2);
    },
  );

  it("keeps a successful dwell retry profile-only", async () => {
    let intersectionCallback: IntersectionObserverCallback | undefined;
    const observer = {
      disconnect: vi.fn(),
      observe: vi.fn(),
      unobserve: vi.fn(),
    } as unknown as IntersectionObserver;
    let now = 0;
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "visible",
    });
    const getPage = vi.fn<FeedApi["getPage"]>().mockResolvedValue(PAGE);
    const sendEvent = vi
      .fn<FeedApi["sendEvent"]>()
      .mockRejectedValueOnce(new ApiError("offline", { code: "NETWORK", kind: "network" }))
      .mockImplementation(async (event) => ({ event_id: event.event_id, status: "accepted" }));
    const getMyProfile = vi.fn().mockResolvedValue(PROFILE);
    render(
      <FeedExperience
        dwellOptions={{
          document,
          now: () => now,
          observerFactory: (callback) => {
            intersectionCallback = callback;
            return observer;
          },
        }}
        feedApi={{ getPage, sendBatch: vi.fn(), sendEvent }}
        idFactory={() => "retry-dwell"}
        profileApi={{ getMyProfile }}
      />,
    );
    const card = (await screen.findByText("First item")).closest("article")!;
    act(() => {
      intersectionCallback?.(
        [
          {
            intersectionRatio: 0.8,
            isIntersecting: true,
            target: card,
          } as unknown as IntersectionObserverEntry,
        ],
        observer,
      );
    });
    now = 80;
    act(() => {
      intersectionCallback?.(
        [
          {
            intersectionRatio: 0,
            isIntersecting: false,
            target: card,
          } as unknown as IntersectionObserverEntry,
        ],
        observer,
      );
    });
    const retry = await screen.findByRole("button", { name: "Retry" });
    fireEvent.click(retry);

    await waitFor(() => expect(sendEvent).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(getMyProfile).toHaveBeenCalledTimes(2));
    expect(getPage).toHaveBeenCalledOnce();
    expect(sendEvent.mock.calls[1]?.[0]).toEqual(sendEvent.mock.calls[0]?.[0]);
  });

  it.each([
    {
      dwellStatus: "accepted" as const,
      expectedFeedCalls: 1,
      label: "only dwell succeeds",
      nonDwellStatus: "rejected" as const,
    },
    {
      dwellStatus: "rejected" as const,
      expectedFeedCalls: 2,
      label: "a non-dwell succeeds",
      nonDwellStatus: "duplicate" as const,
    },
  ])(
    "refreshes a mixed retry batch according to successful event types when $label",
    async ({ dwellStatus, expectedFeedCalls, nonDwellStatus }) => {
      let intersectionCallback: IntersectionObserverCallback | undefined;
      const observer = {
        disconnect: vi.fn(),
        observe: vi.fn(),
        unobserve: vi.fn(),
      } as unknown as IntersectionObserver;
      let now = 0;
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        get: () => "visible",
      });
      const getPage = vi.fn<FeedApi["getPage"]>().mockResolvedValue(PAGE);
      const sendEvent = vi
        .fn<FeedApi["sendEvent"]>()
        .mockRejectedValue(new ApiError("offline", { code: "NETWORK", kind: "network" }));
      const sendBatch = vi.fn<FeedApi["sendBatch"]>().mockImplementation(async (batchId, events) => {
        const results: EventItemResult[] = events.map((event) => {
          const resultStatus = event.event_type === "dwell" ? dwellStatus : nonDwellStatus;
          return {
            error_code: resultStatus === "rejected" ? "exposure_mismatch" : undefined,
            event_id: event.event_id,
            message: resultStatus === "rejected" ? "mismatch" : undefined,
            status: resultStatus,
          };
        });
        return {
          accepted: results.filter((result) => result.status === "accepted").length,
          batch_id: batchId,
          duplicate: results.filter((result) => result.status === "duplicate").length,
          rejected: results.filter((result) => result.status === "rejected").length,
          results,
          semantics: "per_item_atomic_partial_success",
        };
      });
      const getMyProfile = vi.fn().mockResolvedValue(PROFILE);
      let id = 0;
      render(
        <FeedExperience
          dwellOptions={{
            document,
            now: () => now,
            observerFactory: (callback) => {
              intersectionCallback = callback;
              return observer;
            },
          }}
          feedApi={{ getPage, sendBatch, sendEvent }}
          idFactory={() => `mixed-${++id}`}
          profileApi={{ getMyProfile }}
        />,
      );
      const card = (await screen.findByText("First item")).closest("article")!;
      act(() => {
        intersectionCallback?.(
          [
            {
              intersectionRatio: 0.8,
              isIntersecting: true,
              target: card,
            } as unknown as IntersectionObserverEntry,
          ],
          observer,
        );
      });
      now = 60;
      act(() => {
        intersectionCallback?.(
          [
            {
              intersectionRatio: 0,
              isIntersecting: false,
              target: card,
            } as unknown as IntersectionObserverEntry,
          ],
          observer,
        );
      });
      const actions = await screen.findByLabelText("Actions for First item");
      fireEvent.click(within(actions).getByRole("button", { name: "Like" }));
      await waitFor(() => expect(sendEvent).toHaveBeenCalledTimes(2));
      fireEvent.click(screen.getByRole("button", { name: "Retry failed batch" }));

      await waitFor(() => expect(sendBatch).toHaveBeenCalledOnce());
      await waitFor(() => expect(getMyProfile).toHaveBeenCalledTimes(2));
      if (expectedFeedCalls === 2) {
        await waitFor(() => expect(getPage).toHaveBeenCalledTimes(2));
      } else {
        await new Promise((resolve) => setTimeout(resolve, 20));
        expect(getPage).toHaveBeenCalledOnce();
      }
      expect(sendBatch.mock.calls[0]?.[1].map((event) => event.event_type)).toEqual([
        "dwell",
        "like",
      ]);
    },
  );

  it("shows queue-full errors independently while the Feed remains ready", async () => {
    const getPage = vi.fn<FeedApi["getPage"]>().mockResolvedValue(PAGE);
    const sendEvent = vi.fn<FeedApi["sendEvent"]>().mockRejectedValue(
      new ApiError("offline", { code: "NETWORK", kind: "network" }),
    );
    const feedApi: FeedApi = { getPage, sendBatch: vi.fn(), sendEvent };
    render(
      <FeedExperience
        eventCapacity={1}
        feedApi={feedApi}
        idFactory={() => "stable-event"}
        profileApi={{ getMyProfile: vi.fn().mockResolvedValue(PROFILE) }}
      />,
    );
    const actions = await screen.findByLabelText("Actions for First item");
    fireEvent.click(within(actions).getByRole("button", { name: "Like" }));
    await waitFor(() => expect(screen.getByText("failed")).toBeTruthy());
    fireEvent.click(within(actions).getByRole("button", { name: "Share" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("event queue is full");
    expect(screen.getByText("First item")).toBeTruthy();
  });

  it("sends unmount dwell best-effort without refreshing unmounted Feed or profile", async () => {
    let intersectionCallback: IntersectionObserverCallback | undefined;
    const observer = {
      disconnect: vi.fn(),
      observe: vi.fn(),
      unobserve: vi.fn(),
    } as unknown as IntersectionObserver;
    let now = 0;
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "visible",
    });
    const getPage = vi.fn<FeedApi["getPage"]>().mockResolvedValue(PAGE);
    const sendEvent = vi.fn<FeedApi["sendEvent"]>().mockImplementation(async (event) => ({
      event_id: event.event_id,
      status: "accepted",
    }));
    const feedApi: FeedApi = { getPage, sendBatch: vi.fn(), sendEvent };
    const getMyProfile = vi.fn().mockResolvedValue(PROFILE);
    const view = render(
      <FeedExperience
        dwellOptions={{
          document,
          now: () => now,
          observerFactory: (callback) => {
            intersectionCallback = callback;
            return observer;
          },
        }}
        feedApi={feedApi}
        idFactory={() => "dwell-event"}
        profileApi={{ getMyProfile }}
      />,
    );
    const card = (await screen.findByText("First item")).closest("article")!;
    await waitFor(() => expect(getMyProfile).toHaveBeenCalledOnce());
    intersectionCallback?.(
      [
        { intersectionRatio: 0.8, isIntersecting: true, target: card } as unknown as IntersectionObserverEntry,
      ],
      observer,
    );
    now = 120;
    view.unmount();
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(sendEvent).toHaveBeenCalledOnce();
    expect(sendEvent.mock.calls[0]?.[0]).toMatchObject({
      duration_ms: 120,
      event_type: "dwell",
    });
    expect(getPage).toHaveBeenCalledOnce();
    expect(getMyProfile).toHaveBeenCalledOnce();
  });
});

describe("EventFeedback", () => {
  it("keeps more than six partial batch item results viewable with a total count", () => {
    const statuses: EventQueueRecord["status"][] = [
      "accepted",
      "duplicate",
      "rejected",
      "failed",
      "accepted",
      "duplicate",
      "rejected",
      "accepted",
      "failed",
    ];
    const records: EventQueueRecord[] = statuses.map((status, index) => ({
      attempts: 1,
      error: status === "rejected" || status === "failed" ? `error-${index}` : null,
      payload: {
        client_timestamp: "2026-09-02T10:00:00Z",
        event_id: `event-${index}`,
        event_type: "like",
        item_id: `item-${index}`,
        position: index,
        request_id: "request-1",
      },
      result: null,
      retryable: status === "failed" && index === 3,
      status,
    }));
    render(
      <EventFeedback
        onRetry={vi.fn()}
        onRetryBatch={vi.fn()}
        records={records}
      />,
    );
    expect(screen.getByRole("heading", { name: "Interaction delivery (9)" })).toBeTruthy();
    expect(screen.getAllByRole("listitem")).toHaveLength(9);
    expect(screen.getByText("event-0")).toBeTruthy();
    expect(screen.getByText("event-8")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Retry" })).toHaveLength(1);
  });
});
