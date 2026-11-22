"""Auth API — sign in by email, who am I, change my password, reset by mail.

The app is internet-facing (PLAN.md §8 item 4), so every other router sits
behind ``require_user``; ``/auth/login``, ``/auth/options`` and the two reset
routes are the only public doors, and the sign-in and reset doors are
throttled: five wrong attempts in fifteen minutes lock the email for fifteen
minutes, and many more lock the calling address (an office shares one). Users
and roles live in the ``users`` table (:mod:`app.core.users`, spec
FR-055/FR-058); the configured admin is the bootstrap account, whose
environment credential always signs in beside whatever password its row holds.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.config import get_settings
from app.core import mailer, users
from app.core.login_guard import Key, guard
from app.core.security import Principal, bearer_scheme, current_user, decode_access_token, verify_password
from app.db.base import get_db
from app.db.models import User, utcnow
from app.schemas.auth import (
    AuthOptionsOut,
    ForgotPasswordIn,
    LoginRequest,
    MeOut,
    PasswordChangedOut,
    ResetPasswordIn,
    TokenOut,
)
from app.schemas.user import PasswordChangeIn

log = logging.getLogger("ordering.api.auth")

router = APIRouter(tags=["auth"])


def _client_ip(request: Request) -> str:
    """The browser's address — which behind a reverse proxy it is only because
    uvicorn is told to trust the proxy's forwarded headers (backend/Dockerfile,
    ``FORWARDED_ALLOW_IPS``); otherwise every browser would share the proxy's."""
    client = request.client
    return client.host if client else "unknown"


def _masked(identifier: str) -> str:
    """The typed sign-in name, fit for a log line.

    It is often a password typed into the wrong box, so the raw text is never
    logged. An email keeps its first two characters and its domain — enough
    to see which account is being hammered; anything else keeps only its
    length, except the configured admin's name, which is named as such.
    """
    text = (identifier or "").strip()
    if users.is_env_admin(text):
        return "the configured admin"
    local, at, domain = text.rpartition("@")
    if at and local and domain:
        return f"{local[:2]}…@{domain}"
    return f"<{len(text)} chars>"


def _throttle_keys(request: Request, email: str, *, door: str) -> list[Key]:
    """The email's key first (a right password clears it), the address's second.

    The two limits differ on purpose (spec FR-056): one person mistyping five
    times locks their own account's door, while an office behind one NAT
    address keeps signing in — the address limit is far higher, and only
    stops someone spraying many emails from one place.
    """
    settings = get_settings()
    return [
        Key(f"{door}:email:{users.normalise_email(email)}", settings.login_max_failures_per_email),
        Key(f"{door}:ip:{_client_ip(request)}", settings.login_max_failures_per_address),
    ]


