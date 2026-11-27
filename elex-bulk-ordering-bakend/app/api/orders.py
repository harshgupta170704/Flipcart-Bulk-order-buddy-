"""Orders API — the ledger behind Order Units History, plus its exports.

Every endpoint here reads the same :class:`OrderFilter`, parsed once from the
query string: the page, its total, the filter options, and both exports. That
is the point — the export the operator downloads holds exactly the rows the
screen counted, which the old export (every order, whatever the filter) never
did.

A row with status ``failed_over_target`` is an order that was deliberately
**not** placed because the final payable amount exceeded the run's target
(PLAN.md §6a). It is recorded with the amount we saw so the operator can see
how far over it went, and it never stopped the rest of the batch.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from fastapi.responses import FileResponse
from pathlib import Path
from starlette.background import BackgroundTask

from app.api import uploads
from app.api.requests import requests_on_order
from app.core import cancellations, invoices, order_filter, order_status, run_notes, unit_facts, unit_requests
from app.core.order_filter import (
    CARD_MAX_LENGTH,
    DATE_FIELDS,
    DEFAULT_SIZE,
    GSTIN_MAX_LENGTH,
    INVOICE_NUMBER_MAX_LENGTH,
    MAX_SIZE,
    OrderFilter,
    gstin_text,
    month_range,
    normalise_many,
)
from app.core.security import Principal, current_user, require_user
from app.db.base import get_db
from app.db.models import Account, Invoice, Order, OrderStatus, OrderUnit, Setting, utcnow
from app.io import excel, order_ids as id_sheets, orders_csv
from app.io.invoice_store import InvoiceStore
from app.schemas.order import (
    InvoiceOut,
    OrderFilterOptionsOut,
    OrderIdsImportOut,
    OrderIdsRejectedOut,
    OrderOut,
    OrderPage,
    OrderReceivedIn,
    OrderReceivedOut,
    OrderReportImportOut,
    OrderSettleIn,
    OrderViewIn,
    OrderViewOut,
)
from app.schemas.request import RequestOut

log = logging.getLogger("ordering.api.orders")

router = APIRouter(
    prefix="/orders",
    tags=["orders"],
    dependencies=[Depends(require_user)],
)

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: Rows per page. The spec's ledger shows 100; MAX_SIZE is the ceiling so a
#: page is never a disguised full export.
PAGE_SIZE = DEFAULT_SIZE
#: Where the single admin's saved ledger view lives in the settings table.
VIEW_KEY = "orders_view"


# --- the filter, parsed once ---------------------------------------------------
#: Ids and page numbers the database can bind. Anything larger is a mangled
#: URL, and SQLite answers it with an OverflowError (a 500) rather than a 422.
MAX_ID = 2**31 - 1
MAX_PAGE = 1_000_000

_ISO_DAY_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}")
_DELIVERY_STATES = frozenset(order_status.SETTLED | order_status.UNDELIVERED)


def _bound(value: str | None, *, end: bool) -> datetime | None:
    """A query-string date or datetime as the stored (UTC) clock.

    A bare date is read in India Standard Time — the operator's calendar — and
    the end of a range is the START of the next day, so "to 30-09" includes
    the whole of the 30th. A datetime without an offset is IST as well; one
    with an offset is taken as written. Either way the bound must start with
    YYYY-MM-DD: "20261024" is neither a day nor a moment anyone meant.
    """
    if not value:
        return None
    text = value.strip()
    if not _ISO_DAY_PREFIX.match(text):
        raise HTTPException(status_code=422, detail=f"unreadable date {text!r}")
    try:
        if len(text) == 10:
            day = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=orders_csv.IST)
            if end:
                day = day + timedelta(days=1)
            return day.astimezone(orders_csv.UTC_TZ).replace(tzinfo=None)
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"unreadable date {text!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=orders_csv.IST)
    return parsed.astimezone(orders_csv.UTC_TZ).replace(tzinfo=None)


def _as_list(value) -> list:
    """A query parameter or a saved-view value as a list: None → [], one → [it]."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]


def _as_id(value, name: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"{name} must be a whole number") from exc
    if not 1 <= number <= MAX_ID:
        raise HTTPException(status_code=422, detail=f"{name} must be between 1 and {MAX_ID}")
    return number


def exact_many(values) -> tuple[str, ...]:
    """Trim a list of exact wordings, drop blanks and repeats, keep the order.

    No splitting, unlike :func:`normalise_many`: "Out for delivery" is one
    platform wording, not three words to match separately, and the picker
    sends each ticked wording as its own repeated parameter.
    """
    out: list[str] = []
    for raw in values or ():
        text = str(raw).strip()
        if text and text not in out:
            out.append(text)
    return tuple(out)


def _one_text(value, name: str, limit: int) -> str | None:
    """A one-line text filter as typed, trimmed — 422 unless it is text of at
    most ``limit`` characters. The same rule for the query parameter and a
    saved view, so a view never holds a value every load would refuse."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail=f"{name} must be text")
    if len(value) > limit:
        raise HTTPException(status_code=422, detail=f"{name} must be at most {limit} characters")
    return value.strip() or None


_NOT_A_MONTH = "invoice_month must be a month written YYYY-MM"


def _invoice_month(value) -> str | None:
    """A "YYYY-MM" month, trimmed; blank is no filter, anything else a 422."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail=_NOT_A_MONTH)
    text = value.strip()
    if not text:
        return None
    try:
        month_range(text)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_NOT_A_MONTH) from exc
    return text


