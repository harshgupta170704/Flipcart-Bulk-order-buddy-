"""Tests for choosing the address and GST an ordering run uses (ROADMAP §8).

The business runs several entities, each with its own GST, and getting this
wrong fails *quietly*: the order is placed, it just ships to the wrong place
and is billed under the wrong GST number. So these tests are about one thing —
that a run uses the address and GST it was told to, and refuses to run at all
when it cannot.

No browser and no Flipkart: ``app.jobs.tasks.browser_page`` and
``app.jobs.tasks.run_order`` are monkeypatched, and the recorded calls are what
we assert on.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import jobs as jobs_api
from app.core.security import create_access_token
from app.db.models import (
    Account,
    AccountStatus,
    Address,
    Gst,
    Job,
    JobStatus,
    JobType,
    Order,
    OrderStatus,
    Session as SessionModel,
)
from app.jobs import tasks
from tests.fakes import FakeContext, FakePage

PRODUCT = "https://www.flipkart.com/some-item/p/itm123"


# --- helpers -----------------------------------------------------------------
def _account(db, email="buyer@test.com", *, phone=None, with_session=True):
    """An account with a live session, so an order run does not skip it."""
    account = Account(email=email, phone=phone, status=AccountStatus.new)
    db.add(account)
    db.commit()
    if with_session:
        db.add(
            SessionModel(
                account_id=account.id,
                cookies=[{"name": "SN", "value": email}],
                valid=True,
            )
        )
        db.commit()
    return account


def _job(db):
    job = Job(type=JobType.order, status=JobStatus.queued)
    db.add(job)
    db.commit()
    return job


def _gst(db, label="BLAZECOM", *, gstin="29BLAZECOM01Z5", name="Blazecom", default=False):
    row = Gst(label=label, gstin=gstin, business_name=name, is_default=default)
    db.add(row)
    db.commit()
    return row


def _address(
    db,
    label="warehouse",
    *,
    name="Blazecom Warehouse",
    phone=None,
    line1="12 Dock Road",
    floor="2nd Floor",
    city="Bengaluru",
    state="Karnataka",
    pincode="560099",
    gst_id=None,
    default=False,
):
    row = Address(
        label=label,
        name=name,
        phone=phone,
        line1=line1,
        floor=floor,
        city=city,
        state=state,
        pincode=pincode,
        gst_id=gst_id,
        is_default=default,
    )
    db.add(row)
    db.commit()
    return row


def _fake_browser():
    """Stand-in for core.browser.browser_page: yields (context, page)."""

    @contextlib.asynccontextmanager
    async def fake_browser_page(**kwargs):
        page = FakePage()
        page.context = FakeContext(kwargs.get("cookies"))
        yield page.context, page

    return fake_browser_page


def _order_stub():
    """run_order stand-in recording the address/GST it was handed."""
    calls: list[dict] = []

    async def fake_run_order(
        page,
        *,
        product_link,
        address,
        gst,
        card=None,
        qty=1,
        target=None,
        payment_mode=tasks.PAYMENT_CARD,
        **_extra,
    ):
        calls.append({"address": address, "gst": gst})
        return {
            "status": OrderStatus.placed,
            "amount": 500.00,
            "target": target,
            "order_id": "OD-1",
            "pin": address.pin,
            "qty": qty,
        }

    return fake_run_order, calls


@pytest.fixture
def ordering(db, monkeypatch):
    """Patch out the browser and the ordering flow; hand back the call log."""
    monkeypatch.setattr(tasks, "browser_page", _fake_browser())
    stub, calls = _order_stub()
    monkeypatch.setattr(tasks, "run_order", stub)
    return calls


def _run(job, account_ids, **kwargs):
    """Run an order job to completion. Cash on Delivery: no card plumbing."""
    asyncio.run(
        tasks.run_order_job(
            job.id,
            account_ids,
            product_link=PRODUCT,
            payment_mode=tasks.PAYMENT_COD,
            **kwargs,
        )
    )


# --- the ladder: explicit id ------------------------------------------------
def test_an_explicit_address_and_gst_are_the_ones_used(db, ordering):
    """The whole point of the phase: the chosen pair reaches checkout."""
    gst = _gst(db, "DIGIDRIVE", gstin="27DIGIDRIVE01Z5", name="Digidrive LLP")
    address = _address(
        db,
        "digidrive-hub",
        name="Digidrive Hub",
        line1="88 Ring Road",
        floor="Unit 4",
        city="Pune",
        state="Maharashtra",
        pincode="411001",
    )
    # A default pair exists and must be ignored in favour of the chosen one.
    other_gst = _gst(db, "GLOBE", gstin="29GLOBECOMM1Z5", name="Globe Commerce", default=True)
    _address(db, "globe-hq", name="Globe HQ", pincode="560002", gst_id=other_gst.id, default=True)

    account = _account(db, phone="9000000001")
    job = _job(db)

    _run(job, [account.id], address_id=address.id, gst_id=gst.id)

    sent = ordering[0]
    assert sent["address"].name == "Digidrive Hub"
    assert sent["address"].pin == "411001"
    assert sent["address"].area == "88 Ring Road, Unit 4, Pune, Maharashtra"
    assert (sent["gst"].gstin, sent["gst"].business_name) == (
        "27DIGIDRIVE01Z5",
        "Digidrive LLP",
    )

    # ...and the order row records which GST it was billed under.
    order = db.query(Order).one()
    assert order.gst_name == "Digidrive LLP"
    assert order.pin == "411001"


def test_an_unlinked_chosen_address_with_no_gst_falls_to_the_default_gst(db, ordering):
    """The chosen address has no linked GST, so that rung of the GST ladder is
    skipped and the default GST is used."""
    default_gst = _gst(db, "SONIC", gstin="33SONICKOMM1Z5", name="Sonic Kommerce", default=True)
    address = _address(db, "jp-depot", name="JP Depot", pincode="600001")
    assert default_gst.is_default

    account = _account(db, phone="9000000002")
    job = _job(db)

    _run(job, [account.id], address_id=address.id)

    gst = ordering[0]["gst"]
    assert ordering[0]["address"].name == "JP Depot"
    assert (gst.gstin, gst.business_name) == ("33SONICKOMM1Z5", "Sonic Kommerce")


# --- the ladder: the default rows -------------------------------------------
def test_the_default_rows_are_used_when_no_ids_are_given(db, ordering):
    """Read through the address book's own ``default_*`` helpers, not re-derived."""
    _gst(db, "NOT-DEFAULT", gstin="29NOTDEFAULT1Z", name="Not Default")
    _gst(db, "AARTHBODHA", gstin="29AARTHBODHA1Z", name="Aarthbodha", default=True)
    _address(db, "spare", name="Spare Place", pincode="110001")
    _address(
        db,
        "aarthbodha-hq",
        name="Aarthbodha HQ",
        line1="5 Mill Street",
        floor=None,
        city="Chennai",
        state="Tamil Nadu",
        pincode="600042",
        default=True,
    )

    account = _account(db, phone="9000000003")
    job = _job(db)

    _run(job, [account.id])

    sent = ordering[0]
    assert sent["address"].name == "Aarthbodha HQ"
    assert sent["address"].pin == "600042"
    assert sent["address"].area == "5 Mill Street, Chennai, Tamil Nadu"
    assert sent["gst"].business_name == "Aarthbodha"
    # The default GST, not the non-default row.
    assert sent["gst"].gstin == "29AARTHBODHA1Z"


