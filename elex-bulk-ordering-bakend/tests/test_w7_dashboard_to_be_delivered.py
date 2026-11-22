"""The dashboard's "To be delivered" card to spec (FR-040/041), as the review
of 27 Oct 2026 found it.

One model is one row however its title was spelt. A run records the
operator's words, an order report the sheet's, and an order handed in on the
order-id sheet carries no model or brand at all until a read-back reads one —
so the same phone arrived as "T5 Lite" under vivo, " t5 lite " under no brand
and "T5  LITE" under Vivo, each a row of its own. Two brands selling a model
of one name stay two rows, and an order naming the model but no brand joins
the brand only when one brand carries that name. Orders that name no model
are one row, whatever their brand, which the card calls "Model not known yet".

"Ordered 15+ days ago" counts orders placed more than fifteen days before
today, India time (FR-041): whole calendar days, so the minute the dashboard is
opened never splits one day's orders between two answers.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import stats as stats_api
from app.core import order_filter
from app.core.order_filter import OrderFilter
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Order, OrderStatus

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(stats_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def account(db) -> Account:
    row = Account(email="tbd-w7@test.com", status=AccountStatus.new)
    db.add(row)
    db.commit()
    return row


def _ist(day: int, hour: int, minute: int) -> datetime:
    """A wall-clock moment in India in October 2026, as the stored (naive UTC) clock."""
    return datetime(2026, 10, day, hour, minute, tzinfo=IST).astimezone(timezone.utc).replace(tzinfo=None)


def _placed(db, account, number: int, *, brand, model, qty=1, placed_at=None) -> Order:
    order = Order(
        account_id=account.id,
        status=OrderStatus.placed,
        brand=brand,
        model=model,
        qty=qty,
        order_id=f"OD{number:03d}",
        placed_at=placed_at or datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=number),
    )
    db.add(order)
    db.commit()
    return order


# --- FR-040: one row per model, however it was spelt -----------------------------
def test_one_model_is_one_row_whatever_its_case_spacing_or_brand(api, db, account):
    # The same phone, three ways: a run's own words, an order handed in by id
    # whose model was filled in later (no brand), and an order report's.
    first = _placed(db, account, 20, brand="vivo", model="T5 Lite", qty=2)
    _placed(db, account, 5, brand=None, model=" t5 lite ")
    _placed(db, account, 3, brand="Vivo", model="T5  LITE")
    _placed(db, account, 4, brand="HP", model="Backpack")
    # Three orders that name no model: one from the id sheet, one a run placed
    # without a model, one whose report cell was blank.
    _placed(db, account, 6, brand=None, model=None)
    _placed(db, account, 7, brand="Samsung", model=None)
    _placed(db, account, 8, brand=None, model="   ")

    out = api.get("/stats/to-be-delivered").json()

    assert [(m["brand"], m["model"], m["units"], m["orders"]) for m in out["models"]] == [
        # The spelling and the brand with the most units name the row.
        ("vivo", "T5 Lite", 4, 3),
        # One row for every order that names no model, whatever its brand.
        (None, None, 3, 3),
        ("HP", "Backpack", 1, 1),
    ]
    assert (out["total_units"], out["total_orders"]) == (8, 7)
    # The row's oldest order is the oldest of all three spellings.
    phone = out["models"][0]
    assert datetime.fromisoformat(phone["oldest_ordered_at"]) == first.placed_at
    # SC-004: merging rows neither loses nor doubles a unit — still the
    # ledger's own count under the card's filter.
    same_filter = OrderFilter(statuses=(OrderStatus.placed,), delivery_states=("in_transit", "unknown"))
    assert out["total_units"] == order_filter.count(db, same_filter) == 8


def test_two_brands_of_one_model_name_stay_two_rows(api, db, account):
    # POCO and realme both sell a C75: merging them would count one phone's
    # units under the other's name (checker of 27 Oct 2026).
    _placed(db, account, 3, brand="POCO", model="C75", qty=2)
    _placed(db, account, 4, brand="realme", model="C75")
    # Handed in by id, the name read off the page, no brand: whose it is cannot
    # be told, so it is a row of its own rather than a guess.
    _placed(db, account, 5, brand=None, model="c75")

    out = api.get("/stats/to-be-delivered").json()

    assert [(m["brand"], m["model"], m["units"]) for m in out["models"]] == [
        ("POCO", "C75", 2),
        ("realme", "C75", 1),
        (None, "c75", 1),
    ]
    assert out["total_units"] == 4


def test_the_older_view_merges_the_same_way(api, db, account):
    _placed(db, account, 30, brand="vivo", model="T5 Lite")
    _placed(db, account, 40, brand=None, model="t5 LITE")
    _placed(db, account, 2, brand="vivo", model="T5 Lite")  # too recent for the older view

    older = api.get("/stats/to-be-delivered", params={"older_than_days": 15}).json()

    assert [(m["brand"], m["model"], m["units"], m["orders"]) for m in older["models"]] == [("vivo", "T5 Lite", 2, 2)]
    assert (older["total_units"], older["total_orders"]) == (2, 2)


# --- FR-041: "more than 15 days before today", by the calendar ---------------------
@pytest.mark.parametrize(
    "clock",
    [
        (0, 5),  # just after midnight in India: still the 26th in UTC
        (12, 0),
        (23, 55),
    ],
    ids=["00:05 IST", "12:00 IST", "23:55 IST"],
)
def test_a_day_is_in_or_out_whole_whatever_the_clock(api, db, account, monkeypatch, clock):
    hour, minute = clock
    # Today is 27 Oct 2026 in India, at the clock time under test.
    now = datetime(2026, 10, 27, hour, minute, tzinfo=IST).astimezone(timezone.utc)
    monkeypatch.setattr(stats_api, "utcnow", lambda: now)

    # 12 Oct is exactly fifteen days before the 27th — not more than fifteen.
    _placed(db, account, 1, brand="vivo", model="12 Oct, 00:05", placed_at=_ist(12, 0, 5))
    _placed(db, account, 2, brand="vivo", model="12 Oct, 23:55", placed_at=_ist(12, 23, 55))
    # 11 Oct is sixteen days before it.
    _placed(db, account, 3, brand="vivo", model="11 Oct, 00:05", placed_at=_ist(11, 0, 5))
    _placed(db, account, 4, brand="vivo", model="11 Oct, 23:55", placed_at=_ist(11, 23, 55))
    # A date with no time, as an order report files one: exactly midnight in
    # India, the boundary itself (checker of 27 Oct 2026).
    _placed(db, account, 5, brand="vivo", model="12 Oct, date only", placed_at=_ist(12, 0, 0))
    _placed(db, account, 6, brand="vivo", model="11 Oct, date only", placed_at=_ist(11, 0, 0))

    older = api.get("/stats/to-be-delivered", params={"older_than_days": 15}).json()
    counted = sorted(m["model"] for m in older["models"])

    # The orders of one day are treated alike, at any hour of today.
    assert counted == ["11 Oct, 00:05", "11 Oct, 23:55", "11 Oct, date only"]
    assert older["total_units"] == 3
    # And without the toggle every one of them is still to be delivered.
    assert api.get("/stats/to-be-delivered").json()["total_units"] == 6
