"""Mock mode (step A1): an order run that opens no browser and buys nothing.

A mock run goes through account selection, sessions, cards and the unit
budget as a real run does, then makes up each order's outcome
(app.automation.mock_ordering). It must never touch the browser or the
ordering flow, and never write an order row.
"""

from __future__ import annotations

import asyncio
from collections import Counter

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import jobs as jobs_api
from app.automation import mock_ordering
from app.automation.mock_ordering import mock_message, mock_order
from app.automation.ordering import PAYMENT_COD
from app.core.security import create_access_token
from app.db.models import (
    Account,
    AccountStatus,
    Address,
    Gst,
    Job,
    JobItem,
    JobItemStatus,
    JobStatus,
    JobType,
    Order,
    OrderStatus,
    Session as SessionModel,
)
from app.jobs import tasks

PRODUCT = "https://www.flipkart.com/some-phone/p/itm0000000000001"


# --- the made-up outcome ----------------------------------------------------------
def _outcome(job_id, account_id, target=1000.0, qty=2):
    return asyncio.run(
        mock_order(job_id=job_id, account_id=account_id, qty=qty, target=target, payment_mode=PAYMENT_COD)
    )


def test_the_same_run_and_account_always_end_the_same_way():
    assert _outcome(7, 42) == _outcome(7, 42)


def test_outcomes_are_spread_like_a_real_run():
    tally = Counter(_outcome(1, account)["status"] for account in range(2000))
    share = {status: count / 2000 for status, count in tally.items()}
    assert 0.07 < share[OrderStatus.failed] < 0.13
    assert 0.10 < share[OrderStatus.over_target] < 0.17
    assert 0.70 < share[OrderStatus.placed] < 0.82


def test_a_placed_mock_order_is_at_or_under_the_cap_and_names_itself_fake():
    placed = [r for r in (_outcome(3, a) for a in range(300)) if r["status"] == OrderStatus.placed]
    assert placed
    assert all(r["amount"] <= 1000.0 and r["qty"] == 2 for r in placed)
    assert all(r["order_id"].startswith("MOCK-") for r in placed)
    assert "nothing bought" in mock_message(placed[0])


def test_an_over_cap_mock_order_buys_nothing_and_says_why():
    over = next(r for r in (_outcome(3, a) for a in range(300)) if r["status"] == OrderStatus.over_target)
    assert over["amount"] > 1000.0 and over["qty"] == 0
    assert "over the cap" in mock_message(over)


def test_without_a_cap_nothing_comes_out_over_it():
    statuses = {_outcome(5, a, target=None)["status"] for a in range(500)}
    assert OrderStatus.over_target not in statuses


def test_the_delay_is_skipped_at_zero(monkeypatch):
    async def never(_seconds):
        raise AssertionError("slept")

    monkeypatch.setattr(mock_ordering.asyncio, "sleep", never)
    _outcome(1, 1)


# --- the run ------------------------------------------------------------------------
@pytest.fixture
def address_book(db):
    gst = Gst(label="ACME", gstin="29ABCDE1234F1Z5", business_name="Acme Traders", is_default=True)
    db.add(gst)
    db.commit()
    db.add(Address(label="works", name="Acme Traders", line1="1 Industrial Area", pincode="560001", gst_id=gst.id, is_default=True))
    db.commit()


@pytest.fixture
def no_browser(monkeypatch):
    """A mock run that reached the browser or the ordering flow fails the test."""

    def refuse(*_args, **_kwargs):
        raise AssertionError("a mock run must not open a browser or run the ordering flow")

    monkeypatch.setattr(tasks, "browser_page", refuse)
    monkeypatch.setattr(tasks, "run_order", refuse)
    monkeypatch.setattr(tasks, "reserve_order", refuse)
    monkeypatch.setattr(tasks, "record_order_result", refuse)
    monkeypatch.setattr(tasks, "save_order_result", refuse)
    base = tasks._effective_settings

    def instant():
        settings = base()
        settings.mock_order_seconds = 0
        return settings

    monkeypatch.setattr(tasks, "_effective_settings", instant)


def _accounts(db, count, *, with_session=True):
    ids = []
    for n in range(count):
        account = Account(email=f"mock{n}@test.com", status=AccountStatus.session_valid)
        db.add(account)
        db.commit()
        if with_session:
            db.add(SessionModel(account_id=account.id, cookies=[{"name": "SN", "value": str(n)}], valid=True))
            db.commit()
        ids.append(account.id)
    return ids


def _job(db):
    job = Job(type=JobType.order, status=JobStatus.queued)
    db.add(job)
    db.commit()
    return job


def _items(db, job_id):
    db.expire_all()
    return db.query(JobItem).filter(JobItem.job_id == job_id).all()