# --- the GST ladder: chosen, then the address's own, then the default --------
def test_an_unchosen_gst_follows_the_address_before_the_default(db, ordering):
    """Picking one entity's address and leaving the GST unset means that entity,
    not whichever one happens to be the default."""
    linked = _gst(db, "DIGIDRIVE", gstin="27DIGIDRIVE01Z5", name="Digidrive LLP")
    _gst(db, "GLOBE", gstin="29GLOBECOMM1Z5", name="Globe Commerce", default=True)
    address = _address(db, "digidrive-hub", name="Digidrive Hub", pincode="411001", gst_id=linked.id)
    account = _account(db, phone="9000000016")
    job = _job(db)

    _run(job, [account.id], address_id=address.id)

    gst = ordering[0]["gst"]
    assert (gst.gstin, gst.business_name) == ("27DIGIDRIVE01Z5", "Digidrive LLP")
    assert db.query(Order).one().gst_name == "Digidrive LLP"


def test_the_default_address_brings_its_linked_gst_too(db, ordering):
    linked = _gst(db, "DIGIDRIVE", gstin="27DIGIDRIVE01Z5", name="Digidrive LLP")
    _gst(db, "GLOBE", gstin="29GLOBECOMM1Z5", name="Globe Commerce", default=True)
    _address(db, "digidrive-hub", name="Digidrive Hub", pincode="411001", gst_id=linked.id, default=True)
    account = _account(db, phone="9000000021")
    job = _job(db)

    _run(job, [account.id])

    assert ordering[0]["address"].name == "Digidrive Hub"
    assert ordering[0]["gst"].gstin == "27DIGIDRIVE01Z5"


