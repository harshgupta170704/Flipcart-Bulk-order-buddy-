"""Upper bounds on what a request may carry (audit F-40, 25 Oct 2026).

Unbounded id lists and page numbers, and free text longer than its column,
used to end as 500s: SQLite cannot bind an id past 64 bits or more than
32,766 values a statement, and PostgreSQL refuses a value wider than its
column. Each is now a 422 — or, in the order-id sheet, a row refused by name —
before anything starts or is written.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import accounts as accounts_api
from app.api import jobs as jobs_api
from app.api import orders as orders_api
from app.api import sync as sync_api
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Address, Job, Order, OrderStatus, OrderUnit, Tag, utcnow
from app.io import order_ids
from app.schemas import account as account_schema
from app.schemas import order as order_schema

TOO_LARGE_ID = 2**31  # one past what the database binds
CAP = account_schema.MAX_LISTED_IDS


async def _noop() -> None:
    return None


class _Recorder:
    """Stands in for a run: records the call, starts nothing."""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return _noop()


@pytest.fixture
def runs(monkeypatch) -> dict[str, _Recorder]:
    recorders = {name: _Recorder() for name in ("login", "coupon", "order", "sync")}
    monkeypatch.setattr(jobs_api, "run_login_job", recorders["login"])
    monkeypatch.setattr(jobs_api, "run_coupon_job", recorders["coupon"])
    monkeypatch.setattr(jobs_api, "run_order_job", recorders["order"])
    monkeypatch.setattr(sync_api, "run_sync_job", recorders["sync"])
    return recorders


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    for module in (jobs_api, sync_api, accounts_api, orders_api):
        application.include_router(module.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def account(db) -> Account:
    # An order run refuses to start without an Address Book address.
    db.add(Address(label="hq", name="HQ", line1="1 Main Road", pincode="560001", is_default=True))
    row = Account(email="bound@test.com", phone="9000000001", status=AccountStatus.session_valid)
    db.add(row)
    db.commit()
    return row


def _start(api: TestClient, run: str, *, ids: list[int] | None = None, account_filter: dict | None = None):
    """Start ``run`` with an id list or a filter, the way its client sends it."""
    if run == "order":  # multipart, so the filter travels as JSON text
        data = {"product_link": "https://www.flipkart.com/item/p/itm1", "payment_mode": "cod", "target": "1000"}
        if ids is not None:
            data["account_ids"] = ",".join(str(i) for i in ids)
        if account_filter is not None:
            data["account_filter"] = json.dumps(account_filter)
        return api.post("/jobs/order", data=data)
    body: dict = {"coupon_code": "SAVE10"} if run == "coupon" else {}
    if ids is not None:
        body["account_ids"] = ids
    if account_filter is not None:
        body["account_filter"] = account_filter
    return api.post(f"/jobs/{run}", json=body)


# --- the bounds mirror the columns ------------------------------------------------------------
def test_the_text_bounds_are_the_column_widths():
    assert account_schema.MAX_PHONE_LENGTH == Account.__table__.c.phone.type.length
    assert account_schema.MAX_SUBSCRIPTION_LENGTH == Account.__table__.c.subscription_type.type.length
    assert order_schema.MAX_ORDER_ID_LENGTH == Order.__table__.c.order_id.type.length
    assert order_ids.MAX_ID_LENGTH == Order.__table__.c.order_id.type.length
    assert order_ids.MAX_ID_LENGTH == OrderUnit.__table__.c.unit_id.type.length


def test_the_id_and_page_bounds_are_the_ones_the_orders_api_uses():
    assert account_schema.MAX_ID == orders_api.MAX_ID
    assert accounts_api.MAX_PAGE == orders_api.MAX_PAGE
    assert account_schema.MAX_LISTED_IDS == order_ids.MAX_ROWS  # the id sheet's cap


# --- the job-start id lists ---------------------------------------------------------------------
@pytest.mark.parametrize("run", ["login", "coupon", "sync", "order"])
@pytest.mark.parametrize(
    "selection",
    [
        pytest.param({"ids": list(range(1, CAP + 2))}, id="ids-over-the-cap"),
        pytest.param({"ids": [TOO_LARGE_ID]}, id="id-past-what-the-database-binds"),
        pytest.param({"ids": [0]}, id="id-zero"),
        pytest.param({"account_filter": {"ids": list(range(1, CAP + 2))}}, id="filter-ids-over-the-cap"),
        pytest.param({"account_filter": {"ids": [TOO_LARGE_ID]}}, id="filter-id-past-what-the-database-binds"),
        pytest.param({"account_filter": {"tag_ids": list(range(1, account_schema.MAX_LISTED_TAGS + 2))}}, id="filter-tags-over-the-cap"),
        pytest.param({"account_filter": {"tag_ids": [TOO_LARGE_ID]}}, id="filter-tag-past-what-the-database-binds"),
        pytest.param(
            {"account_filter": {"statuses": ["new"] * (len(AccountStatus) + 1)}}, id="filter-statuses-past-the-statuses"
        ),
        pytest.param(
            {"account_filter": {"subscriptions": ["Plus"] * (account_schema.MAX_LISTED_SUBSCRIPTIONS + 1)}},
            id="filter-subscriptions-over-the-cap",
        ),
        pytest.param({"account_filter": {"q": "x" * (account_schema.MAX_SEARCH_CHARS + 1)}}, id="filter-search-too-long"),
    ],
)
def test_an_id_list_past_its_bound_is_a_422_and_starts_nothing(api, db, account, runs, run, selection):
    response = _start(api, run, **selection)

    assert response.status_code == 422, response.text
    assert runs[run].calls == []
    assert db.scalars(select(Job)).all() == []


@pytest.mark.parametrize("run", ["login", "coupon", "sync", "order"])
def test_a_list_exactly_at_the_cap_still_runs(api, db, account, runs, run):
    """The cap is inclusive; ids that do not exist are dropped as before."""
    ids = [account.id] + [i for i in range(1, CAP + 1) if i != account.id][: CAP - 1]
    assert len(ids) == CAP

    response = _start(api, run, ids=ids)

    assert response.status_code == 201, response.text
    assert response.json()["total_items"] == 1
    assert len(runs[run].calls) == 1


def test_the_unit_limit_check_bounds_its_list_like_a_start():
    """The run's pre-flight takes the same selection a start does."""
    from app.api import unit_limits as unit_limits_api

    application = FastAPI()
    application.include_router(unit_limits_api.router)
    client = TestClient(application, headers={"Authorization": f"Bearer {create_access_token('tester', role='admin')}"})

    assert client.post("/unit-limits/check", json={"account_ids": list(range(1, CAP + 2))}).status_code == 422
    assert client.post("/unit-limits/check", json={"account_ids": [TOO_LARGE_ID]}).status_code == 422
    assert client.post("/unit-limits/check", json={"account_ids": [1, 2]}).status_code == 200


