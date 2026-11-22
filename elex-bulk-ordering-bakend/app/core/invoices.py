"""Invoices on file: fetched during a read-back, or uploaded.

The read-back is the natural moment to get an invoice — the browser is signed
in as the account, and the order's page just offered the link. Outside that
window the link is useless: it only answers inside that account's session. So
the fetch happens while the browser context is open, and the recording (the
file in the store, the row in ``invoices``) happens afterwards with the rest of
the account's results.

Every PDF is read as it arrives (app.io.invoice_pdf), and the order id it
prints is held against the order it is being put on: a file that names a
different order is refused rather than filed under the wrong one.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from array import array
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import exists, func, or_, select
from sqlalchemy.exc import DataError, DBAPIError, IntegrityError, SQLAlchemyError, StatementError
from sqlalchemy.orm import Session as DbSession

from app.core import order_filter
from app.core.order_filter import OrderFilter
from app.db.base import SessionLocal
from app.db.models import Account, Invoice, Order, OrderUnit, utcnow
from app.io.invoice_pdf import KIND_PRODUCT, PARSER_VERSION, TOOK_TOO_LONG, ParsedInvoice, parse_invoice
from app.io.invoice_store import InvoiceStore, sha256

log = logging.getLogger("ordering.invoices")

PLATFORM = "flipkart"
#: A PDF the platform serves is a few hundred KB; a minute is generous.
FETCH_TIMEOUT_MS = 60_000
#: Anything larger is not an invoice.
MAX_PDF_BYTES = 25 * 1024 * 1024
#: Flipkart answers an order's invoice link with one zip of all its invoices
#: (``OD….zip`` holding ``file_1.pdf``, ``file_2.pdf``…: the product's, the
#: fees', the transport's). A zip may be this large, hold this many PDFs, and
#: an order offer this many links (one per shipment).
MAX_ZIP_BYTES = 200 * 1024 * 1024
MAX_ZIP_PDFS = 100
MAX_LINKS_PER_ORDER = 6
_ZIP_SIGNATURE = b"PK\x03\x04"
#: How an order's invoices keep being asked for after the first were filed.
#: Flipkart issues them at different times — the transport invoice of the
#: 26 Oct samples is dated four days after the product's — so one download is
#: not all of them: the link is asked again once a day until a product invoice
#: is on file and the first download is ten days old, and never once an order
#: holds a dozen (a link that answers with something new each time).
REFETCH_AFTER_HOURS = 24
REFETCH_FOR_DAYS = 10
MAX_INVOICES_PER_ORDER = 12
#: When each order's link was last asked, in this process: a download that
#: brings nothing new leaves no row to tell by. Lost on a restart, which costs
#: one more download.
_last_asked: dict[tuple[int, str], datetime] = {}
#: SQLite binds at most 999 variables per statement.
_CHUNK = 500
#: What a row says while its PDF is not in the store. The parsed columns keep
#: what an earlier read found, and the row is looked at again at every start,
#: so the file is read the moment it is back.
MISSING_FILE_ERROR = "the PDF is missing from the invoice store"
#: What a row says when the database refused what the reader took off its PDF.
#: The error's type follows in brackets; the row counts as read by the parser
#: of the day, so only a newer parser reads it again (see :func:`_set_aside`).
UNSAVED_READING_ERROR = "what the reader took off the PDF could not be saved"
#: What follows the reader's reason on a row whose PDF an earlier read could
#: read and a re-read could not: the earlier reading stays (see :func:`apply_parse`).
EARLIER_READING_KEPT = "the earlier reading is kept"
#: The start-up backfill: rows per pass, and the breather between passes so
#: the app's own requests get the loop and the database in between.
BACKFILL_CHUNK = 500
BACKFILL_PAUSE_SECONDS = 1.0


def _key(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip().upper()
    return text or None


def orders_on_file(db: DbSession, order_pks: Iterable[int]) -> set[int]:
    """Which of these orders have at least one invoice on file — one query per chunk."""
    wanted = sorted({int(pk) for pk in order_pks})
    found: set[int] = set()
    for start in range(0, len(wanted), _CHUNK):
        chunk = wanted[start : start + _CHUNK]
        found.update(db.scalars(select(Invoice.order_pk).where(Invoice.order_pk.in_(chunk)).distinct()).all())
    return found


def counts_on_file(db: DbSession, order_pks: Iterable[int]) -> dict[int, int]:
    """How many invoices each of these orders has on file (an order has several:
    the product's, the fees', the transport's) — one query per chunk."""
    wanted = sorted({int(pk) for pk in order_pks})
    counts: dict[int, int] = {}
    for start in range(0, len(wanted), _CHUNK):
        chunk = wanted[start : start + _CHUNK]
        rows = db.execute(
            select(Invoice.order_pk, func.count(Invoice.id)).where(Invoice.order_pk.in_(chunk)).group_by(Invoice.order_pk)
        ).all()
        counts.update({int(pk): int(n) for pk, n in rows})
    return counts


def paths_on_file(db: DbSession, order_pks: Iterable[int]) -> dict[int, str]:
    """The product tax invoice's path per order when one is on file, else the
    newest invoice's — for the order-details endpoints. (Flipkart's zip also
    holds fee and transport invoices; audit F-38.)"""
    wanted = sorted({int(pk) for pk in order_pks})
    paths: dict[int, str] = {}
    for start in range(0, len(wanted), _CHUNK):
        chunk = wanted[start : start + _CHUNK]
        rows = db.execute(
            select(Invoice.order_pk, Invoice.path)
            .where(Invoice.order_pk.in_(chunk))
            # Non-product rows first, product rows last (newest last): the last
            # row per order wins below.
            .order_by(Invoice.order_pk, Invoice.kind == "product", Invoice.id)
        ).all()
        for pk, path in rows:
            paths[int(pk)] = path
    return paths


def _on_file_by_order_id(db: DbSession, account_id: int, order_ids: Iterable[str]) -> set[str]:
    wanted = sorted({key for key in (_key(v) for v in order_ids) if key})
    found: set[str] = set()
    for start in range(0, len(wanted), _CHUNK):
        chunk = wanted[start : start + _CHUNK]
        rows = db.execute(
            select(Order.order_id)
            .join(Invoice, Invoice.order_pk == Order.id)
            .where(Order.account_id == account_id, Order.order_id.in_(chunk))
            .distinct()
        ).all()
        found.update(key for (key,) in rows if key)
    return found


def _fetch_not_needed(db: DbSession, account_id: int, order_ids: Iterable[str], *, now: datetime) -> set[str]:
    """The orders whose invoice link need not be asked now (see
    :data:`REFETCH_AFTER_HOURS`): asked within the day; or a product invoice on
    file and the first download ten days old; or a dozen invoices held."""
    wanted = sorted({key for key in (_key(v) for v in order_ids) if key})
    held: dict[str, list[tuple[str, datetime | None]]] = {}
    for start in range(0, len(wanted), _CHUNK):
        chunk = wanted[start : start + _CHUNK]
        rows = db.execute(
            select(Order.order_id, Invoice.kind, Invoice.fetched_at)
            .join(Invoice, Invoice.order_pk == Order.id)
            .where(Order.account_id == account_id, Order.order_id.in_(chunk))
        ).all()
        for order_id, kind, fetched_at in rows:
            if order_id:
                held.setdefault(order_id, []).append((kind, fetched_at))
    naive_now = now.replace(tzinfo=None)
    skip: set[str] = set()
    for order_id in wanted:
        asked = _last_asked.get((account_id, order_id))
        if asked is not None and now - asked < timedelta(hours=REFETCH_AFTER_HOURS):
            skip.add(order_id)
            continue
        rows = held.get(order_id)
        if not rows:
            continue  # nothing on file: ask
        if len(rows) >= MAX_INVOICES_PER_ORDER:
            skip.add(order_id)
            continue
        stamps = [stamp.replace(tzinfo=None) for _kind, stamp in rows if stamp is not None]
        newest, oldest = (max(stamps), min(stamps)) if stamps else (None, None)
        if newest is not None and naive_now - newest < timedelta(hours=REFETCH_AFTER_HOURS):
            skip.add(order_id)
            continue
        has_product = any(kind == KIND_PRODUCT for kind, _stamp in rows)
        if has_product and (oldest is None or naive_now - oldest >= timedelta(days=REFETCH_FOR_DAYS)):
            skip.add(order_id)
    return skip


def pdfs_in_zip(data: bytes, *, label: str) -> list[bytes]:
    """The PDFs inside a zip Flipkart answered an invoice link with, in the
    zip's order, read in memory. Folders, files that are not PDFs and macOS
    resource forks are passed over; a member that cannot be read, or that is
    larger than an invoice, costs only itself; a zip past its bounds gives
    nothing. Never raises: the read-back must not fail over an invoice."""
    import io
    import zipfile

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        members = [
            info
            for info in archive.infolist()
            if not info.is_dir()
            and info.filename.lower().endswith(".pdf")
            and not info.filename.startswith("__MACOSX/")
            and "/._" not in f"/{info.filename}"
        ]
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        log.warning("invoices for %s: the zip could not be opened (%s)", label, type(exc).__name__)
        return []
    if len(members) > MAX_ZIP_PDFS or sum(info.file_size for info in members) > MAX_ZIP_BYTES:
        log.warning("invoices for %s: the zip holds too much to be an order's invoices", label)
        return []
    found: list[bytes] = []
    for info in members:
        if info.file_size > MAX_PDF_BYTES:
            log.warning("invoices for %s: %s in the zip is too large to be an invoice", label, info.filename)
            continue
        try:
            body = archive.read(info)
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, ValueError) as exc:
            log.warning("invoices for %s: %s could not be read from the zip (%s)", label, info.filename, type(exc).__name__)
            continue
        if body.startswith(b"%PDF-"):
            found.append(body)
    return found