def test_a_chosen_gst_still_wins_over_the_linked_one(db, ordering):
    """Allowed — one entity's address may bill under another's GST — the picker
    just says so out loud before the run starts."""
    linked = _gst(db, "DIGIDRIVE", gstin="27DIGIDRIVE01Z5", name="Digidrive LLP")
    chosen = _gst(db, "GLOBE", gstin="29GLOBECOMM1Z5", name="Globe Commerce")
    address = _address(db, "digidrive-hub", name="Digidrive Hub", pincode="411001", gst_id=linked.id)
    account = _account(db, phone="9000000017")
    job = _job(db)

    _run(job, [account.id], address_id=address.id, gst_id=chosen.id)

    assert ordering[0]["gst"].gstin == "29GLOBECOMM1Z5"


def test_with_no_gst_anywhere_the_order_is_placed_without_gst_details(db, ordering, caplog):
    """No GST chosen, linked or default: the run goes ahead with none and says so,
    rather than billing an entity nobody chose."""
    _address(db, "plain", name="Plain Place", pincode="560010", default=True)
    account = _account(db, phone="9000000018")
    job = _job(db)

    with caplog.at_level(logging.WARNING):
        _run(job, [account.id])

    gst = ordering[0]["gst"]
    assert (gst.gstin, gst.business_name) == ("", "")
    order = db.query(Order).one()
    assert order.gst_name is None
    assert order.gstin is None
    assert any("without GST details" in record.getMessage() for record in caplog.records)


# --- no address at all: the run is refused ----------------------------------
def test_a_run_with_no_address_chosen_or_default_is_refused(db, ordering):
    """There is no config fallback: a run that cannot say where it ships does
    not run — and nothing is ordered."""
    _gst(db, "AARTHBODHA", gstin="29AARTHBODHA1Z", name="Aarthbodha", default=True)
    account = _account(db, phone="9000000004")
    job = _job(db)

    _run(job, [account.id])

    db.expire_all()
    job = db.get(Job, job.id)
    assert job.status == JobStatus.failed
    assert "no delivery address" in job.log
    assert "Address Book" in job.log
    assert ordering == []  # run_order was never called
    assert db.query(Order).count() == 0


def test_rows_that_exist_but_are_not_default_are_not_picked_up(db, ordering):
    """Having an address book is not the same as having chosen from it."""
    _gst(db, "BLAZECOM", default=False)
    _address(db, "unflagged", name="Unflagged Place", pincode="400001", default=False)

    account = _account(db, phone="9000000005")
    job = _job(db)

    _run(job, [account.id])

    db.expire_all()
    assert db.get(Job, job.id).status == JobStatus.failed
    assert ordering == []
    assert db.query(Order).count() == 0


# --- an id that does not exist kills the run --------------------------------
def test_a_missing_address_id_fails_the_run_and_places_no_order(db, ordering):
    """Silently shipping elsewhere is the failure this phase prevents."""
    account = _account(db, phone="9000000006")
    job = _job(db)

    _run(job, [account.id], address_id=4242)

    db.expire_all()
    job = db.get(Job, job.id)
    assert job.status == JobStatus.failed
    assert "address_id 4242" in job.log
    assert "does not exist" in job.log

    assert ordering == []  # run_order was never called
    assert db.query(Order).count() == 0