def test_a_mock_run_opens_no_browser_and_writes_no_order(db, address_book, no_browser):
    ids = _accounts(db, 30)
    job = _job(db)

    asyncio.run(tasks.run_order_job(job.id, ids, product_link=PRODUCT, target=1000.0, payment_mode=PAYMENT_COD, mock=True))

    db.expire_all()
    assert db.query(Order).count() == 0
    items = _items(db, job.id)
    assert len(items) == 30
    assert all(item.status in (JobItemStatus.done, JobItemStatus.failed) for item in items)
    assert all(item.message.startswith("[mock]") for item in items)
    assert any(item.status == JobItemStatus.done for item in items)
    assert "MOCK run" in (db.get(Job, job.id).log or "")
    assert db.get(Job, job.id).status != JobStatus.running


def test_a_mock_run_skips_accounts_without_a_session_like_a_real_one(db, address_book, no_browser):
    ids = _accounts(db, 2, with_session=False)
    job = _job(db)

    asyncio.run(tasks.run_order_job(job.id, ids, product_link=PRODUCT, target=1000.0, payment_mode=PAYMENT_COD, mock=True))

    assert all(item.message == "no session" for item in _items(db, job.id))


def test_a_mock_run_stops_at_the_target_quantity(db, address_book, no_browser):
    ids = _accounts(db, 40)
    job = _job(db)

    asyncio.run(
        tasks.run_order_job(
            job.id,
            ids,
            product_link=PRODUCT,
            target=1000.0,
            payment_mode=PAYMENT_COD,
            total_quantity=10,
            qty_per_order=2,
            mock=True,
        )
    )

    placed = [item for item in _items(db, job.id) if item.status == JobItemStatus.done]
    # 10 units in orders of 2: exactly five placed, however the others went.
    assert len(placed) == 5
    assert "placed 2 unit(s)" in placed[0].message


# --- POST /jobs/order -------------------------------------------------------------
class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append(kwargs)

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
def recorder(db, monkeypatch) -> _Recorder:
    db.add(Account(email="api@test.com", status=AccountStatus.session_valid))
    db.add(Address(label="hq", name="HQ", line1="1 Main Road", pincode="560001", is_default=True))
    db.commit()
    rec = _Recorder()
    monkeypatch.setattr(jobs_api, "run_order_job", rec)
    return rec


def _post(api, **fields):
    return api.post("/jobs/order", data={"product_link": PRODUCT, "payment_mode": "cod", "target": "900", **fields})


def test_a_mock_request_reaches_the_run_as_a_mock_and_says_so_on_its_job(api, db, recorder):
    response = _post(api, mode="mock")

    assert response.status_code == 201, response.text
    assert recorder.calls[0]["mock"] is True
    assert response.json()["payload"]["mode"] == "mock"


def test_a_real_request_reads_exactly_as_before(api, db, recorder):
    response = _post(api)

    assert response.status_code == 201, response.text
    assert recorder.calls[0]["mock"] is False
    assert "mode" not in response.json()["payload"]


@pytest.mark.parametrize("mode", ["Mock", " MOCK "])
def test_the_mode_is_read_without_regard_to_case_or_spaces(api, db, recorder, mode):
    assert _post(api, mode=mode).status_code == 201


@pytest.mark.parametrize("mode", ["dry", "auto"])
def test_an_unknown_mode_starts_nothing(api, db, recorder, mode):
    response = _post(api, mode=mode)
    assert response.status_code == 422
    assert recorder.calls == []


def test_a_blank_mode_is_a_real_run(api, db, recorder):
    assert _post(api, mode="").status_code == 201
    assert recorder.calls[0]["mock"] is False


def test_a_mock_card_run_cannot_leave_the_bank_step_to_a_person(api, db, recorder):
    response = api.post(
        "/jobs/order",
        data={"product_link": PRODUCT, "payment_mode": "card", "target": "900", "mode": "mock", "bank_step_by_hand": "true"},
    )
    assert response.status_code == 422
    assert recorder.calls == []


# --- quantity mode: Minimum / Upto (step A2) ------------------------------------
from app.automation.ordering import QuantityOutcome, quantity_refusal  # noqa: E402


def test_upto_takes_what_the_checkout_accepted():
    assert quantity_refusal("upto", 4, QuantityOutcome(1, "cap of 1")) is None


def test_minimum_refuses_a_short_order_and_says_why():
    why = quantity_refusal("minimum", 4, QuantityOutcome(1, "You can only purchase 1 unit."))
    assert why is not None
    assert "only 1 of the 4" in why and "nothing was charged" in why and "purchase 1 unit" in why


def test_minimum_lets_a_full_order_through():
    assert quantity_refusal("minimum", 4, QuantityOutcome(4)) is None
    assert quantity_refusal("minimum", 1, QuantityOutcome(1)) is None


def test_a_minimum_request_reaches_the_run_and_its_job(api, db, recorder):
    response = _post(api, qty_mode="Minimum")

    assert response.status_code == 201, response.text
    assert recorder.calls[0]["qty_mode"] == "minimum"
    assert response.json()["payload"]["qty_mode"] == "minimum"


