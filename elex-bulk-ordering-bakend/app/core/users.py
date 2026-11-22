"""App users: who may sign in, with which role (spec FR-055, FR-058, FR-059).

Two roles (C-7, as recommended): **admin** — users, addresses, GSTs, unit
limits, settings — and **operator** — everything that moves orders: placing,
reading back, uploading, compiling, resolving alerts. Both read everything.

The configured admin (``ORDERING_ADMIN_USERNAME`` / ``…_PASSWORD_HASH``) is the
bootstrap: its row is created at start-up, and the environment's password
always signs in to that row, beside whatever password the row holds — the way
back in when every password is lost. Everyone signs in with an email and a
password; an admin creates a user with a temporary password the user must
change at their first sign-in, and resets a forgotten one the same way, or
the user asks for a reset link by mail when a mail server is configured
(C-13). A token issued before a row's password last changed is refused: a
reset signs that user out everywhere.
"""

from __future__ import annotations

import hmac
import logging
from datetime import datetime, timedelta, timezone

import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.config import get_settings
from app.core.security import (
    DEFAULT_ADMIN_PASSWORD,
    ROLE_ADMIN,
    ROLE_OPERATOR,
    ROLES,
    Principal,
    create_access_token,
    dummy_hash,
    hash_password,
    is_session_token,
    verify_password,
)
from app.db.models import User, UserRole, utcnow

log = logging.getLogger("ordering.users")

#: The least a password may be (spec FR-059 says nothing; this is the floor
#: below which "password" and "12345678" live).
MIN_PASSWORD_CHARS = 10
#: Passwords nobody should be allowed to keep, however long: the ones every
#: guessing list starts with. Compared lower-cased.
COMMON_PASSWORDS = frozenset(
    {
        "password", "password1", "password12", "password123", "passw0rd", "p@ssw0rd",
        "1234567890", "12345678910", "0123456789", "9876543210", "qwertyuiop", "asdfghjkl",
        "administrator", "admin12345", "welcome123", "letmein123", "changeme123",
        "iloveyou123", "flipkart123", "ordering123", "temporary1", "temppassword",
    }
)
#: Fewer distinct characters than this is a pattern, not a password.
MIN_DISTINCT_CHARS = 4
ENV_ADMIN_NAME = "Administrator"
#: The claim that marks a password-reset token, so an access token can never
#: be used as one or the other way round.
RESET_PURPOSE = "password-reset"
_warned_default = False


def normalise_email(value: str | None) -> str:
    """Emails compare case-insensitively and without stray spaces."""
    return (value or "").strip().lower()


def env_admin_email() -> str:
    return normalise_email(get_settings().admin_username)


def is_env_admin(email: str | None) -> bool:
    """Whether this is the configured admin — compared in constant time."""
    expected = env_admin_email()
    if not expected:
        return False
    return hmac.compare_digest(normalise_email(email).encode("utf-8"), expected.encode("utf-8"))


def password_problem(password: str, *, email: str = "") -> str | None:
    """Why a password is not acceptable, or None when it is."""
    text = password or ""
    if len(text) < MIN_PASSWORD_CHARS:
        return f"Use at least {MIN_PASSWORD_CHARS} characters."
    if email and normalise_email(text) == normalise_email(email):
        return "The password cannot be the email address."
    if text.strip().lower() in COMMON_PASSWORDS:
        return "That password is on every guessing list. Choose a less common one."
    if len(set(text)) < MIN_DISTINCT_CHARS:
        return "Use more different characters: a repeated one is easy to guess."
    return None


def env_password_ok(password: str) -> bool:
    """The environment's admin credential, hash first, plaintext as the dev fallback.

    The plaintext default ``config.py`` ships with is refused unless
    ``ORDERING_DEV_MODE`` is on: a deployment that never set a password must
    not be open to a guess of "admin".
    """
    global _warned_default
    settings = get_settings()
    if settings.admin_password_hash:
        return verify_password(password or "", settings.admin_password_hash)
    if not settings.admin_password:
        return False
    if settings.admin_password == DEFAULT_ADMIN_PASSWORD and not settings.dev_mode:
        if not _warned_default:
            _warned_default = True
            log.error(
                "the shipped default admin password is refused at sign-in: set "
                "ORDERING_ADMIN_PASSWORD_HASH (or ORDERING_DEV_MODE=true for a local setup)"
            )
        return False
    return hmac.compare_digest((password or "").encode("utf-8"), settings.admin_password.encode("utf-8"))