def _refuse_if_locked(keys: list[Key]) -> None:
    wait = guard.retry_after(keys)
    if wait:
        minutes = max(1, (wait + 59) // 60)
        raise HTTPException(
            status_code=429,
            detail=f"Too many attempts. Try again in {minutes} minute{'s' if minutes != 1 else ''}.",
            headers={"Retry-After": str(wait)},
        )


def _token_out(user: User) -> TokenOut:
    return TokenOut(
        access_token=users.issue_token(user),
        token_type="bearer",
        expires_in=get_settings().access_token_expire_minutes * 60,
        must_change_password=bool(user.must_change_password),
    )


@router.post("/auth/login", response_model=TokenOut)
def login(payload: LoginRequest, request: Request, db: DbSession = Depends(get_db)) -> TokenOut:
    keys = _throttle_keys(request, payload.username, door="login")
    _refuse_if_locked(keys)
    user = users.authenticate(db, payload.username, payload.password)
    if user is None:
        guard.failed(keys)
        # Deliberately generic: never reveal whether the email or the password
        # was the wrong half (spec FR-056). The password is never logged, and
        # neither is the typed name: it is often the password in the wrong box.
        log.warning("failed login for %s from %s", _masked(payload.username), _client_ip(request))
        raise HTTPException(
            status_code=401,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    guard.succeeded(keys[:1])
    log.info("login ok for %r (%s)", user.email, user.role.value)
    return _token_out(user)


@router.get("/auth/options", response_model=AuthOptionsOut)
def options() -> AuthOptionsOut:
    """What the sign-in page may offer — public, and nothing secret in it."""
    return AuthOptionsOut(password_reset_by_email=mailer.mail_configured())


def _expiry(credentials: HTTPAuthorizationCredentials | None) -> datetime | None:
    if credentials is None:
        return None
    try:
        exp = decode_access_token(credentials.credentials).get("exp")
    except ValueError:
        return None
    return datetime.fromtimestamp(int(exp), tz=timezone.utc) if exp else None


def _me(principal: Principal, expires_at: datetime | None) -> MeOut:
    return MeOut(
        username=principal.email,
        email=principal.email,
        name=principal.name,
        role=principal.role,
        must_change_password=principal.must_change_password,
        is_env_admin=principal.is_env_admin,
        expires_at=expires_at,
    )


@router.get("/auth/me", response_model=MeOut)
def me(
    principal: Principal = Depends(current_user),
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> MeOut:
    """Who the current token belongs to, their role, and when the session ends
    — the UI probes a stored token with this on page load before showing the
    app, and counts the session down from ``expires_at`` (spec C-13)."""
    return _me(principal, _expiry(credentials))


@router.post("/auth/change-password", response_model=PasswordChangedOut)
def change_password(
    payload: PasswordChangeIn,
    request: Request,
    principal: Principal = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> PasswordChangedOut:
    """A password of the user's own (spec FR-059, the user's half).

    The current one must be given: the row's own, or — for the configured
    admin — the environment's. A wrong current password is a 403, not a 401:
    the session is fine, the request is not, and the client must not sign out.
    The change signs the user out everywhere else (older tokens are refused),
    so the answer carries a fresh token for this client to keep going with.
    """
    if principal.id is None:
        raise HTTPException(status_code=409, detail="This session has no user row to change the password of.")
    user = db.get(User, principal.id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    # The same door as sign-in: a token holder guessing the current password
    # here would otherwise have unlimited tries (audit F-31, 25 Oct 2026).
    keys = _throttle_keys(request, user.email, door="change")
    _refuse_if_locked(keys)
    current_ok = (bool(user.password_hash) and verify_password(payload.current_password, user.password_hash)) or (
        users.is_env_admin(user.email) and users.env_password_ok(payload.current_password)
    )
    if not current_ok:
        guard.failed(keys)
        raise HTTPException(status_code=403, detail="The current password is wrong.")
    guard.succeeded(keys)
    problem = users.password_problem(payload.new_password, email=user.email)
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    users.set_password(user, payload.new_password)
    db.commit()
    db.refresh(user)
    log.info("password changed by %s", user.email)
    fresh = _token_out(user)
    expires_at = datetime.fromtimestamp(int(decode_access_token(fresh.access_token)["exp"]), tz=timezone.utc)
    me_out = _me(users.to_principal(user), expires_at)
    return PasswordChangedOut(**me_out.model_dump(), access_token=fresh.access_token, expires_in=fresh.expires_in)


def _send_reset_mail(email: str, token: str) -> None:
    """Runs once the answer has gone out; a failure is logged, never shown."""
    try:
        mailer.send_password_reset(email, token)
    except Exception:  # noqa: BLE001 — the reply has gone; the log says why
        log.exception("could not send the reset mail to %s", email)


@router.post("/auth/forgot-password", status_code=202)
def forgot_password(
    payload: ForgotPasswordIn,
    request: Request,
    background: BackgroundTasks,
    db: DbSession = Depends(get_db),
) -> dict:
    """Ask for a reset link by mail (spec FR-059, C-13).

    Always the same answer, whether or not the email has an account: the
    reply must not say who has one — and must not take measurably longer for
    one either, so the mail is sent after the answer is on its way (a
    background task), not inline where the mail server's round trip would
    give the account away. The mail goes only to an active user's own
    address, and only when a mail server is configured; otherwise the sign-in
    page never offers this, and an admin sets a temporary password.
    """
    keys = _throttle_keys(request, payload.email, door="reset")
    _refuse_if_locked(keys)
    guard.failed(keys)  # every ask counts: five a quarter-hour per email, more per address
    answer = {"detail": "If that email has an account, a reset link is on its way."}
    if not mailer.mail_configured():
        log.warning("reset link asked for %s but no mail server is configured", _masked(payload.email))
        return answer
    wanted = users.normalise_email(payload.email)
    if wanted and "@" not in wanted:
        # The configured admin's name need not be an address ("admin"); a
        # reset link for it would be queued to the string "admin" — and that
        # link signs its reader in (audit F-31, 25 Oct 2026). Same answer.
        log.info("reset link asked for a name that is not an email address; nothing sent")
        return answer
    user = db.scalar(select(User).where(User.email == wanted)) if wanted else None
    if user is None or not user.active:
        log.info("reset link asked for an email without an active account")
        return answer
    background.add_task(_send_reset_mail, user.email, users.reset_token(user))
    return answer


@router.post("/auth/reset-password", response_model=TokenOut)
def reset_password(payload: ResetPasswordIn, db: DbSession = Depends(get_db)) -> TokenOut:
    """Choose a new password with the link from the mail, and be signed in."""
    user = users.user_for_reset_token(db, payload.token)
    if user is None:
        raise HTTPException(status_code=400, detail="This reset link is not valid any more. Ask for a new one.")
    problem = users.password_problem(payload.new_password, email=user.email)
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    users.set_password(user, payload.new_password)
    user.last_login_at = utcnow()
    db.commit()
    db.refresh(user)
    log.info("password reset by mail link for %s", user.email)
    return _token_out(user)
