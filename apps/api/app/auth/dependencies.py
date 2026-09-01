# ruff: noqa: B008
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from apps.api.app.db.models import Role

from .errors import ApiError
from .security import CSRF_COOKIE_NAME, CSRF_HEADER_NAME, SESSION_COOKIE_NAME, csrf_matches
from .service import AuthenticatedUser, AuthService

# FastAPI dependency markers are intentionally evaluated in dependency signatures.

SessionDependency = Callable[..., Session]


@dataclass(frozen=True)
class AuthDependencies:
    current_user: Callable[..., AuthenticatedUser]
    csrf_user: Callable[..., AuthenticatedUser]

    def roles(self, *allowed: Role) -> Callable[..., AuthenticatedUser]:
        async def dependency(
            authenticated: AuthenticatedUser = Depends(self.current_user),
        ) -> AuthenticatedUser:
            if authenticated.user.role not in allowed:
                raise ApiError(403, "forbidden", "The current role cannot perform this action")
            return authenticated

        return dependency

    def csrf_roles(self, *allowed: Role) -> Callable[..., AuthenticatedUser]:
        async def dependency(
            authenticated: AuthenticatedUser = Depends(self.csrf_user),
        ) -> AuthenticatedUser:
            if authenticated.user.role not in allowed:
                raise ApiError(403, "forbidden", "The current role cannot perform this action")
            return authenticated

        return dependency


def build_auth_dependencies(
    get_session: SessionDependency, service: AuthService
) -> AuthDependencies:
    async def current_user(
        request: Request,
        session: Session = Depends(get_session),
    ) -> AuthenticatedUser:
        return service.authenticate(session, request.cookies.get(SESSION_COOKIE_NAME))

    async def csrf_user(
        request: Request,
        authenticated: AuthenticatedUser = Depends(current_user),
    ) -> AuthenticatedUser:
        cookie_value = request.cookies.get(CSRF_COOKIE_NAME, "")
        header_value = request.headers.get(CSRF_HEADER_NAME, "")
        if not csrf_matches(cookie_value, header_value, authenticated.session.csrf_digest):
            raise ApiError(403, "csrf_failed", "CSRF token is missing or invalid")
        return authenticated

    return AuthDependencies(current_user=current_user, csrf_user=csrf_user)
