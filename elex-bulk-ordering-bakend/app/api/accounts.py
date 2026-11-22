"""Accounts API — paged listing, filtering, bulk tagging and GST, Excel import/export.

Phase 7 (ROADMAP §0): the old ``GET /accounts`` returned every row, which is a
multi-megabyte response and ~37,000 DOM rows against the real portal's data. So
nothing in this module ever loads the whole table:

* the listing asks :mod:`app.core.account_filter` for one page plus a SQL
  ``COUNT``, never for rows it then slices in Python;
* bulk tagging is a single ``UPDATE ... WHERE id IN (SELECT ...)`` so it costs
  the same for 37,000 rows as for 3 — and so is giving a selection its GST;
* "select all" is expressed as a *filter* (:class:`AccountFilterIn`), never as a
  list of 37,000 ids — the same filter shape the job-start endpoints take, so a
  filter that shows 412 accounts tags or runs exactly those 412.

The router carries ``require_user`` itself rather than relying on whoever mounts
it, so the routes cannot be exposed unauthenticated by a wiring mistake.
"""

from __future__ import annotations

import asyncio
import io
import logging

from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from app.schemas.account import MAX_LISTED_IDS
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session as DbSession

from app.api import uploads
from app.core import account_filter
from app.core.account_filter import (
    DEFAULT_SIZE,
    DEFAULT_SORT,
    MAX_SIZE,
    SORTABLE,
    AccountFilter,
)
from app.core.security import Principal, current_user, require_admin, require_user
from app.db.base import get_db
from app.db.models import Account, AccountStatus, Gst, Tag
from app.io import excel
from app.schemas.account import (
    MAX_LISTED_TAGS,
    AccountCreate,
    AccountFilterIn,
    AccountOut,
    AccountPage,
    AccountUpdate,
    FilterOptionsOut,
    GstAssignRequest,
    GstAssignResult,
    ImportResult,
    RowId,
    TagAssignRequest,
    TagAssignResult,
)

log = logging.getLogger("ordering.accounts")

router = APIRouter(
    prefix="/accounts",
    tags=["accounts"],
    dependencies=[Depends(require_user)],
)

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: An account's own GST and billing phone are setup (spec C-7, FR-058): its
#: orders bill under that GST whatever GST a run picks, and are placed with
#: that phone, so only an admin sets them — as only an admin links an address
#: to a GST. The rest of an account (phone, status, tag) stays the operator's.
ADMIN_FIELDS = frozenset({"gst_id", "billing_phone"})

#: The highest page asked for, as on the orders and requests listings. Past it
#: the row offset outgrows what the database binds — SQLite answers with an
#: OverflowError, a 500 — and no table here has a millionth page (audit F-40).
MAX_PAGE = 1_000_000


# --- helpers ------------------------------------------------------------------
def _parse_statuses(values: list[str] | None) -> tuple[AccountStatus, ...]:
    """Turn ``?status=a&status=b`` into enum members.

    An unknown value is a 422 naming it: silently dropping it would answer with
    an empty table and no hint that the filter was the problem.
    """
    parsed: list[AccountStatus] = []
    for raw in values or ():
        try:
            parsed.append(AccountStatus(raw))
        except ValueError as exc:
            valid = ", ".join(s.value for s in AccountStatus)
            raise HTTPException(
                status_code=422,
                detail=f"Unknown status '{raw}'. Valid values: {valid}",
            ) from exc
    return tuple(parsed)


def _parse_sort(sort: str | None) -> str:
    """Validate a sort key against the whitelist (never getattr on the model)."""
    key = sort or DEFAULT_SORT
    if key not in SORTABLE:
        valid = ", ".join(sorted(SORTABLE))
        raise HTTPException(
            status_code=422, detail=f"Unknown sort '{key}'. Sortable: {valid}"
        )
    return key


def _require_tag(db: DbSession, tag_id: int) -> Tag:
    """Fetch a tag or 422 — assigning a tag that does not exist is a bad request."""
    tag = db.get(Tag, tag_id)
    if tag is None:
        raise HTTPException(status_code=422, detail=f"No tag with id {tag_id}")
    return tag


def _require_gst(db: DbSession, gst_id: int) -> None:
    """422 for a GST that is not in the Address Book, as the addresses API
    answers: an account quietly linked to no GST would bill under the run's."""
    if db.scalar(select(Gst.id).where(Gst.id == gst_id)) is None:
        raise HTTPException(status_code=422, detail=f"No GST with id {gst_id}")


