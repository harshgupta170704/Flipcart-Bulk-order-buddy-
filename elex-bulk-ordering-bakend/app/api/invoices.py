"""Invoice PDFs handed in by the operator, and the archives compiled from them.

The read-back fetches invoices itself when an order's page offers the link;
``POST /invoices/upload`` is the other way in — PDFs downloaded by hand,
several at a time. Each file is read first (app.io.invoice_pdf) and matched to
its order by the order id in the file name (Flipkart names them that way) or,
failing that, by the one printed on the PDF; a file whose name and PDF name
different orders is refused rather than filed under either. The answer says
what happened to every file.

Extract Invoices Data (spec FR-026–FR-030) is ``GET /invoices/types`` — the
invoice types found on the orders of the units matching the ledger filter — and
``GET /invoices/data.csv?type=…``, the invoice-data CSV of the ticked types.

The rest of this module is Compile Invoices (spec FR-031–FR-039): start a
compilation of the invoice PDFs of the order units matching the ledger filter,
watch its status, list the archives, hand out a short-lived download link, and
delete one. The archive download itself lives on :data:`public_router`, which
carries no bearer dependency: a browser downloads a gigabyte by navigating to
the URL, and such a request cannot carry the header — the token in the query
string is what authorises it (see :mod:`app.core.archives`).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import io
import logging
import re
import zipfile
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session as DbSession

# Private-by-name but shared by design, as api/sync.py does: _spawn is the one
# place a detached run is started, with the done-callback that marks a crash.
from app.api.jobs import _spawn
from app.api.orders import parse_filter
from app.config import get_settings
from app.core import archives as archive_rules
from app.core import invoices, order_filter
from app.core.archives import ArchiveStore
from app.core.order_filter import OrderFilter
from app.core.security import require_user
from app.core.unit_events import naive_utc
from app.db.base import get_db
from app.db.models import CompiledArchive, Job, JobStatus, JobType, Order
from app.io import invoice_data_csv
from app.io.invoice_store import InvoiceStore
from app.jobs.compile_tasks import OUTCOME_FAILED, STOPPED_EARLY, run_compile_job
from app.schemas.job import JobOut

log = logging.getLogger("ordering.api.invoices")

router = APIRouter(
    prefix="/invoices",
    tags=["invoices"],
    dependencies=[Depends(require_user)],
)

#: The archive download: authorised by its token, not by the bearer header.
public_router = APIRouter(prefix="/invoices", tags=["invoices"])

_ORDER_ID_IN_NAME = re.compile(r"OD\d{10,}", re.IGNORECASE)
MAX_FILES = 200

#: Flipkart hands out an order's invoices as one zip (``OD….zip`` holding
#: ``file_1.pdf``, ``file_2.pdf``…), so a zip is accepted and unpacked in
#: memory. Bounded three ways: PDFs per zip, bytes per PDF (the same cap as a
#: loose upload) and bytes per zip, so a zip cannot be used to exhaust memory.
_ZIP_SIGNATURE = b"PK\x03\x04"
MAX_ZIP_MEMBERS = 100
MAX_ZIP_BYTES = 200 * 1024 * 1024
#: And the upload as a whole: what it may hand the reader in all — the loose
#: files' bytes plus what its zips unpack to, which is what stays in memory
#: until the upload is answered. Each file's own cap still holds; this one
#: stops two hundred files of 25 MB each (5 GB) from being held at once. Past
#: it the rest of the upload is not read at all, and each file left over says
#: so (audit F-27, 26 Oct 2026).
MAX_UPLOAD_BYTES = 250 * 1024 * 1024

_ACTIVE = (JobStatus.queued, JobStatus.running)

#: The ledger filter's query parameters, as ``parse_filter`` declares them;
#: the ones that repeat are kept as lists so the job can rebuild the filter.
#: Every one of them, or the zip quietly covers more orders than the screen
#: counted: tests/test_w1d_invoice_filters.py holds this list to parse_filter.
_FILTER_KEYS = (
    "q",
    "status",
    "platform_status",
    "delivery_state",
    "processing_stage",
    "error_code",
    "order_id",
    "email",
    "tracking_id",
    "card",
    "billed_gst",
    "invoice_number",
    "invoice_gstin",
    "invoice_month",
    "account_id",
    "job_id",
    "row",
    "date_field",
    "date_from",
    "date_to",
    "platform",
    "bob",
)
_LIST_KEYS = frozenset(
    {"status", "platform_status", "delivery_state", "processing_stage", "error_code", "order_id", "email", "tracking_id", "platform"}
)


class InvoiceFileOut(BaseModel):
    filename: str
    #: The order the file was matched to — for a mismatch, the one its name claims.
    order_id: str | None = None
    #: stored / duplicate / no_match / mismatch / not_pdf / too_large / not_saved
    status: str
    #: For a file this upload put on file: "read", or "not_read" when the
    #: reader could not make an invoice of it — kept all the same (FR-023),
    #: with why in ``reason``, but in no invoice-data CSV until a reader can
    #: read it (NFR-009: the upload says so, not only the row dialog). None
    #: for a file not put on file.
    reading: str | None = None
    reason: str | None = None


class InvoiceUploadOut(BaseModel):
    stored: int
    files: list[InvoiceFileOut] = Field(default_factory=list)


@router.post("/upload", response_model=InvoiceUploadOut)
async def upload_invoices(
    files: list[UploadFile] = File(...), db: DbSession = Depends(get_db)
) -> InvoiceUploadOut:
    """Put invoice PDFs on file, each matched to its order by the order id in
    the file name or, failing that, printed on the PDF (the owner's own files
    are ``file_1.pdf``, ``file_2.pdf``…: every one prints its order id). A zip
    is unpacked and its PDFs filed one by one under ``<zip>/<member>``, the
    order id in the zip's name standing for each (Flipkart's ``OD….zip``). A
    file whose name and PDF name different orders is refused — put on file for
    neither — with both ids in the reason.

    Sizes are bounded: 25 MB a PDF, 200 MB a zip, and 250 MB for the whole
    upload — the loose files plus what the zips unpack to. A file over its own
    cap is refused by name and the rest goes through; the file that would
    take the upload past its total, and every file after it, is refused
    unread, each saying so."""
    if len(files) > MAX_FILES:
        raise HTTPException(status_code=413, detail=f"at most {MAX_FILES} files per upload")
    store = InvoiceStore.from_settings()
    out = InvoiceUploadOut(stored=0)
    # A zip counts as the PDFs inside it, named "<zip>/<member>" so the order
    # id in the zip's name covers every one; the upload's cap counts them too.
    entries: list[tuple[str, bytes | None, InvoiceFileOut | None]] = []
    # What the upload may still hand the reader (MAX_UPLOAD_BYTES less what
    # the files before took), and whether a file already went past it: from
    # then on nothing more is read.
    room = MAX_UPLOAD_BYTES
    past_total = False
    for file in files:
        name = file.filename or "file"
        if past_total:
            entries.append((name, None, _past_total(name)))
            continue
        try:
            found = await _read_upload(file, name, room=room)
        except _PastTotal:
            past_total = True
            entries.append((name, None, _past_total(name)))
            continue
        room -= sum(len(data) for _entry, data, _answer in found if data is not None)
        entries.extend(found)
        if len(entries) > MAX_FILES:
            raise HTTPException(
                status_code=413, detail=f"at most {MAX_FILES} PDFs per upload (a zip counts as the PDFs inside it)"
            )
    # Two passes on purpose. First every file is read and parsed — the PDF
    # library takes tens of milliseconds a file, so that runs in a thread —
    # with nothing written yet. Only then come the lookups, the writes and
    # the one commit, with no await in between: on SQLite the first write
    # takes the database's write lock for the rest of the transaction, and a
    # handler that paused mid-way would hold it while a read-back tried to
    # record its progress on the same loop.
    answers: list[InvoiceFileOut | None] = [None] * len(entries)  # in upload order, whatever pass decides each
    readable: list[tuple[int, str, bytes, object]] = []
    for index, (name, data, decided) in enumerate(entries):
        if decided is not None:
            answers[index] = decided
            continue
        data = data or b""
        if len(data) > invoices.MAX_PDF_BYTES:
            answers[index] = InvoiceFileOut(filename=name, status="too_large", reason="larger than 25 MB")
            continue
        if not data.startswith(b"%PDF-"):
            answers[index] = InvoiceFileOut(filename=name, status="not_pdf", reason="not a PDF file")
            continue
        readable.append((index, name, data, await asyncio.to_thread(invoices.parse_pdf, data)))
    # The files this upload writes, whose rows are all in the one transaction
    # below: if it fails, the rows go with it, and so must the files, or the
    # store keeps PDFs no row knows (NFR-009). The client still hears of the
    # failure, as before.
    new_files: list[str] = []
    try:
        for index, name, data, parsed in readable:
            answers[index] = _file_on_file(db, store, name, data, parsed, new_files)
            if answers[index].status == "stored":
                out.stored += 1
        db.commit()
    except Exception:
        # Rolled back first, so the check of what is saved reads past nothing
        # this transaction still holds.
        with contextlib.suppress(SQLAlchemyError):
            db.rollback()
        invoices.remove_unsaved_files(store, new_files)
        raise
    if out.stored:
        # A PDF put on file by hand is how a "Product invoice missing" or an
        # "Invoice amount differs" alert is answered: the rules run now, so it
        # closes without waiting for the next read-back. Off the event loop,
        # as after a read-back: a pass waits on the engine's lock.
        from app.core import alerts as alert_rules

        try:
            await asyncio.to_thread(alert_rules.evaluate_in_own_session)
        except Exception:  # noqa: BLE001 — the upload stands; the next pass catches up
            log.exception("the alert rules could not run after an invoice upload")
    out.files = [answer for answer in answers if answer is not None]
    return out


class _PastTotal(Exception):
    """The file would take the upload past :data:`MAX_UPLOAD_BYTES`."""


def _past_total(name: str) -> InvoiceFileOut:
    """The answer for a file the upload's total left no room for."""
    return InvoiceFileOut(
        filename=name,
        status="too_large",
        reason=(
            f"the upload is over {MAX_UPLOAD_BYTES // (1024 * 1024)} MB in all, so this file was not read; "
            "upload it again in a smaller batch"
        ),
    )


