"""The card filter in a compilation's records (audit F-20, 25 Oct 2026).

A Compile Invoices run is started from the ledger's filter, card filter
included. The job row (Compilation Status, the job list) and the archive's row
are read by every signed-in user, so they record the card filter by its last
four digits only — while the run itself is handed the filter as typed, in
memory, and selects exactly the orders the screen showed.
"""

from __future__ import annotations

import asyncio
import json
import zipfile
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import invoices as invoices_api
from app.api import jobs as jobs_api
from app.core import archives, invoices
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, CompiledArchive, Job, JobStatus, JobType, Order, OrderStatus, utcnow
from app.io import invoice_store
from app.io.invoice_store import InvoiceStore
from app.jobs.compile_tasks import run_compile_job

#: The card the run filters by, and another that ends in the same four digits:
#: a run that filtered by what is recorded ("…1111") would take both.
CARD = "4111111111111111"
SAME_LAST_FOUR = "5500000000001111"
OTHER_CARD = "5500000000000004"


@pytest.fixture(autouse=True)
def stores(tmp_path, monkeypatch):
    monkeypatch.setattr(invoice_store, "store_root", lambda: tmp_path / "invoices")
    monkeypatch.setattr(archives, "archive_root", lambda: tmp_path / "archives")
    return tmp_path


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(invoices_api.router)
    application.include_router(jobs_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def spawned(monkeypatch) -> list:
    """What the start handed the engine — the coroutine factory itself, so the
    test runs the very filter the request passed in memory."""
    handed: list = []
    monkeypatch.setattr(invoices_api, "_spawn", lambda factory, job_id: handed.append((factory, job_id)))
    return handed


def _order(db, account: Account, order_id: str, card: str | None) -> Order:
    order = Order(
        account_id=account.id,
        status=OrderStatus.placed,
        model="T5x",
        qty=1,
        order_id=order_id,
        payment_mode="card" if card else "cod",
        card_number=card,
        placed_at=utcnow().replace(tzinfo=None) - timedelta(days=2),
    )
    db.add(order)
    db.commit()
    invoices.attach(db, InvoiceStore.from_settings(), order, b"%PDF-1.4\n%" + order_id.encode() + b"\n", source="upload")
    db.commit()
    return order


@pytest.fixture
def ledger(db) -> Account:
    account = Account(email="cards@test.com", status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    _order(db, account, "OD1000000001", CARD)
    _order(db, account, "OD1000000002", SAME_LAST_FOUR)
    _order(db, account, "OD1000000003", OTHER_CARD)
    _order(db, account, "OD1000000004", None)  # Cash on Delivery
    return account


# --- what is recorded ------------------------------------------------------------------
@pytest.mark.parametrize(
    ("card", "recorded"),
    [
        (CARD, "…1111"),
        ("4111 1111-1111 1111", "…1111"),  # typed with spaces and dashes
        ("1234", "…1234"),
        ("34", "…34"),  # fewer than four digits: all of them, still after the ellipsis
        ("abcd", "…"),  # no digits: still a card filter (it matches nothing)
    ],
)
def test_only_the_last_four_digits_of_a_card_filter_are_recorded(card, recorded):
    raw = {"status": ["placed"], "card": card, "email": ["a@test.com"]}

    out = archives.recorded_filters(raw)

    assert out == {"status": ["placed"], "email": ["a@test.com"], "card": recorded}
    assert raw["card"] == card  # the run's own copy is left as typed


@pytest.mark.parametrize("raw", [{"status": ["placed"]}, {"status": ["placed"], "card": "   "}, {"status": ["placed"], "card": ""}])
def test_no_card_filter_or_a_blank_one_records_no_card(raw):
    assert archives.recorded_filters(raw) == {"status": ["placed"]}


# --- the whole run ----------------------------------------------------------------------
def test_the_job_row_and_the_archive_row_never_hold_the_card_number(api, db, ledger, spawned, stores):
    response = api.post(f"/invoices/compile?status=placed&card={CARD}", json={"prefix": "by-card"})

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["orders"] == 1  # the full filter: one order, not the two ending in 1111
    assert body["job"]["payload"]["filters"] == {"status": ["placed"], "card": "…1111"}
    assert CARD not in response.text
    job = db.get(Job, body["job"]["id"])
    assert CARD not in json.dumps(job.payload, ensure_ascii=False)

    # The run is handed the filter as typed, in memory, and selects with it.
    (factory, job_id), = spawned
    assert job_id == job.id
    asyncio.run(factory())

    db.expire_all()
    job = db.get(Job, job_id)
    assert job.status == JobStatus.completed, job.log
    with zipfile.ZipFile(stores / "archives" / "by-card.zip") as zf:
        assert zf.namelist() == ["OD1000000001.pdf"]
    archive = db.query(CompiledArchive).one()
    assert archive.filters == {"status": ["placed"], "card": "…1111"}
    assert CARD not in json.dumps(archive.filters, ensure_ascii=False)
    assert CARD not in json.dumps(job.payload, ensure_ascii=False)
    assert CARD not in (job.log or "")

    # Nor anywhere a signed-in user reads the run back.
    assert CARD not in api.get("/invoices/compile/status").text
    assert CARD not in api.get("/jobs").text
    assert CARD not in api.get(f"/jobs/{job_id}").text


def test_a_run_started_without_the_api_records_the_archive_filter_masked_too(db, ledger, stores):
    """The archive row is written by the run, whoever started it."""
    job = Job(type=JobType.compile, status=JobStatus.queued, payload={})
    db.add(job)
    db.commit()

    asyncio.run(run_compile_job(job.id, filter_params={"card": SAME_LAST_FOUR}, prefix="direct"))

    with zipfile.ZipFile(stores / "archives" / "direct.zip") as zf:
        assert zf.namelist() == ["OD1000000002.pdf"]
    archive = db.query(CompiledArchive).one()
    assert archive.filters == {"card": "…1111"}
    assert SAME_LAST_FOUR not in json.dumps(archive.filters, ensure_ascii=False)
