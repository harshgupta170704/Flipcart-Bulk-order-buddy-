"""An invoice put on file by hand answers its alert at once (26 Oct 2026).

"Product invoice missing" and "Invoice amount differs" are answered by putting
the PDF on file. The rules used to run only after a read-back or on "Evaluate
now", so an upload left its alert open; an upload that stored a PDF now runs
them, and one that stored nothing does not.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import invoices as invoices_api
from app.core import alerts
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Order, OrderStatus
from app.io import invoice_store

FIXTURES = Path(__file__).parent / "fixtures" / "invoices" / "OD332627468086718100"


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(invoice_store, "store_root", lambda: tmp_path / "invoices")


@pytest.fixture
def evaluations(monkeypatch) -> list[int]:
    calls: list[int] = []
    monkeypatch.setattr(alerts, "evaluate_in_own_session", lambda: calls.append(1) or {})
    return calls


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(invoices_api.router)
    return TestClient(application, headers={"Authorization": f"Bearer {create_access_token('tester', role='admin')}"})


@pytest.fixture
def order(db) -> Order:
    account = Account(email="upload@test.com", status=AccountStatus.new)
    db.add(account)
    db.commit()
    row = Order(account_id=account.id, status=OrderStatus.placed, model="Phone", qty=1, order_id="OD332627468086718100")
    db.add(row)
    db.commit()
    return row


def test_an_upload_that_stores_a_pdf_runs_the_alert_rules(api, order, evaluations):
    pdf = (FIXTURES / "file_1.pdf").read_bytes()

    response = api.post("/invoices/upload", files=[("files", ("OD332627468086718100.pdf", pdf, "application/pdf"))])

    assert response.status_code == 200, response.text
    assert response.json()["stored"] == 1
    assert evaluations == [1]


def test_an_upload_that_stores_nothing_leaves_the_rules_alone(api, order, evaluations):
    response = api.post("/invoices/upload", files=[("files", ("notes.txt", b"not a pdf", "text/plain"))])

    assert response.status_code == 200, response.text
    assert response.json()["stored"] == 0
    assert evaluations == []


def test_a_rules_pass_that_fails_does_not_undo_the_upload(api, order, monkeypatch):
    def broken():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(alerts, "evaluate_in_own_session", broken)
    pdf = (FIXTURES / "file_1.pdf").read_bytes()

    response = api.post("/invoices/upload", files=[("files", ("OD332627468086718100.pdf", pdf, "application/pdf"))])

    assert response.status_code == 200, response.text
    assert response.json()["stored"] == 1
