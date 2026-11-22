"""Compile Invoices: one zip of the invoice PDFs of the order units matching a
ledger filter (spec FR-031–FR-039) — its start, status, cancel, the archives,
their download links and deletion, and the retention sweep."""

from __future__ import annotations

import asyncio
import io
import time
import zipfile
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import invoices as invoices_api
from app.api import jobs as jobs_api
from app.api.orders import build_filter
from app.core import archives, invoices
from app.core.security import create_access_token
from app.db.base import SessionLocal
from app.db.models import (
    Account,
    AccountStatus,
    CompiledArchive,
    Invoice,
    Job,
    JobStatus,
    JobType,
    Order,
    OrderStatus,
    utcnow,
)
from app.io import invoice_store
from app.io.invoice_store import InvoiceStore
from app.jobs import compile_tasks, recovery
from app.jobs.compile_tasks import run_compile_job


@pytest.fixture(autouse=True)
def stores(tmp_path, monkeypatch):
    """Every test writes into its own directories, never the configured ones."""
    monkeypatch.setattr(invoice_store, "store_root", lambda: tmp_path / "invoices")
    monkeypatch.setattr(archives, "archive_root", lambda: tmp_path / "archives")
    return tmp_path


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(invoices_api.router)
    application.include_router(invoices_api.public_router)
    application.include_router(jobs_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def spawned(monkeypatch) -> list[int]:
    """Record the job ids handed to the engine without running them: the tests
    run the job body themselves, so nothing races the assertions."""
    ids: list[int] = []
    monkeypatch.setattr(invoices_api, "_spawn", lambda factory, job_id: ids.append(job_id))
    return ids


def _pdf(tag: str) -> bytes:
    return b"%PDF-1.4\n%" + tag.encode("utf-8") + b"\n"


def _naive(moment: datetime) -> datetime:
    return moment.replace(tzinfo=None) if moment.tzinfo is not None else moment


def _account(db, email: str = "a@test.com") -> Account:
    account = Account(email=email, status=AccountStatus.new)
    db.add(account)
    db.commit()
    return account


def _order(db, account: Account, order_id: str, *, placed_days_ago: int = 3,
           status: OrderStatus = OrderStatus.placed, pdfs: tuple[str, ...] = ()) -> Order:
    order = Order(account_id=account.id, status=status, model="T5x", qty=1, order_id=order_id,
                  placed_at=_naive(utcnow()) - timedelta(days=placed_days_ago))
    db.add(order)
    db.commit()
    store = InvoiceStore.from_settings()
    for tag in pdfs:
        invoices.attach(db, store, order, _pdf(f"{order_id}-{tag}"), source="upload")
    db.commit()
    return order


def _job(db, **fields) -> Job:
    job = Job(type=JobType.compile, status=JobStatus.queued, **fields)
    db.add(job)
    db.commit()
    return job


def _run(job_id: int, filter_params: dict | None = None, prefix: str = "invoices") -> None:
    asyncio.run(run_compile_job(job_id, filter_params=filter_params or {}, prefix=prefix))


# --- the whole thing ---------------------------------------------------------------
def test_a_compilation_zips_the_matching_orders_invoices(api, db, spawned, stores):
    account = _account(db)
    _order(db, account, "OD1", pdfs=("product", "fee"))
    _order(db, account, "OD2", pdfs=("product",))
    _order(db, account, "OD3")  # placed, but no invoice on file: left out
    _order(db, account, "OD4", status=OrderStatus.failed, pdfs=("product",))  # outside the filter

    response = api.post("/invoices/compile?status=placed", json={"prefix": " Oct / invoices!! "})

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["archive_name"] == "Oct_invoices.zip"
    assert (body["orders"], body["units"], body["units_without_invoice"]) == (3, 3, 1)
    assert body["job"]["type"] == "compile"
    assert body["job"]["payload"]["filters"] == {"status": ["placed"]}
    assert spawned == [body["job"]["id"]]

    _run(body["job"]["id"], {"status": ["placed"]}, "Oct_invoices")

    archive = db.query(CompiledArchive).one()
    assert (archive.name, archive.orders, archive.units, archive.invoices, archive.left_out) == (
        "Oct_invoices.zip", 3, 3, 3, 1,
    )
    path = stores / "archives" / "Oct_invoices.zip"
    with zipfile.ZipFile(path) as zf:
        assert sorted(zf.namelist()) == ["OD1-2.pdf", "OD1.pdf", "OD2.pdf"]
        assert zf.read("OD2.pdf") == _pdf("OD2-product")
    assert archive.size == path.stat().st_size
    assert not list((stores / "archives" / ".tmp").glob("*"))  # no half-written file left behind

    job = db.get(Job, body["job"]["id"])
    db.refresh(job)
    assert (job.status, job.progress, job.done_items, job.total_items) == (JobStatus.completed, 100, 3, 3)
    assert job.payload["outcome"] == "completed"
    assert job.payload["archive_id"] == archive.id
    assert "left out" in (job.log or "")

    listed = api.get("/invoices/archives").json()["items"]
    assert [(a["name"], a["invoices"], a["left_out"]) for a in listed] == [("Oct_invoices.zip", 3, 1)]
    status = api.get("/invoices/compile/status").json()
    assert status["active"] is None
    assert status["last"]["payload"]["outcome"] == "completed"


def test_the_job_rebuilds_the_filter_the_request_was_given(api, db, spawned, stores):
    account = _account(db)
    _order(db, account, "OD1", pdfs=("p",), placed_days_ago=2)
    _order(db, account, "OD2", pdfs=("p",), placed_days_ago=40)
    date_from = (_naive(utcnow()) - timedelta(days=10)).date().isoformat()

    response = api.post(
        f"/invoices/compile?status=placed&date_from={date_from}&email=a@test.com&email=B@test.com&page=3",
        json={"prefix": "recent"},
    )

    assert response.status_code == 201, response.text
    raw = response.json()["job"]["payload"]["filters"]
    assert raw == {"status": ["placed"], "date_from": date_from, "email": ["a@test.com", "B@test.com"]}
    flt = build_filter(**raw)
    assert flt.emails == ("a@test.com", "b@test.com")
    assert flt.statuses == (OrderStatus.placed,)
    assert flt.date_from is not None
    assert response.json()["orders"] == 1

    _run(response.json()["job"]["id"], raw, "recent")

    with zipfile.ZipFile(stores / "archives" / "recent.zip") as zf:
        assert zf.namelist() == ["OD1.pdf"]


# --- naming -----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Oct invoices", "Oct_invoices"),
        ("  ..weird//name--  ", "weird_name"),
        ("///", ""),
        ("a" * 80, "a" * 64),
        ("x.y-z_1", "x.y-z_1"),
    ],
)
def test_the_prefix_is_made_file_safe(raw, clean):
    assert archives.clean_prefix(raw) == clean