async def _read_upload(
    file: UploadFile, name: str, *, room: int
) -> list[tuple[str, bytes | None, InvoiceFileOut | None]]:
    """One uploaded file as entries (see :func:`_unpack`), read up to its own
    cap plus one byte, never the whole of whatever was sent: a file past its
    cap is answered by name, not held in memory (audit F-27, 25 Oct 2026).
    Raises :class:`_PastTotal` when the file would take more than ``room``.

    A function of its own so that the bytes read — a zip's, up to 200 MB —
    are let go when it returns, before the next file is read; only what is
    kept for the reader stays."""
    raw = await _read_capped(file)
    if raw.startswith(_ZIP_SIGNATURE):
        if len(raw) > MAX_ZIP_BYTES:
            return [
                (name, None, InvoiceFileOut(filename=name, status="too_large", reason=f"larger than {MAX_ZIP_BYTES // (1024 * 1024)} MB"))
            ]
    elif len(raw) > invoices.MAX_PDF_BYTES:
        return [(name, None, InvoiceFileOut(filename=name, status="too_large", reason="larger than 25 MB"))]
    return _unpack(name, raw, room=room)


async def _read_capped(file: UploadFile) -> bytes:
    """The file's bytes, read no further than its own cap plus one byte —
    the one byte more is how a file past its cap is told. A zip (its first
    four bytes say so) may be read up to :data:`MAX_ZIP_BYTES`; anything else
    is a PDF or nothing usable, and is read up to the PDF cap, so a 150 MB
    PDF costs 25 MB of memory to refuse, not 150."""
    head = await file.read(len(_ZIP_SIGNATURE))
    cap = MAX_ZIP_BYTES if head == _ZIP_SIGNATURE else invoices.MAX_PDF_BYTES
    # Back to the start rather than gluing the rest onto the head: the copy
    # would hold the file twice, if only for a moment.
    await file.seek(0)
    return await file.read(cap + 1)