async def fetch_missing(context, account_id: int, scraped: Sequence[object]) -> dict[str, list[bytes]]:
    """Download the invoices of every observed order that offers a link and
    still needs them (:func:`_fetch_not_needed`). ``context`` is the signed-in
    browser context; its request client carries the session's cookies. Returns
    ``{order_id: [pdf, …]}``.

    A link answers with one PDF or — Flipkart's own way — a zip of all the
    order's invoices, unpacked here in memory. Until 11 Nov 2026 a zip was
    refused as "not a PDF", so the automatic download saved nothing; and an
    order with any invoice on file was never asked again, so a fee invoice
    filed first kept the product's tax invoice out for good. An order's links
    are each asked (two shipments, two links).

    One order's failure is logged and costs only that order's invoices: the
    read-back itself must never fail over a PDF.
    """
    links: dict[str, list[str]] = {}
    for observation in scraped:
        order_id = _key(getattr(observation, "order_id", None))
        url = getattr(observation, "invoice_url", None)
        if not order_id or not url:
            continue
        mine = links.setdefault(order_id, [])
        if str(url) not in mine and len(mine) < MAX_LINKS_PER_ORDER:
            mine.append(str(url))
    if not links:
        return {}

    now = utcnow()
    with SessionLocal() as db:
        skip = _fetch_not_needed(db, account_id, links, now=now)

    fetched: dict[str, list[bytes]] = {}
    for order_id, urls in links.items():
        if order_id in skip:
            continue
        _remember_asked(account_id, order_id, now)
        pdfs: list[bytes] = []
        for url in urls:
            try:
                response = await context.request.get(url, timeout=FETCH_TIMEOUT_MS)
                body = await response.body()
            except Exception as exc:  # noqa: BLE001 — logged, the read-back carries on
                log.warning("invoice for %s could not be fetched: %s", order_id, type(exc).__name__)
                continue
            if not getattr(response, "ok", False):
                log.warning("invoice for %s: the link answered %s", order_id, getattr(response, "status", "?"))
                continue
            if body.startswith(_ZIP_SIGNATURE):
                if len(body) > MAX_ZIP_BYTES:
                    log.warning("invoices for %s: a zip of %d bytes is too large", order_id, len(body))
                    continue
                pdfs.extend(pdfs_in_zip(body, label=order_id))
                continue
            if not body.startswith(b"%PDF-"):
                # A web page here is nearly always Flipkart's sign-in page.
                what = "a web page (the account may be signed out)" if body.lstrip()[:1] == b"<" else "not a PDF"
                log.warning("invoice for %s: the link answered with %s (%d bytes)", order_id, what, len(body))
                continue
            if len(body) > MAX_PDF_BYTES:
                log.warning("invoice for %s: %d bytes is too large to be an invoice", order_id, len(body))
                continue
            pdfs.append(body)
        if pdfs:
            fetched[order_id] = pdfs
    return fetched


