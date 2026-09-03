import { useCallback, useEffect, useRef, useState } from "react";
import type { UserProfileResponse } from "../../api/generated";
import { ApiError } from "../../api/http";
import type { ProfileRailError, ProfileRailState } from "./ProfileRail";
import { profileApi, type ProfileApi } from "./profile-api";

export interface ProfileControllerOptions {
  api?: ProfileApi;
}

export interface ProfileController {
  error?: ProfileRailError;
  isRefreshing: boolean;
  profile: UserProfileResponse | null;
  refresh(): void;
  state: ProfileRailState;
}

function errorView(error: unknown): { error: ProfileRailError; state: ProfileRailState } {
  if (error instanceof ApiError) {
    const message =
      error.kind === "unauthorized"
        ? "Sign in to load your profile."
        : error.kind === "forbidden"
          ? "This account cannot access the requested profile."
          : error.message;
    return {
      error: { message, requestId: error.requestId },
      state: error.kind === "network" ? "offline" : "error",
    };
  }
  return {
    error: { message: error instanceof Error ? error.message : "The profile request failed." },
    state: "error",
  };
}

export function useProfileController(options: ProfileControllerOptions = {}): ProfileController {
  const api = options.api ?? profileApi;
  const [profile, setProfile] = useState<UserProfileResponse | null>(null);
  const [state, setState] = useState<ProfileRailState>("loading");
  const [error, setError] = useState<ProfileRailError>();
  const [isRefreshing, setIsRefreshing] = useState(false);
  const profileRef = useRef(profile);
  const generationRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    profileRef.current = profile;
  }, [profile]);

  const refresh = useCallback(() => {
    const generation = ++generationRef.current;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const retained = profileRef.current !== null;
    setIsRefreshing(retained);
    setError(undefined);
    setState(retained ? "ready" : "loading");
    void api
      .getMyProfile(controller.signal)
      .then((next) => {
        if (generation !== generationRef.current) return;
        profileRef.current = next;
        setProfile(next);
        setState("ready");
      })
      .catch((caught: unknown) => {
        if (controller.signal.aborted || generation !== generationRef.current) return;
        const view = errorView(caught);
        setError(view.error);
        setState(view.state);
      })
      .finally(() => {
        if (generation === generationRef.current) setIsRefreshing(false);
      });
  }, [api]);

  useEffect(() => {
    refresh();
    return () => {
      generationRef.current += 1;
      abortRef.current?.abort();
    };
  }, [refresh]);

  return { error, isRefreshing, profile, refresh, state };
}