def test_a_taken_name_is_numbered(db, stores):
    store = archives.ArchiveStore.from_settings()
    assert store.free_name(db, "oct") == "oct.zip"
    db.add(CompiledArchive(name="oct.zip", prefix="oct", path="oct.zip", size=1))
    db.commit()
    assert store.free_name(db, "oct") == "oct-2.zip"
    (store.root / "oct-2.zip").write_bytes(b"x")  # on disk but unrecorded still counts
    assert store.free_name(db, "oct") == "oct-3.zip"


def test_the_store_refuses_a_name_outside_itself(stores):
    store = archives.ArchiveStore(stores / "archives")
    with pytest.raises(ValueError):
        store.path("../secret.zip")
    assert store.delete("../secret.zip") is False


# --- refusals ---------------------------------------------------------------------
def test_a_start_is_refused_while_one_runs_or_with_nothing_to_compile(api, db, spawned):
    account = _account(db)
    response = api.post("/invoices/compile", json={"prefix": "x"})
    assert response.status_code == 422 and "No order unit" in response.json()["detail"]

    _order(db, account, "OD1")  # a unit, but no invoice on file
    response = api.post("/invoices/compile", json={"prefix": "x"})
    assert response.status_code == 422 and "invoice on file" in response.json()["detail"]

    _order(db, account, "OD2", pdfs=("p",))
    assert api.post("/invoices/compile", json={"prefix": "///"}).status_code == 422
    assert api.post("/invoices/compile", json={"prefix": "x", "extra": 1}).status_code == 422

    first = api.post("/invoices/compile", json={"prefix": "x"})
    assert first.status_code == 201, first.text
    second = api.post("/invoices/compile", json={"prefix": "y"})
    assert second.status_code == 409
    assert "Compilation Status" in second.json()["detail"]
    assert db.query(Job).count() == 1


