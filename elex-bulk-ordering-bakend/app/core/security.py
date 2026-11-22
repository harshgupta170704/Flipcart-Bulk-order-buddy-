"""App authentication primitives: password hashing, JWT access tokens, and the
dependencies that gate a route on a signed-in user or an admin.

The suite is deployed on a public web server (PLAN.md §3, §8 item 4), so it
needs a login of its own. The configured admin (``ORDERING_ADMIN_USERNAME`` /
``ORDERING_ADMIN_PASSWORD_HASH``) is the bootstrap credential; users and their
roles live in the ``users`` table (:mod:`app.core.users`, spec FR-058) and a
token carries the user's id and role. Every protected request reads the row
behind the token (``current_user``, which ``require_user`` wraps), so a user
deactivated or demoted since the token was issued is refused on the next
request rather than at the next sign-in; ``require_admin`` is the gate for
the routes only an admin may use.

Hashing is PBKDF2-SHA256 from the standard library rather than passlib/bcrypt —
one less dependency to install and audit, and PBKDF2 with a high iteration
count is an acceptable KDF for a single operator account. The stored format is

    pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>

so the iteration count travels with the hash and can be raised later without
invalidating existing hashes.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings

log = logging.getLogger("ordering.security")
#: One line per request that changes something, with who made it — the audit
#: trail a shared deployment needs (spec FR-058). Bodies are never logged.
audit = logging.getLogger("ordering.audit")

_ALGO = "pbkdf2_sha256"
#: The width of every digest this module writes (SHA-256).
_DIGEST_BYTES = 32
_SALT_BYTES = 16
# A malformed or hostile stored hash must not be able to pin a CPU for minutes,
# so the iteration count we are willing to replay is bounded.
_MAX_ITERATIONS = 10_000_000
# RFC 7518 §3.2: an HMAC-SHA256 key should be at least as long as the digest.
_MIN_SECRET_BYTES = 32
#: The values a deployment left on the shipped default would carry: the one
#: in ``config.py`` today and the one older ``.env`` files copied.
_DEFAULT_SECRETS: tuple[str, ...] = (
    "change-me-in-production",
    "dev-only-insecure-secret-change-me-in-production",
)
#: The plaintext admin password ``config.py`` ships with. Accepted at sign-in
#: only with ``ORDERING_DEV_MODE=true``, so a deployment that never set a
#: password is not open to the first person who guesses "admin".
DEFAULT_ADMIN_PASSWORD = "admin"

#: The two roles (spec FR-058, C-7). Admins manage users, addresses, GSTs,
#: unit limits and settings; operators do everything else — place orders, run
#: read-backs, upload, compile, resolve alerts.
ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLES: tuple[str, ...] = (ROLE_ADMIN, ROLE_OPERATOR)


# --- Passwords ---------------------------------------------------------------


def hash_password(password: str, *, iterations: int | None = None) -> str:
    """Hash a password for storage — in ``ORDERING_ADMIN_PASSWORD_HASH`` or on
    a user row. The work factor comes from ``ORDERING_PBKDF2_ITERATIONS``
    unless given, so a test suite can turn it down without touching callers."""
    if iterations is None:
        iterations = get_settings().pbkdf2_iterations
    if iterations < 1:
        raise ValueError("iterations must be >= 1")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGO}${iterations}${salt.hex()}${digest.hex()}"


def parse_hash(stored_hash: object) -> tuple[int, bytes, bytes] | None:
    """The (iterations, salt, digest) behind a stored hash of ours, or None.

    None for anything that is not one — malformed, truncated, a foreign
    algorithm, an absurd work factor — and never raises, so a bad config value
    can't turn into a 500 on the login route. It is also how start-up tells a
    pasted hash from one a shell ate the ``$`` parts of.
    """
    try:
        algo, raw_iterations, salt_hex, hash_hex = stored_hash.split("$")  # type: ignore[attr-defined]
        if algo != _ALGO:
            return None
        iterations = int(raw_iterations)
        salt = bytes.fromhex(salt_hex)
        digest = bytes.fromhex(hash_hex)
    except (AttributeError, TypeError, ValueError):
        return None
    if not 1 <= iterations <= _MAX_ITERATIONS or not salt or not digest:
        return None
    if len(digest) != _DIGEST_BYTES:
        # A digest cut short would be verified at its short length and so
        # match a larger share of passwords (audit F-42, 25 Oct 2026); ours
        # are always the full SHA-256 width.
        return None
    return iterations, salt, digest


def is_password_hash(value: object) -> bool:
    """Whether ``value`` is a hash this module made and can verify against."""
    return parse_hash(value) is not None


def verify_password(password: str, stored_hash: str) -> bool:
    """Check a password against a stored hash.

    Never raises: any malformed/truncated/foreign hash is simply a mismatch.
    """
    parsed = parse_hash(stored_hash)
    if parsed is None:
        return False
    iterations, salt, expected = parsed

    digest = hashlib.pbkdf2_hmac(
        "sha256",
        (password or "").encode("utf-8"),
        salt,
        iterations,
        dklen=len(expected),
    )
    # compare_digest, not ==, so a wrong password can't be found byte by byte.
    return hmac.compare_digest(digest, expected)


_dummy_hashes: dict[int, str] = {}


def dummy_hash() -> str:
    """A hash of nothing in particular, at the configured work factor.

    A sign-in whose email has no row (or a row without a password) verifies the
    submitted password against this instead of skipping the check, so an
    unknown email costs the same time as a wrong password (spec FR-056).
    """
    iterations = get_settings().pbkdf2_iterations
    stored = _dummy_hashes.get(iterations)
    if stored is None:
        stored = _dummy_hashes[iterations] = hash_password(secrets.token_urlsafe(16), iterations=iterations)
    return stored


# --- Tokens ------------------------------------------------------------------


def create_access_token(
    subject: str,
    *,
    expires_minutes: int | None = None,
    role: str | None = None,
    user_id: int | None = None,
    extra: dict | None = None,
) -> str:
    """Issue a signed bearer token for ``subject`` (the user's email).

    ``role`` and ``user_id`` ride along as claims so a request can be gated
    without a lookup; :func:`current_user` still consults the row when there
    is one, which is what makes a deactivation take effect immediately.
    ``extra`` carries further claims (the password epoch a sign-in issues).
    """
    if not subject:
        raise ValueError("subject is required")
    settings = get_settings()
    minutes = (
        settings.access_token_expire_minutes
        if expires_minutes is None
        else expires_minutes
    )
    now = datetime.now(timezone.utc)
    claims: dict = {
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(minutes=minutes),
    }
    if role is not None:
        claims["role"] = role
    if user_id is not None:
        claims["uid"] = int(user_id)
    if extra:
        claims.update(extra)
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    """Return the token's claims, or raise ``ValueError``.

    Callers (API and worker alike) shouldn't have to know about PyJWT's
    exception tree, so every failure mode collapses to ValueError.
    """
    settings = get_settings()
    try:
        return jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise ValueError("Token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise ValueError("Invalid token") from exc


def is_session_token(claims: dict) -> bool:
    """Whether verified claims are a session's — a token a sign-in issued.

    A token minted for one purpose, the password-reset link, carries a
    ``purpose`` claim and opens nothing but that purpose's route, however
    good its signature: it travelled by mail, and must not become an 8-hour
    session that outlives the reset. Every bearer path asks this — the REST
    dependency, the ``users`` lookup behind it, and the WebSocket.
    """
    return "purpose" not in claims


# --- FastAPI dependency ------------------------------------------------------

# auto_error=False so a missing header lands in our handler and answers 401 with
# a WWW-Authenticate challenge, instead of HTTPBearer's bare 403.
_bearer_scheme = HTTPBearer(auto_error=False, description="JWT from POST /auth/login")
#: For a route that needs the raw credentials beside the principal (``/auth/me``
#: reads the token's expiry for the session countdown).
bearer_scheme = _bearer_scheme


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _claims_from(credentials: HTTPAuthorizationCredentials | None) -> dict:
    """The verified claims behind a bearer header, or a 401."""
    if credentials is None or (credentials.scheme or "").lower() != "bearer":
        raise _unauthorized("Not authenticated")
    try:
        claims = decode_access_token(credentials.credentials)
    except ValueError as exc:
        raise _unauthorized(str(exc)) from exc
    if not is_session_token(claims):
        raise _unauthorized("Invalid token")
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise _unauthorized("Invalid token")
    return claims


@dataclass(frozen=True)
class Principal:
    """Who is making the request, as the ``users`` table knows them.

    ``id`` is None for a token minted outside a sign-in — the configured admin
    before the table existed, a worker, a test — whose claims are then the
    authority.
    """

    id: int | None
    email: str
    name: str
    role: str
    must_change_password: bool = False
    #: The configured admin, whose environment password is a second key.
    is_env_admin: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN


#: The only routes a user on a temporary password may use until they have
#: chosen their own (spec FR-059): seeing who they are, and changing it.
PASSWORD_CHANGE_PATHS = frozenset({"/auth/me", "/auth/change-password"})
#: The 403's ``detail`` — a code, so the client can tell it from "not allowed".
PASSWORD_CHANGE_REQUIRED = "password_change_required"
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> Principal:
    """The signed-in user, checked against the ``users`` table on every call.

    The row is the authority when there is one: a user deactivated since the
    token was issued is refused now (401, so the client signs out), as is a
    token issued before the row's password last changed (a reset signs the
    user out everywhere); a role changed since is the row's role, not the
    token's. A token a sign-in issued names its row (the ``uid`` claim) and is
    refused once that row is gone. A token without one whose subject has no
    row — the configured admin before any sign-in created it, or a token
    minted by the server itself — is trusted on its claims: its role claim,
    or admin when it is the configured admin's name.

    A user still on a temporary password gets a 403 with the code
    ``password_change_required`` everywhere but ``/auth/me`` and
    ``/auth/change-password``: the UI sends them there first, and this is the
    same rule for anyone talking to the API directly.
    """
    from app.core.users import lookup_principal  # local: users imports this module

    claims = _claims_from(credentials)
    principal = lookup_principal(claims)
    if principal is None:
        raise _unauthorized("This session is no longer valid. Sign in again.")
    path = request.scope.get("path") or request.url.path
    if principal.must_change_password and path not in PASSWORD_CHANGE_PATHS:
        raise HTTPException(status_code=403, detail=PASSWORD_CHANGE_REQUIRED)
    if request.method not in _READ_METHODS:
        audit.info("%s %s by %s", request.method, path, principal.email)
    return principal


def require_user(principal: Principal = Depends(current_user)) -> str:
    """Dependency for protected routes: returns the user's email, or raises 401.

    Use as ``Depends(require_user)`` on a route, or in a router's
    ``dependencies=[...]`` to cover every route in it. It is
    :func:`current_user` under another name, so a deactivated user is refused
    by every router at once; FastAPI resolves the shared dependency once per
    request, so a route that also takes the principal costs no second lookup.
    """
    return principal.email


def require_admin(principal: Principal = Depends(current_user)) -> Principal:
    """Gate for the routes only an admin may use (spec FR-058)."""
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="Only an admin can do this.")
    return principal


# --- Config sanity -----------------------------------------------------------


def refuse_default_secret() -> None:
    """Stop the start-up when tokens would be signed with the shipped key.

    Anyone who has read ``config.py`` knows that key, so with it anyone can
    sign a token for any user, the admin included — and ``check_security_config``
    only logs that, in a line that is easy to miss (audit F-31, 25 Oct 2026).
    The app calls this first thing in its start-up (``app.main.lifespan``),
    before anything is written, so a refused start changes nothing.
    ``ORDERING_DEV_MODE=true`` — a local setup on one machine, never a shared
    deployment — still starts, with the warning.
    """
    settings = get_settings()
    if settings.jwt_secret in _DEFAULT_SECRETS and not settings.dev_mode:
        raise RuntimeError(
            "ORDERING_JWT_SECRET is still the shipped default, so anyone could sign a token "
            "for any user: the app will not start on it. Set ORDERING_JWT_SECRET to a long "
            "random value (python -c 'import secrets; print(secrets.token_urlsafe(48))'), "
            "or set ORDERING_DEV_MODE=true for a local setup only."
        )


def check_security_config() -> list[str]:
    """Return (and log) the reasons this deployment's auth config is unsafe.

    Called at startup so a public deployment shouts about a default signing
    key or a plaintext admin password instead of quietly accepting them —
    and about a configured admin hash that is not one (a shell or a compose
    file ate its ``$`` parts, a paste cut it short): sign-in would only ever
    say "Incorrect email or password", so this is logged as an ERROR that
    names the setting.
    """
    settings = get_settings()
    problems: list[str] = []
    errors: list[str] = []

    if settings.admin_password_hash and not is_password_hash(settings.admin_password_hash):
        errors.append(
            "ORDERING_ADMIN_PASSWORD_HASH is set but is not a hash this app can read, so the "
            "configured admin cannot sign in with it. Expected "
            f"{_ALGO}$<iterations>$<salt>$<hash>, exactly as 'python -m app.core.security hash "
            "<password>' prints it — a shell or compose file may have eaten the '$' parts "
            "(put the value in single quotes), or a paste may have cut it short."
        )

    if settings.jwt_secret in _DEFAULT_SECRETS:
        problems.append(
            "ORDERING_JWT_SECRET is still the shipped default — anyone can mint "
            "valid tokens. Set it to a random value (python -c "
            "'import secrets; print(secrets.token_urlsafe(48))')."
        )
    elif len(settings.jwt_secret.encode("utf-8")) < _MIN_SECRET_BYTES:
        problems.append(
            f"ORDERING_JWT_SECRET is shorter than {_MIN_SECRET_BYTES} bytes, "
            "which is below the recommended HMAC-SHA256 key length."
        )

    if not settings.admin_password_hash and not settings.admin_password:
        problems.append(
            "Neither ORDERING_ADMIN_PASSWORD_HASH nor ORDERING_ADMIN_PASSWORD is set: "
            "the configured admin cannot sign in with an environment password. Set the "
            "hash ('python -m app.core.security hash <password>') or give the admin's "
            "row a password with 'python -m app.cli set-password <email>'."
        )
    elif not settings.admin_password_hash:
        problems.append(
            "ORDERING_ADMIN_PASSWORD_HASH is unset, so the plaintext "
            "ORDERING_ADMIN_PASSWORD is in use. Generate a hash with "
            "'python -m app.core.security hash <password>'."
        )
        if settings.admin_password == DEFAULT_ADMIN_PASSWORD:
            problems.append(
                "The admin password is still the shipped default. It is refused at "
                "sign-in unless ORDERING_DEV_MODE=true (a local setup only)."
            )
    if settings.dev_mode:
        problems.append("ORDERING_DEV_MODE is on: the shipped default admin password is accepted.")

    for problem in problems:
        log.warning("insecure auth config: %s", problem)
    for error in errors:
        log.error("broken auth config: %s", error)
    return problems + errors


if __name__ == "__main__":  # pragma: no cover - operator helper
    # python -m app.core.security hash <password>  ->  paste into .env
    import sys

    if len(sys.argv) == 3 and sys.argv[1] == "hash":
        print(hash_password(sys.argv[2]))
    else:
        print("usage: python -m app.core.security hash <password>", file=sys.stderr)
        raise SystemExit(2)
