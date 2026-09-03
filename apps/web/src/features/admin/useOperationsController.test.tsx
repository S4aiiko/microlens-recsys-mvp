import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { AdminItem, DurableJobResponse, OperationBatchResponse } from "../../api/generated";
import type { AdminApi } from "./admin-api";
import type { OperationDraft } from "./operation-submission";
import { useOperationsController } from "./useOperationsController";

const ITEM: AdminItem = {
  cover: null,
  heat: 3,
  item_id: "item-1",
  online_status: "online",
  state_version: 2,
  title: "Item one",
  updated_at: "2026-09-02T00:00:00Z",
};

const JOB: DurableJobResponse = {
  attempt_count: 0,
  completed_at: null,
  created_at: "2026-09-02T00:00:00Z",
  due_at: "2026-09-02T02:00:00Z",
  idempotency_key: "operation:job-1",
  job_id: "job-1",
  last_error: null,
  max_attempts: 3,
  result: null,
  state: "queued",
  task_name: "operation",
  updated_at: "2026-09-02T00:00:00Z",
};

function draft(overrides: Partial<OperationDraft> = {}): OperationDraft {
  return {
    endsLocal: "",
    kind: "offline",
    maxAttempts: 3,
    priority: 0,
    reason: "moderation",
    scheduled: false,
    scopeType: "all",
    scopeValue: "",
    startsLocal: "2026-09-02T10:00",
    targetPosition: null,
    ...overrides,
  };
}

function batch(batchId: string): OperationBatchResponse {
  return {
    batch_id: batchId,
    created_at: "2026-09-02T00:00:00Z",
    expected_state_version: 2,
    scheduled_at: null,
    status: "succeeded",
  };
}

describe("useOperationsController", () => {
  it("submits immediate offline and restore operations and refreshes audit", async () => {
    const createBatch = vi.fn().mockResolvedValueOnce(batch("offline-1")).mockResolvedValueOnce(batch("restore-1"));
    const listOperations = vi.fn().mockResolvedValue([]);
    const api = {
      createBatch,
      listOperations,
      searchItems: vi.fn().mockResolvedValue([ITEM]),
    } as unknown as AdminApi;
    const { result } = renderHook(() => useOperationsController(api));
    await waitFor(() => expect(result.current.items).toHaveLength(1));

    await act(async () => {
      await result.current.submit(draft(), [ITEM]);
      await result.current.submit(draft({ kind: "restore" }), [ITEM]);
    });

    expect(createBatch).toHaveBeenNthCalledWith(1, expect.objectContaining({ operation_type: "offline", targets: ["item-1"] }));
    expect(createBatch).toHaveBeenNthCalledWith(2, expect.objectContaining({ operation_type: "restore", targets: ["item-1"] }));
    expect(listOperations.mock.calls.length).toBeGreaterThanOrEqual(3);
  });

  it("retries an exact network-failed payload with stable identifiers", async () => {
    const createBatch = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce(batch("retry"));
    const api = {
      createBatch,
      listOperations: vi.fn().mockResolvedValue([]),
      searchItems: vi.fn().mockResolvedValue([ITEM]),
    } as unknown as AdminApi;
    const { result } = renderHook(() => useOperationsController(api));
    await waitFor(() => expect(result.current.items).toHaveLength(1));

    await act(async () => {
      await result.current.submit(draft(), [ITEM]);
    });
    expect(result.current.mutationError?.kind).toBe("network");
    await act(async () => {
      await result.current.submit(draft(), [ITEM]);
    });

    expect(createBatch.mock.calls[1]?.[0]).toBe(createBatch.mock.calls[0]?.[0]);
    expect(createBatch.mock.calls[1]?.[0].batch_id).toBe(createBatch.mock.calls[0]?.[0].batch_id);
  });

  it("creates, reads and cancels a scheduled operation job", async () => {
    const createScheduledOperation = vi.fn().mockResolvedValue({ created: true, job: JOB });
    const getOperationJob = vi.fn().mockResolvedValue(JOB);
    const cancelScheduledOperation = vi.fn().mockResolvedValue({ job: { ...JOB, state: "cancelled" } });
    const api = {
      cancelScheduledOperation,
      createScheduledOperation,
      getOperationJob,
      listOperations: vi.fn().mockResolvedValue([]),
      searchItems: vi.fn().mockResolvedValue([ITEM]),
    } as unknown as AdminApi;
    const { result } = renderHook(() => useOperationsController(api));
    await waitFor(() => expect(result.current.items).toHaveLength(1));

    await act(async () => {
      await result.current.submit(draft({ scheduled: true }), [ITEM]);
    });
    expect(createScheduledOperation).toHaveBeenCalledWith(expect.objectContaining({
      kind: "offline",
      targets: [{ state_version: 2, target_id: "item-1" }],
    }));
    expect("job" in result.current.receipt!).toBe(true);

    await act(async () => {
      await result.current.lookupJob(" job-1 ");
      await result.current.cancelJob(" job-1 ");
    });
    expect(getOperationJob).toHaveBeenCalledWith("job-1");
    expect(cancelScheduledOperation).toHaveBeenCalledWith("job-1");
    expect(result.current.job?.state).toBe("cancelled");
  });
});
