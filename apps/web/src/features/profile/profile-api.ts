import { getMyProfile, type UserProfileResponse } from "../../api/generated";
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

export interface ProfileApi {
  getMyProfile(signal?: AbortSignal): Promise<UserProfileResponse>;
}

export function createProfileApi(client: Client = apiClient): ProfileApi {
  return {
    async getMyProfile(signal) {
      return unwrap(await getMyProfile({ client, signal }));
    },
  };
}

export const profileApi = createProfileApi();