def test_upto_is_the_default_and_leaves_the_job_as_before(api, db, recorder):
    response = _post(api)

    assert recorder.calls[0]["qty_mode"] == "upto"
    assert "qty_mode" not in response.json()["payload"]


def test_an_unknown_quantity_mode_starts_nothing(api, db, recorder):
    assert _post(api, qty_mode="most").status_code == 422
    assert recorder.calls == []


def test_the_run_hands_the_quantity_mode_to_the_ordering_flow(db, address_book, monkeypatch):
    import contextlib

    seen = {}

    @contextlib.asynccontextmanager
    async def fake_browser_page(**_kwargs):
        yield None, None

    async def fake_run_order(page, **kwargs):
        seen.update(kwargs)
        return {"status": OrderStatus.failed, "error": "stop", "payment_mode": PAYMENT_COD}

    monkeypatch.setattr(tasks, "browser_page", fake_browser_page)
    monkeypatch.setattr(tasks, "run_order", fake_run_order)
    monkeypatch.setattr(tasks, "_snapshot", lambda *_a, **_k: None, raising=False)
    ids = _accounts(db, 1)
    job = _job(db)

    asyncio.run(
        tasks.run_order_job(job.id, ids, product_link=PRODUCT, target=1000.0, payment_mode=PAYMENT_COD, qty_mode="minimum")
    )

    assert seen["qty_mode"] == "minimum"


# --- GST mandatory and delivery phones (step A3) --------------------------------
from app.automation.ordering import gst_refusal  # noqa: E402
from app.jobs.tasks import pick_address_phone  # noqa: E402


def test_without_the_switch_a_checkout_naming_no_gst_goes_ahead():
    assert gst_refusal(False, "29ABCDE1234F1Z5", None) is None
    assert gst_refusal(False, None, None) is None


def test_gst_mandatory_needs_a_gst_chosen_and_shown():
    assert "no GST was chosen" in gst_refusal(True, None, None)
    assert "does not show GST 29ABCDE1234F1Z5" in gst_refusal(True, "29ABCDE1234F1Z5", None)
    assert gst_refusal(True, "29ABCDE1234F1Z5", "29ABCDE1234F1Z5") is None


def test_given_phones_are_used_in_turn_and_start_over():
    phones = ["9000000001", "9000000002"]
    assert [pick_address_phone(i, "8111111111", phones, False) for i in range(5)] == [
        "9000000001", "9000000002", "9000000001", "9000000002", "9000000001",
    ]


def test_without_phones_the_account_phone_stays_unless_the_address_phone_is_asked_for():
    assert pick_address_phone(0, "8111111111", None, False) == "8111111111"
    assert pick_address_phone(0, "8111111111", [], True) == ""


def test_phones_and_gst_mandatory_reach_the_run_and_its_job(api, db, recorder):
    response = _post(api, gst_mandatory="true", address_phones="9000000001, 9000000002 9000000003")

    assert response.status_code == 201, response.text
    call = recorder.calls[0]
    assert call["gst_mandatory"] is True
    assert call["address_phones"] == ["9000000001", "9000000002", "9000000003"]
    payload = response.json()["payload"]
    assert payload["gst_mandatory"] is True and payload["address_phones"] == 3


def test_a_run_without_them_reads_as_before(api, db, recorder):
    response = _post(api)
    payload = response.json()["payload"]
    assert recorder.calls[0]["address_phones"] == []
    assert not {"gst_mandatory", "address_phones", "use_address_phone"} & set(payload)


@pytest.mark.parametrize("phones", ["+919000000001", "900000000", "90000000011", "9000-000001"])
def test_a_phone_that_is_not_ten_digits_starts_nothing(api, db, recorder, phones):
    response = _post(api, address_phones=f"9000000002 {phones}")
    assert response.status_code == 422
    assert phones in response.json()["detail"]
    assert recorder.calls == []


def test_the_run_gives_each_account_its_phone_in_turn(db, address_book, monkeypatch):
    import contextlib

    phones_seen = {}

    @contextlib.asynccontextmanager
    async def fake_browser_page(**_kwargs):
        yield None, None

    async def fake_run_order(page, *, address, **kwargs):
        phones_seen[address.phone] = kwargs.get("gst_mandatory")
        return {"status": OrderStatus.failed, "error": "stop", "payment_mode": PAYMENT_COD}

    monkeypatch.setattr(tasks, "browser_page", fake_browser_page)
    monkeypatch.setattr(tasks, "run_order", fake_run_order)
    ids = _accounts(db, 3)
    job = _job(db)

    asyncio.run(
        tasks.run_order_job(
            job.id, ids, product_link=PRODUCT, target=1000.0, payment_mode=PAYMENT_COD,
            address_phones=["9000000001", "9000000002"], gst_mandatory=True,
        )
    )

    assert set(phones_seen) == {"9000000001", "9000000002"}
    assert set(phones_seen.values()) == {True}
