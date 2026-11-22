"""Compile Invoices: one zip of the invoice PDFs of the order units matching a
ledger filter (spec FR-031–FR-037, NFR-005).

Runs in-process like a sync (``api.jobs._spawn``), as ``JobType.compile``. The
zip is written off the event loop (``asyncio.to_thread``) so the ledger stays
responsive, to a temporary name of its own (``ArchiveStore.temp_path``) that is
renamed into the archive store only on success — a failure or a cancel leaves
no half archive and touches no earlier one (FR-035/036). PDFs are already
compressed, so entries are STORED rather than deflated: two hundred units take
seconds, not minutes (C-8).

The ledger filter reaches the run in memory (``filter_params``), whole. What is
written down — the job row's ``filters``, the archive row's — keeps the card
filter to its last four digits (``archives.recorded_filters``, audit F-20).

Progress is by orders rather than accounts, so the account-centric
``JobTracker`` is not used: the job row carries total/done/progress and a short
log, and ``Job.payload`` records the outcome the status endpoint reads —
``archive_name``, ``orders``, ``units``, ``units_without_invoice`` from the
start, then ``outcome`` (completed / cancelled / failed), ``error``,
``archive_id``, ``invoices``, ``missing_files``. Every way the job can end
inside this module records an outcome; the two ways it can end outside it (a
crash in the task's done-callback, a restart) are read as a failure by the
status endpoint, so Compilation Status never shows a failed run as merely
"ended".

The cancel flag is read before the first order, after every order, and once
more before the finished zip is renamed into the store: a Cancel the API
accepted is honoured within one PDF, whatever the size of the run.
"""

from __future__ import annotations

import asyncio
import logging
import os
import zipfile
from dataclasses import dataclass

from sqlalchemy import select

from app.core import order_filter
from app.core.archives import ArchiveStore, recorded_filters
from app.db.base import SessionLocal
from app.db.models import CompiledArchive, Invoice, Job, JobStatus, Order, utcnow
from app.io.invoice_store import InvoiceStore

log = logging.getLogger("ordering.jobs.compile")

#: How often, in orders, progress is written to the job row.
PROGRESS_EVERY = 10
#: Orders per invoice query — well under SQLite's bind-parameter ceiling.
_CHUNK = 400

OUTCOME_COMPLETED = "completed"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_FAILED = "failed"

#: What a restart or a crash outside this module leaves the operator to read.
STOPPED_EARLY = "The server stopped before the compilation finished — no archive was added."


class CompileCancelled(Exception):
    """The operator asked for the compilation to stop."""


@dataclass(frozen=True)
class BuildResult:
    files: int
    missing: int
    size: int


def _stamp(line: str) -> str:
    return f"[{utcnow().isoformat(timespec='seconds')}] {line}\n"


def _note(job: Job, line: str) -> None:
    job.log = f"{job.log or ''}{_stamp(line)}"


def _cancel_requested(job_id: int) -> bool:
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        return bool(job is not None and job.cancel_requested)


def _progress(job_id: int, done: int, total: int) -> None:
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job is None:
            return
        job.done_items = done
        job.progress = round(100 * done / total) if total else 0
        db.commit()


def _finish(job_id: int, status: JobStatus, *, outcome: str, error: str | None = None, **extra) -> None:
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job is None:
            log.error("compile job %s vanished before it could finish", job_id)
            return
        job.status = status
        job.finished_at = utcnow()
        if outcome == OUTCOME_COMPLETED:
            job.progress = 100
            job.done_items = job.total_items
        # Reassign rather than mutate: a plain JSON column is not change-tracked.
        job.payload = {**(job.payload or {}), "outcome": outcome, "error": error, **extra}
        _note(job, error if error else f"compilation {outcome}")
        db.commit()


