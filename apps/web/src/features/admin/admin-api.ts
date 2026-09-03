import {
  cancelOperationJob,
  compareModelVersions,
  createOperationBatch,
  createOperationJob,
  createPromotion,
  debugRecommendationRequest,
  debugUser,
  exportDashboardCsv,
  getDashboardFeeds,
  getDashboardHotItems,
  getDashboardOverview,
  getDashboardTimeseries,
  getOperationJob,
  listAdminUsers,
  listModelVersions,
  listOperations,
  listTrainingJobs,
  searchAdminItems,
  updateUserRole,
  type AdminItem,
  type AuditOperation,
  type DashboardBucket,
  type DashboardFeedDiagnostics,
  type DashboardOverview,
  type DurableJobResponse,
  type FeedType,
  type HotItem,
  type ModelComparisonResponse,
  type ModelVersionResponse,
  type OnlineStatus,
  type OperationBatchRequest,
  type OperationBatchResponse,
  type OperationJobCreateRequest,
  type OperationJobResponse,
  type RecommendationRequestDebugResponse,
  type Role,
  type TrainingJobResponse,
  type User,
  type UserDebugResponse,
} from "../../api/generated";
import type { Client } from "../../api/generated/client";
import { apiClient, toApiError } from "../../api/http";

interface ApiResult<T> {
  data?: T;
  error?: unknown;
  response?: Response;
}

function unwrap<T>(result: ApiResult<T>): T {
  if (result.error !== undefined) throw toApiError(result.error, result.response);
  if (result.data === undefined) {
    throw toApiError(new Error("The API returned no response body."), result.response);
  }
  return result.data;
}

export interface DashboardQuery {
  feedType?: FeedType | null;
  fromUtc: string;
  toUtc: string;
}

export interface AdminApi {
  cancelScheduledOperation(operationId: string): Promise<OperationJobResponse>;
  compareModels(signal?: AbortSignal): Promise<ModelComparisonResponse>;
  createBatch(payload: OperationBatchRequest): Promise<OperationBatchResponse>;
  createPromotion(payload: OperationBatchRequest): Promise<OperationBatchResponse>;
  createScheduledOperation(payload: OperationJobCreateRequest): Promise<OperationJobResponse>;
  debugRequest(requestId: string, signal?: AbortSignal): Promise<RecommendationRequestDebugResponse>;
  debugUser(userId: string, signal?: AbortSignal): Promise<UserDebugResponse>;
  exportDashboardCsv(query: DashboardQuery, signal?: AbortSignal): Promise<Blob | File>;
  getFeedDiagnostics(query: DashboardQuery, signal?: AbortSignal): Promise<DashboardFeedDiagnostics>;
  getHotItems(query: DashboardQuery, signal?: AbortSignal): Promise<HotItem[]>;
  getOperationJob(operationId: string, signal?: AbortSignal): Promise<DurableJobResponse>;
  getOverview(query: DashboardQuery, signal?: AbortSignal): Promise<DashboardOverview>;
  getTimeseries(query: DashboardQuery, signal?: AbortSignal): Promise<DashboardBucket[]>;
  listModels(signal?: AbortSignal): Promise<ModelVersionResponse[]>;
  listOperations(signal?: AbortSignal): Promise<AuditOperation[]>;
  listTrainingJobs(signal?: AbortSignal): Promise<TrainingJobResponse[]>;
  listUsers(signal?: AbortSignal): Promise<User[]>;
  searchItems(
    query: string,
    onlineStatus: OnlineStatus | null,
    signal?: AbortSignal,
  ): Promise<AdminItem[]>;
  updateRole(userId: string, role: Role): Promise<User>;
}

function queryParams(query: DashboardQuery) {
  return {
    feed_type: query.feedType ?? undefined,
    from_utc: query.fromUtc,
    to_utc: query.toUtc,
  };
}

export function createAdminApi(client: Client = apiClient): AdminApi {
  return {
    async cancelScheduledOperation(operationId) {
      return unwrap(
        await cancelOperationJob({ client, path: { operation_id: operationId } }),
      );
    },
    async compareModels(signal) {
      return unwrap(await compareModelVersions({ client, signal }));
    },
    async createBatch(payload) {
      return unwrap(await createOperationBatch({ body: payload, client }));
    },
    async createPromotion(payload) {
      return unwrap(await createPromotion({ body: payload, client }));
    },
    async createScheduledOperation(payload) {
      return unwrap(await createOperationJob({ body: payload, client }));
    },
    async debugRequest(requestId, signal) {
      return unwrap(
        await debugRecommendationRequest({
          client,
          path: { request_id: requestId },
          signal,
        }),
      );
    },
    async debugUser(userId, signal) {
      return unwrap(await debugUser({ client, path: { user_id: userId }, signal }));
    },
    async exportDashboardCsv(query, signal) {
      return unwrap(
        await exportDashboardCsv({ client, query: queryParams(query), signal }),
      );
    },
    async getFeedDiagnostics(query, signal) {
      const { feed_type: _feedType, ...range } = queryParams(query);
      return unwrap(await getDashboardFeeds({ client, query: range, signal }));
    },
    async getHotItems(query, signal) {
      const { feed_type: _feedType, ...range } = queryParams(query);
      return unwrap(
        await getDashboardHotItems({ client, query: { ...range, limit: 20 }, signal }),
      );
    },
    async getOperationJob(operationId, signal) {
      return unwrap(
        await getOperationJob({ client, path: { operation_id: operationId }, signal }),
      );
    },
    async getOverview(query, signal) {
      const { feed_type: _feedType, ...range } = queryParams(query);
      return unwrap(await getDashboardOverview({ client, query: range, signal }));
    },
    async getTimeseries(query, signal) {
      return unwrap(
        await getDashboardTimeseries({ client, query: queryParams(query), signal }),
      );
    },
    async listModels(signal) {
      return unwrap(await listModelVersions({ client, signal }));
    },
    async listOperations(signal) {
      return unwrap(await listOperations({ client, signal }));
    },
    async listTrainingJobs(signal) {
      return unwrap(await listTrainingJobs({ client, signal }));
    },
    async listUsers(signal) {
      return unwrap(await listAdminUsers({ client, signal }));
    },
    async searchItems(query, onlineStatus, signal) {
      return unwrap(
        await searchAdminItems({
          client,
          query: { online_status: onlineStatus, query: query || null },
          signal,
        }),
      );
    },
    async updateRole(userId, role) {
      return unwrap(await updateUserRole({ body: { role, user_id: userId }, client }));
    },
  };
}

export const adminApi = createAdminApi();
