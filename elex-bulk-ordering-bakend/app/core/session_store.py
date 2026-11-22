"""Session (cookie) persistence — replaces the legacy ``cookes/*.pkl`` files
and the scattered ``getSession`` helpers.

Cookies are Playwright's own list-of-dicts format, stored as JSON on the
``Session`` row, so what we save is exactly what ``context.add_cookies`` needs
back — no pickle, no format conversion.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session as DbSession

from app.db.models import Account, AccountStatus, Session as SessionModel

log = logging.getLogger("ordering.sessions")


def save_session(db: DbSession, account: Account, cookies: list[dict]) -> SessionModel:
    """Store a fresh cookie set and delete the ones it replaces.

    Only the newest set is ever loaded into a browser (``load_cookies``), and
    nothing shows an account's earlier ones: the Sessions page lists each
    account's status, and the dashboard counts accounts by that status too
    (app.api.stats).
    A superseded set is of no use to the app, yet it may still sign in to the
    Flipkart account until Flipkart expires it, so it is deleted rather than
    kept as ``valid=False``. Spec NFR-003 as written allows no stored platform
    session at all; until the owner settles that (decision D-a), the tool
    keeps no more of one than a run needs: one set per account.
    """
    for old in list(account.sessions):
        db.delete(old)
    session = SessionModel(account_id=account.id, cookies=cookies, valid=True)
    db.add(session)
    account.status = AccountStatus.session_valid
    db.commit()
    db.refresh(session)
    return session


def load_cookies(db: DbSession, account: Account) -> list[dict] | None:
    """Return the latest valid cookie set for an account, or None.

    None as well for an account marked ``session_invalid``, whatever cookies
    it still holds: the mark is the app's own finding that the account is
    signed out. Every path that marks it so also deletes its sets
    (:func:`forget_session`, and ``python -m app.cli check --fix``), so this
    guards older data and a set saved by another process in the moment
    between. Such cookies handed to a coupon or order run would open a
    signed-out browser, and a login run (force off, as the Sessions page
    starts one) would skip the account as "already has session", so it would
    never be signed in again (review of 27 Oct 2026). The status is what the
    timer's read-backs go by too (``app.jobs.schedule``).
    """
    if account.status == AccountStatus.session_invalid:
        return None
    return stored_cookies(db, account)


def stored_cookies(db: DbSession, account: Account) -> list[dict] | None:
    """The newest valid cookie set the account holds, whatever its status, or
    None: for ``python -m app.cli check``, whose job is to test such a set and,
    with ``--fix``, delete a dead one — it must still find the set of an
    account a read-back marked signed out (checker of 27 Oct 2026). Runs load
    through :func:`load_cookies`."""
    session = db.scalar(
        select(SessionModel)
        .where(SessionModel.account_id == account.id, SessionModel.valid.is_(True))
        .order_by(SessionModel.id.desc())
    )
    if session is None or not session.cookies:
        return None
    return session.cookies


def has_valid_session(db: DbSession, account: Account) -> bool:
    """Whether a run can use the account's session: :func:`load_cookies` has
    one to give, so an account marked ``session_invalid`` has none."""
    return load_cookies(db, account) is not None


def drop_invalid(db: DbSession) -> int:
    """Delete every cookie set marked invalid; returns how many went.

    No browser is ever given one (:func:`load_cookies` takes the newest valid
    set) and no page shows one, yet a set may still sign in to its Flipkart
    account until Flipkart expires it. Sets superseded before 27 Oct 2026
    were kept as invalid instead of deleted (:func:`save_session` deletes them
    now), so the start-up pass clears those (spec NFR-003; review of 27 Oct
    2026)."""
    gone = db.execute(delete(SessionModel).where(SessionModel.valid.is_(False))).rowcount or 0
    db.commit()
    return gone


def drop_invalid_in_own_session() -> int:
    """:func:`drop_invalid` at start-up, in a session of its own. Never stops a
    start: a database that cannot be cleared now is cleared at the next."""
    from app.db.base import SessionLocal  # local: the engine is built from settings at import

    try:
        with SessionLocal() as db:
            gone = drop_invalid(db)
    except SQLAlchemyError as exc:
        log.warning("could not clear invalid sign-in cookie sets (%s)", type(exc).__name__)
        return 0
    if gone:
        log.info("deleted %d invalid sign-in cookie set(s)", gone)
    return gone


def forget_session(db: DbSession, account: Account, *, since: datetime | None = None) -> bool:
    """The account's sign-in no longer works: delete its cookie sets and mark
    it ``session_invalid``, in one commit. True if it did.

    For a read-back that finds Flipkart signed out, and a login run that ends
    without a session. A dead set left behind as valid went on counting as a
    valid session on the dashboard, made a plain login run pass the account
    over as "already has session", and was loaded into order and coupon runs
    (review of 27 Oct 2026). No run is to load it again, and the set a failed
    login leaves behind may still sign in until Flipkart expires it, so it is
    deleted, as :func:`save_session` deletes the sets it replaces (spec NFR-003).

    ``since`` is when the caller took the account's sign-in in hand: a
    read-back loading its cookies, a login run starting. A set saved after
    that is a sign-in that finished meanwhile, not the one that failed, so it
    is kept and the account left as that sign-in left it (False).
    """
    if since is not None:
        cutoff = _naive_utc(since)
        if any(s.created_at is not None and _naive_utc(s.created_at) > cutoff for s in account.sessions):
            return False
    for old in list(account.sessions):
        db.delete(old)
    account.status = AccountStatus.session_invalid
    db.commit()
    return True


def _naive_utc(moment: datetime) -> datetime:
    """A stamp as the plain DateTime column holds it: UTC, without a zone."""
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(timezone.utc).replace(tzinfo=None)