def _unique(name: str, used: set[str]) -> str:
    """``name``, or a numbered variant when an entry already has it — two order
    rows can share an order id, and a zip with two entries of one name loses
    one of them on extraction."""
    if name not in used:
        used.add(name)
        return name
    stem, dot, ext = name.rpartition(".")
    n = 2
    while True:
        candidate = f"{stem}-dup{n}{dot}{ext}" if dot else f"{name}-dup{n}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        n += 1


def _build(
    job_id: int,
    pks: list[int],
    name: str,
    store: ArchiveStore,
    invoices: InvoiceStore,
    narrow: tuple = (),
) -> BuildResult:
    """Write the zip — in a worker thread, with its own short DB sessions.
    ``narrow`` holds the filter's invoice conditions, when it set any: each
    order then gives only the PDFs that meet them."""
    temp = store.temp_path(name)
    files = missing = done = 0
    total = len(pks)
    used: set[str] = set()
    try:
        if _cancel_requested(job_id):
            raise CompileCancelled()
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for start in range(0, total, _CHUNK):
                chunk = pks[start : start + _CHUNK]
                with SessionLocal() as db:
                    rows = db.execute(
                        select(Invoice.order_pk, Invoice.path, Order.order_id)
                        .join(Order, Order.id == Invoice.order_pk)
                        .where(Invoice.order_pk.in_(chunk), *narrow)
                        .order_by(Invoice.order_pk, Invoice.id)
                    ).all()
                by_order: dict[int, list[tuple[str, str | None]]] = {}
                for pk, path, order_id in rows:
                    by_order.setdefault(int(pk), []).append((path, order_id))
                for pk in chunk:
                    for index, (relative, order_id) in enumerate(by_order.get(pk, [])):
                        try:
                            source = invoices.path(relative)
                        except ValueError:
                            source = None
                        if source is None or not source.is_file():
                            missing += 1
                            continue
                        base = order_id or f"order-{pk}"
                        arcname = _unique(f"{base}.pdf" if index == 0 else f"{base}-{index + 1}.pdf", used)
                        archive.write(source, arcname)
                        files += 1
                    done += 1
                    # One indexed row read per order is nothing next to a copy.
                    if _cancel_requested(job_id):
                        raise CompileCancelled()
                    if done % PROGRESS_EVERY == 0 or done == total:
                        _progress(job_id, done, total)
        if _cancel_requested(job_id):
            raise CompileCancelled()
        size = temp.stat().st_size
        os.replace(temp, store.root / name)
        return BuildResult(files=files, missing=missing, size=size)
    except BaseException:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        raise


def _discard(store: ArchiveStore, name: str) -> None:
    """A finished zip nothing will ever list: remove it rather than strand it."""
    try:
        (store.root / name).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:  # noqa: BLE001 — logged; the start-up reclaim gets it
        log.warning("could not remove the unrecorded archive %s: %s", name, exc)