def _selection(account_ids: list[int] | None, filter_in: AccountFilterIn | None, action: str):
    """The WHERE clause for a bulk action's "which accounts?", or None when
    the id list is empty and nothing is to change.

    The filter form compiles to ``id IN (SELECT id FROM accounts WHERE ...)``,
    so a selection of 37,000 accounts is one statement and loads no row.
    """
    if account_ids is not None:
        if not account_ids:
            return None
        return Account.id.in_(account_ids)
    flt = filter_in.to_filter()  # the body's validator guarantees one of the two
    if flt.is_empty():
        log.warning("bulk %s with an empty filter — every account matches", action)
    return Account.id.in_(account_filter.apply(select(Account.id), flt))


def _tag_counts(db: DbSession) -> list[dict]:
    """Every tag with how many accounts hold it.

    An outer join, so a freshly created tag still appears (count 0) and can be
    picked in the filter UI before anything is tagged with it.
    """
    rows = db.execute(
        select(Tag.id, Tag.name, func.count(Account.id))
        .outerjoin(Account, Account.tag_id == Tag.id)
        .group_by(Tag.id, Tag.name)
        .order_by(Tag.name.asc())
    ).all()
    return [
        {"id": int(tag_id), "name": name, "count": int(count)}
        for tag_id, name, count in rows
    ]


# --- listing ------------------------------------------------------------------
@router.get("", response_model=AccountPage)
def list_accounts(
    db: DbSession = Depends(get_db),
    page: int = Query(1, ge=1, le=MAX_PAGE),
    size: int = Query(DEFAULT_SIZE, ge=1),
    q: str | None = Query(None, description="substring of email or phone"),
    status: list[str] | None = Query(None),
    # Bounded like the filter body's tag_ids: an id the database cannot bind
    # is a 422, not an OverflowError (audit F-40).
    tag_id: list[RowId] | None = Query(None, max_length=MAX_LISTED_TAGS),
    untagged: bool = Query(False, description="include accounts with no tag"),
    subscription: list[str] | None = Query(None),
    domain: list[str] | None = Query(None, max_length=200),
    sort: str = Query(DEFAULT_SORT),
    desc: bool = Query(False),
) -> AccountPage:
    """One page of accounts plus the total number matching the filter."""
    flt = AccountFilter(
        q=(q or "").strip() or None,
        statuses=_parse_statuses(status),
        tag_ids=tuple(tag_id or ()),
        include_untagged=untagged,
        subscriptions=tuple(subscription or ()),
        domains=tuple(d for d in ((x or "").strip().lower() for x in domain or ()) if d),
    )
    # Clamped rather than rejected: an over-large page is a client being greedy,
    # not a broken request, and MAX_SIZE still protects the response size.
    size = min(size, MAX_SIZE)

    rows, total = account_filter.page(
        db, flt, page_number=page, size=size, sort=_parse_sort(sort), descending=desc
    )
    return AccountPage(
        items=[AccountOut.model_validate(row) for row in rows],
        total=total,
        page=page,
        size=size,
        pages=(total + size - 1) // size,  # 0 when total is 0
    )


@router.get("/filter-options", response_model=FilterOptionsOut)
def filter_options(db: DbSession = Depends(get_db)) -> FilterOptionsOut:
    """Distinct statuses / subscriptions / tags with counts, for the filter UI."""
    opts = account_filter.options(db)
    return FilterOptionsOut(
        statuses=opts.statuses,
        subscriptions=opts.subscriptions,
        tags=_tag_counts(db),
        total=opts.total,
        untagged=opts.untagged,
        domains=opts.domains,
    )


# --- create -------------------------------------------------------------------
@router.post("", response_model=AccountOut, status_code=201)
def create_account(payload: AccountCreate, db: DbSession = Depends(get_db)) -> AccountOut:
    # One mailbox is one Flipkart account, however its email is capitalised:
    # "Buyer.One@gmail.com" beside "buyer.one@gmail.com" was a second row for
    # the same buyer, ordered for twice and counted against its limits apart
    # (11 Nov 2026). Stored lower-case, as the sheet import stores it.
    email = str(payload.email).strip().lower()
    existing = db.scalar(select(Account).where(func.lower(Account.email) == email))
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"{existing.email} is already in Accounts")
    account = Account(email=email, phone=payload.phone, status=AccountStatus.new)
    db.add(account)
    db.commit()
    db.refresh(account)
    return AccountOut.model_validate(account)


