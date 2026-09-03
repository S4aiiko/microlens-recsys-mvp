import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router";
import { useSession } from "../auth/session";
import { type ApiError, toApiError } from "../api/http";
import { AuthSurface } from "../features/auth";
import { FeedExperience } from "../features/feed";
import { getSafeIntendedPath } from "./intended-path";

function formError(error: unknown): ApiError {
  return toApiError(error);
}

export function LoginPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { login } = useSession();
  const [error, setError] = useState<ApiError | null>(null);
  const [pending, setPending] = useState(false);

  async function handleSubmit(credentials: { password: string; username: string }) {
    setError(null);
    setPending(true);
    try {
      await login(credentials);
      navigate(getSafeIntendedPath(searchParams.get("from")), { replace: true });
    } catch (caught) {
      setError(formError(caught));
    } finally {
      setPending(false);
    }
  }

  const registered = searchParams.get("registered") === "1";

  return (
    <AuthSurface
      error={
        error
          ? { message: error.message, requestId: error.requestId, title: "Login failed" }
          : undefined
      }
      mode="login"
      onModeChange={() => navigate("/register")}
      onSubmit={(credentials) => void handleSubmit(credentials)}
      status={pending ? "submitting" : error ? "error" : registered ? "success" : "idle"}
    />
  );
}

export function RegisterPage() {
  const navigate = useNavigate();
  const { register } = useSession();
  const [error, setError] = useState<ApiError | null>(null);
  const [pending, setPending] = useState(false);

  async function handleSubmit(credentials: { password: string; username: string }) {
    setError(null);
    setPending(true);
    try {
      await register(credentials);
      navigate("/login?registered=1", { replace: true });
    } catch (caught) {
      setError(formError(caught));
    } finally {
      setPending(false);
    }
  }

  return (
    <AuthSurface
      error={
        error
          ? { message: error.message, requestId: error.requestId, title: "Registration failed" }
          : undefined
      }
      mode="register"
      onModeChange={() => navigate("/login")}
      onSubmit={(credentials) => void handleSubmit(credentials)}
      status={pending ? "submitting" : error ? "error" : "idle"}
    />
  );
}

export function FeedFoundationPage() {
  return <FeedExperience />;
}