def env_credential_configured() -> bool:
    settings = get_settings()
    return bool(settings.admin_password_hash or settings.admin_password)


def ensure_env_admin(db: DbSession) -> User | None:
    """The configured admin as an active admin row — created on first sight.

    ``password_hash`` starts empty: the environment's credential is the row's
    key until the admin sets a password of their own, and stays a second key
    after that. A row that already existed under that email (the setting was
    pointed at an operator, say) is made an active admin, since that is what
    the setting means; its password and name are kept.
    """
    email = env_admin_email()
    if not email:
        return None
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(email=email, name=ENV_ADMIN_NAME, password_hash="", role=UserRole.admin, active=True)
        db.add(user)
        db.commit()
        db.refresh(user)
        log.info("created the configured admin's user row (%s)", email)
        return user
    changed = []
    if user.role != UserRole.admin:
        user.role = UserRole.admin
        changed.append("made an admin")
    if not user.active:
        user.active = True
        changed.append("reactivated")
    if changed:
        db.commit()
        db.refresh(user)
        log.warning("the configured admin's row (%s) was %s", email, " and ".join(changed))
    return user


def seed_env_admin() -> None:
    """Start-up entry point; never the reason the app fails to boot."""
    from app.db.base import SessionLocal  # local: base imports the models this module uses

    if env_admin_email() and not env_credential_configured():
        log.error(
            "no password is configured for the admin %r (ORDERING_ADMIN_PASSWORD_HASH and "
            "ORDERING_ADMIN_PASSWORD are both blank): set the hash, or give the row a password "
            "with 'python -m app.cli set-password <email>'",
            env_admin_email(),
        )
    with SessionLocal() as db:
        try:
            ensure_env_admin(db)
        except Exception:  # noqa: BLE001 — logged, not fatal
            log.exception("could not create the configured admin's user row")


def authenticate(db: DbSession, email: str, password: str) -> User | None:
    """The user these credentials sign in, or None.

    Exactly one password hash is verified whatever the email: the row's own
    when it has one, a dummy otherwise, so an unknown email takes as long as a
    wrong password (spec FR-056). The configured admin's environment password
    is checked as well, for that email only.
    """
    wanted = normalise_email(email)
    user = db.scalar(select(User).where(User.email == wanted)) if wanted else None
    has_own = user is not None and bool(user.password_hash)
    row_ok = verify_password(password or "", user.password_hash if has_own else dummy_hash()) and has_own
    env_ok = is_env_admin(wanted) and env_password_ok(password or "")
    if env_ok and user is None:
        user = ensure_env_admin(db)
    if user is None or not user.active or not (row_ok or env_ok):
        return None
    user.last_login_at = utcnow()
    db.commit()
    return user


def set_password(user: User, password: str) -> None:
    """A password of the user's own: ends the must-change state and signs the
    user out everywhere else (tokens older than this moment are refused).
    Never commits."""
    user.password_hash = hash_password(password)
    user.must_change_password = False
    user.password_changed_at = utcnow()


def password_epoch(user: User) -> int:
    """The moment the row's password last changed, in microseconds; 0 when never.

    Carried in every token issued for the row (the ``pch`` claim) and compared
    exactly, so a token from before a change is refused and the one handed out
    right after it is not — whatever the clock's second says.
    """
    moment = user.password_changed_at
    if moment is None:
        return 0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(round(moment.timestamp() * 1_000_000))


def issue_token(user: User) -> str:
    """An access token for this row — at sign-in and right after a password change."""
    return create_access_token(
        user.email, role=user.role.value, user_id=user.id, extra={"pch": password_epoch(user)}
    )