def _remember_asked(account_id: int, order_id: str, now: datetime) -> None:
    """Note that this order's link was asked now; the oldest notes go when
    there are too many to matter."""
    if len(_last_asked) > 20_000:
        for key in sorted(_last_asked, key=_last_asked.get)[:10_000]:
            _last_asked.pop(key, None)
    _last_asked[(account_id, order_id)] = now


class OrderMismatch(ValueError):
    """The PDF prints one order id and was offered for another order.

    Raised by :func:`attach` in place of filing the invoice under either: a
    wrongly named upload, or a link that answered with another order's
    invoice, has to be seen rather than mis-filed in silence.
    """

    def __init__(self, expected: str, found: str) -> None:
        self.expected = expected
        self.found = found
        super().__init__(f"the PDF is the invoice of order {found}, not of {expected}")


class NotSaved(RuntimeError):
    """The invoice's row could not be saved at all, and was rolled back alone.

    Raised by :func:`attach` (which logs it) when the database refused the
    row both with what the reader took off the PDF and without it, or once
    when the trouble was the database's own state — locked, full, the
    connection gone, the schema out of step. The PDF that call put in the
    store is taken out again, so no file is left there without its row
    (NFR-009), and every other row of the caller's stands: the caller says
    so and carries on (audit F-28).
    """

    def __init__(self, order_id: str, error: str) -> None:
        self.error = error
        super().__init__(f"the invoice of order {order_id} could not be saved ({error})")


def record(account_id: int, fetched: dict[str, bytes | Sequence[bytes]], *, source: str = "sync") -> int:
    """Put fetched PDFs in the store and on the ledger — one per order, or the
    several a zip held. Returns how many were new.

    A PDF that prints another order's id is not kept — the link answered with
    the wrong invoice — and the warning names both orders. One whose row the
    database will not take is skipped (``attach`` logged why): the read-back
    fetches it again next time, and the account's other invoices stand.
    """
    if not fetched:
        return 0
    store = InvoiceStore.from_settings()
    saved = 0
    with SessionLocal() as db:
        for order_id, data in fetched.items():
            order = db.scalar(
                select(Order)
                .where(Order.account_id == account_id, Order.order_id == order_id)
                .order_by(Order.id.desc())
                .limit(1)
            )
            if order is None:
                log.warning("invoice for %s fetched but the order is not in the ledger", order_id)
                continue
            for pdf in [data] if isinstance(data, (bytes, bytearray)) else list(data):
                try:
                    row = attach(db, store, order, bytes(pdf), source=source)
                except OrderMismatch as exc:
                    log.warning("invoice fetched for %s was not kept: %s", order_id, exc)
                    continue
                except NotSaved:
                    continue
                if row is not None:
                    saved += 1
        db.commit()
    return saved


