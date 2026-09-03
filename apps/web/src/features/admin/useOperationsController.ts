import { useCallback, useEffect, useRef, useState } from "react";
import type {
  AdminItem,
  AuditOperation,
  DurableJobResponse,
  OnlineStatus,
  OperationBatchResponse,
  OperationJobResponse,
  Role,
  User,
} from "../../api/generated";
import { toApiError, type ApiError } from "../../api/http";
import { adminApi, type AdminApi } from "./admin-api";
import {
  OperationSubmissionTracker,
  OperationValidationError,
  type OperationDraft,
  type PreparedOperation,
} from "./operation-submission";

export type OperationReceipt = OperationBatchResponse | OperationJobResponse;

export function useOperationsController(api: AdminApi = adminApi) {
  const [items, setItems] = useState<AdminItem[]>([]);
  const [itemsError, setItemsError] = useState<ApiError | null>(null);
  const [itemsLoading, setItemsLoading] = useState(false);
  const [audit, setAudit] = useState<AuditOperation[]>([]);
  const [auditError, setAuditError] = useState<ApiError | null>(null);
  const [receipt, setReceipt] = useState<OperationReceipt | null>(null);
  const [mutationError, setMutationError] = useState<ApiError | null>(null);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [mutating, setMutating] = useState(false);
  const [job, setJob] = useState<DurableJobResponse | null>(null);
  const [jobError, setJobError] = useState<ApiError | null>(null);
  const itemGeneration = useRef(0);
  const auditGeneration = useRef(0);
  const itemAbort = useRef<AbortController | null>(null);
  const auditAbort = useRef<AbortController | null>(null);
  const tracker = useRef(new OperationSubmissionTracker());

  const search = useCallback(
    async (query: string, status: OnlineStatus | null) => {
      const generation = ++itemGeneration.current;
      itemAbort.current?.abort();
      const controller = new AbortController();
      itemAbort.current = controller;
      setItemsError(null);
      setItemsLoading(true);
      try {
        const result = await api.searchItems(query, status, controller.signal);
        if (generation !== itemGeneration.current || controller.signal.aborted) return;
        setItems(result);
      } catch (error) {
        if (generation !== itemGeneration.current || controller.signal.aborted) return;
        setItemsError(toApiError(error));
      } finally {
        if (generation === itemGeneration.current && !controller.signal.aborted) {
          setItemsLoading(false);
        }
      }
    },
    [api],
  );

  const loadAudit = useCallback(async () => {
    const generation = ++auditGeneration.current;
    auditAbort.current?.abort();
    const controller = new AbortController();
    auditAbort.current = controller;
    setAuditError(null);
    try {
      const result = await api.listOperations(controller.signal);
      if (generation !== auditGeneration.current || controller.signal.aborted) return;
      setAudit(result);
    } catch (error) {
      if (generation !== auditGeneration.current || controller.signal.aborted) return;
      setAuditError(toApiError(error));
    }
  }, [api]);

  const submit = useCallback(
    async (draft: OperationDraft, selected: readonly AdminItem[]) => {
      setMutationError(null);
      setValidationError(null);
      setMutating(true);
      let prepared: PreparedOperation;
      try {
        prepared = tracker.current.prepare(draft, selected);
        const result =
          prepared.kind === "scheduled"
            ? await api.createScheduledOperation(prepared.payload)
            : prepared.payload.operation_type === "promote"
              ? await api.createPromotion(prepared.payload)
              : await api.createBatch(prepared.payload);
        tracker.current.finish();
        setReceipt(result);
        await Promise.all([search("", null), loadAudit()]);
        return result;
      } catch (error) {
        if (error instanceof OperationValidationError) {
          tracker.current.finish();
          setValidationError(error.message);
          return null;
        }
        const apiError = toApiError(error);
        if (apiError.kind !== "network") tracker.current.finish();
        setMutationError(apiError);
        return null;
      } finally {
        setMutating(false);
      }
    },
    [api, loadAudit, search],
  );

  const lookupJob = useCallback(
    async (operationId: string) => {
      setJobError(null);
      try {
        setJob(await api.getOperationJob(operationId.trim()));
      } catch (error) {
        setJobError(toApiError(error));
      }
    },
    [api],
  );

  const cancelJob = useCallback(
    async (operationId: string) => {
      setJobError(null);
      try {
        const result = await api.cancelScheduledOperation(operationId.trim());
        setReceipt(result);
        setJob(result.job);
      } catch (error) {
        setJobError(toApiError(error));
      }
    },
    [api],
  );

  useEffect(() => {
    void search("", null);
    void loadAudit();
    return () => {
      itemAbort.current?.abort();
      auditAbort.current?.abort();
    };
  }, [loadAudit, search]);

  return {
    audit,
    auditError,
    cancelJob,
    items,
    itemsError,
    itemsLoading,
    job,
    jobError,
    loadAudit,
    lookupJob,
    mutating,
    mutationError,
    receipt,
    search,
    submit,
    validationError,
  };
}

export function useRoleManagementController(api: AdminApi = adminApi, role: Role) {
  const [users, setUsers] = useState<User[]>([]);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(role === "admin");
  const [updatingId, setUpdatingId] = useState<string | null>(null);
  const generation = useRef(0);
  const activeController = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    if (role !== "admin") return;
    const currentGeneration = ++generation.current;
    activeController.current?.abort();
    const controller = new AbortController();
    activeController.current = controller;
    setLoading(true);
    setError(null);
    try {
      const result = await api.listUsers(controller.signal);
      if (currentGeneration !== generation.current || controller.signal.aborted) return;
      setUsers(result);
    } catch (caught) {
      if (currentGeneration !== generation.current || controller.signal.aborted) return;
      setError(toApiError(caught));
    } finally {
      if (currentGeneration === generation.current && !controller.signal.aborted) setLoading(false);
    }
  }, [api, role]);

  useEffect(() => {
    void load();
    return () => activeController.current?.abort();
  }, [load]);

  const update = useCallback(
    async (userId: string, nextRole: Role) => {
      setUpdatingId(userId);
      setError(null);
      try {
        const updated = await api.updateRole(userId, nextRole);
        setUsers((current) => current.map((user) => (user.id === updated.id ? updated : user)));
      } catch (caught) {
        setError(toApiError(caught));
      } finally {
        setUpdatingId(null);
      }
    },
    [api],
  );

  return { error, loading, refresh: load, update, updatingId, users };
}
