import type {
  AdminItem,
  OperationBatchRequest,
  OperationJobCreateRequest,
} from "../../api/generated";
import { shanghaiLocalToUtc } from "./admin-time";

export interface OperationDraft {
  endsLocal: string;
  kind: "promote" | "offline" | "restore";
  maxAttempts: number;
  priority: number;
  reason: string;
  scheduled: boolean;
  scopeType: "all" | "user" | "feed";
  scopeValue: string;
  startsLocal: string;
  targetPosition: number | null;
}

export type PreparedOperation =
  | { kind: "immediate"; payload: OperationBatchRequest }
  | { kind: "scheduled"; payload: OperationJobCreateRequest };

export type OperationIdFactory = () => string;

export class OperationValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "OperationValidationError";
  }
}

function normalizedTargets(items: readonly AdminItem[]): AdminItem[] {
  const unique = new Map(items.map((item) => [item.item_id, item]));
  const targets = [...unique.values()].sort((left, right) =>
    left.item_id.localeCompare(right.item_id),
  );
  if (targets.length === 0) throw new OperationValidationError("Select at least one item.");
  if (targets.length > 100) {
    throw new OperationValidationError("A batch can contain at most 100 items.");
  }
  return targets;
}

function validateDraft(draft: OperationDraft): void {
  if (!draft.reason.trim()) throw new OperationValidationError("A reason is required.");
  if (draft.reason.trim().length > 500) {
    throw new OperationValidationError("The reason cannot exceed 500 characters.");
  }
  if (draft.priority < 0 || !Number.isInteger(draft.priority)) {
    throw new OperationValidationError("Priority must be a non-negative integer.");
  }
  if (draft.targetPosition !== null && (draft.targetPosition < 0 || !Number.isInteger(draft.targetPosition))) {
    throw new OperationValidationError("Target position must be a non-negative integer.");
  }
  if (draft.scopeType !== "all" && !draft.scopeValue.trim()) {
    throw new OperationValidationError("The selected scope requires a value.");
  }
  if (draft.kind !== "promote") {
    if (
      draft.scopeType !== "all" ||
      draft.scopeValue.trim() ||
      draft.priority !== 0 ||
      draft.targetPosition !== null ||
      draft.endsLocal
    ) {
      throw new OperationValidationError(
        "Scope, priority, position and end time apply only to promotions.",
      );
    }
  }
  if (draft.scheduled && (!Number.isInteger(draft.maxAttempts) || draft.maxAttempts < 1 || draft.maxAttempts > 100)) {
    throw new OperationValidationError("Max attempts must be an integer from 1 to 100.");
  }
}

function fingerprint(draft: OperationDraft, items: readonly AdminItem[]): string {
  return JSON.stringify({
    draft: { ...draft, reason: draft.reason.trim(), scopeValue: draft.scopeValue.trim() },
    targets: normalizedTargets(items).map((item) => [item.item_id, item.state_version]),
  });
}

export class OperationSubmissionTracker {
  private pending: { fingerprint: string; prepared: PreparedOperation } | null = null;

  constructor(private readonly createId: OperationIdFactory = () => crypto.randomUUID()) {}

  prepare(draft: OperationDraft, selectedItems: readonly AdminItem[]): PreparedOperation {
    validateDraft(draft);
    const targets = normalizedTargets(selectedItems);
    const nextFingerprint = fingerprint(draft, targets);
    if (this.pending?.fingerprint === nextFingerprint) return this.pending.prepared;

    const startsAtUtc = shanghaiLocalToUtc(draft.startsLocal);
    const endsAtUtc = draft.endsLocal ? shanghaiLocalToUtc(draft.endsLocal) : null;
    if (endsAtUtc && Date.parse(endsAtUtc) <= Date.parse(startsAtUtc)) {
      throw new OperationValidationError("The promotion end must be after its start.");
    }

    const operationId = this.createId();
    const prepared: PreparedOperation = draft.scheduled
      ? {
          kind: "scheduled",
          payload: {
            due_at: startsAtUtc,
            ends_at_utc: endsAtUtc,
            idempotency_key: `operation:${operationId}`,
            kind: draft.kind,
            max_attempts: draft.maxAttempts,
            operation_id: operationId,
            priority: draft.priority,
            reason: draft.reason.trim(),
            scope_type: draft.scopeType,
            scope_value: draft.scopeType === "all" ? null : draft.scopeValue.trim(),
            target_position: draft.targetPosition,
            targets: targets.map((item) => ({
              state_version: item.state_version,
              target_id: item.item_id,
            })),
          },
        }
      : {
          kind: "immediate",
          payload: {
            batch_id: operationId,
            ends_at_utc: endsAtUtc,
            operation_type: draft.kind,
            priority: draft.priority,
            reason: draft.reason.trim(),
            scope_type: draft.scopeType,
            scope_value: draft.scopeType === "all" ? null : draft.scopeValue.trim(),
            semantics: "preflight_then_all_or_nothing_transaction",
            starts_at_utc: startsAtUtc,
            target_position: draft.targetPosition,
            targets: targets.map((item) => item.item_id),
          },
        };

    this.pending = { fingerprint: nextFingerprint, prepared };
    return prepared;
  }

  finish(): void {
    this.pending = null;
  }
}