def attach(
    db: DbSession,
    store: InvoiceStore,
    order: Order,
    data: bytes,
    *,
    source: str,
    parsed: ParsedInvoice | None = None,
    new_files: list[str] | None = None,
) -> Invoice | None:
    """Store one PDF for an order, read it, and add its row; None when it is
    already on file. A PDF the parser cannot read is kept all the same, with
    the reason on the row (spec FR-023 first, FR-028 second).

    ``parsed`` is the PDF's reading when the caller already has it (the upload
    reads a file first to learn which order it is for); otherwise it is read
    here. A PDF that prints a different order id than the order's is refused
    with :class:`OrderMismatch` before anything is written — never filed under
    either order.

    The row is saved in a savepoint of its own, so one the database refuses
    is rolled back alone and the caller's other rows stand (audit F-28). When
    it is what the reader took off the PDF that is refused, the PDF is kept
    without it, like an unreadable one, the reason on the row; when not even
    that row can be saved, :class:`NotSaved` says so, and the file this call
    put in the store goes too (NFR-009).

    ``new_files``, when given, collects the store path of every file this
    call wrote, as it is written: a caller whose own write then fails —
    its commit, or anything before it — hands them to
    :func:`remove_unsaved_files`, so the batch leaves no PDF in the store
    without its row.
    """
    digest = sha256(data)
    existing = db.scalar(
        select(Invoice.id).where(Invoice.order_pk == order.id, Invoice.sha256 == digest).limit(1)
    )
    if existing is not None:
        return None
    if parsed is None:
        parsed = parse_pdf(data)
    printed, expected = _key(parsed.order_id), _key(order.order_id)
    if printed and expected and printed != expected:
        raise OrderMismatch(expected, printed)
    # One invoice number is one invoice, whatever its bytes: Flipkart writes
    # the PDF afresh at each download, so the same invoice fetched and then
    # uploaded — or fetched twice — differed by a few bytes, was filed twice,
    # and doubled its lines in the invoice-data CSV (11 Nov 2026).
    number = (parsed.invoice_number or "").strip()[: Invoice.__table__.c.invoice_number.type.length]
    if number:
        twin = db.scalar(
            select(Invoice.id)
            .where(Invoice.order_pk == order.id, Invoice.invoice_number == number, Invoice.kind == parsed.kind)
            .limit(1)
        )
        if twin is not None:
            return None
    label = order.order_id or f"row {order.id}"
    # The file goes into the store first, so a row never points at a file
    # that is not there; the same bytes put again land on the same path. It
    # comes out again only if this call wrote it and its row is not saved:
    # a file that was there already may be another row's.
    relative, added = store.add(order.order_id or f"ORDER{order.id}", data, digest)
    if added and new_files is not None:
        new_files.append(relative)
    facts = {
        "order_pk": order.id,
        "platform": PLATFORM,
        "order_id": order.order_id,
        "path": relative,
        "size": len(data),
        "sha256": digest,
        "source": source,
    }
    row = Invoice(**facts)
    apply_parse(row, parsed)
    failure = _add_alone(db, row)
    if failure is None:
        return row
    if not _refused_for_its_values(failure):
        log.warning("invoice for order %s could not be saved (%s); nothing was recorded", label, type(failure).__name__)
        if added:
            remove_unsaved_files(store, [relative], check=None)
        raise NotSaved(label, type(failure).__name__)
    log.warning(
        "invoice for order %s: what the reader took off the PDF could not be saved (%s); the PDF is kept without it",
        label,
        type(failure).__name__,
    )
    bare = Invoice(**facts)
    _set_aside(bare, failure)
    again = _add_alone(db, bare)
    if again is not None:
        log.warning("invoice for order %s could not be saved without its reading either (%s)", label, type(again).__name__)
        if added:
            remove_unsaved_files(store, [relative], check=None)
        raise NotSaved(label, type(again).__name__)
    return bare


def remove_unsaved_files(
    store: InvoiceStore, paths: Iterable[str], *, check: Callable[[], DbSession] | None = SessionLocal
) -> list[str]:
    """Take out of the store the PDFs a failed write had put there, so none
    is left without its row (spec NFR-009: a failure leaves stored data
    unchanged). ``paths`` are files that write itself added (see
    :func:`attach`), never ones that were on file before it.

    With ``check`` — a fresh session, by default — a file that a saved row
    points at after all stays: a commit can fail on its way back from a
    database that did commit. If the database cannot be asked, every file
    stays, since a PDF left over is harmless and a row whose PDF is gone is
    not. ``check=None`` is for a row known to be rolled back (its own
    savepoint). A file that cannot be removed is logged and left: the
    write's own failure is what the caller reports. Returns the paths
    removed.
    """
    wanted = list(dict.fromkeys(paths))
    if not wanted:
        return []
    kept: set[str] = set()
    if check is not None:
        try:
            with check() as fresh:
                for start in range(0, len(wanted), _CHUNK):
                    chunk = wanted[start : start + _CHUNK]
                    kept.update(fresh.scalars(select(Invoice.path).where(Invoice.path.in_(chunk))).all())
        except SQLAlchemyError as exc:
            log.warning(
                "%d new invoice file(s) left in the store: whether a saved row points at them could not be checked (%s)",
                len(wanted),
                type(exc).__name__,
            )
            return []
    removed: list[str] = []
    for relative in wanted:
        if relative in kept:
            continue
        try:
            if store.remove(relative):
                removed.append(relative)
        except OSError as exc:
            log.warning("invoice file %s could not be taken back after a failed save (%s)", relative, type(exc).__name__)
    if removed:
        log.info("%d invoice file(s) taken back out of the store after a failed save", len(removed))
    return removed


