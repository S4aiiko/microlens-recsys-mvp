# ruff: noqa: B008
from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.app.db.models import Role, User

from .dependencies import AuthDependencies
from .errors import ApiError
from .rate_limit import RegistrationLimiter, RegistrationLimiterUnavailable
from .schemas import LoginRequest, RegisterRequest, RoleUpdateRequest, UserResponse
from .security import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME, CookieSettings
from .service import AuthenticatedUser, AuthService

# FastAPI dependency markers are intentionally evaluated in route signatures.


def user_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        username=user.username,
        role=user.role,
        status=user.status,
        created_at=user.created_at,
    )


def build_auth_router(
    *,
    get_session: Callable[..., Session],
    service: AuthService,
    dependencies: AuthDependencies,
    limiter: RegistrationLimiter,
    cookies: CookieSettings,
) -> APIRouter:
    router = APIRouter(prefix="/api/auth", tags=["auth"])

    @router.post(
        "/register",
        response_model=UserResponse,
        status_code=status.HTTP_201_CREATED,
        operation_id="registerUser",
    )
    async def register(
        payload: RegisterRequest,
        request: Request,
        session: Session = Depends(get_session),
    ) -> UserResponse:
        identity = request.client.host if request.client else "unknown"
        try:
            allowed = await limiter.allow(identity)
        except RegistrationLimiterUnavailable as exc:
            raise ApiError(
                503,
                "registration_limiter_unavailable",
                "Registration is temporarily unavailable",
            ) from exc
        if not allowed:
            raise ApiError(429, "registration_rate_limited", "Too many registration attempts")
        with session.begin():
            user = service.register(session, payload.username, payload.password)
        return user_response(user)

    @router.post("/login", response_model=UserResponse, operation_id="loginUser")
    def login(
        payload: LoginRequest,
        response: Response,
        session: Session = Depends(get_session),
    ) -> UserResponse:
        with session.begin():
            user, issued = service.login(session, payload.username, payload.password)
        max_age = max(0, int(service.tokens.settings.lifetime.total_seconds()))
        response.set_cookie(
            SESSION_COOKIE_NAME,
            issued.token,
            httponly=True,
            secure=cookies.secure,
            samesite=cookies.same_site,
            domain=cookies.domain,
            path=cookies.path,
            max_age=max_age,
            expires=issued.expires_at,
        )
        response.set_cookie(
            CSRF_COOKIE_NAME,
            issued.csrf_token,
            httponly=False,
            secure=cookies.secure,
            samesite=cookies.same_site,
            domain=cookies.domain,
            path=cookies.path,
            max_age=max_age,
            expires=issued.expires_at,
        )
        return user_response(user)

    @router.get(
        "/me",
        response_model=UserResponse,
        operation_id="getCurrentUser",
        openapi_extra={
            "security": [{"cookieAuth": []}],
            "x-required-roles": [role.value for role in Role],
        },
    )
    def me(
        authenticated: AuthenticatedUser = Depends(dependencies.current_user),
    ) -> UserResponse:
        return user_response(authenticated.user)

    @router.post(
        "/logout",
        status_code=status.HTTP_204_NO_CONTENT,
        operation_id="logoutUser",
        openapi_extra={
            "security": [{"cookieAuth": [], "csrfHeader": []}],
            "x-required-roles": [role.value for role in Role],
        },
    )
    def logout(
        response: Response,
        session: Session = Depends(get_session),
        authenticated: AuthenticatedUser = Depends(dependencies.csrf_user),
    ) -> Response:
        service.revoke(session, authenticated)
        session.commit()
        response.delete_cookie(
            SESSION_COOKIE_NAME,
            path=cookies.path,
            domain=cookies.domain,
            secure=cookies.secure,
            httponly=True,
            samesite=cookies.same_site,
        )
        response.delete_cookie(
            CSRF_COOKIE_NAME,
            path=cookies.path,
            domain=cookies.domain,
            secure=cookies.secure,
            httponly=False,
            samesite=cookies.same_site,
        )
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    return router


def build_role_admin_router(
    *, get_session: Callable[..., Session], dependencies: AuthDependencies
) -> APIRouter:
    router = APIRouter(prefix="/api/admin", tags=["admin"])

    @router.get(
        "/users",
        response_model=list[UserResponse],
        operation_id="listAdminUsers",
        openapi_extra={
            "security": [{"cookieAuth": []}],
            "x-required-roles": [Role.ADMIN.value],
        },
    )
    def list_users(
        session: Session = Depends(get_session),
        _authenticated: AuthenticatedUser = Depends(dependencies.roles(Role.ADMIN)),
    ) -> list[UserResponse]:
        users = session.scalars(select(User).order_by(User.created_at, User.id)).all()
        return [user_response(user) for user in users]

    @router.put(
        "/roles",
        response_model=UserResponse,
        operation_id="updateUserRole",
        openapi_extra={
            "security": [{"cookieAuth": [], "csrfHeader": []}],
            "x-required-roles": [Role.ADMIN.value],
        },
    )
    def update_role(
        payload: RoleUpdateRequest,
        session: Session = Depends(get_session),
        authenticated: AuthenticatedUser = Depends(dependencies.csrf_roles(Role.ADMIN)),
    ) -> UserResponse:
        user = session.get(User, payload.user_id, with_for_update=True)
        if user is None:
            raise ApiError(404, "user_not_found", "User does not exist")
        if user.id == authenticated.user.id and payload.role != Role.ADMIN:
            raise ApiError(409, "cannot_demote_self", "An admin cannot demote its own session")
        user.role = payload.role
        session.flush()
        session.commit()
        return user_response(user)

    return router
