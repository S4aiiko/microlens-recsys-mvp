import { act, render } from "@testing-library/react";
import { createElement, StrictMode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MAX_DWELL_MS } from "./event-queue";
import type { FeedEntry } from "./FeedWorkspace";
import { DwellTracker, useDwellTracker, type DwellMeasurement } from "./dwell";

const ENTRY: FeedEntry = {
  item: {
    cover: null,
    item_id: "item-1",
    model_version: "model-1",
    position: 4,
    reason: "reason",
    score: 0.8,
    source: "dssm",
    title: "Tracked item",
  },
  requestId: "request-1",
};

function setDocumentVisibility(initial: DocumentVisibilityState) {
  let visibility = initial;
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => visibility,
  });
  return (next: DocumentVisibilityState) => {
    visibility = next;
    document.dispatchEvent(new Event("visibilitychange"));
  };
}

function fakeObserver() {
  let callback: IntersectionObserverCallback | undefined;
  const observer = {
    disconnect: vi.fn(),
    observe: vi.fn(),
    unobserve: vi.fn(),
  } as unknown as IntersectionObserver;
  return {
    emit(target: Element, isIntersecting: boolean, intersectionRatio: number) {
      callback?.(
        [
          { intersectionRatio, isIntersecting, target } as unknown as IntersectionObserverEntry,
        ],
        observer,
      );
    },
    factory(next: IntersectionObserverCallback) {
      callback = next;
      return observer;
    },
    observer,
  };
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("DwellTracker", () => {
  it("counts only visible-tab time and emits once when the card leaves view", () => {
    const native = fakeObserver();
    let now = 0;
    const changeVisibility = setDocumentVisibility("visible");
    const onDwell = vi.fn();
    const tracker = new DwellTracker({
      document,
      now: () => now,
      observerFactory: native.factory,
      onDwell,
    });
    const element = document.createElement("article");
    tracker.observe(ENTRY, element);

    native.emit(element, true, 0.7);
    now = 100;
    changeVisibility("hidden");
    now = 500;
    changeVisibility("visible");
    now = 600;
    native.emit(element, false, 0);
    now = 900;
    native.emit(element, false, 0);

    expect(onDwell).toHaveBeenCalledOnce();
    expect(onDwell).toHaveBeenCalledWith({ durationMs: 200, entry: ENTRY });
    tracker.disconnect(false);
  });

  it("does not emit for an initially non-visible observation", () => {
    const native = fakeObserver();
    setDocumentVisibility("visible");
    const onDwell = vi.fn();
    const tracker = new DwellTracker({ document, observerFactory: native.factory, onDwell });
    const element = document.createElement("article");
    tracker.observe(ENTRY, element);
    native.emit(element, false, 0);
    expect(onDwell).not.toHaveBeenCalled();
    tracker.disconnect();
  });

  it("clamps an oversized positive dwell measurement", () => {
    const native = fakeObserver();
    setDocumentVisibility("visible");
    let now = 0;
    const onDwell = vi.fn();
    const tracker = new DwellTracker({
      document,
      now: () => now,
      observerFactory: native.factory,
      onDwell,
    });
    const element = document.createElement("article");
    tracker.observe(ENTRY, element);
    native.emit(element, true, 1);
    now = MAX_DWELL_MS + 50_000;
    native.emit(element, false, 0);
    expect(onDwell).toHaveBeenCalledWith({ durationMs: MAX_DWELL_MS, entry: ENTRY });
    tracker.disconnect(false);
  });

  it("flushes a positive active measurement on pagehide", () => {
    const native = fakeObserver();
    setDocumentVisibility("visible");
    let now = 10;
    const onDwell = vi.fn();
    const tracker = new DwellTracker({
      document,
      now: () => now,
      observerFactory: native.factory,
      onDwell,
    });
    const element = document.createElement("article");
    tracker.observe(ENTRY, element);
    native.emit(element, true, 0.8);
    now = 85;
    window.dispatchEvent(new Event("pagehide"));
    expect(onDwell).toHaveBeenCalledOnce();
    expect(onDwell).toHaveBeenCalledWith({ durationMs: 75, entry: ENTRY });
    tracker.disconnect(false);
  });

  it("safely supports a missing native observer", () => {
    setDocumentVisibility("visible");
    const original = globalThis.IntersectionObserver;
    // @ts-expect-error Native observer is intentionally absent in this test environment.
    delete globalThis.IntersectionObserver;
    const onDwell = vi.fn();
    const tracker = new DwellTracker({ document, onDwell });
    tracker.observe(ENTRY, document.createElement("article"));
    window.dispatchEvent(new Event("pagehide"));
    expect(onDwell).not.toHaveBeenCalled();
    tracker.disconnect();
    globalThis.IntersectionObserver = original;
  });
});

function DwellHarness({
  now,
  observerFactory,
  onDwell,
}: {
  now(): number;
  observerFactory: (callback: IntersectionObserverCallback) => IntersectionObserver;
  onDwell(measurement: DwellMeasurement): void;
}) {
  const bind = useDwellTracker({ document, now, observerFactory, onDwell });
  return createElement("article", { ref: (element) => bind(ENTRY, element) });
}

describe("useDwellTracker", () => {
  it("is StrictMode-safe and flushes only once with one observer/listener teardown", () => {
    vi.useFakeTimers();
    setDocumentVisibility("visible");
    const native = fakeObserver();
    const factory = vi.fn(native.factory);
    const addListener = vi.spyOn(document, "addEventListener");
    const removeListener = vi.spyOn(document, "removeEventListener");
    let now = 0;
    const onDwell = vi.fn();
    const view = render(
      createElement(
        StrictMode,
        null,
        createElement(DwellHarness, {
          now: () => now,
          observerFactory: factory,
          onDwell,
        }),
      ),
    );
    const element = view.container.querySelector("article")!;
    native.emit(element, true, 0.8);
    now = 120;
    view.unmount();
    act(() => vi.runAllTimers());

    expect(onDwell).toHaveBeenCalledOnce();
    expect(onDwell).toHaveBeenCalledWith({ durationMs: 120, entry: ENTRY });
    expect(factory).toHaveBeenCalledOnce();
    expect(native.observer.disconnect).toHaveBeenCalledOnce();
    expect(
      addListener.mock.calls.filter(([eventName]) => eventName === "visibilitychange"),
    ).toHaveLength(1);
    expect(
      removeListener.mock.calls.filter(([eventName]) => eventName === "visibilitychange"),
    ).toHaveLength(1);
  });
});