#: What saving one row can raise: the database's own errors, wrapped by
#: SQLAlchemy, and those a driver raises bare for a value it cannot put in a
#: statement at all — text it cannot encode, a number too large for it, a
#: value its JSON adapter cannot serialise. PostgreSQL's driver serialises a
#: JSON column itself and raises the TypeError unwrapped, where SQLite's path
#: wraps it (seen on the PostgreSQL suite, 26 Oct 2026).
_SAVE_FAILURES = (SQLAlchemyError, UnicodeError, OverflowError, TypeError, ValueError)


def _add_alone(db: DbSession, row: Invoice) -> BaseException | None:
    """Add ``row`` in a savepoint of its own and flush it; the error, or None.

    On an error the savepoint is rolled back and the row leaves the session
    with it, so nothing of it is flushed later by anyone else; the rows
    before it are untouched (audit F-28). Only a failure to save is caught
    (``_SAVE_FAILURES``): anything else is a fault in this code and is raised
    as before.
    """
    try:
        with db.begin_nested():
            db.add(row)
    except _SAVE_FAILURES as exc:
        return exc
    return None


def _refused_for_its_values(exc: BaseException) -> bool:
    """True when the row was refused for what it holds — a value too long or
    not storable, a broken constraint, a value the driver could not even put
    in the statement — which would be refused the same way every time, so
    the row is set aside (:func:`_set_aside`). Anything else — locked, full,
    disconnected, the schema out of step — is the database's state, says
    nothing about the row, and is tried again later: setting every row aside
    over it would lose every reading until the next parser."""
    if isinstance(exc, (DataError, IntegrityError, UnicodeError, OverflowError, TypeError, ValueError)):
        return True
    return isinstance(exc, StatementError) and not isinstance(exc, DBAPIError)


def _set_aside(row: Invoice, failure: BaseException) -> None:
    """Mark a row whose reading the database refused: read by this parser,
    with the reason. The start-up backfill re-reads only rows an older parser
    read (or none did), so the row is not tried again at every start for
    ever; a newer parser reads it again. Nothing else on the row changes."""
    row.parse_error = f"{UNSAVED_READING_ERROR} ({type(failure).__name__})"
    row.parsed_at = utcnow().replace(tzinfo=None)
    row.parser_version = PARSER_VERSION


# --- what the PDF says ----------------------------------------------------------------
def parse_pdf(data: bytes) -> ParsedInvoice:
    """parse_invoice never raises by contract; this is the belt to its braces."""
    try:
        return parse_invoice(data)
    except Exception as exc:  # noqa: BLE001 — the file is kept whatever the parser did
        log.exception("invoice parser failed")
        return ParsedInvoice(error=f"parser failed: {exc}"[:300])


def _midnight(value: date | None) -> datetime | None:
    return None if value is None else datetime.combine(value, time.min)


def _clamp(value: str | None, width: int) -> str | None:
    """A parsed string cut to its column's width. PostgreSQL refuses a value
    longer than the column (SQLite stores it silently), and one over-long fee
    title would fail an upload or stall the start-up backfill at that row for
    ever (audit F-28, 25 Oct 2026); a cut label is read, a refused row is not."""
    if value is None:
        return None
    value = _storable(value)
    return value if len(value) <= width else value[: width - 1] + "…"


def _storable(value: str | None) -> str | None:
    """Text the database can hold: a PDF font mapped as Identity can hand the
    reader lone surrogate code points, which a UTF-8 driver refuses outright,
    and NUL characters, which no PostgreSQL text column holds. A surrogate
    becomes a '?'; a NUL is dropped."""
    if value is None:
        return None
    return value.encode("utf-8", "replace").decode("utf-8").replace("\x00", "")


