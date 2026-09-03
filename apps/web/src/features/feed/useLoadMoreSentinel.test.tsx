import { render, waitFor } from "@testing-library/react";
import { useEffect } from "react";
import { describe, expect, it, vi } from "vitest";
import { useLoadMoreSentinel } from "./useLoadMoreSentinel";

function Harness({
  observerFactory,
  onLoadMore,
}: {
  observerFactory?: (
    callback: IntersectionObserverCallback,
    options: IntersectionObserverInit,
  ) => IntersectionObserver;
  onLoadMore(): void;
}) {
  const ref = useLoadMoreSentinel({ enabled: true, observerFactory, onLoadMore });
  useEffect(() => undefined, []);
  return <div data-testid="sentinel" ref={ref} />;
}

describe("useLoadMoreSentinel", () => {
  it("uses an injected native observer and requests more when intersecting", async () => {
    let callback: IntersectionObserverCallback | undefined;
    const observer = {
      disconnect: vi.fn(),
      observe: vi.fn(),
    } as unknown as IntersectionObserver;
    const onLoadMore = vi.fn();
    render(
      <Harness
        observerFactory={(next) => {
          callback = next;
          return observer;
        }}
        onLoadMore={onLoadMore}
      />,
    );
    await waitFor(() => expect(observer.observe).toHaveBeenCalledOnce());
    callback?.([{ isIntersecting: true } as IntersectionObserverEntry], observer);
    expect(onLoadMore).toHaveBeenCalledOnce();
  });

  it("does not throw without native IntersectionObserver", () => {
    const original = globalThis.IntersectionObserver;
    // @ts-expect-error Native observer is intentionally absent in this test environment.
    delete globalThis.IntersectionObserver;
    expect(() =>
      render(
        <Harness onLoadMore={vi.fn()} />,
      ),
    ).not.toThrow();
    globalThis.IntersectionObserver = original;
  });
});