async def run_compile_job(job_id: int, *, filter_params: dict, prefix: str) -> None:
    """Build the archive for the job's filter; see the module docstring."""
    # The one reading of the ledger filter lives with the orders API.
    from app.api.orders import build_filter

    try:
        flt = build_filter(**filter_params)
    except Exception as exc:  # noqa: BLE001 — an HTTPException for a bad value
        detail = getattr(exc, "detail", None) or str(exc)
        _finish(job_id, JobStatus.failed, outcome=OUTCOME_FAILED, error=f"the filter could not be applied: {detail}")
        return

    store = ArchiveStore.from_settings()
    name: str | None = None
    try:
        with SessionLocal() as db:
            pks = order_filter.order_pks(db, flt)
            with_invoice, without_invoice = order_filter.unit_invoice_split(db, flt)
            name = store.free_name(db, prefix)
            job = db.get(Job, job_id)
            if job is None:
                log.error("compile job %s does not exist", job_id)
                return
            job.status = JobStatus.running
            job.total_items = len(pks)
            job.done_items = 0
            job.progress = 0
            job.finished_at = None
            job.payload = {
                **(job.payload or {}),
                "archive_name": name,
                "orders": len(pks),
                "units": with_invoice + without_invoice,
                "units_without_invoice": without_invoice,
            }
            _note(job, f"compiling the invoices of {len(pks)} order(s) into {name}")
            if without_invoice:
                _note(job, f"{without_invoice} unit(s) have no invoice on file and are left out")
            db.commit()

        if not pks:
            _finish(job_id, JobStatus.failed, outcome=OUTCOME_FAILED, error="no order unit matches the filter")
            return
        if _cancel_requested(job_id):
            raise CompileCancelled()

        narrow = tuple(order_filter.invoice_conditions(flt) or ())
        result = await asyncio.to_thread(_build, job_id, pks, name, store, InvoiceStore.from_settings(), narrow)

        if result.files == 0:
            # Every PDF the ledger lists is gone from the store: an empty zip
            # would be a false "compiled". Say so instead.
            _discard(store, name)
            _finish(
                job_id,
                JobStatus.failed,
                outcome=OUTCOME_FAILED,
                error=(
                    f"none of the {result.missing} invoice file(s) on the ledger is in the store — "
                    "read the orders back or upload the PDFs again"
                ),
                missing_files=result.missing,
            )
            return

        archive_id = _record(job_id, name, prefix, result, len(pks), with_invoice + without_invoice, without_invoice, filter_params)
        if archive_id is None:
            _discard(store, name)
            _finish(
                job_id,
                JobStatus.failed,
                outcome=OUTCOME_FAILED,
                error="the archive was built but could not be recorded — see the server log",
            )
            return
        if result.missing:
            log.warning("compile job %s: %d invoice file(s) on the ledger were missing from the store", job_id, result.missing)
        _finish(
            job_id,
            JobStatus.completed,
            outcome=OUTCOME_COMPLETED,
            error=None,
            archive_id=archive_id,
            invoices=result.files,
            missing_files=result.missing,
            size=result.size,
        )
    except CompileCancelled:
        log.info("compile job %s cancelled by the operator", job_id)
        # Like a cancelled sync or order run: the job finished, by request.
        _finish(
            job_id,
            JobStatus.completed,
            outcome=OUTCOME_CANCELLED,
            error="Cancelled by the operator — no archive was added.",
        )
    except asyncio.CancelledError:
        # The server is shutting down while the thread may still be writing:
        # record the failure now; the start-up reclaim removes what it leaves.
        _finish(job_id, JobStatus.failed, outcome=OUTCOME_FAILED, error=STOPPED_EARLY)
        raise
    except Exception as exc:  # noqa: BLE001 — recorded on the job, never raised out
        log.exception("compile job %s failed", job_id)
        _finish(job_id, JobStatus.failed, outcome=OUTCOME_FAILED, error=f"{type(exc).__name__}: {exc}")


def _record(
    job_id: int,
    name: str,
    prefix: str,
    result: BuildResult,
    orders: int,
    units: int,
    without_invoice: int,
    filter_params: dict,
) -> int | None:
    """The archive's row; None (logged) when it could not be written.

    The row says which filter the archive covers, and it is kept as long as
    the archive — no place for a card number — so the card filter goes on it
    by its last four digits only (audit F-20), whoever started the run.
    """
    try:
        with SessionLocal() as db:
            row = CompiledArchive(
                name=name,
                prefix=prefix,
                path=name,
                size=result.size,
                orders=orders,
                units=units,
                invoices=result.files,
                left_out=without_invoice + result.missing,
                missing_files=result.missing,
                filters=recorded_filters(filter_params),
                job_id=job_id,
            )
            db.add(row)
            if result.missing:
                job = db.get(Job, job_id)
                if job is not None:
                    _note(job, f"{result.missing} invoice file(s) on the ledger are missing from the store and were left out")
            db.commit()
            return int(row.id)
    except Exception:  # noqa: BLE001 — logged; the caller discards the file
        log.exception("compile job %s: the archive row could not be written", job_id)
        return None
