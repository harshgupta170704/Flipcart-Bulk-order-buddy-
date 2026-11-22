"""A run states its price cap, or says on purpose that it has none (audit F-23).

``POST /jobs/order`` used to read a blank target as "no cap": every order went
through at whatever the final amount came to, with only an info box on the
form to say so. Now a run gives ``target`` or sets ``no_cap``. Neither is a 422
naming the target, both is a 422 naming ``no_cap``, and either refusal starts
nothing: no job row, no run, and on a card run the card file is never read. A
run with no cap says so on its job, so its history shows it was chosen.
"""

from __future__ import annotations

import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.api import jobs as jobs_api
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Address, Job, JobStatus

PRODUCT = "https://www.flipkart.com/pigeon-pressure-cooker/p/itm0000000000001"
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CARD_NUMBER = "4111111111111111"


class _Recorder:
    """Stands in for ``run_order_job``; records the call synchronously."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})

        async def _noop() -> None:
            return None

        return _noop()


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(jobs_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def account(db) -> Account:
    """One account and a default address, so a request with a cap would start."""
    row = Account(email="cap@test.com", phone="9000000031", status=AccountStatus.session_valid)
    db.add(row)
    db.add(Address(label="hq", name="HQ", line1="1 Main Road", pincode="560001", is_default=True))
    db.commit()
    return row


@pytest.fixture
def run(monkeypatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(jobs_api, "run_order_job", recorder)
    return recorder


def _cod(**fields) -> dict:
    """A Cash on Delivery run (no card file needed), plus the fields under test."""
    return {"product_link": PRODUCT, "payment_mode": "cod", **fields}


def _cards(email: str) -> dict:
    """A one-card sheet for ``email``, as the form uploads it."""
    workbook = Workbook()
    workbook.active.append(["Email", "Card Number", "Exp Month", "Exp Year", "CVV"])
    workbook.active.append([email, CARD_NUMBER, "12", "28", "321"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return {"cards_file": ("cards.xlsx", buffer.getvalue(), _XLSX_MIME)}


def _refused_for(response, field: str) -> str:
    """Assert a 422 naming ``field`` and nothing else; return its message."""
    assert response.status_code == 422, response.text
    errors = response.json()["detail"]
    assert [error["loc"] for error in errors] == [["body", field]]
    return errors[0]["msg"]


@pytest.mark.parametrize(
    "fields",
    [{}, {"target": ""}, {"no_cap": "false"}, {"target": "", "no_cap": ""}],
    ids=["nothing sent", "a blank target", "no_cap false", "both blank"],
)
def test_a_run_that_states_no_cap_is_a_422_naming_the_target(api, db, account, run, fields):
    message = _refused_for(api.post("/jobs/order", data=_cod(**fields)), "target")

    # The words the form shows, and the flag an API caller would set.
    assert "price cap" in message and "No price cap" in message and "no_cap=true" in message
    assert run.calls == []  # no run started
    assert db.query(Job).count() == 0  # and no job row written


@pytest.mark.parametrize("flag", ["true", "1", "on"])
def test_a_cap_and_no_cap_together_are_a_422_naming_no_cap(api, db, account, run, flag):
    message = _refused_for(api.post("/jobs/order", data=_cod(target="18250", no_cap=flag)), "no_cap")

    assert "contradict" in message
    assert run.calls == []
    assert db.query(Job).count() == 0


def test_an_unreadable_no_cap_is_a_422_naming_it(api, db, account, run):
    _refused_for(api.post("/jobs/order", data=_cod(no_cap="maybe")), "no_cap")
    assert run.calls == []
    assert db.query(Job).count() == 0


def test_no_cap_on_purpose_starts_an_uncapped_run_and_says_so_on_the_job(api, db, account, run):
    response = api.post("/jobs/order", data=_cod(no_cap="true"))

    assert response.status_code == 201, response.text
    assert run.calls[0]["kwargs"]["target"] is None  # the engine's "no cap"
    payload = response.json()["payload"]
    assert (payload["target"], payload["no_cap"]) == (None, True)
    # Kept on the row, so the run's history still says it was uncapped on purpose.
    assert db.get(Job, response.json()["id"]).payload["no_cap"] is True


def test_a_capped_run_records_its_target_and_no_no_cap_key(api, db, account, run):
    """The target says what the cap was; a ``"no_cap": false`` beside it would
    add nothing, and old runs' payloads read exactly as they did."""
    for fields in ({"target": "18250"}, {"target": "18250", "no_cap": "false"}):
        response = api.post("/jobs/order", data=_cod(**fields))

        assert response.status_code == 201, response.text
        payload = response.json()["payload"]
        assert payload["target"] == 18250.0
        assert "no_cap" not in payload
        # The stub never finishes a run; the next start would be refused as a
        # second run in progress, so this one is marked done.
        db.get(Job, response.json()["id"]).status = JobStatus.completed
        db.commit()

    assert [call["kwargs"]["target"] for call in run.calls] == [18250.0, 18250.0]


@pytest.mark.parametrize(
    ("fields", "field"),
    [({}, "target"), ({"target": "18250", "no_cap": "true"}, "no_cap")],
    ids=["no cap stated", "both stated"],
)
def test_a_card_run_that_does_not_state_its_cap_never_reads_the_card_file(
    api, db, account, run, monkeypatch, fields, field
):
    def never(*_args, **_kwargs):
        raise AssertionError("the card file must not be read for a run that cannot start")

    monkeypatch.setattr(jobs_api.card_sheets, "parse_card_sheet", never)

    response = api.post(
        "/jobs/order",
        data={"product_link": PRODUCT, "payment_mode": "card", **fields},
        files=_cards(account.email),
    )

    _refused_for(response, field)
    assert CARD_NUMBER not in response.text
    assert run.calls == []
    assert db.query(Job).count() == 0


def test_a_card_run_with_no_cap_on_purpose_records_it_and_hands_the_cards_over(api, db, account, run):
    response = api.post(
        "/jobs/order",
        data={"product_link": PRODUCT, "payment_mode": "card", "no_cap": "true"},
        files=_cards(account.email),
    )

    assert response.status_code == 201, response.text
    payload = response.json()["payload"]
    assert (payload["target"], payload["no_cap"]) == (None, True)
    kwargs = run.calls[0]["kwargs"]
    assert kwargs["target"] is None
    assert set(kwargs["cards"]) == {account.id}
    assert CARD_NUMBER not in response.text


def test_the_cap_is_asked_for_before_the_one_run_at_a_time_guard(api, db, account, run):
    """With a run already going, a request that states no cap is told about
    the cap — the field it can fix there and then — not only about the run."""
    assert api.post("/jobs/order", data=_cod(target="18250")).status_code == 201

    _refused_for(api.post("/jobs/order", data=_cod()), "target")
    assert api.post("/jobs/order", data=_cod(target="18250")).status_code == 409
    assert len(run.calls) == 1