#: The Range presets a saved view may keep: the spans relative to today that
#: the ledger's Range select offers (DatePreset in frontend/src/lib/types.ts).
#: The page works a preset's dates out again whenever it restores the view, so
#: "Last 7 days" reopens on the last seven days, not the seven it was saved on
#: (spec FR-011). A range typed by hand is kept as its dates alone.
DATE_PRESETS = ("7d", "30d", "month")


def _as_bob(value) -> bool | None:
    """The BoB-orders toggle: true, false, or unset (both)."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    raise HTTPException(status_code=422, detail="bob must be true or false")


def build_filter(
    *,
    q=None,
    status=None,
    platform_status=None,
    delivery_state=None,
    processing_stage=None,
    error_code=None,
    order_id=None,
    email=None,
    tracking_id=None,
    card=None,
    billed_gst=None,
    invoice_number=None,
    invoice_gstin=None,
    invoice_month=None,
    account_id=None,
    job_id=None,
    row=None,
    date_field=order_filter.DEFAULT_DATE_FIELD,
    date_from=None,
    date_to=None,
    date_preset=None,
    platform=None,
    bob=None,
) -> OrderFilter:
    """The filter from its raw values — 422 for anything the page could not apply.

    One builder for the query string and for a saved view, so a view can only
    be saved if applying it would work. A view's date preset is checked here
    too, though it narrows nothing itself: the dates beside it do.
    """
    if not isinstance(date_field, str) or date_field not in DATE_FIELDS:
        raise HTTPException(
            status_code=422, detail=f"date_field must be one of {sorted(DATE_FIELDS)}"
        )
    if date_preset is not None and date_preset not in DATE_PRESETS:
        raise HTTPException(
            status_code=422, detail=f"date_preset must be one of {list(DATE_PRESETS)}"
        )
    outcomes = [str(getattr(value, "value", value)).strip() for value in _as_list(status)]
    unknown_outcomes = [value for value in outcomes if value not in order_filter.OUTCOMES]
    if unknown_outcomes:
        raise HTTPException(
            status_code=422,
            detail=f"unknown outcome(s): {unknown_outcomes}; one of {list(order_filter.OUTCOMES)}",
        )
    statuses = tuple(dict.fromkeys(outcomes))
    delivery_states = normalise_many(_as_list(delivery_state), lower=True)
    unknown = [state for state in delivery_states if state not in _DELIVERY_STATES]
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown delivery state(s): {unknown}")
    # The same shape and limit as the query parameter, so a saved view never
    # holds a card the list endpoint will refuse on every load.
    if card is not None and not isinstance(card, str):
        raise HTTPException(status_code=422, detail="card must be text")
    if card is not None and len(card) > CARD_MAX_LENGTH:
        raise HTTPException(
            status_code=422, detail=f"card must be at most {CARD_MAX_LENGTH} characters"
        )
    return OrderFilter(
        q=str(q or "").strip() or None,
        statuses=statuses,
        platform_statuses=exact_many(_as_list(platform_status)),
        delivery_states=delivery_states,
        processing_stages=exact_many(_as_list(processing_stage)),
        error_codes=exact_many(_as_list(error_code)),
        order_ids=normalise_many(_as_list(order_id), upper=True),
        emails=normalise_many(_as_list(email), lower=True),
        tracking_ids=normalise_many(_as_list(tracking_id), upper=True),
        card=(card or "").strip() or None,
        billed_gst=gstin_text(_one_text(billed_gst, "billed_gst", GSTIN_MAX_LENGTH)) or None,
        invoice_number=_one_text(invoice_number, "invoice_number", INVOICE_NUMBER_MAX_LENGTH),
        invoice_gstin=gstin_text(_one_text(invoice_gstin, "invoice_gstin", GSTIN_MAX_LENGTH)) or None,
        invoice_month=_invoice_month(invoice_month),
        account_id=_as_id(account_id, "account_id"),
        job_id=_as_id(job_id, "job_id"),
        platforms=normalise_many(_as_list(platform), lower=True),
        bob=_as_bob(bob),
        order_row=_as_id(row, "row"),
        date_field=date_field,
        date_from=_bound(None if date_from is None else str(date_from), end=False),
        date_to=_bound(None if date_to is None else str(date_to), end=True),
    )


def parse_filter(
    q: str | None = Query(None, description="substring of the product name"),
    status: list[str] | None = Query(
        None,
        description="the tool's outcome: an order status, or 'cancelled' (Flipkart took the order, then cancelled it)",
    ),
    platform_status: list[str] | None = Query(
        None, description="exact platform wordings, one per repeated parameter"
    ),
    delivery_state: list[str] | None = Query(None),
    processing_stage: list[str] | None = Query(
        None, description="pipeline stages reached by the attempt, one per repeated parameter"
    ),
    error_code: list[str] | None = Query(
        None, description="classified error codes, one per repeated parameter"
    ),
    order_id: list[str] | None = Query(None),
    email: list[str] | None = Query(None),
    tracking_id: list[str] | None = Query(None),
    card: str | None = Query(
        None,
        max_length=CARD_MAX_LENGTH,
        description="part of the card number, e.g. its last four digits",
    ),
    billed_gst: str | None = Query(
        None,
        max_length=GSTIN_MAX_LENGTH,
        description="a GSTIN or how it starts: the GST the run billed under, or the one the order page shows",
    ),
    invoice_number: str | None = Query(
        None,
        max_length=INVOICE_NUMBER_MAX_LENGTH,
        description="part of the number of an invoice on file",
    ),
    invoice_gstin: str | None = Query(
        None,
        max_length=GSTIN_MAX_LENGTH,
        description="a GSTIN or how it starts: the seller or the buyer on an invoice on file",
    ),
    invoice_month: str | None = Query(
        None, description="YYYY-MM: an invoice on file dated in that month"
    ),
    account_id: int | None = Query(None, ge=1, le=MAX_ID),
    job_id: int | None = Query(None, ge=1, le=MAX_ID),
    row: int | None = Query(
        None, ge=1, le=MAX_ID, description="one order by the ledger's own row id (as an alert names it)"
    ),
    date_field: str = Query(order_filter.DEFAULT_DATE_FIELD),
    date_from: str | None = Query(
        None, description="YYYY-MM-DD, or an ISO datetime; IST unless an offset is given"
    ),
    date_to: str | None = Query(
        None,
        description=(
            "YYYY-MM-DD (inclusive), or an ISO datetime (exclusive); "
            "IST unless an offset is given"
        ),
    ),
    platform: list[str] | None = Query(None, description="platforms the unit was bought on"),
    bob: bool | None = Query(None, description="true: only orders this tool placed; false: only the others"),
) -> OrderFilter:
    """The one filter every endpoint below reads."""
    return build_filter(
        platform=platform,
        bob=bob,
        q=q,
        status=status,
        platform_status=platform_status,
        delivery_state=delivery_state,
        processing_stage=processing_stage,
        error_code=error_code,
        order_id=order_id,
        email=email,
        tracking_id=tracking_id,
        card=card,
        billed_gst=billed_gst,
        invoice_number=invoice_number,
        invoice_gstin=invoice_gstin,
        invoice_month=invoice_month,
        account_id=account_id,
        job_id=job_id,
        row=row,
        date_field=date_field,
        date_from=date_from,
        date_to=date_to,
    )


# --- listing -------------------------------------------------------------------
@router.get("", response_model=OrderPage)
def list_orders(
    flt: OrderFilter = Depends(parse_filter),
    page: int = Query(default=1, ge=1, le=MAX_PAGE),
    size: int = Query(default=PAGE_SIZE, ge=1, le=MAX_SIZE),
    db: DbSession = Depends(get_db),
) -> OrderPage:
    """One page of orders, newest first, plus the FULL match count.

    The count is what the page shows as "Showing N of M": without it an
    operator reading a capped list cannot tell 100 orders from 10,000.
    """
    units, total = order_filter.page(db, flt, page_number=page, size=size)
    # How many invoices each order on this page has on file, in one query: the
    # PDF column says "3 PDFs" and the row dialog lists them.
    counts = invoices.counts_on_file(db, {order.id for _unit, order, _email, _n in units})
    # The open cancel / reschedule requests on this page's units, in one query
    # (spec FR-020), so a row can wear a "Cancel requested" tag.
    open_kinds = unit_requests.open_kinds_by_unit(db, [unit.id for unit, *_rest in units])
    # And which of them someone asked to cancel (open or done): such a unit
    # was cancelled at the account's request, whatever Flipkart's page says.
    asked = cancellations.units_with_cancel_request(db, [unit.id for unit, *_rest in units])
    # A unit of a cancelled order that says nothing of who cancelled it (one
    # Flipkart never listed) takes its order's verdict, as the dashboard and
    # the alert read the order. Its other units may be on another page, so the
    # order's reading is looked up — only for such orders: a page without one
    # costs no query more.
    silent = {
        order.id
        for unit, order, _email, _n in units
        if cancellations.placed_then_cancelled(order)
        and cancellations.says_nothing(unit, requested=unit.id in asked)
    }
    readings = cancellations.cancel_readings(db, sorted(silent)) if silent else {}
    # What the product invoices of this page's orders say (each unit's IMEI,
    # the name billed to), in one query for the page, and only for orders
    # with an invoice on file.
    product = unit_facts.product_facts(db, [pk for pk, n in counts.items() if n])
    # Why a run's attempt did not end as a placed order, in the run's own
    # words, in one query for the page (app.core.run_notes).
    said = run_notes.messages_for(
        db, {order.id: order for _unit, order, _email, _n in units if order.status != OrderStatus.placed}.values()
    )
    from app.core.order_outcomes import classify as classify_outcome

    items = []
    for unit, order, email, in_order in units:
        out = OrderOut.from_unit(
            unit,
            order,
            email,
            units_in_order=in_order,
            cancel_requested=unit.id in asked,
            order_reading=readings.get(order.id),
            product=product.get(order.id),
        )
        out.invoices_on_file = counts.get(order.id, 0)
        out.invoice_on_file = out.invoices_on_file > 0
        out.open_request_kinds = open_kinds.get(unit.id, [])
        out.run_message = run_notes.reason_text(said.get(order.id))
        if not out.processing_stage:
            st, ec = classify_outcome(getattr(order.status, "value", str(order.status)), out.run_message)
            out.processing_stage = st.value
            if out.error_code is None and ec is not None:
                out.error_code = ec.value
        items.append(out)
    return OrderPage(
        items=items,
        total=total,
        page=page,
        size=size,
        pages=max(1, -(-total // size)),
    )


@router.get("/filter-options", response_model=OrderFilterOptionsOut)
def filter_options(db: DbSession = Depends(get_db)) -> OrderFilterOptionsOut:
    """Distinct outcomes and platform wordings with counts, grouped by the
    portal's reading, so the status picker offers only what exists."""
    opts = order_filter.options(db)
    return OrderFilterOptionsOut(
        statuses=opts.statuses,
        platform_statuses=opts.platform_statuses,
        delivery_states=opts.delivery_states,
        processing_stages=opts.processing_stages,
        error_codes=opts.error_codes,
        total=opts.total,
    )


