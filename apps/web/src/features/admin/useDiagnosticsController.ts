import { useCallback, useEffect, useRef, useState } from "react";
import type {
  ModelComparisonResponse,
  ModelVersionResponse,
  RecommendationRequestDebugResponse,
  TrainingJobResponse,
  UserDebugResponse,
} from "../../api/generated";
import { toApiError, type ApiError } from "../../api/http";
import { adminApi, type AdminApi } from "./admin-api";

interface LookupState<T> {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
}

const EMPTY_LOOKUP = { data: null, error: null, loading: false };

export function useDiagnosticsController(api: AdminApi = adminApi) {
  const [user, setUser] = useState<LookupState<UserDebugResponse>>(EMPTY_LOOKUP);
  const [request, setRequest] =
    useState<LookupState<RecommendationRequestDebugResponse>>(EMPTY_LOOKUP);
  const userGeneration = useRef(0);
  const requestGeneration = useRef(0);
  const userAbort = useRef<AbortController | null>(null);
  const requestAbort = useRef<AbortController | null>(null);

  const lookupUser = useCallback(
    async (userId: string) => {
      const id = userId.trim();
      if (!id) return;
      const generation = ++userGeneration.current;
      userAbort.current?.abort();
      const controller = new AbortController();
      userAbort.current = controller;
      setUser((current) => ({ ...current, error: null, loading: true }));
      try {
        const data = await api.debugUser(id, controller.signal);
        if (generation !== userGeneration.current || controller.signal.aborted) return;
        setUser({ data, error: null, loading: false });
      } catch (error) {
        if (generation !== userGeneration.current || controller.signal.aborted) return;
        setUser((current) => ({ ...current, error: toApiError(error), loading: false }));
      }
    },
    [api],
  );

  const lookupRequest = useCallback(
    async (requestId: string) => {
      const id = requestId.trim();
      if (!id) return;
      const generation = ++requestGeneration.current;
      requestAbort.current?.abort();
      const controller = new AbortController();
      requestAbort.current = controller;
      setRequest((current) => ({ ...current, error: null, loading: true }));
      try {
        const data = await api.debugRequest(id, controller.signal);
        if (generation !== requestGeneration.current || controller.signal.aborted) return;
        setRequest({ data, error: null, loading: false });
      } catch (error) {
        if (generation !== requestGeneration.current || controller.signal.aborted) return;
        setRequest((current) => ({ ...current, error: toApiError(error), loading: false }));
      }
    },
    [api],
  );

  useEffect(
    () => () => {
      userAbort.current?.abort();
      requestAbort.current?.abort();
    },
    [],
  );

  return { lookupRequest, lookupUser, request, user };
}

interface ModelsState {
  comparison: ModelComparisonResponse | null;
  comparisonError: ApiError | null;
  error: ApiError | null;
  jobs: TrainingJobResponse[];
  loading: boolean;
  models: ModelVersionResponse[];
}

export function useModelsController(api: AdminApi = adminApi) {
  const [state, setState] = useState<ModelsState>({
    comparison: null,
    comparisonError: null,
    error: null,
    jobs: [],
    loading: true,
    models: [],
  });
  const generation = useRef(0);
  const activeController = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    const currentGeneration = ++generation.current;
    activeController.current?.abort();
    const controller = new AbortController();
    activeController.current = controller;
    setState((current) => ({ ...current, error: null, loading: true }));
    try {
      const [models, jobs] = await Promise.all([
        api.listModels(controller.signal),
        api.listTrainingJobs(controller.signal),
      ]);
      let comparison: ModelComparisonResponse | null = null;
      let comparisonError: ApiError | null = null;
      try {
        comparison = await api.compareModels(controller.signal);
      } catch (error) {
        comparisonError = toApiError(error);
      }
      if (generation.current !== currentGeneration || controller.signal.aborted) return;
      setState({
        comparison,
        comparisonError,
        error: null,
        jobs,
        loading: false,
        models,
      });
    } catch (error) {
      if (generation.current !== currentGeneration || controller.signal.aborted) return;
      setState((current) => ({ ...current, error: toApiError(error), loading: false }));
    }
  }, [api]);

  useEffect(() => {
    void load();
    return () => activeController.current?.abort();
  }, [load]);

  return { refresh: load, state };
}
