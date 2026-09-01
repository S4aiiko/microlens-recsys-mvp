import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import type { LoginRequest, RegisterRequest, User } from "../api/generated";
import { authApi, type AuthApi } from "../api/auth-api";
import { ApiError, toApiError } from "../api/http";

export type SessionState =
  | { status: "anonymous" }
  | { error: ApiError; status: "error" }
  | { status: "hydrating" }
  | { status: "authenticated"; user: User };

interface SessionContextValue {
  login(input: LoginRequest): Promise<User>;
  logout(): Promise<void>;
  refresh(): Promise<void>;
  register(input: RegisterRequest): Promise<User>;
  state: SessionState;
}

const SessionContext = createContext<SessionContextValue | null>(null);

export interface SessionProviderProps {
  api?: AuthApi;
  children: ReactNode;
}

export function SessionProvider({ api = authApi, children }: SessionProviderProps) {
  const [state, setState] = useState<SessionState>({ status: "hydrating" });

  const refresh = useCallback(async () => {
    setState({ status: "hydrating" });
    try {
      const user = await api.getCurrentUser();
      setState({ status: "authenticated", user });
    } catch (error) {
      const apiError = toApiError(error);
      setState(
        apiError.kind === "unauthorized"
          ? { status: "anonymous" }
          : { error: apiError, status: "error" },
      );
    }
  }, [api]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const value = useMemo<SessionContextValue>(
    () => ({
      async login(input) {
        const user = await api.login(input);
        setState({ status: "authenticated", user });
        return user;
      },
      async logout() {
        try {
          await api.logout();
        } catch (error) {
          const apiError = toApiError(error);
          if (apiError.kind !== "unauthorized") throw apiError;
        }
        setState({ status: "anonymous" });
      },
      refresh,
      register: (input) => api.register(input),
      state,
    }),
    [api, refresh, state],
  );

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionContextValue {
  const value = useContext(SessionContext);
  if (!value) throw new Error("useSession must be used inside SessionProvider");
  return value;
}
