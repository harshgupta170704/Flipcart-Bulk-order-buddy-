"""Pydantic schemas for the users API (spec FR-055, FR-058, FR-059).

A password never travels back: ``UserOut`` carries no hash, and a temporary
password set by an admin is returned exactly once, in the response to the
call that set it, for the admin to pass on.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

Role = Literal["admin", "operator"]


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    name: str
    role: Role
    active: bool
    #: True until the user has replaced the temporary password an admin set.
    must_change_password: bool
    created_at: datetime
    last_login_at: datetime | None = None
    #: The configured admin, whose row cannot be deactivated or demoted: the
    #: environment's credential is the way back in when every password is lost.
    is_env_admin: bool = False


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    name: str = Field(default="", max_length=120)
    role: Role = "operator"
    #: Set by the admin and handed over out of band; the user must change it
    #: at their first sign-in.
    temporary_password: str = Field(min_length=1, max_length=256)


class UserPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=120)
    role: Role | None = None
    active: bool | None = None


class PasswordResetIn(BaseModel):
    """``POST /users/{id}/reset-password``: the admin's half of FR-059."""

    model_config = ConfigDict(extra="forbid")

    temporary_password: str = Field(min_length=1, max_length=256)


class PasswordChangeIn(BaseModel):
    """``POST /auth/change-password``: the user's own half."""

    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)