def apply_parse(row: Invoice, parsed: ParsedInvoice) -> None:
    """Write what the parser read onto the row — every parsed column, so a
    re-read with a newer parser leaves nothing stale behind.

    A reading the time limit cut short the first time is left marked unread,
    so the start-up backfill reads it once more; a second time out is final.
    A busy machine, not the file, is the usual reason a PDF runs out of time.

    A re-read that finds no invoice in a PDF an earlier read found one in — a
    newer parser that fails on it, or runs out of time — writes nothing over
    that reading (spec NFR-009: a failure leaves stored data unchanged), so
    the invoice stays in Extract Invoices Data. As for a missing file, only
    ``parse_error`` says what happened, the reason followed by
    :data:`EARLIER_READING_KEPT`. The row then counts as read by this parser,
    so it is not read at every start and a newer parser tries again; a first
    time out is left to be read once more, as above.
    """
    timed_out = (parsed.error or "").startswith(TOOK_TOO_LONG)
    second_time_out = timed_out and (row.parse_error or "").startswith(TOOK_TOO_LONG)
    if parsed.invoice_type is None and row.invoice_type is not None:
        reason = parsed.error or "the reader found no invoice on it"
        log.warning("invoice %s: its PDF could not be read again (%s); %s", row.id, reason, EARLIER_READING_KEPT)
        row.parse_error = _storable(f"{reason}; {EARLIER_READING_KEPT}")
        if timed_out and not second_time_out:
            return  # stamped as the backfill found it, so the next start reads it again
        row.parsed_at = utcnow().replace(tzinfo=None)
        row.parser_version = PARSER_VERSION
        return
    row.kind = parsed.kind
    row.invoice_type = _clamp(parsed.invoice_type, 64)
    row.invoice_number = _clamp(parsed.invoice_number, 64)
    row.invoice_date = _midnight(parsed.invoice_date)
    row.order_date = _midnight(parsed.order_date)
    row.seller_name = _clamp(parsed.seller_name, 255)
    row.seller_gstin = _clamp(parsed.seller_gstin, 32)
    row.buyer_name = _clamp(parsed.buyer_name, 255)
    row.buyer_gstin = _clamp(parsed.buyer_gstin, 32)
    row.grand_total = parsed.grand_total
    row.original_invoice = _clamp(parsed.original_invoice, 64)
    row.reason = _storable(parsed.reason)
    row.lines = parsed.lines_as_dicts()
    row.parse_error = _storable(parsed.error)
    if timed_out and not second_time_out:
        row.parsed_at = None
        row.parser_version = None
        return
    row.parsed_at = utcnow().replace(tzinfo=None)
    row.parser_version = PARSER_VERSION


@dataclass(frozen=True)
class BackfillChunk:
    """What one pass of :func:`backfill_unparsed` did."""

    #: Rows whose PDF was read and written.
    parsed: int = 0
    #: Rows whose PDF is not in the store — left as they were, the error noted.
    missing: int = 0
    #: Rows the database would not save — each rolled back alone and logged by
    #: id, then set aside for this parser or left for the next start.
    unsaved: int = 0
    #: The highest row id looked at; None when no row was left to look at.
    last_id: int | None = None

    @property
    def looked_at(self) -> int:
        return self.parsed + self.missing + self.unsaved


def backfill_unparsed(
    limit: int = BACKFILL_CHUNK,
    *,
    store: InvoiceStore | None = None,
    after_id: int = 0,
    should_stop: Callable[[], bool] | None = None,
) -> BackfillChunk:
    """Read the PDFs on file that were never parsed, or were parsed by an
    older parser — up to ``limit`` rows with an id above ``after_id``, in
    their own session. :func:`backfill_all` walks the whole table this way.

    A row whose file is missing from the store keeps everything an earlier
    read found, and its parser version: only ``parse_error`` says the file is
    missing, so the row is read when the file comes back rather than written
    off. A PDF a newer parser cannot read, or runs out of time on, keeps the
    earlier reading too (:func:`apply_parse`). ``should_stop`` is asked
    between rows (the app shutting down); what was done so far is committed
    when it says so.

    Each row is written in a savepoint of its own (audit F-28): a row the
    database refuses is rolled back alone, logged by its id and the error's
    type, and the pass carries on. A refused reading is set aside — the row
    marked read by this parser, the reason in ``parse_error`` — so the same
    row does not stop or slow every start for ever; only a failure of the
    database's own state leaves the row to be tried at the next start.
    """
    store = store or InvoiceStore.from_settings()
    parsed = missing = unsaved = 0
    last_id: int | None = None
    with SessionLocal() as db:
        # Committed row by row (below), and the rows read here stay loaded
        # across those commits rather than being re-read one by one.
        db.expire_on_commit = False
        rows = db.scalars(
            select(Invoice)
            .where(
                Invoice.id > int(after_id),
                or_(
                    Invoice.parsed_at.is_(None),
                    Invoice.parser_version.is_(None),
                    Invoice.parser_version < PARSER_VERSION,
                ),
            )
            .order_by(Invoice.id)
            .limit(max(1, int(limit)))
        ).all()
        for row in rows:
            if should_stop is not None and should_stop():
                break
            # Taken before any write: a row rolled back below is expired, and
            # its id is what the log names.
            invoice_id = last_id = row.id
            try:
                try:
                    path = store.path(row.path)
                    data = path.read_bytes() if path.is_file() else None
                except (OSError, ValueError):
                    data = None
                reading = None if data is None else parse_pdf(data)
                try:
                    with db.begin_nested():
                        if reading is None:
                            row.parse_error = MISSING_FILE_ERROR
                        else:
                            apply_parse(row, reading)
                except _SAVE_FAILURES as exc:  # this row alone is rolled back; the pass carries on
                    unsaved += 1
                    if reading is None or not _refused_for_its_values(exc):
                        # Nothing about the row to give up on: the missing-file
                        # note, or the database's own trouble.
                        log.warning(
                            "invoice %s could not be saved (%s); tried again at the next start", invoice_id, type(exc).__name__
                        )
                        continue
                    log.warning(
                        "invoice %s: what the reader took off the PDF could not be saved (%s); set aside until a newer parser",
                        invoice_id,
                        type(exc).__name__,
                    )
                    try:
                        with db.begin_nested():
                            _set_aside(row, exc)
                    except _SAVE_FAILURES as again:  # logged; tried again at the next start
                        log.warning("invoice %s could not be set aside either (%s)", invoice_id, type(again).__name__)
                    continue
                if reading is None:
                    missing += 1
                else:
                    parsed += 1
            finally:
                # Every row ends its transaction before the next PDF is read.
                # On SQLite a savepoint rolled back leaves the transaction open,
                # and parsing on inside it would hold the database's write lock
                # for the rest of the pass — the order runs' records waiting
                # behind a PDF reader.
                db.commit()
        db.commit()
    if parsed or missing or unsaved:
        log.info(
            "invoice backfill: %d PDF(s) read, %d missing from the store, %d could not be saved",
            parsed,
            missing,
            unsaved,
        )
    return BackfillChunk(parsed=parsed, missing=missing, unsaved=unsaved, last_id=last_id)


