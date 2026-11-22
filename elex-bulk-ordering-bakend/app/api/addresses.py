"""Addresses API — where an order actually ships (ROADMAP §2 Phase 8).

Until this phase the delivery address came from four flat config values, so every
order went to one hard-coded place under one GST. The business runs several
entities, and that failure is *quiet*: the order succeeds, it just ships to the
wrong address and bills the wrong registration. So addresses are rows now, and a
run names the one it wants.

Two rules carry the weight here:

* exactly one row may be the default, and it is cleared on the others with a
  single bulk UPDATE in the same transaction as the write — never by loading
  every address and looping;
* an unknown ``gst_id`` is a 422 and writes nothing, because an address silently
  linked to no GST is the same wrong invoice in a different disguise.

``POST /jobs/order`` picks a row by id; :func:`default_address` is what a run
uses when it names none. With neither, the run is refused — there is no config
fallback any more, because the one there was is how orders shipped to the wrong
place without anyone noticing.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import selectinload

from app.core.security import require_admin, require_user
from app.db.base import get_db
from app.db.models import Address, Gst
from app.schemas.address import AddressOut, AddressPatch, AddressWrite

log = logging.getLogger("ordering.addresses")

router = APIRouter(
    prefix="/addresses",
    tags=["addresses"],
    dependencies=[Depends(require_user)],
)

#: The fields a PATCH may write, in no particular order.
_PATCH_FIELDS = (
    "label",
    "name",
    "phone",
    "line1",
    "floor",
    "city",
    "state",
    "pincode",
    "gst_id",
    "is_default",
)


# --- shared default handling --------------------------------------------------
# Lives on this module rather than in a third file because ``gsts.py`` can import
# it without a cycle (nothing here imports that router), and the rule is one
# rule: "exactly one default" means the same thing for both tables.
def clear_other_defaults(
    db: DbSession, model: type[Address] | type[Gst], keep_id: int
) -> None:
    """Clear ``is_default`` on every row of ``model`` except ``keep_id``.

    One UPDATE, no rows loaded: the whole point of the lists is that there may
    be many of them, and "exactly one default" is a statement about the table,
    not about the rows that happen to be in this session.
    """
    db.execute(
        update(model)
        .where(model.id != keep_id, model.is_default.is_(True))
        .values(is_default=False)
        .execution_options(synchronize_session=False)
    )


def default_address(db: DbSession) -> Address | None:
    """The address a run ships to when the caller named none.

    ``None`` is a real answer — nothing promotes a replacement when the default
    is deleted, and the caller then refuses to run. The linked GST is eager
    loaded because callers (``app/jobs/tasks.py``) read ``address.gst`` from
    short-lived sessions that may already be closed by then.
    """
    return db.scalars(
        select(Address)
        .options(selectinload(Address.gst))
        .where(Address.is_default.is_(True))
        .order_by(Address.id.asc())
        .limit(1)
    ).first()


# --- helpers ------------------------------------------------------------------
def _reject_duplicate(
    db: DbSession, label: str, *, exclude_id: int | None = None
) -> None:
    """409 if another address already has this label."""
    stmt = select(Address.id).where(Address.label == label)
    if exclude_id is not None:
        stmt = stmt.where(Address.id != exclude_id)
    if db.scalar(stmt) is not None:
        raise HTTPException(
            status_code=409, detail=f"An address labelled '{label}' exists"
        )


def _require_gst(db: DbSession, gst_id: int) -> None:
    """422, not 404: an unknown GST makes the *body* wrong, not the URL."""
    if db.scalar(select(Gst.id).where(Gst.id == gst_id)) is None:
        raise HTTPException(status_code=422, detail=f"No GST with id {gst_id}")


def _conflict(label: str) -> HTTPException:
    return HTTPException(
        status_code=409, detail=f"An address labelled '{label}' exists"
    )


# --- routes -------------------------------------------------------------------
@router.get("", response_model=list[AddressOut])
def list_addresses(db: DbSession = Depends(get_db)) -> list[AddressOut]:
    """Every address with its GST inlined, alphabetically by label."""
    addresses = list(
        db.scalars(
            select(Address)
            .options(selectinload(Address.gst))  # one extra query, not one per row
            .order_by(Address.label.asc())
        ).all()
    )
    return [AddressOut.model_validate(address) for address in addresses]


@router.post("", response_model=AddressOut, status_code=201, dependencies=[Depends(require_admin)])
def create_address(
    payload: AddressWrite, db: DbSession = Depends(get_db)
) -> AddressOut:
    _reject_duplicate(db, payload.label)
    if payload.gst_id is not None:
        _require_gst(db, payload.gst_id)

    address = Address(
        label=payload.label,
        name=payload.name,
        phone=payload.phone,
        line1=payload.line1,
        floor=payload.floor,
        city=payload.city,
        state=payload.state,
        pincode=payload.pincode,
        gst_id=payload.gst_id,
        is_default=payload.is_default,
    )
    db.add(address)
    try:
        # Flush for the id, then clear the other defaults in the *same*
        # transaction, so the table is never briefly two-defaulted.
        db.flush()
        if address.is_default:
            clear_other_defaults(db, Address, address.id)
        db.commit()
    except IntegrityError as exc:
        # The unique index is the real authority; two concurrent creates of the
        # same label must answer 409 rather than 500.
        db.rollback()
        raise _conflict(payload.label) from exc
    db.refresh(address)
    log.info("created address %s (%s)", address.id, address.label)
    return AddressOut.model_validate(address)


@router.patch("/{address_id}", response_model=AddressOut, dependencies=[Depends(require_admin)])
def update_address(
    address_id: int, payload: AddressPatch, db: DbSession = Depends(get_db)
) -> AddressOut:
    """Edit one address.

    Only the fields actually present in the body are written, so a PATCH that
    sends ``phone`` cannot blank ``floor`` by omission — and ``gst_id: null``
    unlinks the GST on purpose.
    """
    address = db.get(Address, address_id)
    if address is None:
        raise HTTPException(status_code=404, detail="Address not found")

    sent = payload.model_fields_set
    label = payload.label
    if "label" in sent and label is not None:
        _reject_duplicate(db, label, exclude_id=address_id)
    if "gst_id" in sent and payload.gst_id is not None:
        _require_gst(db, payload.gst_id)

    for field in _PATCH_FIELDS:
        if field in sent:
            setattr(address, field, getattr(payload, field))

    try:
        db.flush()
        if "is_default" in sent and payload.is_default:
            clear_other_defaults(db, Address, address_id)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _conflict(label or "") from exc
    db.refresh(address)
    return AddressOut.model_validate(address)


@router.delete("/{address_id}", status_code=204, dependencies=[Depends(require_admin)])
def delete_address(address_id: int, db: DbSession = Depends(get_db)) -> None:
    """Delete one address. Its GST is untouched — the link is not ownership.

    Deleting the default leaves the table with no default, which is allowed:
    until another is marked, a run that names no address is refused (422 at
    ``POST /jobs/order``, ``DestinationError`` in the job body and the CLI)
    rather than silently shipped to whichever row we would have promoted.
    """
    if db.scalar(select(Address.id).where(Address.id == address_id)) is None:
        raise HTTPException(status_code=404, detail="Address not found")

    db.execute(delete(Address).where(Address.id == address_id))
    db.commit()
    log.info("deleted address %s", address_id)