def test_a_missing_gst_id_fails_the_run_too(db, ordering):
    address = _address(db, "real", name="Real Place", pincode="560001")
    account = _account(db, phone="9000000007")
    job = _job(db)

    _run(job, [account.id], address_id=address.id, gst_id=9191)

    db.expire_all()
    job = db.get(Job, job.id)
    assert job.status == JobStatus.failed
    assert "gst_id 9191" in job.log
    assert ordering == []
    assert db.query(Order).count() == 0


# --- phone: the account's own wins ------------------------------------------
def test_the_accounts_phone_wins_over_the_address_phone(db, ordering):
    """Pre-existing behaviour: that number receives the delivery OTP."""
    address = _address(db, "with-phone", phone="9888888888", pincode="560003")
    account = _account(db, "has-phone@test.com", phone="9000000008")
    job = _job(db)

    _run(job, [account.id], address_id=address.id)

    assert ordering[0]["address"].phone == "9000000008"


def test_the_address_phone_is_the_fallback_when_the_account_has_none(db, ordering):
    address = _address(db, "with-phone", phone="9888888888", pincode="560003")
    account = _account(db, "no-phone@test.com", phone=None)
    job = _job(db)

    _run(job, [account.id], address_id=address.id)

    assert ordering[0]["address"].phone == "9888888888"


def test_the_phone_is_empty_when_neither_has_one(db, ordering):
    address = _address(db, "no-phone", phone=None, pincode="560003")
    account = _account(db, "no-phone@test.com", phone=None)
    job = _job(db)

    _run(job, [account.id], address_id=address.id)

    assert ordering[0]["address"].phone == ""


# --- the composed area line -------------------------------------------------
@pytest.mark.parametrize(
    ("parts", "expected"),
    [
        (
            {"line1": "12 Dock Road", "floor": "2nd Floor", "city": "Bengaluru", "state": "Karnataka"},
            "12 Dock Road, 2nd Floor, Bengaluru, Karnataka",
        ),
        # Blanks are dropped, so the line never carries a dangling comma.
        ({"line1": "12 Dock Road", "floor": None, "city": "Bengaluru", "state": None}, "12 Dock Road, Bengaluru"),
        ({"line1": "12 Dock Road", "floor": "", "city": "", "state": ""}, "12 Dock Road"),
        ({"line1": "12 Dock Road", "floor": "  ", "city": "Pune", "state": None}, "12 Dock Road, Pune"),
        ({"line1": " 12 Dock Road ", "floor": None, "city": None, "state": None}, "12 Dock Road"),
    ],
)
def test_the_area_line_joins_the_filled_parts_only(db, ordering, parts, expected):
    address = _address(db, "composed", pincode="560004", **parts)
    account = _account(db, phone="9000000009")
    job = _job(db)

    _run(job, [account.id], address_id=address.id)

    area = ordering[0]["address"].area
    assert area == expected
    assert ", ," not in area
    assert not area.endswith(",")


# --- resolved once per run, not per account ---------------------------------
def test_every_account_in_a_batch_ships_to_the_same_place(db, ordering):
    """A batch split across two addresses would be the quiet failure again."""
    _gst(db, "BLAZECOM", default=True)
    address = _address(db, "one-place", name="One Place", pincode="560005")
    first = _account(db, "a@test.com", phone="9000000010")
    second = _account(db, "b@test.com", phone="9000000011")
    job = _job(db)

    _run(job, [first.id, second.id], address_id=address.id)

    assert len(ordering) == 2
    assert {call["address"].name for call in ordering} == {"One Place"}
    assert {call["address"].pin for call in ordering} == {"560005"}
    assert {o.gst_name for o in db.query(Order).all()} == {"Blazecom"}