def test_status_before_any_compilation(api):
    assert api.get("/invoices/compile/status").json() == {
        "active": None,
        "last": None,
        "archive_retention_days": 90,
    }


def test_a_run_failed_outside_the_job_body_still_reads_as_a_failure(api, db):
    """A restart's sweep and a crash in the task's done-callback write the
    reason to the log only; Compilation Status must still say it failed."""
    job = Job(type=JobType.compile, status=JobStatus.running, payload={"archive_name": "x.zip"})
    db.add(job)
    db.commit()
    assert recovery.fail_orphaned_jobs(db, redis_url="") == [job.id]

    last = api.get("/invoices/compile/status").json()["last"]
    assert last["status"] == "failed"
    assert last["payload"]["outcome"] == "failed"
    assert last["payload"]["error"] == recovery.REASON

    bare = Job(type=JobType.compile, status=JobStatus.failed)
    db.add(bare)
    db.commit()
    last = api.get("/invoices/compile/status").json()["last"]
    assert last["payload"]["error"] == compile_tasks.STOPPED_EARLY


def test_a_second_start_is_refused_while_a_run_is_running(api, db, spawned):
    account = _account(db)
    _order(db, account, "OD1", pdfs=("p",))
    db.add(Job(type=JobType.compile, status=JobStatus.running))
    db.commit()

    response = api.post("/invoices/compile", json={"prefix": "x"})

    assert response.status_code == 409
    assert spawned == []


# --- cancel and failure -------------------------------------------------------------
def test_a_cancel_leaves_no_archive_and_no_file(db, stores):
    """Three orders, the flag already up: nothing is written. Like a cancelled
    sync, the job ends completed-by-request, with the outcome saying cancelled."""
    account = _account(db)
    for n in range(3):
        _order(db, account, f"OD{n}", pdfs=("p",))
    job = _job(db, cancel_requested=True)

    _run(job.id, {}, "cancelled")

    db.refresh(job)
    assert job.status == JobStatus.completed
    assert job.payload["outcome"] == "cancelled"
    assert "no archive was added" in job.payload["error"].lower()
    assert db.query(CompiledArchive).count() == 0
    assert not (stores / "archives" / "cancelled.zip").exists()
    assert not list((stores / "archives" / ".tmp").glob("*"))


def test_a_cancel_during_the_last_orders_is_still_honoured(db, stores, monkeypatch):
    """The flag is read after every order and once more before the rename, so
    a Cancel that lands at order 10 of 12 stops the run at order 11."""
    account = _account(db)
    for n in range(12):
        _order(db, account, f"OD{n:02d}", pdfs=("p",))
    job = _job(db)
    seen: list[int] = []
    real_progress = compile_tasks._progress

    def cancel_at_ten(job_id: int, done: int, total: int) -> None:
        seen.append(done)
        real_progress(job_id, done, total)
        if done == 10:
            with SessionLocal() as other:
                other.get(Job, job_id).cancel_requested = True
                other.commit()

    monkeypatch.setattr(compile_tasks, "_progress", cancel_at_ten)

    _run(job.id, {}, "late-cancel")

    db.refresh(job)
    assert job.payload["outcome"] == "cancelled"
    assert seen == [10]  # progress was written at 10; the run stopped before 12
    assert db.query(CompiledArchive).count() == 0
    assert not (stores / "archives" / "late-cancel.zip").exists()
    assert not list((stores / "archives" / ".tmp").glob("*"))