def to_principal(user: User) -> Principal:
    return Principal(
        id=user.id,
        email=user.email,
        name=user.name or "",
        role=user.role.value if isinstance(user.role, UserRole) else str(user.role),
        must_change_password=bool(user.must_change_password),
        is_env_admin=is_env_admin(user.email),
    )


def issued_before_password_change(claims: dict, user: User) -> bool:
    """A token minted before the row's password last changed is spent.

    The token's ``pch`` is the password epoch it was issued under; a token
    without one (issued before this rule existed, or minted by the server
    for a row) counts as issued under epoch 0, so it stays good only while
    the row has never had its password changed.
    """
    issued_under = claims.get("pch", 0)
    if not isinstance(issued_under, int) or isinstance(issued_under, bool):
        return True
    return issued_under != password_epoch(user)


def lookup_principal(claims: dict) -> Principal | None:
    """The principal behind verified token claims (see ``security.current_user``).

    A token with a ``uid`` claim — every sign-in issues one — was minted for
    that row, and is that row's or nothing: None when the row is gone (rows
    are never deleted, so the database was wiped or restored from an older
    copy), when its id now belongs to another email (a wiped database hands
    ids out again), when the row is deactivated, or when the token predates
    its last password change. Its role claim is never trusted on its own
    (audit F-42, 25 Oct 2026): the row's role is the authority.

    A token without one was minted by the server itself — the configured
    admin before any sign-in made its row, a worker, a test — and is the row
    its subject names when there is one, judged as above; otherwise it is
    trusted for the role it names, or as admin when it names the configured
    admin, or as an operator. A reset-link token (``purpose`` claim) is never
    a principal: it opens the reset route and nothing else.
    """
    from app.db.base import SessionLocal  # local: base imports the models this module uses

    if not is_session_token(claims):
        return None
    subject = str(claims.get("sub") or "")
    with SessionLocal() as db:
        if "uid" in claims:
            uid = claims["uid"]
            user = db.get(User, uid) if isinstance(uid, int) and not isinstance(uid, bool) else None
            if user is None or normalise_email(user.email) != normalise_email(subject):
                return None
        else:
            user = db.scalar(select(User).where(User.email == normalise_email(subject))) if subject else None
        if user is not None:
            if not user.active or issued_before_password_change(claims, user):
                return None
            return to_principal(user)
    role = claims.get("role")
    if role not in ROLES:
        role = ROLE_ADMIN if is_env_admin(subject) else ROLE_OPERATOR
    return Principal(id=None, email=subject, name="", role=role, is_env_admin=is_env_admin(subject))


# --- Reset links (spec FR-059 / C-13) -----------------------------------------


def reset_token(user: User) -> str:
    """A signed, short-lived, single-use token for the reset link.

    Single-use because it carries the second the password last changed: once
    the reset goes through that moment moves, and the token no longer matches.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    claims = {
        "sub": user.email,
        "uid": int(user.id),
        "purpose": RESET_PURPOSE,
        "pch": password_epoch(user),
        "iat": now,
        "exp": now + timedelta(minutes=settings.password_reset_minutes),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def user_for_reset_token(db: DbSession, token: str) -> User | None:
    """The user a reset token still belongs to, or None (expired, used, forged)."""
    settings = get_settings()
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub", "uid", "purpose", "pch"]},
        )
    except jwt.InvalidTokenError:
        return None
    if claims.get("purpose") != RESET_PURPOSE:
        return None
    uid = claims.get("uid")
    user = db.get(User, uid) if isinstance(uid, int) and not isinstance(uid, bool) else None
    if user is None or not user.active:
        return None
    # The id must still be the address the link was mailed for: a row id
    # handed on to someone else after a restore must not let the link open
    # their account (as lookup_principal checks for a session token).
    if normalise_email(user.email) != normalise_email(str(claims.get("sub") or "")):
        return None
    if password_epoch(user) != claims.get("pch"):
        return None
    return user