# --- page numbers ---------------------------------------------------------------------------------
def test_the_accounts_page_number_is_bounded(api, account):
    assert api.get("/accounts", params={"page": accounts_api.MAX_PAGE + 1}).status_code == 422
    assert api.get("/accounts", params={"page": 10**20}).status_code == 422  # was an OverflowError, a 500
    last = api.get("/accounts", params={"page": accounts_api.MAX_PAGE})
    assert last.status_code == 200, last.text
    assert (last.json()["items"], last.json()["total"]) == ([], 1)


def test_the_accounts_tag_filter_is_bounded(api, db, account):
    tag = Tag(name="VIP")
    db.add(tag)
    db.commit()
    assert api.get("/accounts", params={"tag_id": TOO_LARGE_ID}).status_code == 422  # was a 500
    too_many = [("tag_id", str(i)) for i in range(1, account_schema.MAX_LISTED_TAGS + 2)]
    assert api.get("/accounts", params=too_many).status_code == 422
    assert api.get("/accounts", params={"tag_id": tag.id}).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"account_ids": [TOO_LARGE_ID]}, id="ids"),
        pytest.param({"filter": {"ids": [TOO_LARGE_ID]}}, id="filter-ids"),
        pytest.param({"filter": {"tag_ids": [TOO_LARGE_ID]}}, id="filter-tags"),
    ],
)
def test_bulk_tagging_refuses_an_id_the_database_cannot_bind(api, account, body):
    assert api.post("/accounts/tag", json={"tag_id": None, **body}).status_code == 422  # was a 500