# --- bulk tagging -------------------------------------------------------------
class EmailsLookupIn(BaseModel):
    """The Smart Bulk Order form's "Upload CSV": the email ids to run on."""

    emails: list[str] = Field(default_factory=list, max_length=MAX_LISTED_IDS)
    #: Only accounts in these states count as matched (e.g. a valid session).
    statuses: list[AccountStatus] = Field(default_factory=list)


class EmailsLookupOut(BaseModel):
    #: The matched accounts, in the order the file named them.
    ids: list[int]
    #: Emails with no account at all.
    unmatched: list[str]
    #: Emails whose account is not in one of the asked states.
    not_eligible: list[str]


@router.post("/by-emails", response_model=EmailsLookupOut)
def accounts_by_emails(payload: EmailsLookupIn, db: DbSession = Depends(get_db)) -> EmailsLookupOut:
    """Turn an uploaded list of email ids into account ids (case-insensitive)."""
    wanted = list(dict.fromkeys(e.strip().lower() for e in payload.emails if e and e.strip()))
    found: dict[str, Account] = {}
    for start in range(0, len(wanted), 500):
        chunk = wanted[start : start + 500]
        for row in db.scalars(select(Account).where(func.lower(Account.email).in_(chunk))):
            found[row.email.lower()] = row
    allowed = set(payload.statuses)
    ids: list[int] = []
    unmatched: list[str] = []
    not_eligible: list[str] = []
    for email in wanted:
        row = found.get(email)
        if row is None:
            unmatched.append(email)
        elif allowed and row.status not in allowed:
            not_eligible.append(email)
        else:
            ids.append(row.id)
    return EmailsLookupOut(ids=ids, unmatched=unmatched, not_eligible=not_eligible)


@router.post("/tag", response_model=TagAssignResult)
def tag_accounts(
    payload: TagAssignRequest, db: DbSession = Depends(get_db)
) -> TagAssignResult:
    """Set (or clear) the tag on a selection of accounts in one statement.

    The filter form is the one that has to scale: it compiles to
    ``UPDATE accounts SET tag_id = ? WHERE id IN (SELECT id FROM accounts
    WHERE ...)``, so tagging 37,000 rows is one round trip and never loads a row
    into Python.
    """
    if payload.tag_id is not None:
        _require_tag(db, payload.tag_id)

    where = _selection(payload.account_ids, payload.filter, "tag")
    if where is None:
        return TagAssignResult(updated=0)  # nothing selected, nothing to do

    result = db.execute(
        update(Account)
        .where(where)
        .values(tag_id=payload.tag_id)  # updated_at follows via Column.onupdate
        .execution_options(synchronize_session=False)
    )
    db.commit()
    updated = int(result.rowcount or 0)
    log.info("bulk tag -> tag_id=%s on %d account(s)", payload.tag_id, updated)
    return TagAssignResult(updated=updated)


# --- bulk GST -----------------------------------------------------------------
@router.post("/gst", response_model=GstAssignResult, dependencies=[Depends(require_admin)])
def assign_gst(
    payload: GstAssignRequest, db: DbSession = Depends(get_db)
) -> GstAssignResult:
    """Give a selection of accounts their own GST (or take it off) in one statement.

    An account's own GST wins over the one a run picks (app.jobs.tasks), so
    this is how the accounts of one entity are made to bill under its
    registration whichever GST a run shows. Selected like bulk tagging — by
    ids, at most MAX_LISTED_IDS of them, or by the filter the page shows —
    and written as one ``UPDATE``, so 37,000 accounts cost one round trip.
    ``gst_id: null`` puts the selection back on the run's GST.

    Admin only: an account's own GST is setup (``ADMIN_FIELDS``, spec C-7).
    """
    if payload.gst_id is not None:
        _require_gst(db, payload.gst_id)

    where = _selection(payload.account_ids, payload.filter, "GST")
    if where is None:
        return GstAssignResult(updated=0)

    result = db.execute(
        update(Account)
        .where(where)
        .values(gst_id=payload.gst_id)  # updated_at follows via Column.onupdate
        .execution_options(synchronize_session=False)
    )
    db.commit()
    updated = int(result.rowcount or 0)
    log.info("bulk GST -> gst_id=%s on %d account(s)", payload.gst_id, updated)
    return GstAssignResult(updated=updated)