def test_a_missing_pdf_file_is_reported_and_an_all_missing_run_fails(db, stores):
    account = _account(db)
    kept = _order(db, account, "OD1", pdfs=("p",))
    gone = _order(db, account, "OD2", pdfs=("p",))
    store = InvoiceStore.from_settings()
    for invoice in db.query(Invoice).filter(Invoice.order_pk == gone.id).all():
        store.path(invoice.path).unlink()
    job = _job(db)

    _run(job.id, {}, "partial")

    db.refresh(job)
    assert job.payload["outcome"] == "completed"
    assert job.payload["missing_files"] == 1
    archive = db.query(CompiledArchive).one()
    assert (archive.invoices, archive.missing_files, archive.left_out) == (1, 1, 1)
    assert "missing from the store" in (job.log or "")
    with zipfile.ZipFile(stores / "archives" / "partial.zip") as zf:
        assert zf.namelist() == ["OD1.pdf"]

    # Every file gone: no empty zip pretending to be an archive.
    for invoice in db.query(Invoice).filter(Invoice.order_pk == kept.id).all():
        store.path(invoice.path).unlink()
    second = _job(db)
    _run(second.id, {}, "empty")
    db.refresh(second)
    assert second.status == JobStatus.failed
    assert second.payload["outcome"] == "failed"
    assert "none of the 2 invoice file(s)" in second.payload["error"]
    assert not (stores / "archives" / "empty.zip").exists()
    assert db.query(CompiledArchive).count() == 1


def test_two_order_rows_sharing_an_id_get_distinct_entry_names(db, stores):
    account = _account(db)
    _order(db, account, "OD1", pdfs=("first",))
    _order(db, account, "OD1", pdfs=("second",))  # the same id on a second row
    job = _job(db)

    _run(job.id, {}, "dup")

    with zipfile.ZipFile(stores / "archives" / "dup.zip") as zf:
        assert sorted(zf.namelist()) == ["OD1-dup2.pdf", "OD1.pdf"]
    assert db.query(CompiledArchive).one().invoices == 2


def test_a_row_that_cannot_be_written_discards_the_zip_and_records_the_failure(db, stores, monkeypatch):
    account = _account(db)
    _order(db, account, "OD1", pdfs=("p",))
    job = _job(db)

    class Broken(CompiledArchive):
        def __init__(self, *args, **kwargs):
            raise RuntimeError("integer out of range")

    monkeypatch.setattr(compile_tasks, "CompiledArchive", Broken)

    _run(job.id, {}, "unrecorded")

    db.refresh(job)
    assert job.status == JobStatus.failed
    assert job.payload["outcome"] == "failed"
    assert "could not be recorded" in job.payload["error"]
    assert not (stores / "archives" / "unrecorded.zip").exists()  # not stranded
    assert archives.ArchiveStore.from_settings().free_name(db, "unrecorded") == "unrecorded.zip"


def test_progress_is_written_every_ten_orders_and_at_the_end(db, stores, monkeypatch):
    account = _account(db)
    for n in range(25):
        _order(db, account, f"OD{n:02d}", pdfs=("p",))
    job = _job(db)
    calls: list[tuple[int, int]] = []
    real_progress = compile_tasks._progress

    def record(job_id: int, done: int, total: int) -> None:
        calls.append((done, total))
        real_progress(job_id, done, total)

    monkeypatch.setattr(compile_tasks, "_progress", record)

    _run(job.id, {}, "progress")

    assert calls == [(10, 25), (20, 25), (25, 25)]
    with zipfile.ZipFile(stores / "archives" / "progress.zip") as zf:
        assert len(zf.namelist()) == 25
        assert {info.compress_type for info in zf.infolist()} == {zipfile.ZIP_STORED}


def test_the_zip_is_opened_with_zip64_allowed(db, stores, monkeypatch):
    account = _account(db)
    _order(db, account, "OD1", pdfs=("p",))
    job = _job(db)
    opened: list[dict] = []
    real_zipfile = compile_tasks.zipfile.ZipFile

    class Recording(real_zipfile):
        def __init__(self, *args, **kwargs):
            opened.append(dict(kwargs))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(compile_tasks.zipfile, "ZipFile", Recording)

    _run(job.id, {}, "zip64")

    assert opened and opened[0]["allowZip64"] is True
    assert opened[0]["compression"] == zipfile.ZIP_STORED