# --- the saved view --------------------------------------------------------------
#: Every column the ledger can show, by the id the page uses (OrderTable's
#: registry). A saved view may name no other: a view of unknown columns would
#: open as a table of nothing but the actions column. The two lists must
#: stay identical — a column the registry ships and this set lacks makes
#: every Quick Save a 422 — so tests/test_view_columns.py reads the registry
#: out of the frontend source and compares.
VIEW_COLUMNS = frozenset(
    {
        "platform",
        "bob_order",
        "ref_code",
        "po_number",
        "account_email",
        "placed_at",
        "created_at",
        "model",
        "status",
        "processing_stage",
        "error_code",
        "platform_status",
        "order_id",
        "unit_id",
        "qty",
        "amount",
        "target",
        "payment_mode",
        "card_number",
        "billed_gst",
        "delivery_date",
        "promised_date",
        "delivered_date",
        "days_late",
        "payment_method",
        "amount_paid",
        "delivery_otp",
        "tracking_id",
        "delivery_address",
        "seller",
        "gst_name",
        "gstin",
        "billing_phone",
        "billing_name",
        "imei",
        "unit_amount",
        "last_tracking_update",
        "received",
        "pin",
        "brand",
        "category",
        "job_id",
        "synced_at",
        "source",
        "invoice",
        "actions",
    }
)
#: The filter keys a view may hold, by the argument of build_filter each feeds.
VIEW_FILTER_KEYS = {
    "q": "q",
    "status": "status",
    "statuses": "status",
    "platform_statuses": "platform_status",
    "delivery_states": "delivery_state",
    "processing_stage": "processing_stage",
    "processing_stages": "processing_stage",
    "error_code": "error_code",
    "error_codes": "error_code",
    "order_ids": "order_id",
    "emails": "email",
    "tracking_ids": "tracking_id",
    "card": "card",
    "billed_gst": "billed_gst",
    "invoice_number": "invoice_number",
    "invoice_gstin": "invoice_gstin",
    "invoice_month": "invoice_month",
    "account_id": "account_id",
    "date_field": "date_field",
    "date_from": "date_from",
    "date_to": "date_to",
    "date_preset": "date_preset",
    "platforms": "platform",
    "bob": "bob",
}
#: Part of what the page applies, never part of a view: a run's deep link
#: (a run is not a view) and the paging.
VIEW_IGNORED_KEYS = frozenset({"job_id", "order_row", "page", "size"})