def _unpack(
    name: str, data: bytes, *, room: int | None = None
) -> list[tuple[str, bytes | None, InvoiceFileOut | None]]:
    """What one upload holds, as ``(name, bytes, None)`` entries still to be
    read or ``(name, None, answer)`` ones already decided.

    A plain file is one entry. A zip — the way Flipkart hands out an order's
    invoices — becomes one entry per PDF inside it, named ``<zip>/<member>``,
    so the order id in the zip's name is matched (and checked against the
    print) for each. Folders, non-PDF members and macOS resource forks are
    passed over; a member too large, one that cannot be read (damaged or
    encrypted), a zip with no PDF, with too many or unpacking to too much are
    answered, never raised, so the rest of the upload still goes through.

    ``room`` is what the upload may still hand the reader. A file that would
    take more — a plain file longer than it, a zip whose PDFs say they unpack
    to more — raises :class:`_PastTotal`, a zip before any of it is unpacked.
    """
    if not data.startswith(_ZIP_SIGNATURE):
        if room is not None and len(data) > room:
            raise _PastTotal
        return [(name, data, None)]
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        members = [m for m in archive.infolist() if not m.is_dir()]
    except zipfile.BadZipFile:
        return [(name, None, InvoiceFileOut(filename=name, status="not_pdf", reason="not a PDF, and not a zip that can be opened"))]
    pdfs = [m for m in members if m.filename.lower().endswith(".pdf") and "__macosx/" not in m.filename.lower()]
    if not pdfs:
        return [(name, None, InvoiceFileOut(filename=name, status="not_pdf", reason="the zip holds no PDF"))]
    if len(pdfs) > MAX_ZIP_MEMBERS:
        return [
            (name, None, InvoiceFileOut(filename=name, status="too_large", reason=f"the zip holds more than {MAX_ZIP_MEMBERS} PDFs"))
        ]
    if sum(m.file_size for m in pdfs) > MAX_ZIP_BYTES:
        return [
            (
                name,
                None,
                InvoiceFileOut(
                    filename=name, status="too_large", reason=f"the zip unpacks to more than {MAX_ZIP_BYTES // (1024 * 1024)} MB"
                ),
            )
        ]
    # Weighed on the sizes the zip declares, before a byte is unpacked; a
    # member never unpacks to more than it declares (zipfile stops there and
    # the checksum refuses the rest). The members refused below as too large
    # are never kept, so they take no room.
    if room is not None and sum(m.file_size for m in pdfs if m.file_size <= invoices.MAX_PDF_BYTES) > room:
        raise _PastTotal
    entries: list[tuple[str, bytes | None, InvoiceFileOut | None]] = []
    for member in pdfs:
        entry = f"{name}/{member.filename.rsplit('/', 1)[-1]}"
        if member.file_size > invoices.MAX_PDF_BYTES:
            entries.append((entry, None, InvoiceFileOut(filename=entry, status="too_large", reason="larger than 25 MB")))
            continue
        try:
            blob = archive.read(member)
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, ValueError) as exc:
            reason = str(exc).strip() or exc.__class__.__name__
            entries.append(
                (entry, None, InvoiceFileOut(filename=entry, status="not_pdf", reason=f"could not be read from the zip: {reason}"[:200]))
            )
            continue
        if len(blob) > invoices.MAX_PDF_BYTES:  # the declared size was not the truth
            entries.append((entry, None, InvoiceFileOut(filename=entry, status="too_large", reason="larger than 25 MB")))
            continue
        entries.append((entry, blob, None))
    return entries


