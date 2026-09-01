import {
  getCurrentUser,
  loginUser,
  logoutUser,
  registerUser,
  type LoginRequest,
  type RegisterRequest,
  type User,
} from "./generated";
import type { Client } from "./generated/client";
import { apiClient, toApiError } from "./http";

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

export interface AuthApi {
  getCurrentUser(): Promise<User>;
  login(input: LoginRequest): Promise<User>;
  logout(): Promise<void>;
  register(input: RegisterRequest): Promise<User>;
}

export function createAuthApi(client: Client = apiClient): AuthApi {
  return {
    async getCurrentUser() {
      return unwrap(await getCurrentUser({ client }));
    },
    async login(input) {
      return unwrap(await loginUser({ body: input, client }));
    },
    async logout() {
      const result = await logoutUser({ client });
      if (result.error !== undefined) throw toApiError(result.error, result.response);
    },
    async register(input) {
      return unwrap(await registerUser({ body: input, client }));
    },
  };
}

export const authApi = createAuthApi();