def _validated_view(payload: OrderViewIn) -> dict:
    """The view as stored: known columns, filters the page could apply, no empties."""
    unknown_columns = [column for column in payload.columns if column not in VIEW_COLUMNS]
    if unknown_columns:
        raise HTTPException(status_code=422, detail=f"unknown column(s): {unknown_columns}")
    filters: dict = {}
    kwargs: dict = {}
    for key, value in payload.filters.items():
        if key in VIEW_IGNORED_KEYS or value in (None, "", [], {}):
            continue
        if key not in VIEW_FILTER_KEYS:
            raise HTTPException(status_code=422, detail=f"unknown filter {key!r}")
        argument = VIEW_FILTER_KEYS[key]
        if argument in kwargs:  # "status" and "statuses" both given: one list
            kwargs[argument] = _as_list(kwargs[argument]) + _as_list(value)
        else:
            kwargs[argument] = value
        filters[key] = value
    build_filter(**kwargs)  # 422 when the page could not apply it
    return {"columns": list(dict.fromkeys(payload.columns)), "filters": filters}


def _view_key(principal: Principal) -> str:
    """One saved view per user (the spec's per-user views, FR-058). A token
    with no user row — the server's own — keeps the shared key it always had."""
    return VIEW_KEY if principal.id is None else f"{VIEW_KEY}:{principal.id}"


@router.get("/view", response_model=OrderViewOut | None)
def get_view(
    principal: Principal = Depends(current_user), db: DbSession = Depends(get_db)
) -> OrderViewOut | None:
    """The saved columns and filters, or null when nothing has been saved."""
    row = db.get(Setting, _view_key(principal))
    if row is None or not isinstance(row.value, dict):
        return None
    payload = row.value
    return OrderViewOut(
        columns=list(payload.get("columns") or []),
        filters=dict(payload.get("filters") or {}),
        saved_at=payload.get("saved_at"),
    )


@router.put("/view", response_model=OrderViewOut)
def save_view(
    payload: OrderViewIn,
    principal: Principal = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> OrderViewOut:
    """Quick Save: keep the current columns and filters as the view."""
    cleaned = _validated_view(payload)
    stored = {**cleaned, "saved_at": utcnow().isoformat()}
    key = _view_key(principal)
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=stored))
    else:
        # Reassign rather than mutate: a plain JSON column is not change-tracked.
        row.value = stored
    db.commit()
    return OrderViewOut(**cleaned, saved_at=stored["saved_at"])