def _file_on_file(
    db: DbSession, store: InvoiceStore, name: str, data: bytes, parsed, new_files: list[str] | None = None
) -> InvoiceFileOut:
    """Match one readable PDF to its order and put it on file; all synchronous,
    so the transaction is never held open across an await. ``new_files``
    collects the store path of a file this call wrote (see
    :func:`app.core.invoices.attach`)."""
    match = _ORDER_ID_IN_NAME.search(name)
    named_id = match.group(0).upper() if match else None
    printed_id = (parsed.order_id or "").strip().upper() or None
    if named_id and printed_id and named_id != printed_id:
        return InvoiceFileOut(
            filename=name,
            order_id=named_id,
            status="mismatch",
            reason=f"the file name says {named_id} but the PDF is the invoice of {printed_id} — not put on file for either",
        )
    order_id = named_id or printed_id
    if order_id is None:
        return InvoiceFileOut(filename=name, status="no_match", reason="no order id (OD…) in the file name or on the PDF")
    order = db.scalar(select(Order).where(Order.order_id == order_id).order_by(Order.id.desc()).limit(1))
    if order is None:
        return InvoiceFileOut(filename=name, order_id=order_id, status="no_match", reason=f"{order_id} is not in the ledger")
    try:
        row = invoices.attach(db, store, order, data, source="upload", parsed=parsed, new_files=new_files)
    except invoices.OrderMismatch as exc:
        return InvoiceFileOut(filename=name, order_id=order_id, status="mismatch", reason=str(exc))
    except invoices.NotSaved as exc:
        # Rolled back alone: the upload's other files are on file all the same
        # (audit F-28).
        return InvoiceFileOut(
            filename=name,
            order_id=order_id,
            status="not_saved",
            reason=f"could not be saved ({exc.error}); the other files went through — upload this one again",
        )
    if row is None:
        return InvoiceFileOut(filename=name, order_id=order_id, status="duplicate", reason="already on file")
    if row.invoice_type is None:
        # The same test Extract Invoices Data applies: a PDF with no type read
        # off it is in no type's count and in no CSV.
        return InvoiceFileOut(
            filename=name,
            order_id=order_id,
            status="stored",
            reading="not_read",
            reason=row.parse_error or "the reader found no invoice on it",
        )
    return InvoiceFileOut(filename=name, order_id=order_id, status="stored", reading="read")


