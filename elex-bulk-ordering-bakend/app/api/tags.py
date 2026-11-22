"""Tags API — the operator's segmentation layer (ROADMAP §2 Phase 7).

A tag is how a 37,062-row account list becomes a workable selection: tags are
the filters, and the filter is what starts a run. So the rules here are about
never losing accounts to a tag edit:

* deleting a tag **untags** its accounts (one bulk UPDATE) and deletes none —
  the tag is a label, not an owner;
* names are unique, because two tags called "VIP" make every filter ambiguous.

Assigning a tag to accounts lives on the accounts router (``POST
/accounts/tag``), where the account filter already is.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.core.security import require_user
from app.db.base import get_db
from app.db.models import Account, Tag
from app.schemas.tag import TagOut, TagWrite

log = logging.getLogger("ordering.accounts")

router = APIRouter(
    prefix="/tags",
    tags=["tags"],
    dependencies=[Depends(require_user)],
)


def _counts(db: DbSession, tag_ids: list[int] | None = None) -> dict[int, int]:
    """How many accounts hold each tag — grouped in SQL, never per tag."""
    stmt = select(Account.tag_id, func.count()).where(Account.tag_id.is_not(None))
    if tag_ids is not None:
        stmt = stmt.where(Account.tag_id.in_(tag_ids))
    rows = db.execute(stmt.group_by(Account.tag_id)).all()
    return {int(tag_id): int(count) for tag_id, count in rows}


def _out(tag: Tag, count: int) -> TagOut:
    return TagOut(id=tag.id, name=tag.name, account_count=count)


def _reject_duplicate(db: DbSession, name: str, *, exclude_id: int | None = None) -> None:
    """409 if another tag already has this name."""
    stmt = select(Tag.id).where(Tag.name == name)
    if exclude_id is not None:
        stmt = stmt.where(Tag.id != exclude_id)
    if db.scalar(stmt) is not None:
        raise HTTPException(status_code=409, detail=f"A tag named '{name}' exists")


@router.get("", response_model=list[TagOut])
def list_tags(db: DbSession = Depends(get_db)) -> list[TagOut]:
    """Every tag with its account count, alphabetically."""
    tags = list(db.scalars(select(Tag).order_by(Tag.name.asc())).all())
    counts = _counts(db)
    return [_out(tag, counts.get(tag.id, 0)) for tag in tags]


@router.post("", response_model=TagOut, status_code=201)
def create_tag(payload: TagWrite, db: DbSession = Depends(get_db)) -> TagOut:
    _reject_duplicate(db, payload.name)
    tag = Tag(name=payload.name)
    db.add(tag)
    try:
        db.commit()
    except IntegrityError as exc:
        # The unique index is the real authority; two concurrent creates of the
        # same name must answer 409 rather than 500.
        db.rollback()
        raise HTTPException(
            status_code=409, detail=f"A tag named '{payload.name}' exists"
        ) from exc
    db.refresh(tag)
    return _out(tag, 0)


@router.patch("/{tag_id}", response_model=TagOut)
def rename_tag(
    tag_id: int, payload: TagWrite, db: DbSession = Depends(get_db)
) -> TagOut:
    tag = db.get(Tag, tag_id)
    if tag is None:
        raise HTTPException(status_code=404, detail="Tag not found")

    _reject_duplicate(db, payload.name, exclude_id=tag_id)
    tag.name = payload.name
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409, detail=f"A tag named '{payload.name}' exists"
        ) from exc
    db.refresh(tag)
    return _out(tag, _counts(db, [tag_id]).get(tag_id, 0))


@router.delete("/{tag_id}", status_code=204)
def delete_tag(tag_id: int, db: DbSession = Depends(get_db)) -> None:
    """Delete the tag and untag its accounts — the accounts themselves survive."""
    if db.scalar(select(Tag.id).where(Tag.id == tag_id)) is None:
        raise HTTPException(status_code=404, detail="Tag not found")

    untagged = db.execute(
        update(Account)
        .where(Account.tag_id == tag_id)
        .values(tag_id=None)
        .execution_options(synchronize_session=False)
    )
    # A Core DELETE rather than ``db.delete(tag)``: the ORM would de-associate
    # the children by loading every account that holds the tag, which is the one
    # thing this phase exists to avoid. The UPDATE above already cleared them.
    db.execute(delete(Tag).where(Tag.id == tag_id))
    db.commit()
    log.info("deleted tag %s; untagged %d account(s)", tag_id, untagged.rowcount or 0)
