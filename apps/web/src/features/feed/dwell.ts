import { useCallback, useEffect, useRef } from "react";
import type { FeedEntry } from "./FeedWorkspace";
import { MAX_DWELL_MS } from "./event-queue";

export const DWELL_VISIBILITY_THRESHOLD = 0.5;

export interface DwellMeasurement {
  durationMs: number;
  entry: FeedEntry;
}

export interface DwellTrackerOptions {
  document?: Document;
  now?: () => number;
  observerFactory?: (
    callback: IntersectionObserverCallback,
    options: IntersectionObserverInit,
  ) => IntersectionObserver;
  onDwell(measurement: DwellMeasurement): void;
  threshold?: number;
}

interface TrackedEntry {
  accumulatedMs: number;
  element: Element;
  emitted: boolean;
  entry: FeedEntry;
  intersecting: boolean;
  startedAt: number | null;
}

function defaultObserverFactory(
  callback: IntersectionObserverCallback,
  options: IntersectionObserverInit,
): IntersectionObserver {
  if (typeof IntersectionObserver === "undefined") {
    return {
      disconnect() {},
      observe() {},
      root: null,
      rootMargin: "0px",
      scrollMargin: "0px",
      takeRecords: () => [],
      thresholds: [Number(options.threshold ?? 0)],
      unobserve() {},
    };
  }
  return new IntersectionObserver(callback, options);
}

export function feedEntryKey(entry: FeedEntry): string {
  return `${entry.requestId}:${entry.item.item_id}:${entry.item.position}`;
}

export class DwellTracker {
  private readonly document: Document;
  private readonly now: () => number;
  private readonly observer: IntersectionObserver;
  private readonly onDwell: DwellTrackerOptions["onDwell"];
  private readonly threshold: number;
  private readonly tracked = new Map<string, TrackedEntry>();
  private readonly keysByElement = new Map<Element, string>();

  constructor(options: DwellTrackerOptions) {
    this.document = options.document ?? document;
    this.now = options.now ?? (() => performance.now());
    this.onDwell = options.onDwell;
    this.threshold = options.threshold ?? DWELL_VISIBILITY_THRESHOLD;
    const observerFactory = options.observerFactory ?? defaultObserverFactory;
    this.observer = observerFactory(this.handleIntersections, { threshold: this.threshold });
    this.document.addEventListener("visibilitychange", this.handleVisibility);
    this.document.defaultView?.addEventListener("pagehide", this.handlePageHide);
  }

  observe(entry: FeedEntry, element: Element): void {
    const key = feedEntryKey(entry);
    const existing = this.tracked.get(key);
    if (existing?.element === element) return;
    if (existing) this.remove(key, true);
    this.tracked.set(key, {
      accumulatedMs: 0,
      element,
      emitted: false,
      entry,
      intersecting: false,
      startedAt: null,
    });
    this.keysByElement.set(element, key);
    this.observer.observe(element);
  }

  unobserve(entry: FeedEntry, flush = true): void {
    this.remove(feedEntryKey(entry), flush);
  }

  disconnect(flush = true): void {
    for (const key of [...this.tracked.keys()]) this.remove(key, flush);
    this.observer.disconnect();
    this.document.removeEventListener("visibilitychange", this.handleVisibility);
    this.document.defaultView?.removeEventListener("pagehide", this.handlePageHide);
  }

  private readonly handleIntersections: IntersectionObserverCallback = (entries) => {
    for (const observation of entries) {
      const key = this.keysByElement.get(observation.target);
      const tracked = key ? this.tracked.get(key) : undefined;
      if (!tracked || tracked.emitted) continue;
      const visible = observation.isIntersecting && observation.intersectionRatio >= this.threshold;
      tracked.intersecting = visible;
      if (visible && this.document.visibilityState === "visible") this.resume(tracked);
      else if (!visible) {
        this.pause(tracked);
        this.emit(tracked);
      }
    }
  };

  private readonly handleVisibility = (): void => {
    for (const tracked of this.tracked.values()) {
      if (this.document.visibilityState === "visible" && tracked.intersecting) this.resume(tracked);
      else this.pause(tracked);
    }
  };

  private readonly handlePageHide = (): void => {
    for (const tracked of this.tracked.values()) {
      this.pause(tracked);
      this.emit(tracked);
    }
  };

  private resume(tracked: TrackedEntry): void {
    if (tracked.startedAt === null && !tracked.emitted) tracked.startedAt = this.now();
  }

  private pause(tracked: TrackedEntry): void {
    if (tracked.startedAt === null) return;
    tracked.accumulatedMs += Math.max(0, this.now() - tracked.startedAt);
    tracked.startedAt = null;
  }

  private emit(tracked: TrackedEntry): void {
    if (tracked.emitted || tracked.accumulatedMs <= 0) return;
    tracked.emitted = true;
    this.onDwell({
      durationMs: Math.min(MAX_DWELL_MS, Math.max(0, Math.round(tracked.accumulatedMs))),
      entry: tracked.entry,
    });
  }

  private remove(key: string, flush: boolean): void {
    const tracked = this.tracked.get(key);
    if (!tracked) return;
    this.pause(tracked);
    if (flush) this.emit(tracked);
    this.observer.unobserve(tracked.element);
    this.keysByElement.delete(tracked.element);
    this.tracked.delete(key);
  }
}

export interface UseDwellTrackerOptions extends Omit<DwellTrackerOptions, "onDwell"> {
  onDwell(measurement: DwellMeasurement): void;
}

export function useDwellTracker(options: UseDwellTrackerOptions) {
  const optionsRef = useRef(options);
  optionsRef.current = options;
  const trackerRef = useRef<DwellTracker | null>(null);
  const nodesRef = useRef(new Map<string, { element: Element; entry: FeedEntry }>());
  const pendingDisconnectRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (pendingDisconnectRef.current !== null) {
      clearTimeout(pendingDisconnectRef.current);
      pendingDisconnectRef.current = null;
    }
    if (!trackerRef.current) {
      trackerRef.current = new DwellTracker({
        document: optionsRef.current.document,
        now: optionsRef.current.now,
        observerFactory: optionsRef.current.observerFactory,
        onDwell: (measurement) => optionsRef.current.onDwell(measurement),
        threshold: optionsRef.current.threshold,
      });
      for (const { element, entry } of nodesRef.current.values()) {
        trackerRef.current.observe(entry, element);
      }
    }
    return () => {
      pendingDisconnectRef.current = setTimeout(() => {
        trackerRef.current?.disconnect(true);
        trackerRef.current = null;
        pendingDisconnectRef.current = null;
      }, 0);
    };
  }, []);

  return useCallback((entry: FeedEntry, element: Element | null) => {
    const key = feedEntryKey(entry);
    if (element) {
      nodesRef.current.set(key, { element, entry });
      trackerRef.current?.observe(entry, element);
      return;
    }
    const previous = nodesRef.current.get(key);
    nodesRef.current.delete(key);
    if (!previous) return;
    setTimeout(() => {
      if (!nodesRef.current.has(key)) trackerRef.current?.unobserve(previous.entry, true);
    }, 0);
  }, []);
}
