"""GSTs API — the registrations orders bill under (ROADMAP §2 Phase 8).

The business runs several entities (AARTHBODHA, BLAZECOM, DIGIDRIVE, GLOBE
COMMERCE, JP MOBILITY, SONIC KOMMERCE), each with its own GSTIN. One flat config
value billed everything to whichever one happened to be pasted in, which is
wrong *quietly* — the order goes through and the invoice names the wrong company.

The rule that matters most here is :func:`delete_gst`: a GST is a label an
address bills under, not its owner. Deleting one must null ``gst_id`` on its
addresses in a single UPDATE and lose no address row — the same shape as
deleting a tag on the accounts side. An account can have a GST of its own too
(which wins over the run's, app.jobs.tasks), and deleting the GST unlinks
those accounts the same way: one UPDATE, every account kept.

``POST /jobs/order`` picks a row by id. When it names none, the run bills under
the GST its address is linked to, then :func:`default_gst`; with neither it is
placed without GST details and says so in its log — never under a config value.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.api.addresses import clear_other_defaults
from app.core.security import require_admin, require_user
from app.db.base import get_db
from app.db.models import Account, Address, Gst
from app.schemas.address import GstDeleted, GstOut, GstPatch, GstWrite

log = logging.getLogger("ordering.addresses")

router = APIRouter(
    prefix="/gsts",
    tags=["gsts"],
    dependencies=[Depends(require_user)],
)

#: The fields a PATCH may write.
_PATCH_FIELDS = ("label", "gstin", "business_name", "is_default")


def default_gst(db: DbSession) -> Gst | None:
    """The GST a run bills under when the caller named none.

    ``None`` is a real answer: deleting the default promotes nothing, and a run
    with no GST chosen or linked is then placed without GST details rather than
    billed to an entity nobody chose.
    """
    return db.scalars(
        select(Gst)
        .where(Gst.is_default.is_(True))
        .order_by(Gst.id.asc())
        .limit(1)
    ).first()


# --- helpers ------------------------------------------------------------------
def _counts(
    db: DbSession, gst_ids: list[int] | None = None, *, model: type[Address] | type[Account] = Address
) -> dict[int, int]:
    """How many addresses (or accounts) bill under each GST — grouped in SQL,
    never per row."""
    stmt = select(model.gst_id, func.count()).where(model.gst_id.is_not(None))
    if gst_ids is not None:
        stmt = stmt.where(model.gst_id.in_(gst_ids))
    rows = db.execute(stmt.group_by(model.gst_id)).all()
    return {int(gst_id): int(count) for gst_id, count in rows}


def _out(gst: Gst, count: int, accounts: int = 0) -> GstOut:
    return GstOut(
        id=gst.id,
        label=gst.label,
        gstin=gst.gstin,
        business_name=gst.business_name,
        is_default=gst.is_default,
        address_count=count,
        account_count=accounts,
        created_at=gst.created_at,
    )


def _reject_duplicate(
    db: DbSession, label: str, *, exclude_id: int | None = None
) -> None:
    """409 if another GST already has this label."""
    stmt = select(Gst.id).where(Gst.label == label)
    if exclude_id is not None:
        stmt = stmt.where(Gst.id != exclude_id)
    if db.scalar(stmt) is not None:
        raise HTTPException(status_code=409, detail=f"A GST labelled '{label}' exists")


def _conflict(label: str) -> HTTPException:
    return HTTPException(status_code=409, detail=f"A GST labelled '{label}' exists")


# --- routes -------------------------------------------------------------------
@router.get("", response_model=list[GstOut])
def list_gsts(db: DbSession = Depends(get_db)) -> list[GstOut]:
    """Every GST with its address and account counts, alphabetically by label."""
    gsts = list(db.scalars(select(Gst).order_by(Gst.label.asc())).all())
    counts = _counts(db)
    accounts = _counts(db, model=Account)
    return [_out(gst, counts.get(gst.id, 0), accounts.get(gst.id, 0)) for gst in gsts]


@router.post("", response_model=GstOut, status_code=201, dependencies=[Depends(require_admin)])
def create_gst(payload: GstWrite, db: DbSession = Depends(get_db)) -> GstOut:
    _reject_duplicate(db, payload.label)

    gst = Gst(
        label=payload.label,
        gstin=payload.gstin,
        business_name=payload.business_name,
        is_default=payload.is_default,
    )
    db.add(gst)
    try:
        # Flush for the id, then clear the other defaults in the *same*
        # transaction, so the table is never briefly two-defaulted.
        db.flush()
        if gst.is_default:
            clear_other_defaults(db, Gst, gst.id)
        db.commit()
    except IntegrityError as exc:
        # The unique index is the real authority; two concurrent creates of the
        # same label must answer 409 rather than 500.
        db.rollback()
        raise _conflict(payload.label) from exc
    db.refresh(gst)
    log.info("created GST %s (%s)", gst.id, gst.label)
    # A fresh GST has no addresses yet, so the count is known without a query.
    return _out(gst, 0)


@router.patch("/{gst_id}", response_model=GstOut, dependencies=[Depends(require_admin)])
def update_gst(
    gst_id: int, payload: GstPatch, db: DbSession = Depends(get_db)
) -> GstOut:
    """Edit one GST.

    Only the fields actually present in the body are written, so a PATCH that
    sends ``gstin`` cannot blank ``business_name`` by omission.
    """
    gst = db.get(Gst, gst_id)
    if gst is None:
        raise HTTPException(status_code=404, detail="GST not found")

    sent = payload.model_fields_set
    label = payload.label
    if "label" in sent and label is not None:
        _reject_duplicate(db, label, exclude_id=gst_id)

    for field in _PATCH_FIELDS:
        if field in sent:
            setattr(gst, field, getattr(payload, field))

    try:
        db.flush()
        if "is_default" in sent and payload.is_default:
            clear_other_defaults(db, Gst, gst_id)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _conflict(label or "") from exc
    db.refresh(gst)
    return _out(
        gst,
        _counts(db, [gst_id]).get(gst_id, 0),
        _counts(db, [gst_id], model=Account).get(gst_id, 0),
    )


@router.delete("/{gst_id}", response_model=GstDeleted, dependencies=[Depends(require_admin)])
def delete_gst(gst_id: int, db: DbSession = Depends(get_db)) -> GstDeleted:
    """Delete the GST and unlink its addresses and accounts — every row survives.

    Deleting the default leaves the table with no default, and nothing is
    promoted in its place: a run that names no GST then bills under its
    address's linked GST, or without GST details, rather than under whichever
    entity we would have picked for them. An account that had this GST as its
    own bills under its runs' GST from now on.

    Answers how many addresses and accounts it unlinked, counted by the
    UPDATEs that unlinked them: the page that asked words its note from these,
    since the list it loaded earlier may no longer be right.
    """
    if db.scalar(select(Gst.id).where(Gst.id == gst_id)) is None:
        raise HTTPException(status_code=404, detail="GST not found")

    unlinked = db.execute(
        update(Address)
        .where(Address.gst_id == gst_id)
        .values(gst_id=None)
        .execution_options(synchronize_session=False)
    )
    # The same for accounts, by hand rather than left to the foreign key's
    # ON DELETE SET NULL: a column init_db added to an older database has no
    # foreign key at all, and an account must never be left naming a GST
    # that no longer exists.
    accounts = db.execute(
        update(Account)
        .where(Account.gst_id == gst_id)
        .values(gst_id=None)
        .execution_options(synchronize_session=False)
    )
    # A Core DELETE rather than ``db.delete(gst)``: the ORM would de-associate
    # the children by loading every address linked to it, which is the one thing
    # these lists exist to avoid. The UPDATEs above already cleared them.
    db.execute(delete(Gst).where(Gst.id == gst_id))
    db.commit()
    gone = GstDeleted(unlinked_addresses=unlinked.rowcount or 0, unlinked_accounts=accounts.rowcount or 0)
    log.info(
        "deleted GST %s; unlinked %d address(es) and %d account(s)",
        gst_id,
        gone.unlinked_addresses,
        gone.unlinked_accounts,
    )
    return gone
