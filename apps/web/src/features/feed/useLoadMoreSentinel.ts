import { useCallback, useEffect, useRef } from "react";

export interface LoadMoreSentinelOptions {
  enabled: boolean;
  onLoadMore(): void;
  observerFactory?: (
    callback: IntersectionObserverCallback,
    options: IntersectionObserverInit,
  ) => IntersectionObserver;
}

function defaultObserverFactory(
  callback: IntersectionObserverCallback,
  options: IntersectionObserverInit,
): IntersectionObserver {
  return new IntersectionObserver(callback, options);
}

export function useLoadMoreSentinel({
  enabled,
  onLoadMore,
  observerFactory = defaultObserverFactory,
}: LoadMoreSentinelOptions) {
  const elementRef = useRef<Element | null>(null);
  const onLoadMoreRef = useRef(onLoadMore);
  onLoadMoreRef.current = onLoadMore;

  useEffect(() => {
    const element = elementRef.current;
    if (!enabled || !element) return;
    if (observerFactory === defaultObserverFactory && typeof IntersectionObserver === "undefined") {
      return;
    }
    const observer = observerFactory(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) onLoadMoreRef.current();
      },
      { rootMargin: "240px 0px", threshold: 0 },
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, [enabled, observerFactory]);

  return useCallback((element: Element | null) => {
    elementRef.current = element;
  }, []);
}
