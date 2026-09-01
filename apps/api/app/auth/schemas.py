from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from apps.api.app.db.models import AccountStatus, Role


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=12, max_length=256)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    password: str


class UserResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    username: str
    role: Role
    status: AccountStatus
    created_at: datetime


class RoleUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: uuid.UUID
    role: Role