@router.delete("/view")
def clear_view(principal: Principal = Depends(current_user), db: DbSession = Depends(get_db)) -> dict:
    """Default View: forget the saved columns and filters."""
    row = db.get(Setting, _view_key(principal))
    if row is not None:
        db.delete(row)
        db.commit()
    return {"cleared": True}


# --- a list of order ids to track ------------------------------------------------
#: The answer to a sheet the reader cannot open: one fixed sentence, never the
#: spreadsheet library's own message.
UNREADABLE_ID_SHEET = (
    "Could not read the sheet: it is not a readable .csv or .xlsx file — save it again from Excel and retry."
)


@router.post("/import-ids", response_model=OrderIdsImportOut)
async def import_order_ids(request: Request, db: DbSession = Depends(get_db)) -> OrderIdsImportOut:
    """Hand in a sheet of order ids (CSV or XLSX) for the tool to track.

    The operator's own process starts from an Excel of order ids; this is that
    sheet. Each readable row becomes an order to read back under the account
    whose email it names; the answer says what to read back next
    (``account_ids`` and ``order_ids`` for ``POST /jobs/sync``), and lists
    every row it could not use, with the reason.

    The sheet comes as a form upload in its ``file`` field, read here in
    memory (app.api.uploads): it may be the legacy workbook, whose columns T
    to W hold card numbers, expiries and CVVs the reader never looks at.
    """
    upload = await uploads.read_sheet(request)
    name = upload.filename
    if not name.lower().endswith((".csv", ".xlsx", ".xlsm")):
        raise HTTPException(status_code=400, detail="Upload a .csv or .xlsx sheet")
    # Read and filed on a worker thread, as an order report is: a sheet of
    # 5,000 rows took 7 s, and on the event loop that held every other request,
    # the live run view and any order run with it (review of 27 Oct 2026). The
    # session is only ever used by one thread at a time.
    try:
        sheet = await asyncio.to_thread(id_sheets.read_id_sheet, upload.data, name)
    except id_sheets.IdSheetError as exc:
        # The reader's own refusal of the whole sheet (too many rows, a
        # missing column), worded for the operator.
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception as exc:  # noqa: BLE001 — a clean message, not a traceback
        # Never the parser's own message: openpyxl quotes a badly typed cell's
        # text in it, and the sheet handed in here can be the legacy workbook,
        # whose card columns (T to W) the reader otherwise leaves unread. So,
        # as for an account or a card sheet, the log names the error's type
        # and the answer is one fixed sentence.
        log.warning("id sheet unreadable: %s", type(exc).__name__)
        raise HTTPException(status_code=422, detail=UNREADABLE_ID_SHEET) from None
    outcome = await asyncio.to_thread(id_sheets.import_sheet, db, sheet)
    return OrderIdsImportOut(
        created=outcome.created,
        existing=outcome.existing,
        rejected=[OrderIdsRejectedOut(row=r.row_number, reason=r.reason) for r in outcome.rejected],
        account_ids=outcome.account_ids,
        order_ids=outcome.order_ids,
    )


# --- an order report: one row per unit -----------------------------------------------
#: The answer to a report the reader cannot open at all: one fixed sentence,
#: never the spreadsheet library's own message (it can quote a cell).
UNREADABLE_REPORT = (
    "Could not read the report: it is not a readable .csv or .xlsx file — save it again from Excel and retry."
)
#: The answer when filing a report met the database's one-unit-per-id rule
#: (spec FR-024) after all — another upload or a read-back recording the same
#: unit at the same moment. One fixed sentence, never the database's message,
#: which quotes the values; and true whether nothing or part of the report
#: had gone in (on SQLite it is filed in chunks: REPORT_CHUNK_ROWS).
REPORT_COLLIDED = (
    "The report stopped: one of its units was being recorded by another upload or a read-back at the same "
    "moment. Upload the report again — a unit it already brought in is not added twice."
)
#: Rows an order report files between commits on SQLite. SQLite lets one
#: connection write at a time, and a report of 20,000 new units filed as one
#: transaction held that lock for 18 s (measured 27 Oct 2026, review of that
#: day): a run recording an order, or a read-back, waited behind it, and one
#: that waited longer than the database's lock wait (app.db.base.SQLITE_LOCK_WAIT)
#: failed. Committed by chunks of whole orders, the longest wait is one
#: chunk's. PostgreSQL never holds a writer up behind another's rows, so there
#: a report stays one transaction.
REPORT_CHUNK_ROWS = 500


