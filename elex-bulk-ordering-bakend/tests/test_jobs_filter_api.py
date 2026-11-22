"""Starting a run from a *filter* rather than a list of ids (ROADMAP §0).

The portal holds 37,062 accounts, so "run on everything matching this" must
travel as a filter and be resolved server-side. These tests pin the three
properties that makes safe:

* the filter selects exactly the accounts the operator was looking at, and
  those ids are what the engine is handed;
* resolution order is the same on all three start endpoints (filter, then
  ids, then everything);
* a selection over ``max_accounts_per_run`` is refused *before* anything
  starts — one mis-click must not launch 37,000 browser runs.

Nothing here starts a browser: the three engine entry points are imported into
``app.api.jobs`` as plain names, so they are monkeypatched with a recorder.
"""

from __future__ import annotations

import io
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import select

from app.api import jobs as jobs_api
from app.config import get_settings
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Address, Job, JobType, Tag

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# 16 digits, so a substring scan over JSON cannot collide with a timestamp.
CARD_NUMBER = "4111111111111111"
CARD_CVV = "321"


# --- doubles ------------------------------------------------------------------
async def _noop() -> None:
    return None


class _Recorder:
    """Stand-in for a jobs-engine task (``run_login_job`` & friends).

    Records the call *synchronously*: ``_spawn`` builds the coroutine inside the
    request, so an assertion never races the background task.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        return _noop()


# --- fixtures -----------------------------------------------------------------
@pytest.fixture
def api_app() -> FastAPI:
    application = FastAPI()
    application.include_router(jobs_api.router)
    return application


@pytest.fixture
def api(api_app: FastAPI) -> TestClient:
    token = create_access_token("tester", role="admin")
    return TestClient(api_app, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture(autouse=True)
def _address_book(db):
    """``POST /jobs/order`` refuses a run with no Address Book address; one
    default is on file so these tests can be about the filter."""
    db.add(Address(label="hq", name="HQ", line1="1 Main Road", pincode="560001", is_default=True))
    db.commit()


class _Fixture:
    """The seeded accounts, by the name each test refers to them by."""

    def __init__(self, rows: dict[str, Account], tags: dict[str, Tag]) -> None:
        self.rows = rows
        self.tags = tags

    def id(self, *names: str) -> list[int]:
        return [self.rows[name].id for name in names]


@pytest.fixture
def seeded(db) -> _Fixture:
    """Four accounts spanning every filterable column, so one filter per column.

    Deliberately small: what is under test is which rows a filter picks, not
    paging. The ceiling tests lower the limit instead of inserting 5,001 rows.
    """
    verified = Tag(name="VERIFIED")
    cold = Tag(name="COLD")
    db.add_all([verified, cold])
    db.commit()

    rows = {
        # valid + gold + VERIFIED
        "gold_a": Account(
            email="gold-a@test.com",
            phone="9000000001",
            status=AccountStatus.session_valid,
            subscription_type="ACTIVE-PREMIUM-GOLD",
            tag_id=verified.id,
        ),
        # valid + gold + COLD
        "gold_b": Account(
            email="gold-b@test.com",
            phone="9000000002",
            status=AccountStatus.session_valid,
            subscription_type="ACTIVE-PREMIUM-GOLD",
            tag_id=cold.id,
        ),
        # blocked + no subscription + untagged
        "blocked": Account(
            email="blocked@test.com",
            phone="9000000003",
            status=AccountStatus.blocked,
            tag_id=None,
        ),
        # new + plus + VERIFIED, and a different email domain for q= tests
        "plus": Account(
            email="plus@other.com",
            phone="8000000004",
            status=AccountStatus.new,
            subscription_type="PLUS",
            tag_id=verified.id,
        ),
    }
    db.add_all(list(rows.values()))
    db.commit()
    for row in rows.values():
        db.refresh(row)
    return _Fixture(rows, {"verified": verified, "cold": cold})


@pytest.fixture
def login(monkeypatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(jobs_api, "run_login_job", recorder)
    return recorder


@pytest.fixture
def coupon(monkeypatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(jobs_api, "run_coupon_job", recorder)
    return recorder


@pytest.fixture
def order(monkeypatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(jobs_api, "run_order_job", recorder)
    return recorder


@pytest.fixture
def ceiling(monkeypatch):
    """Lower ``max_accounts_per_run`` for one test.

    Patching the cached Settings object exercises the real config path (the
    endpoint reads ``get_settings().max_accounts_per_run``), and monkeypatch
    puts the value back afterwards.
    """

    def _set(limit: int) -> None:
        monkeypatch.setattr(get_settings(), "max_accounts_per_run", limit)

    return _set


def _cards_xlsx(emails: list[str]) -> bytes:
    wb = Workbook()
    sheet = wb.active
    sheet.append(["Email", "Card Number", "Exp Month", "Exp Year", "CVV"])
    for email in emails:
        sheet.append([email, CARD_NUMBER, "12", "28", CARD_CVV])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _order_form(**extra: object) -> dict:
    """Minimal COD order form — COD keeps the card file out of filter tests. A
    run states its price cap (audit F-23), so the form carries one."""
    return {
        "product_link": "https://www.flipkart.com/some-item/p/itm123",
        "payment_mode": "cod",
        "target": "1000",
        **extra,
    }


# --- a filter selects exactly the right accounts ------------------------------
def test_login_filter_selects_exactly_the_matching_accounts(
    api, db, seeded, login
) -> None:
    response = api.post(
        "/jobs/login",
        json={"account_filter": {"statuses": ["session_valid"]}, "force": True},
    )
    assert response.status_code == 201, response.text

    expected = seeded.id("gold_a", "gold_b")
    body = response.json()
    assert body["total_items"] == 2
    # The task is handed the resolved ids, not the filter.
    assert login.calls == [{"args": (body["id"], expected), "kwargs": {"force": True}}]
    assert db.get(Job, body["id"]).total_items == 2


@pytest.mark.parametrize(
    ("filter_body", "expected"),
    [
        # one per filterable column, so a dropped clause cannot pass silently
        ({"q": "other.com"}, ("plus",)),
        ({"q": "8000000004"}, ("plus",)),
        ({"statuses": ["blocked", "new"]}, ("blocked", "plus")),
        ({"subscriptions": ["PLUS"]}, ("plus",)),
        ({"include_untagged": True}, ("blocked",)),
        # tag_ids OR include_untagged — "Untagged + COLD" is one selection
        ({"tag_ids": ["cold"], "include_untagged": True}, ("gold_b", "blocked")),
        ({"tag_ids": ["verified"]}, ("gold_a", "plus")),
        # AND across columns
        ({"statuses": ["session_valid"], "tag_ids": ["cold"]}, ("gold_b",)),
        # ids that no longer exist are dropped, not rejected
        ({"ids": ["gold_a", 999999]}, ("gold_a",)),
    ],
)
def test_every_filter_field_narrows_the_run(
    api, seeded, coupon, filter_body: dict, expected: tuple[str, ...]
) -> None:
    """Names in the parameters above are resolved to real ids here."""
    body = dict(filter_body)
    if "tag_ids" in body:
        body["tag_ids"] = [seeded.tags[name].id for name in body["tag_ids"]]
    if "ids" in body:
        body["ids"] = [
            seeded.rows[i].id if isinstance(i, str) else i for i in body["ids"]
        ]

    response = api.post(
        "/jobs/coupon", json={"account_filter": body, "coupon_code": "SAVE10"}
    )
    assert response.status_code == 201, response.text
    assert coupon.calls[0]["args"][1] == seeded.id(*expected)


def test_order_takes_the_filter_as_a_json_form_field(api, seeded, order) -> None:
    """``/jobs/order`` is multipart, so the filter arrives as a JSON string."""
    response = api.post(
        "/jobs/order",
        data=_order_form(
            account_filter=json.dumps({"subscriptions": ["ACTIVE-PREMIUM-GOLD"]}),
        ),
    )
    assert response.status_code == 201, response.text

    body = response.json()
    assert body["total_items"] == 2
    assert order.calls[0]["args"] == (body["id"], seeded.id("gold_a", "gold_b"))
    assert order.calls[0]["kwargs"]["cards"] is None


# --- resolution order: filter, then ids, then everything ----------------------
def test_account_ids_still_select_a_subset(api, seeded, login) -> None:
    chosen = seeded.id("blocked")
    response = api.post("/jobs/login", json={"account_ids": chosen})
    assert response.status_code == 201, response.text
    assert response.json()["total_items"] == 1
    assert login.calls[0]["args"][1] == chosen


@pytest.mark.parametrize("body", [{}, {"account_ids": None, "account_filter": None}])
def test_neither_filter_nor_ids_means_every_account(api, seeded, login, body) -> None:
    response = api.post("/jobs/login", json=body)
    assert response.status_code == 201, response.text
    assert login.calls[0]["args"][1] == seeded.id("gold_a", "gold_b", "blocked", "plus")


def test_an_empty_filter_object_also_means_every_account(api, seeded, login) -> None:
    """A filter that narrows nothing selects everything — same as the table."""
    response = api.post("/jobs/login", json={"account_filter": {}})
    assert response.status_code == 201, response.text
    assert len(login.calls[0]["args"][1]) == 4


def test_the_filter_wins_when_both_are_supplied(api, seeded, coupon, order) -> None:
    """One resolution order, stated once: a filter beats an explicit id list."""
    response = api.post(
        "/jobs/coupon",
        json={
            "account_ids": seeded.id("plus"),
            "account_filter": {"statuses": ["session_valid"]},
            "coupon_code": "SAVE10",
        },
    )
    assert response.status_code == 201, response.text
    assert coupon.calls[0]["args"][1] == seeded.id("gold_a", "gold_b")

    # ...and identically on the multipart endpoint.
    placed = api.post(
        "/jobs/order",
        data=_order_form(
            account_ids=",".join(str(i) for i in seeded.id("plus")),
            account_filter=json.dumps({"statuses": ["session_valid"]}),
        ),
    )
    assert placed.status_code == 201, placed.text
    assert order.calls[0]["args"][1] == seeded.id("gold_a", "gold_b")


# --- the ceiling --------------------------------------------------------------
def test_over_the_ceiling_is_a_422_naming_the_count_and_the_limit(
    api, db, seeded, login, ceiling
) -> None:
    ceiling(2)

    response = api.post("/jobs/login", json={})  # all four accounts
    assert response.status_code == 422

    detail = response.json()["detail"]
    assert "4" in detail and "2" in detail, detail
    # Nothing started, and no job row was left behind to look queued forever.
    assert login.calls == []
    assert db.scalars(select(Job)).all() == []


@pytest.mark.parametrize("job", ["login", "coupon", "order"])
def test_the_ceiling_applies_to_every_start_endpoint(
    api, seeded, login, coupon, order, ceiling, job: str
) -> None:
    ceiling(1)
    recorders = {"login": login, "coupon": coupon, "order": order}

    if job == "order":
        response = api.post("/jobs/order", data=_order_form())
    else:
        response = api.post(f"/jobs/{job}", json={"coupon_code": "SAVE10"})

    assert response.status_code == 422, response.text
    assert recorders[job].calls == []


def test_a_selection_exactly_at_the_ceiling_is_allowed(api, seeded, login, ceiling):
    """The limit is inclusive — cap+1 rows are fetched only to spot an overflow."""
    ceiling(2)

    response = api.post(
        "/jobs/login", json={"account_filter": {"statuses": ["session_valid"]}}
    )
    assert response.status_code == 201, response.text
    assert login.calls[0]["args"][1] == seeded.id("gold_a", "gold_b")


# --- nothing matched ----------------------------------------------------------
@pytest.mark.parametrize(
    "filter_body",
    [
        {"q": "nobody@nowhere.test"},
        {"statuses": ["not_registered"]},
        {"ids": [999999]},
    ],
)
def test_a_filter_that_matches_nothing_is_a_400(
    api, db, seeded, login, filter_body: dict
) -> None:
    response = api.post("/jobs/login", json={"account_filter": filter_body})
    assert response.status_code == 400
    assert login.calls == []
    assert db.scalars(select(Job)).all() == []


def test_an_empty_database_is_still_a_400(api, db, login) -> None:
    assert api.post("/jobs/login", json={}).status_code == 400
    assert login.calls == []


# --- malformed filters --------------------------------------------------------
@pytest.mark.parametrize(
    "raw",
    [
        "{not json",
        "[]",
        '"session_valid"',
        '{"statuses": ["nonsense"]}',
        '{"statuses_typo": ["blocked"]}',  # extra="forbid": a typo must be loud
    ],
)
def test_order_rejects_a_malformed_filter_with_a_422(
    api, db, seeded, order, raw: str
) -> None:
    """A bad filter must never degrade into "no filter" — that means all 37k."""
    response = api.post("/jobs/order", data=_order_form(account_filter=raw))
    assert response.status_code == 422, response.text
    assert order.calls == []
    assert db.scalars(select(Job)).all() == []


@pytest.mark.parametrize("raw", ["", "   "])
def test_an_absent_filter_form_field_is_not_an_error(api, seeded, order, raw) -> None:
    """Blank means "not given", because a form always sends the field."""
    response = api.post("/jobs/order", data=_order_form(account_filter=raw))
    assert response.status_code == 201, response.text
    assert len(order.calls[0]["args"][1]) == 4


def test_a_malformed_filter_is_a_422_on_the_json_endpoints_too(
    api, seeded, login
) -> None:
    response = api.post(
        "/jobs/login", json={"account_filter": {"statuses": ["nonsense"]}}
    )
    assert response.status_code == 422
    assert login.calls == []


# --- provenance in Job.payload ------------------------------------------------
def test_payload_records_the_filter_and_the_count(api, db, seeded, coupon) -> None:
    """A historical run must still say what it covered, in non-sensitive terms."""
    filter_body = {
        "statuses": ["session_valid"],
        "subscriptions": ["ACTIVE-PREMIUM-GOLD"],
    }
    response = api.post(
        "/jobs/coupon", json={"account_filter": filter_body, "coupon_code": "SAVE10"}
    )
    assert response.status_code == 201, response.text

    body = response.json()
    assert body["payload"] == {
        "coupon_code": "SAVE10",
        "account_filter": filter_body,
        "account_count": 2,
        "started_by": "tester",
    }
    assert db.get(Job, body["id"]).payload == body["payload"]


def test_payload_records_how_the_selection_was_made(api, db, seeded, login) -> None:
    """Ids and "everything" are recorded too, so the key is always present."""
    from app.db.models import Job, JobStatus

    chosen = seeded.id("plus")
    by_ids = api.post("/jobs/login", json={"account_ids": chosen}).json()
    assert by_ids["payload"] == {
        "force": False,
        "account_filter": {"ids": chosen},
        "account_count": 1,
        "started_by": "tester",
    }

    # The stub never finishes a run, and a second login run is refused while
    # one is in progress — so the first is marked done before the next starts.
    db.get(Job, by_ids["id"]).status = JobStatus.completed
    db.commit()
    everything = api.post("/jobs/login", json={}).json()
    assert everything["payload"] == {
        "force": False,
        "account_filter": None,
        "account_count": 4,
        "started_by": "tester",
    }


def test_a_filtered_order_run_records_the_filter_but_no_card_data(
    api, db, seeded, order
) -> None:
    """PLAN.md §5 is unchanged by provenance: the filter goes in, cards do not."""
    emails = [seeded.rows[name].email for name in ("gold_a", "gold_b")]
    response = api.post(
        "/jobs/order",
        data={
            "product_link": "https://www.flipkart.com/some-item/p/itm123",
            "qty": "2",
            "target": "1000.5",
            "account_filter": json.dumps({"statuses": ["session_valid"]}),
        },
        files={"cards_file": ("cards.xlsx", _cards_xlsx(emails), _XLSX_MIME)},
    )
    assert response.status_code == 201, response.text

    body = response.json()
    assert body["payload"] == {
        "product_link": "https://www.flipkart.com/some-item/p/itm123",
        "qty": 2,
        "target": 1000.5,
        "payment_mode": "card",
        "card_rows_skipped": [],
        "account_filter": {"statuses": ["session_valid"]},
        "account_count": 2,
        "started_by": "tester",
    }
    assert CARD_NUMBER not in response.text
    assert CARD_CVV not in response.text

    job = db.get(Job, body["id"])
    assert job.type is JobType.order
    persisted = json.dumps({"payload": job.payload, "log": job.log}, default=str)
    assert CARD_NUMBER not in persisted
    assert CARD_CVV not in persisted

    # The cards still reached the task in memory, for the filtered accounts only.
    cards = order.calls[0]["kwargs"]["cards"]
    assert set(cards) == set(seeded.id("gold_a", "gold_b"))
    assert cards[seeded.rows["gold_a"].id].number == CARD_NUMBER
