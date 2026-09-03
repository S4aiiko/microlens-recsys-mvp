import { describe, expect, it } from "vitest";
import type { AdminItem } from "../../api/generated";
import {
  OperationSubmissionTracker,
  type OperationDraft,
} from "./operation-submission";

const ITEM: AdminItem = {
  cover: null,
  heat: 10,
  item_id: "item-1",
  online_status: "online",
  state_version: 7,
  title: "Item one",
  updated_at: "2026-09-02T00:00:00Z",
};

function draft(overrides: Partial<OperationDraft> = {}): OperationDraft {
  return {
    endsLocal: "",
    kind: "offline",
    maxAttempts: 3,
    priority: 0,
    reason: "policy",
    scheduled: false,
    scopeType: "all",
    scopeValue: "",
    startsLocal: "2026-09-02T10:00",
    targetPosition: null,
    ...overrides,
  };
}

describe("OperationSubmissionTracker", () => {
  it("reuses the exact payload and identifiers for a network retry", () => {
    let next = 0;
    const tracker = new OperationSubmissionTracker(() => `id-${++next}`);
    const first = tracker.prepare(draft(), [ITEM]);
    const retry = tracker.prepare(draft(), [ITEM]);

    expect(retry).toBe(first);
    expect(first).toEqual({
      kind: "immediate",
      payload: expect.objectContaining({
        batch_id: "id-1",
        operation_type: "offline",
        semantics: "preflight_then_all_or_nothing_transaction",
        targets: ["item-1"],
      }),
    });
  });

  it("creates a new identifier after any field or target state changes", () => {
    let next = 0;
    const tracker = new OperationSubmissionTracker(() => `id-${++next}`);
    const first = tracker.prepare(draft(), [ITEM]);
    const changed = tracker.prepare(draft({ reason: "new reason" }), [ITEM]);
    const stateChanged = tracker.prepare(draft({ reason: "new reason" }), [
      { ...ITEM, state_version: 8 },
    ]);

    expect(first.kind === "immediate" && first.payload.batch_id).toBe("id-1");
    expect(changed.kind === "immediate" && changed.payload.batch_id).toBe("id-2");
    expect(stateChanged.kind === "immediate" && stateChanged.payload.batch_id).toBe("id-3");
  });

  it("enforces the 100-target maximum before creating an identifier", () => {
    let ids = 0;
    const tracker = new OperationSubmissionTracker(() => `id-${++ids}`);
    const items = Array.from({ length: 101 }, (_, index) => ({
      ...ITEM,
      item_id: `item-${index}`,
    }));

    expect(() => tracker.prepare(draft(), items)).toThrow("at most 100");
    expect(ids).toBe(0);
  });

  it("builds scheduled promotion scope, state versions and retry metadata", () => {
    const tracker = new OperationSubmissionTracker(() => "operation-1");
    expect(
      tracker.prepare(
        draft({
          endsLocal: "2026-09-02T13:00",
          kind: "promote",
          maxAttempts: 5,
          priority: 9,
          scheduled: true,
          scopeType: "feed",
          scopeValue: "explore",
          targetPosition: 2,
        }),
        [ITEM],
      ),
    ).toEqual({
      kind: "scheduled",
      payload: {
        due_at: "2026-09-02T02:00:00.000Z",
        ends_at_utc: "2026-09-02T05:00:00.000Z",
        idempotency_key: "operation:operation-1",
        kind: "promote",
        max_attempts: 5,
        operation_id: "operation-1",
        priority: 9,
        reason: "policy",
        scope_type: "feed",
        scope_value: "explore",
        target_position: 2,
        targets: [{ state_version: 7, target_id: "item-1" }],
      },
    });
  });
});