# --- Extract Invoices Data (spec FR-026–FR-030) ---------------------------------------------
class InvoiceTypeOut(BaseModel):
    """One entry of the dialog's checkbox list."""

    type: str
    #: Invoices of this type on the matching units' orders.
    invoices: int
    #: Matching units whose order has an invoice of this type.
    units: int


class InvoiceTypesOut(BaseModel):
    types: list[InvoiceTypeOut] = Field(default_factory=list)
    #: Units matching the filter, and how many of them have no invoice the
    #: parser could read — so nothing of theirs can be in any CSV (FR-030).
    units: int
    units_without_invoice: int


@router.get("/types", response_model=InvoiceTypesOut)
def invoice_types(flt: OrderFilter = Depends(parse_filter), db: DbSession = Depends(get_db)) -> InvoiceTypesOut:
    """The invoice types found on the orders of the units matching the ledger
    filter, with counts — what Extract Invoices Data lists (FR-026, C-10)."""
    with_invoice, without_invoice = invoices.unit_split(db, flt)
    return InvoiceTypesOut(
        types=[InvoiceTypeOut(**item) for item in invoices.type_counts(db, flt)],
        units=with_invoice + without_invoice,
        units_without_invoice=without_invoice,
    )


@router.get("/data.csv")
def invoice_data_csv_export(
    ticked: list[str] | None = Query(None, alias="type", description="an invoice type to include, repeated"),
    flt: OrderFilter = Depends(parse_filter),
    db: DbSession = Depends(get_db),
) -> StreamingResponse:
    """Download the invoice data of the ticked types for the units matching
    the filter, one row per invoice line (FR-027/028), as
    ``invoice_data_<date>_<HHMM>.csv``. Which invoices go in, and the rows
    they make, are settled before the first byte goes out, so
    ``X-Invoice-Rows`` and ``X-Units-Left-Out`` ride on the response — the
    latter the units with no readable invoice of the ticked types, which is
    exactly the units with no row in this file (FR-030). No type ticked is a
    422 (FR-029). Not capped: the rows are the parsed lines, not the PDFs, and
    they are read a batch at a time as the text is written (audit F-39), so
    memory holds the invoices' ids, never the file.
    """
    types = [t.strip() for t in (ticked or []) if t and t.strip()]
    if not types:
        raise HTTPException(status_code=422, detail="Tick at least one invoice type.")
    _with_invoice, without_invoice = invoices.unit_split(db, flt, types)
    selection = invoices.data_selection(db, flt, types)
    headers = {
        "Content-Disposition": f'attachment; filename="{invoice_data_csv.export_filename()}"',
        "X-Invoice-Rows": str(selection.rows),
        "X-Units-Left-Out": str(without_invoice),
    }
    return StreamingResponse(
        invoice_data_csv.stream(invoices.data_pairs(db, selection)),
        media_type="text/csv; charset=utf-8",
        headers=headers,
    )


# --- Fetch invoices from Flipkart ------------------------------------------------------
class FetchStartOut(BaseModel):
    job: JobOut
    #: Orders (with a Flipkart order id) whose invoices will be asked for.
    orders: int
    accounts: int


def fetch_filter(flt: OrderFilter) -> OrderFilter:
    """The filter a fetch walks: the ledger filter without its invoice filters.
    Those match the invoices *on file*, so before the first fetch they match
    no order at all — an "October invoices" filter would fetch nothing. The
    CSV applies them afterwards, to what was fetched."""
    return dataclasses.replace(flt, invoice_number=None, invoice_gstin=None, invoice_month=None)


def _active_invoice_fetch(db: DbSession) -> Job | None:
    """The "Fetch invoices" run still queued or running, if any (a read-back
    job whose payload says ``invoices_only``)."""
    for job in db.scalars(
        select(Job).where(Job.type == JobType.sync, Job.status.in_((JobStatus.queued, JobStatus.running)))
    ):
        if (job.payload or {}).get("invoices_only"):
            return job
    return None