# --- the API --------------------------------------------------------------
class _Recorder:
    """Stand-in for ``run_order_job``, recording the call synchronously.

    ``_spawn`` builds the coroutine inside the request, so an assertion made
    after the response never races a background task.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})

        async def _noop() -> None:
            return None

        return _noop()


@pytest.fixture
def api():
    """The real jobs router on a local app — app/main.py is not ours."""
    application = FastAPI()
    application.include_router(jobs_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def task_recorder(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(jobs_api, "run_order_job", recorder)
    return recorder


def test_posting_an_unknown_address_id_is_a_422_and_starts_nothing(
    db, api, task_recorder
):
    _account(db, phone="9000000012")

    response = api.post(
        "/jobs/order",
        data={"product_link": PRODUCT, "payment_mode": "cod", "target": "18250", "address_id": "4242"},
    )

    assert response.status_code == 422
    assert "address_id 4242" in response.json()["detail"]
    assert task_recorder.calls == []
    assert db.query(Job).count() == 0  # not even a queued row


def test_posting_an_unknown_gst_id_is_a_422_naming_it(db, api, task_recorder):
    _account(db, phone="9000000013")

    response = api.post(
        "/jobs/order",
        data={"product_link": PRODUCT, "payment_mode": "cod", "target": "18250", "gst_id": "9191"},
    )

    assert response.status_code == 422
    assert "gst_id 9191" in response.json()["detail"]
    assert task_recorder.calls == []
    assert db.query(Job).count() == 0


def test_posting_a_chosen_address_and_gst_passes_them_through_and_records_them(
    db, api, task_recorder
):
    gst = _gst(db, "JPMOBILITY", gstin="29JPMOBILITY1Z", name="JP Mobility")
    address = _address(db, "jp-hub", name="JP Hub", pincode="560006", gst_id=gst.id)
    account = _account(db, phone="9000000014")

    response = api.post(
        "/jobs/order",
        data={
            "product_link": PRODUCT,
            "payment_mode": "cod",
            "target": "18250",
            "address_id": str(address.id),
            "gst_id": str(gst.id),
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["payload"]["address_id"] == address.id
    assert body["payload"]["gst_id"] == gst.id

    kwargs = task_recorder.calls[0]["kwargs"]
    assert kwargs["address_id"] == address.id
    assert kwargs["gst_id"] == gst.id
    assert task_recorder.calls[0]["args"] == (body["id"], [account.id])

    # Persisted, so a historical run still says where it shipped.
    job = db.get(Job, body["id"])
    assert (job.payload["address_id"], job.payload["gst_id"]) == (address.id, gst.id)


def test_an_order_post_without_a_chosen_pair_uses_the_defaults_and_records_nothing(
    db, api, task_recorder
):
    """No ids in the request: the job resolves the default rows itself, and the
    payload records no choice — a stored null would read as a deliberate one."""
    _address(db, "hq", name="HQ", pincode="560001", default=True)
    _account(db, phone="9000000015")

    response = api.post(
        "/jobs/order", data={"product_link": PRODUCT, "payment_mode": "cod", "target": "18250"}
    )

    assert response.status_code == 201
    kwargs = task_recorder.calls[0]["kwargs"]
    assert kwargs["address_id"] is None
    assert kwargs["gst_id"] is None

    payload = response.json()["payload"]
    assert "address_id" not in payload
    assert "gst_id" not in payload


def test_an_order_post_with_no_address_on_file_is_a_422_and_starts_nothing(
    db, api, task_recorder
):
    """Said at the door, before a job row exists — not as a run that fails."""
    _account(db, phone="9000000019")

    response = api.post(
        "/jobs/order", data={"product_link": PRODUCT, "payment_mode": "cod", "target": "18250"}
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "No delivery address" in detail
    assert "Address Book" in detail
    assert task_recorder.calls == []
    assert db.query(Job).count() == 0


def test_an_unflagged_address_does_not_satisfy_the_check(db, api, task_recorder):
    _address(db, "spare", name="Spare Place", pincode="560001", default=False)
    _account(db, phone="9000000020")

    response = api.post(
        "/jobs/order", data={"product_link": PRODUCT, "payment_mode": "cod", "target": "18250"}
    )

    assert response.status_code == 422
    assert task_recorder.calls == []


def test_an_unflagged_address_chosen_by_id_is_fine(db, api, task_recorder):
    address = _address(db, "spare", name="Spare Place", pincode="560001", default=False)
    _account(db, phone="9000000022")

    response = api.post(
        "/jobs/order",
        data={"product_link": PRODUCT, "payment_mode": "cod", "target": "18250", "address_id": str(address.id)},
    )

    assert response.status_code == 201
    assert task_recorder.calls[0]["kwargs"]["address_id"] == address.id
