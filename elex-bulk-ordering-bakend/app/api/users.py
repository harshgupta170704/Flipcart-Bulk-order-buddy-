"""Users API — who may sign in and what they may do (spec FR-058, FR-059).

Admin-only, every route: the router carries ``require_admin``. An admin
creates a user with a temporary password (handed over out of band; the user
must change it at their first sign-in), changes a name or a role, deactivates
a user (their existing token is refused on their next request, not at their
next sign-in), and resets a forgotten password the same way it was first set
— which signs that user out everywhere, since tokens older than the change
are refused. There is no delete: a user who ran things stays on file as who
ran them.

Two rows are protected from the admin's own hand: an admin cannot deactivate
or demote themselves (the one way to lock everybody out), and the configured
admin stays an active admin — the environment's credential is the way back
in when every password is lost, which is also why no admin resets that row's
password here: the environment's password is its reset.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.core.security import Principal, hash_password, require_admin
from app.core.users import is_env_admin, normalise_email, password_problem
from app.db.base import get_db
from app.db.models import User, UserRole, utcnow
from app.schemas.user import PasswordResetIn, UserCreate, UserOut, UserPatch

log = logging.getLogger("ordering.users")

router = APIRouter(
    prefix="/users",
    tags=["users"],
    dependencies=[Depends(require_admin)],
)


def _out(user: User) -> UserOut:
    out = UserOut.model_validate(user)
    out.is_env_admin = is_env_admin(user.email)
    return out


def _get(db: DbSession, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def _email_taken(db: DbSession, email: str) -> bool:
    return db.scalar(select(User.id).where(User.email == email)) is not None


@router.get("", response_model=list[UserOut])
def list_users(db: DbSession = Depends(get_db)) -> list[UserOut]:
    rows = db.scalars(select(User).order_by(User.email.asc())).all()
    return [_out(user) for user in rows]


@router.post("", response_model=UserOut, status_code=201)
def create_user(payload: UserCreate, me: Principal = Depends(require_admin), db: DbSession = Depends(get_db)) -> UserOut:
    """Add a user with a temporary password they must change at first sign-in."""
    email = normalise_email(payload.email)
    problem = password_problem(payload.temporary_password, email=email)
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    taken = HTTPException(status_code=409, detail="A user with this email already exists.")
    if _email_taken(db, email):
        raise taken
    user = User(
        email=email,
        name=payload.name.strip(),
        password_hash=hash_password(payload.temporary_password),
        role=UserRole(payload.role),
        active=True,
        must_change_password=True,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        # Two admins adding the same email at once: the unique index decides,
        # and the loser gets the same answer as if they had been second.
        db.rollback()
        raise taken from None
    db.refresh(user)
    log.info("user %s created as %s by %s", email, payload.role, me.email)
    return _out(user)


@router.patch("/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    payload: UserPatch,
    me: Principal = Depends(require_admin),
    db: DbSession = Depends(get_db),
) -> UserOut:
    """Change a name, a role, or whether the user may sign in."""
    user = _get(db, user_id)
    if payload.name is not None:
        user.name = payload.name.strip()
    if payload.role is not None and UserRole(payload.role) != user.role:
        if me.id == user.id:
            raise HTTPException(status_code=409, detail="You cannot change your own role.")
        if is_env_admin(user.email) and UserRole(payload.role) != UserRole.admin:
            raise HTTPException(status_code=409, detail="The configured admin stays an admin.")
        user.role = UserRole(payload.role)
    if payload.active is not None and payload.active != user.active:
        if me.id == user.id:
            raise HTTPException(status_code=409, detail="You cannot deactivate yourself.")
        if is_env_admin(user.email) and not payload.active:
            raise HTTPException(status_code=409, detail="The configured admin cannot be deactivated.")
        user.active = payload.active
    db.commit()
    db.refresh(user)
    log.info("user %s updated by %s", user.email, me.email)
    return _out(user)


@router.post("/{user_id}/reset-password", response_model=UserOut)
def reset_password(
    user_id: int,
    payload: PasswordResetIn,
    me: Principal = Depends(require_admin),
    db: DbSession = Depends(get_db),
) -> UserOut:
    """Set a temporary password the user must change at their next sign-in.
    Their existing sessions end: tokens older than the change are refused."""
    user = _get(db, user_id)
    if is_env_admin(user.email):
        raise HTTPException(
            status_code=409,
            detail="The configured admin's password is not reset here: the environment's password signs them in.",
        )
    problem = password_problem(payload.temporary_password, email=user.email)
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    user.password_hash = hash_password(payload.temporary_password)
    user.must_change_password = True
    user.password_changed_at = utcnow()
    db.commit()
    db.refresh(user)
    log.info("password of %s reset by %s", user.email, me.email)
    return _out(user)