# --- free text against its column -------------------------------------------------------------
def test_an_account_phone_wider_than_its_column_is_a_422(api, db):
    width = account_schema.MAX_PHONE_LENGTH
    assert api.post("/accounts", json={"email": "wide@test.com", "phone": "9" * (width + 1)}).status_code == 422
    created = api.post("/accounts", json={"email": "fits@test.com", "phone": "9" * width})
    assert created.status_code == 201, created.text
    assert db.scalar(select(Account).where(Account.email == "wide@test.com")) is None


@pytest.mark.parametrize(
    ("field", "width"),
    [("phone", account_schema.MAX_PHONE_LENGTH), ("subscription_type", account_schema.MAX_SUBSCRIPTION_LENGTH)],
)
def test_an_account_edit_wider_than_its_column_is_a_422_and_changes_nothing(api, db, account, field, width):
    before = getattr(account, field)

    assert api.patch(f"/accounts/{account.id}", json={field: "X" * (width + 1)}).status_code == 422
    db.expire_all()
    assert getattr(db.get(Account, account.id), field) == before

    fits = api.patch(f"/accounts/{account.id}", json={field: "X" * width})
    assert fits.status_code == 200, fits.text
    assert fits.json()[field] == "X" * width


def test_a_settle_order_id_wider_than_its_column_is_a_422(api, db, account):
    row = Order(account_id=account.id, qty=1, status=OrderStatus.unconfirmed, placed_at=utcnow())
    db.add(row)
    db.commit()
    width = order_schema.MAX_ORDER_ID_LENGTH
    too_wide = "OD" + "1" * (width - 1)  # matches the OD-and-digits shape, one character too many

    response = api.post(f"/orders/{row.id}/settle", json={"status": "placed", "order_id": too_wide})

    assert response.status_code == 422
    db.expire_all()
    assert (db.get(Order, row.id).status, db.get(Order, row.id).order_id) == (OrderStatus.unconfirmed, None)
    fits = "OD" + "1" * (width - 2)
    settled = api.post(f"/orders/{row.id}/settle", json={"status": "placed", "order_id": fits})
    assert settled.status_code == 200, settled.text
    assert settled.json()["order_id"] == fits


def test_the_id_sheet_refuses_an_order_id_or_unit_id_wider_than_its_column_by_name():
    width = order_ids.MAX_ID_LENGTH
    text = (
        "Email,Order ID,Unit ID\n"
        f"a@x.com,OD{'1' * (width - 2)},UNIT1\n"  # exactly the width: kept
        f"a@x.com,OD{'2' * (width - 1)},UNIT2\n"  # one past it
        f"a@x.com,OD3333333333,{'U' * (width + 1)}\n"
    )

    sheet = order_ids.read_id_sheet(text.encode("utf-8"), "ids.csv")

    assert [row.order_id for row in sheet.rows] == [f"OD{'1' * (width - 2)}"]
    reasons = {r.row_number: r.reason for r in sheet.rejected}
    assert reasons[3] == f"order id is {width + 1} characters long; at most {width} fit"
    assert reasons[4] == f"unit id is {width + 1} characters long; at most {width} fit"


def test_the_id_sheet_upload_answers_wide_ids_row_by_row_and_imports_the_rest(api, db, account):
    width = order_ids.MAX_ID_LENGTH
    text = (
        "Email,Order ID,Unit ID\n"
        f"{account.email},OD1111111111,\n"
        f"{account.email},OD{'2' * (width - 1)},\n"
        f"{account.email},OD3333333333,{'U' * (width + 1)}\n"
    )

    response = api.post("/orders/import-ids", files={"file": ("ids.csv", text.encode("utf-8"), "text/csv")})

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["created"], body["order_ids"]) == (1, ["OD1111111111"])
    assert [(r["row"], r["reason"].split(" is ")[0]) for r in body["rejected"]] == [(3, "order id"), (4, "unit id")]
    db.expire_all()
    assert [o.order_id for o in db.query(Order).all()] == ["OD1111111111"]