async def backfill_all(
    *,
    chunk: int = BACKFILL_CHUNK,
    pause: float = BACKFILL_PAUSE_SECONDS,
    store: InvoiceStore | None = None,
) -> int:
    """Read every PDF on file the parser has not read, or read with an older
    version: ``chunk`` rows at a time in a worker thread, with a breather in
    between, so the app serves requests while a large store is parsed. Stops
    when a pass finds nothing more. Returns how many rows were read.

    One row the database will not save never ends the run: it is rolled back
    alone and logged, and the walk goes on past it (see
    :func:`backfill_unparsed`). Nothing here raises out of the task it runs
    in: any other failure is logged and ends the run (the next start resumes
    where it got to), and a cancel — the app shutting down — tells the pass in
    hand to stop after the PDF it is on.
    """
    stop = threading.Event()
    after = 0
    total = missing = unsaved = 0
    try:
        while True:
            done = await asyncio.to_thread(
                backfill_unparsed, chunk, store=store, after_id=after, should_stop=stop.is_set
            )
            if done.last_id is None:
                break
            total += done.parsed
            missing += done.missing
            unsaved += done.unsaved
            after = done.last_id
            await asyncio.sleep(pause)
    except asyncio.CancelledError:
        stop.set()
        log.info("invoice backfill stopped by shutdown after %d PDF(s)", total)
        raise
    except Exception:  # noqa: BLE001 — a start-up chore must never take the app down
        log.exception("invoice backfill failed after %d PDF(s); the next start carries on", total)
        return total
    if total or missing or unsaved:
        log.info(
            "invoice backfill done: %d PDF(s) read, %d missing from the store, %d could not be saved",
            total,
            missing,
            unsaved,
        )
    return total


#: The backfill tasks started and not yet finished. asyncio keeps only weak
#: references to tasks, so one nobody holds on to could be collected mid-run.
_BACKGROUND: set[asyncio.Task] = set()


def start_backfill_in_background(**options) -> asyncio.Task:
    """Start :func:`backfill_all` on the running loop and hand back its
    handle — for the app's lifespan, in place of waiting for the backfill
    before serving the first request. ``handle.cancel()`` at shutdown stops
    it after the PDF in hand; any other failure is logged inside the task."""
    task = asyncio.get_running_loop().create_task(backfill_all(**options), name="invoice-backfill")
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return task


# --- the invoice-data selection (spec FR-026, FR-027, FR-030) ----------------------------
def _matching_units(flt: OrderFilter, *columns):
    """``columns`` over the units matching the filter — from the unit and
    joined to its order and account like every ledger query (the same joins,
    so the same rows as the page)."""
    return order_filter.apply(
        select(*columns)
        .select_from(OrderUnit)
        .join(Order, Order.id == OrderUnit.order_pk)
        .join(Account, Account.id == Order.account_id),
        flt,
    )


def _has_readable_invoice(types: Sequence[str] | None = None, flt: OrderFilter | None = None):
    """EXISTS: the unit's order has an invoice the parser could read — of one
    of ``types`` when given, and meeting ``flt``'s invoice filters when it
    sets any. A PDF the parser could not read has no type, so it is in no
    type's count and in no CSV; it does not count here either."""
    clause = exists().where(Invoice.order_pk == Order.id, Invoice.invoice_type.is_not(None))
    if types is not None:
        clause = clause.where(Invoice.invoice_type.in_(tuple(dict.fromkeys(t for t in types if t))))
    conditions = order_filter.invoice_conditions(flt) if flt is not None else None
    if conditions:
        clause = clause.where(*conditions)
    return clause


def unit_split(db: DbSession, flt: OrderFilter, types: Sequence[str] | None = None) -> tuple[int, int]:
    """``(units whose order has a readable invoice, units whose order has none)``
    — an invoice of the ticked ``types`` when given, of any type otherwise.
    Computed in SQL.

    The second number is what "N units have no invoice on file and are left
    out" reports (spec FR-030), and it is defined by what the CSV writes: a
    unit whose only invoice could not be read, or whose invoices are all of
    other types, contributes no row, so it is counted here rather than dropped
    in silence. (A compilation zips the files themselves, readable or not, so
    it counts with app.core.order_filter.unit_invoice_split instead.)
    """
    base = _matching_units(flt, func.count())
    readable = _has_readable_invoice(types, flt)
    with_invoice = int(db.scalar(base.where(readable)) or 0)
    without = int(db.scalar(base.where(~readable)) or 0)
    return with_invoice, without


