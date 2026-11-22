"""Inventory API (BOB parity — stock summary, batches, transfer notes, mismatch CSV, COD card update).

All numbers are computed directly from PostgreSQL/SQLite tables (zero hardcoding):
- Delivered quantities come from ``orders`` (units with ``delivery_state == 'delivered'``)
- Opening / Received / Approved quantities come from ``inventory_batches``
- Transferred quantities come from ``inventory_transfers``
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.core.card_pool import digits
from app.core.security import require_user
from app.db.base import get_db
from app.db.models import Account, InventoryBatch, InventoryTransfer, Order, OrderStatus

router = APIRouter(prefix="/inventory", tags=["inventory"], dependencies=[Depends(require_user)])


class StockRowOut(BaseModel):
    platform: str
    product_name: str
    opening_qty: int
    delivered_qty: int
    expected_qty: int
    received_qty: int
    approved_qty: int
    transferred_qty: int
    mismatch_qty: int
    to_be_transferred_qty: int
    completed: bool


class BatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    platform: str = Field(default="flipkart", max_length=32)
    product_name: str = Field(min_length=1, max_length=255)
    opening_qty: int = Field(default=0, ge=0)
    received_qty: int = Field(default=0, ge=0)
    approved_qty: int = Field(default=0, ge=0)
    status: str = Field(default="active", pattern="^(active|completed)$")
    notes: str | None = Field(default=None, max_length=1000)


class BatchPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    opening_qty: int | None = Field(default=None, ge=0)
    received_qty: int | None = Field(default=None, ge=0)
    approved_qty: int | None = Field(default=None, ge=0)
    status: str | None = Field(default=None, pattern="^(active|completed)$")
    notes: str | None = Field(default=None, max_length=1000)


class BatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    platform: str
    product_name: str
    opening_qty: int
    received_qty: int
    approved_qty: int
    status: str
    notes: str | None
    created_by: str | None
    created_at: datetime
    updated_at: datetime


class TransferIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference: str = Field(min_length=1, max_length=120)
    platform: str = Field(default="flipkart", max_length=32)
    product_name: str = Field(min_length=1, max_length=255)
    quantity: int = Field(ge=1)
    destination: str | None = Field(default=None, max_length=255)
    notes: str | None = Field(default=None, max_length=1000)


class TransferOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    reference: str
    platform: str
    product_name: str
    quantity: int
    destination: str | None
    notes: str | None
    created_by: str | None
    created_at: datetime


class CodCardsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    csv_text: str = Field(min_length=1, max_length=500_000)


class CodCardsOut(BaseModel):
    updated: int
    not_found: list[str]
    skipped: list[str]


def _compute_stock(db: DbSession) -> list[StockRowOut]:
    delivered: dict[tuple[str, str], int] = defaultdict(int)
    opening: dict[tuple[str, str], int] = defaultdict(int)
    received: dict[tuple[str, str], int] = defaultdict(int)
    approved: dict[tuple[str, str], int] = defaultdict(int)
    transferred: dict[tuple[str, str], int] = defaultdict(int)
    keys: set[tuple[str, str]] = set()

    orders = db.scalars(
        select(Order).where(
            Order.status == OrderStatus.placed,
            Order.delivery_state == "delivered",
        )
    ).all()
    for order in orders:
        platform = "flipkart"
        product = (order.model or "Unknown Product").strip()
        k = (platform, product)
        keys.add(k)
        delivered[k] += int(order.qty or 1)

    for batch in db.scalars(select(InventoryBatch)).all():
        k = (batch.platform.strip().lower(), batch.product_name.strip())
        keys.add(k)
        opening[k] += int(batch.opening_qty or 0)
        received[k] += int(batch.received_qty or 0)
        approved[k] += int(batch.approved_qty or 0)

    for tr in db.scalars(select(InventoryTransfer)).all():
        k = (tr.platform.strip().lower(), tr.product_name.strip())
        keys.add(k)
        transferred[k] += int(tr.quantity or 0)

    rows: list[StockRowOut] = []
    for platform, product in sorted(keys, key=lambda pair: (pair[0], pair[1].lower())):
        op_q = opening[(platform, product)]
        del_q = delivered[(platform, product)]
        exp_q = op_q + del_q
        rec_q = received[(platform, product)]
        app_q = approved[(platform, product)]
        tr_q = transferred[(platform, product)]
        mismatch = exp_q - rec_q
        to_transfer = max(0, app_q - tr_q)
        completed = exp_q > 0 and mismatch == 0 and to_transfer == 0 and tr_q >= exp_q
        rows.append(
            StockRowOut(
                platform=platform,
                product_name=product,
                opening_qty=op_q,
                delivered_qty=del_q,
                expected_qty=exp_q,
                received_qty=rec_q,
                approved_qty=app_q,
                transferred_qty=tr_q,
                mismatch_qty=mismatch,
                to_be_transferred_qty=to_transfer,
                completed=completed,
            )
        )
    return rows


@router.get("/stock", response_model=list[StockRowOut])
def list_stock(
    tab: str = Query("active", pattern="^(active|completed|all)$"),
    q: str = Query("", max_length=120),
    db: DbSession = Depends(get_db),
) -> list[StockRowOut]:
    rows = _compute_stock(db)
    if tab == "active":
        rows = [r for r in rows if not r.completed]
    elif tab == "completed":
        rows = [r for r in rows if r.completed]
    needle = q.strip().lower()
    if needle:
        rows = [r for r in rows if needle in r.product_name.lower() or needle in r.platform.lower()]
    return rows


@router.get("/mismatch-report.csv")
def download_mismatch_report(db: DbSession = Depends(get_db)) -> Response:
    rows = [r for r in _compute_stock(db) if r.mismatch_qty != 0]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Platform",
        "Product Name",
        "Opening Quantity",
        "Quantity Delivered from Platform",
        "Expected Quantity",
        "Quantity Received",
        "Quantity Approved",
        "Quantity Transferred",
        "Mismatch Quantity",
        "Quantity To be Transferred",
    ])
    for r in rows:
        writer.writerow([
            r.platform,
            r.product_name,
            r.opening_qty,
            r.delivered_qty,
            r.expected_qty,
            r.received_qty,
            r.approved_qty,
            r.transferred_qty,
            r.mismatch_qty,
            r.to_be_transferred_qty,
        ])
    return Response(
        content=buf.getvalue().encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="inventory_mismatch_report.csv"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/cod-cards", response_model=CodCardsOut)
def update_cod_cards(payload: CodCardsIn, db: DbSession = Depends(get_db)) -> CodCardsOut:
    """Update card_number on COD orders from a CSV of order_id,card_number."""
    reader = csv.reader(io.StringIO(payload.csv_text))
    updated = 0
    not_found: list[str] = []
    skipped: list[str] = []
    for idx, row in enumerate(reader, 1):
        cells = [c.strip() for c in row]
        if not any(cells):
            continue
        if idx == 1 and any(h.lower().replace(" ", "_") in ("order_id", "card_number", "card") for h in cells):
            continue
        if len(cells) < 2:
            skipped.append(f"Row {idx}: expected order_id,card_number")
            continue
        oid = cells[0]
        card = digits(cells[1])
        if not oid or len(card) < 4:
            skipped.append(f"Row {idx}: invalid order_id or card digits")
            continue
        orders = db.scalars(select(Order).where(Order.order_id == oid)).all()
        if not orders:
            not_found.append(oid)
            continue
        for order in orders:
            order.card_number = card
            updated += 1
    db.commit()
    return CodCardsOut(updated=updated, not_found=not_found, skipped=skipped)


@router.get("/batches", response_model=list[BatchOut])
def list_batches(db: DbSession = Depends(get_db)) -> list[BatchOut]:
    rows = db.scalars(select(InventoryBatch).order_by(InventoryBatch.id.desc())).all()
    return [BatchOut.model_validate(r) for r in rows]


@router.post("/batches", response_model=BatchOut, status_code=201)
def create_batch(
    payload: BatchIn, db: DbSession = Depends(get_db), who: str = Depends(require_user)
) -> BatchOut:
    batch = InventoryBatch(
        name=payload.name.strip(),
        platform=payload.platform.strip().lower() or "flipkart",
        product_name=payload.product_name.strip(),
        opening_qty=payload.opening_qty,
        received_qty=payload.received_qty,
        approved_qty=payload.approved_qty,
        status=payload.status,
        notes=(payload.notes or "").strip() or None,
        created_by=who,
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return BatchOut.model_validate(batch)


@router.patch("/batches/{batch_id}", response_model=BatchOut)
def update_batch(batch_id: int, payload: BatchPatch, db: DbSession = Depends(get_db)) -> BatchOut:
    batch = db.get(InventoryBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Inventory batch not found.")
    sent = payload.model_fields_set
    if "name" in sent and payload.name:
        batch.name = payload.name.strip()
    if "opening_qty" in sent and payload.opening_qty is not None:
        batch.opening_qty = payload.opening_qty
    if "received_qty" in sent and payload.received_qty is not None:
        batch.received_qty = payload.received_qty
    if "approved_qty" in sent and payload.approved_qty is not None:
        batch.approved_qty = payload.approved_qty
    if "status" in sent and payload.status:
        batch.status = payload.status
    if "notes" in sent:
        batch.notes = (payload.notes or "").strip() or None
    db.commit()
    db.refresh(batch)
    return BatchOut.model_validate(batch)


@router.delete("/batches/{batch_id}", status_code=204)
def delete_batch(batch_id: int, db: DbSession = Depends(get_db)) -> Response:
    batch = db.get(InventoryBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Inventory batch not found.")
    db.delete(batch)
    db.commit()
    return Response(status_code=204)


@router.get("/transfers", response_model=list[TransferOut])
def list_transfers(db: DbSession = Depends(get_db)) -> list[TransferOut]:
    rows = db.scalars(select(InventoryTransfer).order_by(InventoryTransfer.id.desc())).all()
    return [TransferOut.model_validate(r) for r in rows]


@router.post("/transfers", response_model=TransferOut, status_code=201)
def create_transfer(
    payload: TransferIn, db: DbSession = Depends(get_db), who: str = Depends(require_user)
) -> TransferOut:
    tr = InventoryTransfer(
        reference=payload.reference.strip(),
        platform=payload.platform.strip().lower() or "flipkart",
        product_name=payload.product_name.strip(),
        quantity=payload.quantity,
        destination=(payload.destination or "").strip() or None,
        notes=(payload.notes or "").strip() or None,
        created_by=who,
    )
    db.add(tr)
    db.commit()
    db.refresh(tr)
    return TransferOut.model_validate(tr)


@router.delete("/transfers/{transfer_id}", status_code=204)
def delete_transfer(transfer_id: int, db: DbSession = Depends(get_db)) -> Response:
    tr = db.get(InventoryTransfer, transfer_id)
    if tr is None:
        raise HTTPException(status_code=404, detail="Transfer note not found.")
    db.delete(tr)
    db.commit()
    return Response(status_code=204)