def test_a_failure_records_why_and_leaves_earlier_archives_alone(db, stores, monkeypatch):
    account = _account(db)
    _order(db, account, "OD1", pdfs=("p",))
    root = stores / "archives"
    root.mkdir(parents=True, exist_ok=True)
    (root / "earlier.zip").write_bytes(b"old")
    db.add(CompiledArchive(name="earlier.zip", prefix="earlier", path="earlier.zip", size=3))
    db.commit()
    job = _job(db)

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(compile_tasks.zipfile, "ZipFile", boom)

    _run(job.id, {}, "later")

    db.refresh(job)
    assert job.status == JobStatus.failed
    assert job.payload["outcome"] == "failed"
    assert "disk full" in job.payload["error"]
    assert "disk full" in (job.log or "")
    assert [a.name for a in db.query(CompiledArchive).all()] == ["earlier.zip"]
    assert (root / "earlier.zip").read_bytes() == b"old"
    assert not list((root / ".tmp").glob("*"))


def test_a_filter_that_matches_nothing_fails_the_job_rather_than_an_empty_zip(db, stores):
    job = _job(db)

    _run(job.id, {"status": ["placed"]}, "empty")

    db.refresh(job)
    assert job.status == JobStatus.failed
    assert "no order unit" in job.payload["error"]
    assert not (stores / "archives" / "empty.zip").exists()


# --- download and delete -------------------------------------------------------------
def test_a_download_needs_a_fresh_token_for_that_archive(api, db, stores):
    account = _account(db)
    _order(db, account, "OD1", pdfs=("p",))
    job = _job(db)
    _run(job.id, {}, "mine")
    archive = db.query(CompiledArchive).one()

    assert api.get(f"/invoices/archives/{archive.id}/download").status_code == 401

    grant = api.post(f"/invoices/archives/{archive.id}/download-token").json()
    assert grant["url"].startswith(f"/invoices/archives/{archive.id}/download?token=")
    assert grant["expires_in"] == archives.DOWNLOAD_TOKEN_SECONDS
    response = api.get(grant["url"])
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
    assert 'filename="mine.zip"' in response.headers["content-disposition"]
    assert zipfile.ZipFile(io.BytesIO(response.content)).namelist() == ["OD1.pdf"]

    # Another archive's token, an expired one, a forged lifetime or a signature
    # that is not even hex: all 401, never a 500.
    other = api.get(f"/invoices/archives/{archive.id + 1}/download", params={"token": grant["token"]})
    assert other.status_code == 401
    stale = archives.make_download_token(archive.id, now=time.time() - 10_000)
    assert api.get(f"/invoices/archives/{archive.id}/download", params={"token": stale}).status_code == 401
    far = f"{int(time.time()) + 10_000_000}.{'a' * 64}"
    assert api.get(f"/invoices/archives/{archive.id}/download", params={"token": far}).status_code == 401
    odd = f"{int(time.time()) + 60}.ÿ" + "a" * 63
    assert api.get(f"/invoices/archives/{archive.id}/download", params={"token": odd}).status_code == 401
    assert api.post("/invoices/archives/999/download-token").status_code == 404

    # The client probes with HEAD before it navigates.
    assert api.head(grant["url"]).status_code == 200
    assert api.head(f"/invoices/archives/{archive.id}/download").status_code == 401

    # A file gone from the store: no link is issued, and a stale link is a 404.
    (stores / "archives" / "mine.zip").unlink()
    assert api.post(f"/invoices/archives/{archive.id}/download-token").status_code == 404
    assert api.get(grant["url"]).status_code == 404


def test_the_download_needs_no_bearer_on_the_real_app_but_everything_else_does(db, stores):
    """The property the whole design rests on: a browser navigation carries no
    header. Through app.main, so the wiring of public_router is what is pinned."""
    from app.main import app

    account = _account(db)
    _order(db, account, "OD1", pdfs=("p",))
    job = _job(db)
    _run(job.id, {}, "public")
    archive = db.query(CompiledArchive).one()
    anon = TestClient(app)

    response = anon.get(f"/invoices/archives/{archive.id}/download", params={"token": archives.make_download_token(archive.id)})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")

    assert anon.get("/invoices/archives").status_code == 401
    assert anon.post(f"/invoices/archives/{archive.id}/download-token").status_code == 401
    assert anon.get("/invoices/compile/status").status_code == 401