def type_counts(db: DbSession, flt: OrderFilter) -> list[dict]:
    """The invoice types found on the orders of the matching units, with how
    many invoices and how many of the matching units each covers (C-10: the
    list is built from what is on file, so a new fee type needs no code).
    "Product" first, then the rest by name. With invoice filters set, only
    the invoices meeting them are counted, as only those go in the CSV."""
    units = _matching_units(flt, OrderUnit.id.label("unit_id"), Order.id.label("order_pk")).subquery()
    # An invoice is per order and a unit may share its order with others, so
    # the join repeats both: each is counted once, DISTINCT on its own id.
    rows = db.execute(
        select(
            Invoice.invoice_type,
            func.count(func.distinct(Invoice.id)),
            func.count(func.distinct(units.c.unit_id)),
        )
        .join(units, units.c.order_pk == Invoice.order_pk)
        .where(Invoice.invoice_type.is_not(None), *(order_filter.invoice_conditions(flt) or ()))
        .group_by(Invoice.invoice_type)
    ).all()
    found = [{"type": kind, "invoices": int(n), "units": int(u)} for kind, n, u in rows]
    return sorted(found, key=lambda item: (item["type"] != "Product", item["type"].lower()))


#: Orders, or invoices, per batch of the invoice-data CSV (audit F-39, 26 Oct
#: 2026). Each batch is one short query read to its end before anything is
#: written: a read left open while a browser downloads would hold SQLite's
#: lock against every writer — the order runs' records — for as long as the
#: download takes. Read at call time, so a test can make batches small.
DATA_CHUNK = _CHUNK


def _data_order() -> tuple:
    """The invoice-data CSV's order: newest order first (an attempt that never
    became an order by when it was recorded), ties on the order's id, then
    each order's invoices by the date they print and their id."""
    return (
        func.coalesce(Order.placed_at, Order.created_at).desc().nulls_last(),
        Order.id.desc(),
        Invoice.invoice_date.asc().nulls_last(),
        Invoice.id.asc(),
    )


@dataclass(frozen=True)
class DataSelection:
    """What the invoice-data CSV holds, settled before its first byte goes
    out: the invoices, in the file's order, and how many rows they make (an
    invoice is one row per line it has, at least one)."""

    #: Ids only — eight bytes an invoice — never the rows themselves.
    invoice_ids: Sequence[int]
    rows: int


def data_selection(
    db: DbSession, flt: OrderFilter, types: Sequence[str], *, chunk: int | None = None
) -> DataSelection:
    """The invoices of a ticked type on the orders of the matching units, in
    the CSV's order (:func:`_data_order`), and the rows they make. An invoice
    is per order, so an order with three matching units contributes its
    invoices once.

    Read in batches of ``chunk`` orders, each batch's lines counted and let
    go before the next is read: memory holds the ids, not the invoices. The
    orders come first, newest first, as plain ids; each batch then reads its
    invoices in the same order, so the batches laid end to end are the order
    one query over everything would give.
    """
    wanted = tuple(dict.fromkeys(t for t in types if t))
    # array("q"): a machine integer per id rather than a Python object each.
    ids: array[int] = array("q")
    rows = 0
    if not wanted:
        return DataSelection(invoice_ids=ids, rows=rows)
    size = max(1, int(chunk or DATA_CHUNK))
    # With invoice filters set, only the invoices meeting them: an October
    # filter's CSV is October's invoices, not every invoice of the orders.
    narrow = tuple(order_filter.invoice_conditions(flt) or ())
    matching = order_filter.matching_orders(flt).subquery()
    order_pks = db.scalars(
        select(Order.id)
        .where(
            Order.id.in_(select(matching.c.id)),
            exists().where(Invoice.order_pk == Order.id, Invoice.invoice_type.in_(wanted), *narrow),
        )
        .order_by(*_data_order()[:2])
    ).all()
    for start in range(0, len(order_pks), size):
        batch = order_pks[start : start + size]
        found = db.execute(
            select(Invoice.id, Invoice.lines)
            .join(Order, Order.id == Invoice.order_pk)
            .where(Invoice.order_pk.in_(batch), Invoice.invoice_type.in_(wanted), *narrow)
            .order_by(*_data_order())
        ).all()
        for invoice_id, lines in found:
            ids.append(int(invoice_id))
            rows += max(1, len(lines or []))
    return DataSelection(invoice_ids=ids, rows=rows)


def data_pairs(db: DbSession, selection: DataSelection, *, chunk: int | None = None) -> Iterator[tuple[Invoice, Order]]:
    """``(invoice, order)`` for every invoice of the selection, in its order,
    read ``chunk`` invoices at a time as the CSV is written — so the file's
    size never sits in memory, and no query is left open between batches.

    An invoice gone since it was selected is left out; one changed since is
    written as it now is.
    """
    size = max(1, int(chunk or DATA_CHUNK))
    ids = selection.invoice_ids
    for start in range(0, len(ids), size):
        batch = [int(invoice_id) for invoice_id in ids[start : start + size]]
        found = {
            invoice.id: (invoice, order)
            for invoice, order in db.execute(
                select(Invoice, Order).join(Order, Order.id == Invoice.order_pk).where(Invoice.id.in_(batch))
            ).all()
        }
        for invoice_id in batch:
            pair = found.get(invoice_id)
            if pair is not None:
                yield pair
