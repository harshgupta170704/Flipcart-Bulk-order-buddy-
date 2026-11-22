"""What the invoices on file and the order page say about one unit beyond its
own row: its IMEI, the name the order was billed to, and its last tracking
update (spec FR-001, FR-013, FR-017, SC-007; spec audit of 27 Oct 2026).

Read-only: nothing here is stored. The IMEI and the billing name come from
the order's product invoices, as app.core.invoices read them off the PDFs;
the last tracking update from the unit's own timeline, as the read-back took
it off the order page. They are loaded for many orders at once — a page of
the ledger, a chunk of an export — and never with one query per row.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.db.models import Invoice, OrderUnit
from app.io.invoice_pdf import KIND_PRODUCT

#: Ids per IN clause: SQLite's bound-parameter ceiling is 999 on older builds.
_CHUNK = 500
#: An IMEI is 15 digits (an IMEISV 16). A line of a product invoice lists one
#: per unit it bills, separated by commas, spaces or slashes.
_IMEI = re.compile(r"(?<!\d)\d{15,16}(?!\d)")
IST = ZoneInfo("Asia/Kolkata")

#: How a unit's IMEI was told: the order's only unit; one of as many IMEIs as
#: the order has units, taken by position; or every IMEI of the order, when
#: which is whose cannot be told.
SCOPE_UNIT = "unit"
SCOPE_POSITION = "position"
SCOPE_ORDER = "order"


@dataclass(frozen=True)
class ProductFacts:
    """What an order's product invoices say: every IMEI on their lines, in
    invoice and line order, each once; and the name the order was billed to."""

    imeis: tuple[str, ...] = ()
    billing_name: str | None = None


def product_facts(db: DbSession, order_pks: Iterable[int]) -> dict[int, ProductFacts]:
    """``{order: its ProductFacts}`` for the orders with a product invoice on
    file: one query per chunk of orders. The oldest invoice first (a PDF
    replaced later still lists the same unit), its lines in their order."""
    wanted = sorted({int(pk) for pk in order_pks})
    imeis: dict[int, list[str]] = {}
    names: dict[int, str] = {}
    for start in range(0, len(wanted), _CHUNK):
        rows = db.execute(
            select(Invoice.order_pk, Invoice.lines, Invoice.buyer_name)
            .where(Invoice.order_pk.in_(wanted[start : start + _CHUNK]), Invoice.kind == KIND_PRODUCT)
            .order_by(Invoice.order_pk, Invoice.invoice_date.asc().nulls_last(), Invoice.id)
        ).all()
        for order_pk, lines, buyer in rows:
            found = imeis.setdefault(int(order_pk), [])
            for line in lines or ():
                text = str(line.get("imei") or "") if isinstance(line, dict) else ""
                found.extend(imei for imei in _IMEI.findall(text) if imei not in found)
            if int(order_pk) not in names and (buyer or "").strip():
                names[int(order_pk)] = buyer.strip()
    return {pk: ProductFacts(tuple(found), names.get(pk)) for pk, found in imeis.items()}


def unit_imei(facts: ProductFacts | None, position: int, units_in_order: int) -> tuple[str | None, str | None]:
    """The unit's IMEI as the ledger shows it, and how it was told
    (:data:`SCOPE_UNIT`, :data:`SCOPE_POSITION`, :data:`SCOPE_ORDER`).

    The order's only unit has whatever its invoices list. When they list as
    many IMEIs as the order has units, the unit takes the one at its position,
    as the invoice lists them — said so, since the invoice does not name the
    unit. Any other count gives every IMEI of the order, for the whole order:
    which is whose cannot be told, and a guess would be worse than none.
    """
    found = facts.imeis if facts is not None else ()
    if not found:
        return None, None
    if units_in_order <= 1:
        return ", ".join(found), SCOPE_UNIT
    if len(found) == units_in_order and 1 <= position <= len(found):
        return found[position - 1], SCOPE_POSITION
    return ", ".join(found), SCOPE_ORDER


def _day(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def last_tracking_update(unit: OrderUnit) -> tuple[datetime, str] | None:
    """The newest step of the unit's timeline that had happened by the time
    the page was read: ``(its date, its wording)``, or None.

    The order page dates the steps still to come too ("Delivery, expected by
    Sun 1st Nov"), and a promise is no update, so a step dated after the day
    of the read is passed over. Undated steps say nothing of when.
    """
    from app.core.order_units import timeline_steps  # local: order_units imports this module's neighbours

    steps = timeline_steps(unit.timeline) or []
    read = unit.synced_at
    if read is not None:
        read_day = (read if read.tzinfo else read.replace(tzinfo=timezone.utc)).astimezone(IST).date()
    else:
        read_day = datetime.now(IST).date()
    best: tuple[datetime, str] | None = None
    for step in steps:
        when = _day(step.get("date"))
        status = str(step.get("status") or "").strip()
        if when is None or not status or when.date() > read_day:
            continue
        if best is None or when >= best[0]:
            best = (when, status)
    return best