# --- Excel --------------------------------------------------------------------
@router.post("/import", response_model=ImportResult)
async def import_accounts(request: Request, db: DbSession = Depends(get_db)) -> dict:
    """Import the accounts of a workbook in the legacy layout (app.io.excel),
    sent as a form upload with the workbook in its ``file`` field.

    The form is read here, in memory (app.api.uploads), not by a file
    parameter: in the legacy layout columns T to W hold every account's card
    number, expiry and CVV, and Starlette's usual reader writes a workbook
    over 1 MiB, as the one of every account is, to a temp file on disk.
    """
    upload = await uploads.read_sheet(request)
    name = upload.filename.lower()
    if name.endswith(".xls"):
        # The old binary Excel format cannot be read here; say so instead of
        # failing on the first cell (audit F-26, 25 Oct 2026).
        raise HTTPException(status_code=400, detail="Save the sheet as .xlsx first — the old .xls format cannot be read")
    if not name.endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Please upload an .xlsx file")

    try:
        # Read and filed on a worker thread, as an order report is: the
        # workbook of every account (37,000 rows) took 16 s, and on the event
        # loop that held every other request, the live run view and any order
        # run with it (review of 27 Oct 2026). The session is only ever used
        # by one thread at a time.
        return await asyncio.to_thread(excel.import_accounts, io.BytesIO(upload.data), db)
    except excel.NoAccountsFound:
        # Read, but nothing in it is an email: say that, not "0 created".
        raise HTTPException(
            status_code=422,
            detail=(
                "No email addresses were found in the sheet. Put the buyer accounts' emails in one column "
                "(a heading such as “Email” helps), their phone numbers beside them, and upload it again."
            ),
        ) from None
    except Exception as exc:  # noqa: BLE001 — surface a clean error to the client
        # Never the parser's own message: openpyxl quotes a badly typed cell's
        # text in it, and an account sheet in the legacy layout carries card
        # columns (T to W) that the import otherwise leaves unread. So, as for
        # a card sheet, the log names the error's type and the answer is one
        # fixed sentence.
        log.warning("account sheet unreadable: %s", type(exc).__name__)
        raise HTTPException(
            status_code=422,
            detail="Could not read the file: it is not a readable .xlsx workbook — save it again from Excel and retry.",
        ) from None


@router.get("/export")
def export_accounts(db: DbSession = Depends(get_db)) -> StreamingResponse:
    data = excel.export_accounts(db)
    headers = {"Content-Disposition": 'attachment; filename="accounts.xlsx"'}
    return StreamingResponse(io.BytesIO(data), media_type=_XLSX_MIME, headers=headers)


# --- single account -----------------------------------------------------------
# Declared last on purpose: "/{account_id}" would otherwise shadow the literal
# sub-paths above ("/export", "/import", "/tag", "/gst", "/filter-options") the
# moment one of them gains a method this route also serves.
@router.patch("/{account_id}", response_model=AccountOut)
def update_account(
    account_id: int,
    payload: AccountUpdate,
    db: DbSession = Depends(get_db),
    principal: Principal = Depends(current_user),
) -> AccountOut:
    """Edit phone / status / subscription_type / tag_id / gst_id /
    billing_phone on one account.

    Only the fields actually present in the body are written, so a PATCH that
    sends ``phone`` cannot blank the tag by omission — and ``tag_id: null``
    clears it on purpose, as ``gst_id: null`` puts the account back on the
    run's GST and ``billing_phone: null`` (or blank) back on its own phone.
    A body naming ``gst_id`` or ``billing_phone`` is an admin's to send
    (``ADMIN_FIELDS``); an operator may send the rest.
    """
    if payload.model_fields_set & ADMIN_FIELDS:
        # Before the row is looked up, as the admin-only routes' dependency
        # runs: an operator gets the same 403 whether or not the account exists.
        require_admin(principal)
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    sent = payload.model_fields_set
    if "tag_id" in sent and payload.tag_id is not None:
        _require_tag(db, payload.tag_id)
    if "gst_id" in sent and payload.gst_id is not None:
        _require_gst(db, payload.gst_id)

    if "parent_email" in sent and payload.parent_email:
        parent_email = payload.parent_email.strip().lower()
        if parent_email == account.email.lower():
            raise HTTPException(status_code=422, detail="An account cannot be its own parent.")
        parent = db.scalar(select(Account).where(func.lower(Account.email) == parent_email))
        if parent is None:
            raise HTTPException(status_code=422, detail=f"No account has the email {payload.parent_email!r}.")
        if not parent.is_parent:
            parent.is_parent = True
        payload.parent_email = parent.email
    for field in ("phone", "status", "subscription_type", "tag_id", "gst_id", "billing_phone", "is_parent", "parent_email"):
        if field in sent:
            setattr(account, field, getattr(payload, field))

    db.commit()
    db.refresh(account)
    return AccountOut.model_validate(account)
