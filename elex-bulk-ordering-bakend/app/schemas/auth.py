"""Pydantic schemas for the auth API."""

from datetime import datetime

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    #: The user's email (spec FR-055). Still called ``username`` on the wire so
    #: an older client keeps working; the configured admin's name is accepted
    #: as-is even when it is not an email.
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=1, max_length=256)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds, so the UI can refresh before expiry
    #: Set when an admin gave this user a temporary password: the UI takes
    #: them to change it before anything else.
    must_change_password: bool = False


class MeOut(BaseModel):
    """Who the token belongs to, and what they may do."""

    #: The email; kept under the old name so an older client keeps working.
    username: str
    email: str = ""
    name: str = ""
    role: str = "operator"
    must_change_password: bool = False
    #: The configured admin: the environment's password stays a second key
    #: for this account whatever password it sets in the app.
    is_env_admin: bool = False
    #: When the token expires, for the session countdown (spec C-13).
    expires_at: datetime | None = None


class PasswordChangedOut(MeOut):
    """``POST /auth/change-password``: who you are now, plus a fresh token —
    the change signs the user out everywhere else."""

    access_token: str
    expires_in: int


class AuthOptionsOut(BaseModel):
    """What the sign-in page may offer (``GET /auth/options``, public)."""

    #: A mail server is configured, so "Forgot password?" can send a link.
    password_reset_by_email: bool = False


class ForgotPasswordIn(BaseModel):
    email: str = Field(min_length=1, max_length=150)


class ResetPasswordIn(BaseModel):
    token: str = Field(min_length=1, max_length=2048)
    new_password: str = Field(min_length=1, max_length=256)