@router.post("/fetch", response_model=FetchStartOut, status_code=201)
async def start_invoice_fetch(
    flt: OrderFilter = Depends(parse_filter),
    db: DbSession = Depends(get_db),
    who: str = Depends(require_user),
) -> FetchStartOut:
    """Download from Flipkart, store and parse every invoice of the orders
    whose units match the ledger filter — one zip per order from Flipkart's
    invoice API, under the account that placed it. Runs as a read-back job
    (one at a time, and it gives an account up to an order run), watched on
    ``GET /jobs/{id}``. Then Extract Invoices Data writes the CSV."""
    from app.api.jobs import _create_job
    from app.api.sync import _selection_from_orders
    from app.jobs.sync_tasks import run_sync_job

    # One fetch at a time — but not "no read-back at all": the timer starts
    # one every few minutes, so the button would nearly always be refused. A
    # fetch writes only invoice rows, and the same PDF twice for an order is
    # one row (uq_invoices_order_sha256), so it may run beside a read-back.
    active = _active_invoice_fetch(db)
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Invoice fetch #{active.id} is already running — wait for it to finish.",
        )
    matching = order_filter.matching_orders(fetch_filter(flt)).subquery()
    order_ids = sorted(
        {
            str(order_id).strip().upper()
            for order_id in db.scalars(
                select(Order.order_id).where(Order.id.in_(select(matching.c.id)), Order.order_id.is_not(None))
            ).all()
            if order_id and str(order_id).strip()
        }
    )
    if not order_ids:
        raise HTTPException(status_code=422, detail="No order matching the current filter has a Flipkart order id.")
    selection = _selection_from_orders(db, order_ids)
    job = _create_job(
        db,
        JobType.sync,
        selection,
        {"invoices_only": True, "orders": len(order_ids), "order_ids": order_ids},
        started_by=who,
    )
    out = JobOut.model_validate(job)
    log.info("starting invoice fetch job %s: %s order(s) over %s account(s)", out.id, len(order_ids), len(selection.ids))
    account_ids = list(selection.ids)
    _spawn(lambda: run_sync_job(out.id, account_ids, order_ids=order_ids, invoices_only=True), out.id)
    return FetchStartOut(job=out, orders=len(order_ids), accounts=len(account_ids))


# --- Compile Invoices ------------------------------------------------------------------
class CompileIn(BaseModel):
    """Body of ``POST /invoices/compile``: the Invoice Name Prefix (FR-031)."""

    model_config = ConfigDict(extra="forbid")

    prefix: str = Field(min_length=1, max_length=120)


class CompileStartOut(BaseModel):
    job: JobOut
    #: ``<prefix>.zip``, numbered when the prefix was already taken.
    archive_name: str
    orders: int
    units: int
    #: Units whose order has no invoice on file — left out (FR-030).
    units_without_invoice: int


class CompileStatusOut(BaseModel):
    """What "Compilation Status" shows: the run in progress, and the last one
    (whose ``payload.outcome`` says completed / cancelled / failed and whose
    ``payload.error`` says why — FR-034/036/038), plus how long archives are
    kept (0: until deleted), so the dialog can say so truthfully."""

    active: JobOut | None = None
    last: JobOut | None = None
    archive_retention_days: int = 0


class ArchiveOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    size: int
    orders: int
    units: int
    invoices: int
    left_out: int
    missing_files: int = 0
    created_at: datetime
    job_id: int | None = None


class ArchivePageOut(BaseModel):
    """One page of the archive list, newest first, and how many archives there
    are in all (FR-038: the new archive "or any earlier archive").

    Paged by a key, not an offset: Delete in the same dialog takes rows out of
    the list already shown, and an offset counted after that would step over
    as many archives as were deleted. ``next_before`` names the last archive
    of this page; the next page is every archive older than it, however many
    were deleted in between."""

    items: list[ArchiveOut] = Field(default_factory=list)
    #: Every archive, on every page: the dialog's "Showing 200 of 230".
    total: int
    #: What to send as ``before`` for the next page — ``<created_at>,<id>`` of
    #: this page's last archive — or None when this page reaches the oldest.
    next_before: str | None = None


class DownloadGrantOut(BaseModel):
    token: str
    #: Relative to the API root; the client prefixes its base.
    url: str
    expires_in: int


def _raw_filter(request: Request) -> dict:
    """The ledger filter's query parameters as they came, for the job to rebuild
    through ``build_filter`` — the one reading of the filter. Handed to the run
    in memory; what is recorded is :func:`app.core.archives.recorded_filters`
    of it, which keeps the card number out."""
    raw: dict = {}
    for key, value in request.query_params.multi_items():
        if key not in _FILTER_KEYS:
            continue
        if key in _LIST_KEYS:
            raw.setdefault(key, []).append(value)
        else:
            raw[key] = value
    return raw