def test_delete_removes_the_file_and_the_row(api, db, stores):
    account = _account(db)
    _order(db, account, "OD1", pdfs=("p",))
    job = _job(db)
    _run(job.id, {}, "gone")
    archive = db.query(CompiledArchive).one()
    path = stores / "archives" / "gone.zip"
    assert path.exists()

    assert api.delete(f"/invoices/archives/{archive.id}").status_code == 204

    assert not path.exists()
    assert db.query(CompiledArchive).count() == 0
    assert api.delete(f"/invoices/archives/{archive.id}").status_code == 404


# --- retention and recovery ------------------------------------------------------------
def test_the_sweep_deletes_only_archives_past_the_retention(db, stores):
    root = stores / "archives"
    root.mkdir(parents=True, exist_ok=True)
    now = _naive(utcnow())
    old = CompiledArchive(name="old.zip", prefix="old", path="old.zip", size=1, created_at=now - timedelta(days=100))
    new = CompiledArchive(name="new.zip", prefix="new", path="new.zip", size=1, created_at=now - timedelta(days=10))
    for row in (old, new):
        (root / row.name).write_bytes(b"z")
    db.add_all([old, new])
    db.commit()

    assert archives.sweep_expired(db, days=0) == []  # 0 keeps everything
    assert archives.sweep_expired(db, days=90) == ["old.zip"]
    assert [a.name for a in db.query(CompiledArchive).all()] == ["new.zip"]
    assert not (root / "old.zip").exists()
    assert (root / "new.zip").exists()
    assert archives.sweep_in_own_session() == []  # nothing else is due at the default 90 days


def test_start_up_reclaims_what_an_interrupted_compilation_left(db, stores):
    root = stores / "archives"
    (root / ".tmp").mkdir(parents=True, exist_ok=True)
    (root / ".tmp" / "half.zip.part").write_bytes(b"half")
    (root / "orphan.zip").write_bytes(b"finished but never recorded")
    (root / "kept.zip").write_bytes(b"recorded")
    (root / "notes.txt").write_bytes(b"not ours to judge")
    db.add(CompiledArchive(name="kept.zip", prefix="kept", path="kept.zip", size=8))
    db.commit()

    removed = archives.reclaim_unrecorded(db)

    assert sorted(removed) == [".tmp/half.zip.part", "orphan.zip"]
    assert (root / "kept.zip").exists()
    assert (root / "notes.txt").exists()
    assert not (root / "orphan.zip").exists()
    assert archives.reclaim_in_own_session() == []


def test_the_daily_sweep_task_sweeps_and_the_app_sweeps_at_start_up(db, stores, monkeypatch):
    from app import main as main_module
    from app.main import app

    root = stores / "archives"
    root.mkdir(parents=True, exist_ok=True)
    old = CompiledArchive(name="old.zip", prefix="old", path="old.zip", size=1,
                          created_at=_naive(utcnow()) - timedelta(days=100))
    (root / "old.zip").write_bytes(b"z")
    db.add(old)
    db.commit()

    with TestClient(app):  # the lifespan: reclaim, then the sweep
        pass
    assert db.query(CompiledArchive).count() == 0
    assert not (root / "old.zip").exists()

    swept: list[int] = []
    monkeypatch.setattr(main_module, "ARCHIVE_SWEEP_SECONDS", 0)
    monkeypatch.setattr(main_module, "sweep_in_own_session", lambda: swept.append(1) or [])

    async def two_ticks() -> None:
        task = asyncio.create_task(main_module._sweep_archives_daily())
        while len(swept) < 2:
            await asyncio.sleep(0)
        task.cancel()

    asyncio.run(asyncio.wait_for(two_ticks(), timeout=5))
    assert len(swept) >= 2


def test_an_orphaned_compilation_is_marked_failed_at_start_up(db):
    job = Job(type=JobType.compile, status=JobStatus.running)
    db.add(job)
    db.commit()

    assert recovery.fail_orphaned_jobs(db, redis_url="") == [job.id]
    db.refresh(job)
    assert job.status == JobStatus.failed
