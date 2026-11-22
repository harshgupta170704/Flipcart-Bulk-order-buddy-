"""The compilation's half-written zip has a name of its writer's own (audit
F-39, 25 Oct 2026), as the invoice store's ``.part`` files do: two
compilations given one archive name must not write one file between them."""

from __future__ import annotations

import re
import threading
import zipfile
from datetime import timedelta

import pytest

from app.core import archives, invoices
from app.db.models import Account, AccountStatus, Job, JobStatus, JobType, Order, OrderStatus, utcnow
from app.io import invoice_store
from app.io.invoice_store import InvoiceStore
from app.jobs import compile_tasks

_PART = re.compile(r"^same\.zip\.\d+-[0-9a-f]{8}\.part$")


@pytest.fixture(autouse=True)
def stores(tmp_path, monkeypatch):
    monkeypatch.setattr(invoice_store, "store_root", lambda: tmp_path / "invoices")
    monkeypatch.setattr(archives, "archive_root", lambda: tmp_path / "archives")
    return tmp_path


def test_every_call_names_a_temporary_file_of_its_own(stores):
    store = archives.ArchiveStore.from_settings()

    first, second = store.temp_path("same.zip"), store.temp_path("same.zip")

    assert first != second
    for path in (first, second):
        assert path.parent == store.root / archives.TEMP_DIR
        assert _PART.match(path.name), path.name


def test_the_start_up_reclaim_still_clears_what_an_interrupted_writer_left(db, stores):
    store = archives.ArchiveStore.from_settings()
    left = store.temp_path("same.zip")
    left.write_bytes(b"half a zip")

    assert archives.reclaim_unrecorded(db, store=store) == [f"{archives.TEMP_DIR}/{left.name}"]
    assert not left.exists()


def test_two_builds_of_one_archive_name_never_share_a_half_written_file(db, stores, monkeypatch):
    """Build A stops after its first order, its zip open and half written;
    build B of the same name runs from start to finish; then A finishes. With
    one shared temporary name B truncated A's file under it and moved it
    away, so A's rename failed and the archive was a mix of both. Now each
    finishes whole."""
    account = Account(email="zip@test.com", status=AccountStatus.new)
    db.add(account)
    db.commit()
    pks = []
    for n in range(3):
        order = Order(account_id=account.id, status=OrderStatus.placed, model="T5x", qty=1, order_id=f"OD{n}",
                      placed_at=utcnow().replace(tzinfo=None) - timedelta(days=n + 1))
        db.add(order)
        db.commit()
        invoices.attach(db, InvoiceStore.from_settings(), order, b"%PDF-1.4\n%" + bytes([65 + n]) * 4096 + b"\n", source="upload")
        db.commit()
        pks.append(order.id)
    job_a, job_b = (Job(type=JobType.compile, status=JobStatus.running) for _ in range(2))
    db.add_all([job_a, job_b])
    db.commit()

    a_paused, b_finished = threading.Event(), threading.Event()
    asked = {"a": 0}

    def cancel_requested(job_id: int) -> bool:
        # Read before the zip is opened, then after every order: A's second
        # read is after its first order, with its zip half written.
        if job_id == job_a.id:
            asked["a"] += 1
            if asked["a"] == 2:
                a_paused.set()
                assert b_finished.wait(10)
        return False

    monkeypatch.setattr(compile_tasks, "_cancel_requested", cancel_requested)
    store = archives.ArchiveStore.from_settings()
    outcome: dict = {}

    def build_a() -> None:
        try:
            outcome["a"] = compile_tasks._build(job_a.id, pks, "same.zip", store, InvoiceStore.from_settings())
        except BaseException as exc:  # noqa: BLE001 — reported by the assertions below
            outcome["a"] = exc

    thread = threading.Thread(target=build_a)
    thread.start()
    assert a_paused.wait(10)
    outcome["b"] = compile_tasks._build(job_b.id, pks, "same.zip", store, InvoiceStore.from_settings())
    b_finished.set()
    thread.join(10)

    assert isinstance(outcome["a"], compile_tasks.BuildResult), outcome["a"]
    assert outcome["a"].files == outcome["b"].files == 3
    with zipfile.ZipFile(store.root / "same.zip") as zf:
        assert zf.testzip() is None
        assert sorted(zf.namelist()) == ["OD0.pdf", "OD1.pdf", "OD2.pdf"]
    assert outcome["a"].size == (store.root / "same.zip").stat().st_size  # A's whole zip, renamed last
    assert not list((store.root / archives.TEMP_DIR).glob("*"))