def _active_compile(db: DbSession) -> Job | None:
    return db.scalar(
        select(Job)
        .where(Job.type == JobType.compile, Job.status.in_(_ACTIVE))
        .order_by(Job.id.desc())
        .limit(1)
    )


@router.post("/compile", response_model=CompileStartOut, status_code=201)
async def start_compile(
    payload: CompileIn,
    request: Request,
    flt: OrderFilter = Depends(parse_filter),
    db: DbSession = Depends(get_db),
) -> CompileStartOut:
    """Start zipping the invoice PDFs of the order units matching the filter.

    One compilation at a time (FR-037): a second start is a 409 that points at
    Compilation Status. Nothing to compile — no matching unit, or none with an
    invoice on file — is a 422 before any job row exists.
    """
    prefix = archive_rules.clean_prefix(payload.prefix)
    if not prefix:
        raise HTTPException(status_code=422, detail="The invoice name prefix needs at least one letter or digit.")
    active = _active_compile(db)
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Compilation #{active.id} is already in progress — see Compilation Status "
                "under Download compiled invoices, and wait for it to finish or cancel it."
            ),
        )
    with_invoice, without_invoice = order_filter.unit_invoice_split(db, flt)
    if with_invoice + without_invoice == 0:
        raise HTTPException(status_code=422, detail="No order unit matches the current filter.")
    if with_invoice == 0:
        raise HTTPException(
            status_code=422,
            detail=(
                f"None of the {without_invoice:,} matching order units has an invoice on file. "
                "Read the orders back from Flipkart or upload the PDFs first."
            ),
        )
    orders = len(order_filter.order_pks(db, flt))
    raw = _raw_filter(request)
    name = ArchiveStore.from_settings().free_name(db, prefix)
    job = Job(
        type=JobType.compile,
        status=JobStatus.queued,
        total_items=orders,
        payload={
            "prefix": prefix,
            "archive_name": name,
            # Every signed-in user reads this row (Compilation Status, the job
            # list), so the card filter is recorded by its last four digits;
            # the run below is handed the whole filter, in memory (audit F-20).
            "filters": archive_rules.recorded_filters(raw),
            "orders": orders,
            "units": with_invoice + without_invoice,
            "units_without_invoice": without_invoice,
        },
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    _spawn(lambda: run_compile_job(job.id, filter_params=raw, prefix=prefix), job.id)
    return CompileStartOut(
        job=JobOut.model_validate(job),
        archive_name=name,
        orders=orders,
        units=with_invoice + without_invoice,
        units_without_invoice=without_invoice,
    )


def _status_job(job: Job) -> JobOut:
    """The job as Compilation Status reads it. A run that failed outside the
    job body — a crash in the task's done-callback, a restart's sweep — wrote
    its reason to the log and no ``payload.outcome``; it is a failure all the
    same, so the outcome is filled in here rather than read as "ended"."""
    out = JobOut.model_validate(job)
    payload = dict(out.payload or {})
    if job.status == JobStatus.failed and not payload.get("outcome"):
        lines = [line for line in (job.log or "").splitlines() if line.strip()]
        payload.update(outcome=OUTCOME_FAILED, error=lines[-1].strip() if lines else STOPPED_EARLY)
        out.payload = payload
    return out


@router.get("/compile/status", response_model=CompileStatusOut)
def compile_status(db: DbSession = Depends(get_db)) -> CompileStatusOut:
    """The compilation in progress, if any, and the most recent one."""
    active = _active_compile(db)
    last = db.scalar(select(Job).where(Job.type == JobType.compile).order_by(Job.id.desc()).limit(1))
    return CompileStatusOut(
        active=None if active is None else _status_job(active),
        last=None if last is None else _status_job(last),
        archive_retention_days=max(0, int(get_settings().archive_retention_days)),
    )


def _archive(db: DbSession, archive_id: int) -> CompiledArchive:
    row = db.get(CompiledArchive, archive_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Archive not found")
    return row


#: Archives on a page of the list when the client does not say, and the most
#: one page may hold.
ARCHIVES_PAGE_SIZE = 200
ARCHIVES_MAX_PAGE_SIZE = 1000

#: ``before`` as :func:`_archive_key` writes it. The id is what follows the
#: last comma: an ISO stamp may hold one of its own, as its decimal mark.
#: Eighteen digits at most, so it always fits the database's integer.
_ARCHIVE_KEY = re.compile(r"(?P<at>.+),(?P<id>\d{1,18})")


def _archive_key(row: CompiledArchive) -> str:
    """Where the list stands after ``row``: its stamp to the microsecond, then its id."""
    return f"{row.created_at.isoformat(timespec='microseconds')},{row.id}"


def _read_archive_key(before: str) -> tuple[datetime, int]:
    """``before`` as the (created_at, id) the next page starts after. A stamp
    with a zone is read as its UTC moment, which is how the column holds it.

    Anything else is a 422, never the first page: answered with the newest
    archives again, a client asking for older ones would repeat what it
    already shows."""
    match = _ARCHIVE_KEY.fullmatch(before.strip())
    try:
        if match is None:
            raise ValueError(before)
        return naive_utc(datetime.fromisoformat(match["at"].strip())), int(match["id"])
    except (ValueError, OverflowError):
        raise HTTPException(
            status_code=422,
            detail=(
                "'before' must be the next_before of the page before: "
                "an archive's created_at and id, as <created_at>,<id>."
            ),
        ) from None


@router.get("/archives", response_model=ArchivePageOut)
def list_archives(
    before: str | None = Query(
        default=None,
        max_length=80,
        description="the next_before of the page before; leave out for the newest archives",
    ),
    limit: int = Query(default=ARCHIVES_PAGE_SIZE, ge=1, le=ARCHIVES_MAX_PAGE_SIZE),
    db: DbSession = Depends(get_db),
) -> ArchivePageOut:
    """Compiled archives, newest first (FR-038), ``limit`` to a page: the
    newest, or with ``before`` the ones older than the page that gave it out.

    Ordered by when each was compiled, then by id, so two of the same moment
    keep one order and a page can end between them; the next page starts
    exactly after the last archive of this one (see :class:`ArchivePageOut`).
    """
    query = select(CompiledArchive)
    if before is not None:
        at, archive_id = _read_archive_key(before)
        query = query.where(
            or_(
                CompiledArchive.created_at < at,
                and_(CompiledArchive.created_at == at, CompiledArchive.id < archive_id),
            )
        )
    # One more than the page holds: whether it comes back says if there is a next page.
    rows = db.scalars(
        query.order_by(CompiledArchive.created_at.desc(), CompiledArchive.id.desc()).limit(limit + 1)
    ).all()
    page, more = rows[:limit], len(rows) > limit
    total = db.scalar(select(func.count()).select_from(CompiledArchive)) or 0
    return ArchivePageOut(
        items=[ArchiveOut.model_validate(row) for row in page],
        total=total,
        next_before=_archive_key(page[-1]) if more else None,
    )


@router.post("/archives/{archive_id}/download-token", response_model=DownloadGrantOut)
def archive_download_token(archive_id: int, db: DbSession = Depends(get_db)) -> DownloadGrantOut:
    """A link that opens this one archive for a couple of minutes.

    Refused here, with a 404 the client can show, when the file is gone: a
    link that would only lead to an error page is not a link.
    """
    row = _archive(db, archive_id)
    if not ArchiveStore.from_settings().exists(row.name):
        raise HTTPException(status_code=404, detail="the archive file is missing from the store")
    token = archive_rules.make_download_token(row.id)
    return DownloadGrantOut(
        token=token,
        url=f"/invoices/archives/{row.id}/download?token={token}",
        expires_in=archive_rules.DOWNLOAD_TOKEN_SECONDS,
    )


@router.delete("/archives/{archive_id}", status_code=204)
def delete_archive(archive_id: int, db: DbSession = Depends(get_db)) -> None:
    """Delete one archive: the file, then the row (FR-039)."""
    row = _archive(db, archive_id)
    ArchiveStore.from_settings().delete(row.name)
    db.delete(row)
    db.commit()


@public_router.api_route("/archives/{archive_id}/download", methods=["GET", "HEAD"])
def download_archive(
    archive_id: int,
    token: str | None = Query(default=None),
    db: DbSession = Depends(get_db),
) -> FileResponse:
    """Stream the zip. Authorised by the short-lived token, not a bearer.

    HEAD is served too: the client probes with it before navigating, so a
    refused link becomes an error in the app and never a bare JSON page in
    the app's place.
    """
    if not archive_rules.check_download_token(archive_id, token):
        raise HTTPException(
            status_code=401,
            detail="This download link has expired — open Download compiled invoices and download again.",
        )
    row = _archive(db, archive_id)
    store = ArchiveStore.from_settings()
    try:
        path = store.path(row.name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="archive path is not in the store") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="the archive file is missing from the store")
    return FileResponse(path, media_type="application/zip", filename=row.name)