@router.post("/import-report", response_model=OrderReportImportOut)
async def import_order_report(
    request: Request,
    principal: Principal = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> OrderReportImportOut:
    """Bring units in from an order report (CSV or XLSX, one row per unit, in
    the template's columns — docs/specs/order-unit-ledger/order-report-format.md;
    spec C-1, FR-021, FR-022, FR-024, FR-060).

    A unit already held is updated — only its empty fields, once the
    read-back has seen it on Flipkart — and a new one joins its order, or a
    new order under the account the row names. Each unreadable row is refused
    with its reason; the rest go in — in one transaction on PostgreSQL, in
    chunks of whole orders on SQLite (:data:`REPORT_CHUNK_ROWS`). Read in
    memory like the other sheet uploads (app.api.uploads): a report is a sheet
    like any other, and card columns in it are never read.
    """
    from sqlalchemy.exc import IntegrityError

    from app.io import order_report

    upload = await uploads.read_sheet(request)
    name = upload.filename
    if not name.lower().endswith((".csv", ".xlsx", ".xlsm")):
        raise HTTPException(status_code=400, detail="Upload a .csv or .xlsx report")
    # Read, filed and checked on a worker thread, as an invoice upload is: a
    # report of thousands of rows would otherwise hold every other request,
    # and the live run view's socket, until it was done. The session is only
    # ever used by one thread at a time.
    try:
        report = await asyncio.to_thread(order_report.read_report, upload.data, name)
    except order_report.ReportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception as exc:  # noqa: BLE001 — a fixed sentence, never the parser's words
        log.warning("order report unreadable: %s", type(exc).__name__)
        raise HTTPException(status_code=422, detail=UNREADABLE_REPORT) from None
    chunk = REPORT_CHUNK_ROWS if db.get_bind().dialect.name == "sqlite" else None

    def file_report() -> order_report.ReportOutcome:
        try:
            filed = order_report.import_report(db, report, uploaded_by=principal.email, commit_every=chunk)
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            log.warning("order report met a unit recorded meanwhile: %s", type(exc).__name__)
            raise HTTPException(status_code=409, detail=REPORT_COLLIDED) from None
        return filed

    outcome = await asyncio.to_thread(file_report)
    if outcome.created or outcome.updated:
        # Rows that change units can answer or raise an alert (an address
        # change, an order now cancelled): the rules run now, as after an
        # invoice upload, rather than at the next pass.
        from app.core.alerts import evaluate_in_own_session

        await asyncio.to_thread(evaluate_in_own_session)
    return OrderReportImportOut(
        created=outcome.created,
        updated=outcome.updated,
        unchanged=outcome.unchanged,
        kept_platform=outcome.kept_platform,
        rejected=[OrderIdsRejectedOut(row=r.row_number, reason=r.reason) for r in outcome.rejected],
        not_kept=outcome.not_kept,
    )


# --- the courier's multi-track sheet, filled in -------------------------------------
#: The answer to a multi-track sheet the reader cannot open: one fixed sentence.
UNREADABLE_MULTI_TRACK = (
    "Could not read the sheet: it is not a readable .csv or .xlsx file — save it again from Excel and retry."
)


@router.post("/multi-track")
async def fill_multi_track(request: Request, db: DbSession = Depends(get_db)) -> StreamingResponse:
    """E-Kart's multi_track sheet, sent back as the same file with four columns
    added after its own: Account, Delivery OTP, Last 6 Digits Of Phone and
    Flipkart Status (app.io.multi_track). Rows are matched by tracking ID, else
    by a one-unit order's ID. ``X-Rows`` and ``X-Matched`` say how many were
    found. Nothing in the ledger changes."""
    from urllib.parse import quote

    from app.io import multi_track

    upload = await uploads.read_sheet(request)
    name = upload.filename
    if not name.lower().endswith((".csv", ".xlsx", ".xlsm")):
        raise HTTPException(status_code=400, detail="Upload the multi_track .xlsx (or a .csv of it)")
    try:
        outcome = await asyncio.to_thread(multi_track.fill, db, upload.data, name)
    except multi_track.MultiTrackError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception as exc:  # noqa: BLE001 — a fixed sentence, never the parser's words
        log.warning("multi-track sheet unreadable: %s", type(exc).__name__)
        raise HTTPException(status_code=422, detail=UNREADABLE_MULTI_TRACK) from None
    is_csv = name.lower().endswith(".csv")
    stem = name.rsplit(".", 1)[0] or "multi_track"
    out_name = f"{stem} - with OTP and phone.{'csv' if is_csv else 'xlsx'}"
    ascii_name = re.sub(r"[^A-Za-z0-9 ._+()-]", "_", out_name)
    return StreamingResponse(
        iter([outcome.data]),
        media_type="text/csv; charset=utf-8" if is_csv else _XLSX_MIME,
        headers={
            "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(out_name)}",
            "X-Rows": str(outcome.rows),
            "X-Matched": str(outcome.matched),
            "Access-Control-Expose-Headers": "X-Rows, X-Matched, Content-Disposition",
        },
    )


@router.get("/report-template.csv")
def order_report_template() -> StreamingResponse:
    """The order report's header row, to fill in: one row per unit below it."""
    from app.io import order_report

    text = "\ufeff" + ",".join(order_report.TEMPLATE_HEADERS) + "\r\n"
    return StreamingResponse(
        iter([text]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="order-report-template.csv"'},
    )


# --- goods in hand ------------------------------------------------------------------
@router.post("/received", response_model=OrderReceivedOut)
def mark_received(
    payload: OrderReceivedIn,
    principal: Principal = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> OrderReceivedOut:
    """Mark the ticked ledger rows' goods as in hand — who and when — or take
    the mark back (spec FR-001, FR-017's "Received"). An operator's job, like
    the cancel requests: it records what a person saw, never anything the
    platform said. A row already as asked is left as it is, its first mark
    kept."""
    wanted = list(dict.fromkeys(payload.unit_ids))
    units = db.scalars(select(OrderUnit).where(OrderUnit.id.in_(wanted))).all()
    found = {unit.id for unit in units}
    changed = 0
    now = utcnow()
    for unit in units:
        if payload.received and unit.received_at is None:
            unit.received_at, unit.received_by = now, principal.email
            changed += 1
        elif not payload.received and unit.received_at is not None:
            unit.received_at, unit.received_by = None, None
            changed += 1
    db.commit()
    return OrderReceivedOut(changed=changed, missing=[pk for pk in wanted if pk not in found])


# --- accounting references ------------------------------------------------------
class OrderReferencesIn(BaseModel):
    """PO Number, RTGS source / beneficiary and e-way bill: blank clears one."""

    model_config = ConfigDict(extra="forbid")

    po_number: str | None = Field(default=None, max_length=64)
    rtgs_source: str | None = Field(default=None, max_length=128)
    rtgs_beneficiary: str | None = Field(default=None, max_length=128)
    ewb_number: str | None = Field(default=None, max_length=32)


@router.patch("/{order_id}/references", response_model=OrderOut)
def set_order_references(order_id: int, payload: OrderReferencesIn, db: DbSession = Depends(get_db)) -> OrderOut:
    """Fill in an order's accounting references; they go to its export columns."""
    row = db.get(Order, order_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such order")
    for field in payload.model_fields_set:
        value = getattr(payload, field)
        setattr(row, field, (value or "").strip() or None)
    db.commit()
    db.refresh(row)
    email = db.scalar(select(Account.email).where(Account.id == row.account_id))
    return OrderOut.from_order(row, email)


# --- settling an unconfirmed order -------------------------------------------------
@router.post("/{order_id}/settle", response_model=OrderOut)
def settle_order(
    order_id: int, payload: OrderSettleIn, db: DbSession = Depends(get_db)
) -> OrderOut:
    """Record what an operator found on Flipkart for an *unconfirmed* order.

    An unconfirmed order was sent but never confirmed, so it may exist. Until
    someone looks, it counts against unit limits and raises a critical alert.
    This is how they close it: ``placed`` with the id Flipkart shows, or
    ``failed`` when it is not there. Only an unconfirmed order can be settled —
    rewriting any other outcome by hand would falsify the record.
    """
    row = db.get(Order, order_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such order")
    if row.status != OrderStatus.unconfirmed:
        raise HTTPException(
            status_code=409,
            detail=f"only an unconfirmed order can be settled; this one is {row.status.value}",
        )
    if payload.status == "placed":
        if not payload.order_id:
            raise HTTPException(
                status_code=422, detail="settling as placed needs the order id Flipkart shows"
            )
        wanted = payload.order_id.strip().upper()
        holder = db.scalar(select(Order).where(Order.order_id == wanted, Order.id != row.id).limit(1))
        if holder is not None:
            # The id-sheet import refuses this too: invoices and read-backs key
            # on the id, and two rows with one id hide each other (audit F-22).
            raise HTTPException(
                status_code=409,
                detail=f"order {wanted} is already in the ledger (row {holder.id}); an id cannot be on two orders",
            )
        from app.core.order_outcomes import ErrorCode, OrderStage

        row.order_id = payload.order_id  # normalised by the model on assignment
        row.status = OrderStatus.placed
        row.processing_stage = OrderStage.COMPLETED.value
        row.error_code = None
        # placed_at stays: the order was created when it was submitted.
    else:
        from app.core.order_outcomes import ErrorCode, OrderStage

        row.status = OrderStatus.failed
        row.processing_stage = OrderStage.ORDER_CONFIRMATION.value
        row.error_code = ErrorCode.CONFIRMATION_UNVERIFIED.value
        row.placed_at = None
    db.commit()
    db.refresh(row)
    # The critical "may exist" alert is about this row and is now answered:
    # close it at once, not at the next read-back (until 11 Nov 2026 the
    # dashboard went on showing it after the operator had settled the order).
    try:
        from app.core.alerts import evaluate_in_own_session

        evaluate_in_own_session()
    except Exception:  # noqa: BLE001 — the order is settled either way
        log.exception("could not re-check the alerts after settling order row %s", row.id)
    email = db.scalar(select(Account.email).where(Account.id == row.account_id))
    return OrderOut.from_order(row, email)


# --- the invoice on file -----------------------------------------------------------
@router.get("/{order_id}/invoice.pdf")
def download_invoice(order_id: int, db: DbSession = Depends(get_db)) -> FileResponse:
    """The order's invoice PDF from the store — the product's tax invoice when
    one is on file (Flipkart's zip also holds fee and transport invoices; the
    newest of those is not "the invoice" — audit F-38), else the newest."""
    row = db.scalar(
        select(Invoice)
        .where(Invoice.order_pk == order_id)
        .order_by((Invoice.kind == "product").desc(), Invoice.id.desc())
        .limit(1)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="no invoice on file for this order")
    store = InvoiceStore.from_settings()
    try:
        path = store.path(row.path)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="invoice path is not in the store") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="the invoice file is missing from the store")
    return FileResponse(
        path, media_type="application/pdf", filename=f"{row.order_id or f'order-{order_id}'}.pdf"
    )


@router.get("/{order_id}/invoices", response_model=list[InvoiceOut])
def list_order_invoices(order_id: int, db: DbSession = Depends(get_db)) -> list[InvoiceOut]:
    """Every invoice on file for the order, oldest first — the product's, the
    fees', the transport's — with what the reader took off each PDF: the row
    dialog's Invoices list (the owner's ask of 26 Oct 2026)."""
    if db.get(Order, order_id) is None:
        raise HTTPException(status_code=404, detail="Order not found")
    rows = db.scalars(select(Invoice).where(Invoice.order_pk == order_id).order_by(Invoice.id)).all()
    return [InvoiceOut.from_row(row, order_id) for row in rows]


@router.get("/{order_id}/invoices/{invoice_id}.pdf")
def download_one_invoice(order_id: int, invoice_id: int, db: DbSession = Depends(get_db)) -> FileResponse:
    """One particular invoice PDF of the order, named after its invoice number."""
    row = db.get(Invoice, invoice_id)
    if row is None or row.order_pk != order_id:
        raise HTTPException(status_code=404, detail="no such invoice on this order")
    store = InvoiceStore.from_settings()
    try:
        path = store.path(row.path)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="invoice path is not in the store") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="the invoice file is missing from the store")
    label = row.invoice_number or (row.invoice_type or "invoice").replace(" ", "-").lower()
    return FileResponse(
        path, media_type="application/pdf", filename=f"{row.order_id or f'order-{order_id}'}-{label}.pdf"
    )


# --- the cancel / reschedule requests on an order's units ---------------------------
@router.get("/{order_id}/requests", response_model=list[RequestOut])
def list_order_requests(order_id: int, db: DbSession = Depends(get_db)) -> list[RequestOut]:
    """Every cancel / reschedule request recorded on the order's units, open or
    closed, newest first, each naming its unit (``unit_pk``): the row dialog
    shows its own unit's (spec FR-013, FR-020). Loaded once when the dialog
    opens, so the ledger's list never reads them row by row."""
    # An id no row can have is "no such order", not the database's overflow.
    if not 1 <= order_id <= MAX_ID or db.get(Order, order_id) is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return requests_on_order(db, order_id)


# --- exports ---------------------------------------------------------------------
@router.get("/exports")
def list_exports(limit: int = Query(100, ge=1, le=500), db: DbSession = Depends(get_db)) -> list[dict]:
    """The CSV Downloads tab: the exports made, newest first."""
    from app.db.models import ExportLog

    rows = db.scalars(select(ExportLog).order_by(ExportLog.id.desc()).limit(limit)).all()
    return [
        {
            "id": row.id,
            "kind": row.kind,
            "file_name": row.file_name,
            "query": row.query,
            "created_by": row.created_by,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@router.get("/export.csv")
def export_orders_csv(
    request: Request,
    principal: Principal = Depends(current_user),
    flt: OrderFilter = Depends(parse_filter),
    sheet: str | None = Query(
        None,
        description=(
            "a layout: blank for the spec's full ledger, or one of the owner's "
            "sheets — out_for_delivery, order_data"
        ),
    ),
    db: DbSession = Depends(get_db),
) -> StreamingResponse:
    """Download the orders matching the current filter as a CSV.

    There is no row cap (the owner's decision of 25 Oct 2026 on C-9): the rows
    are read from the database as the text is written out, one line at a
    time, however many match. Dates in IST; long identifiers written so a
    spreadsheet keeps every digit (see app.io.orders_csv). ``sheet`` picks
    one of the owner's own layouts instead of the full ledger.
    """
    try:
        layout = orders_csv.sheet_named(sheet)
    except KeyError:
        raise HTTPException(
            status_code=422, detail=f"sheet must be one of {sorted(orders_csv.SHEETS)}"
        ) from None
    name = orders_csv.export_filename(sheet=None if sheet is None else layout.name)
    headers = {"Content-Disposition": f'attachment; filename="{name}"'}
    _log_export(db, request, kind=sheet or "order_units", file_name=name, who=principal.email)
    return StreamingResponse(
        orders_csv.stream(order_filter.export_rows(db, flt), sheet=layout),
        media_type="text/csv; charset=utf-8",
        headers=headers,
    )


def _log_export(db: DbSession, request: Request, *, kind: str, file_name: str, who: str | None = None) -> None:
    """Note the export for the CSV Downloads tab; never fails the download."""
    from app.db.models import ExportLog

    try:
        db.add(
            ExportLog(
                kind=kind,
                file_name=file_name,
                query=str(request.url.query or "")[:4000],
                created_by=who,
            )
        )
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


def _remove_export(path: Path) -> None:
    """Delete an export's temporary file; one already gone is fine."""
    path.unlink(missing_ok=True)


class _TemporaryFileResponse(FileResponse):
    """A file made for this one answer. The background task deletes it once
    it is sent; and when sending stops part-way — the client went away, the
    server is stopping — Starlette skips background tasks, so it is deleted
    here then too. Each request builds its own file, so nothing is lost."""

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            _remove_export(Path(self.path))


@router.get("/export")
def export_orders(
    flt: OrderFilter = Depends(parse_filter), db: DbSession = Depends(get_db)
) -> FileResponse:
    """Download the matching orders in the legacy workbook layout (.xlsx).
    No row cap either (C-9), and nothing of the file held in memory: the
    workbook is written row by row to a private temporary file
    (app.io.excel.export_orders) and streamed from disk, then deleted."""
    path = excel.export_orders(db, flt)
    return _TemporaryFileResponse(
        path, media_type=_XLSX_MIME, filename="orders.xlsx", background=BackgroundTask(_remove_export, path)
    )
