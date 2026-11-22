"""The change feed's API (app.api.changes): GET /changes lists what came after
the caller's own seen mark — the last 7 days without one, never more than 30
days back — newest first and paged; POST /changes/seen moves that mark, for
the caller alone."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import changes as changes_api
from app.core import users
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Order, OrderStatus, UnitEvent, User, UserRole, naive_utcnow


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(changes_api.router)
    return TestClient(application)


def _user(db, email: str, *, seen_at: datetime | None = None) -> dict:
    """A user row, and the bearer header a sign-in would give it."""
    row = User(email=email, name=email.split("@")[0], role=UserRole.operator, changes_seen_at=seen_at)
    db.add(row)
    db.commit()
    return {"Authorization": f"Bearer {users.issue_token(row)}"}


def _order(db, *, email: str = "phones@test.com", order_id: str = "OD100000000001", model: str = "vivo T5x 5G") -> Order:
    account = db.query(Account).filter(Account.email == email).one_or_none()
    if account is None:
        account = Account(email=email, status=AccountStatus.session_valid)
        db.add(account)
        db.commit()
    order = Order(account_id=account.id, qty=1, model=model, status=OrderStatus.placed, order_id=order_id)
    db.add(order)
    db.commit()
    return order


def _event(db, order: Order, *, at: datetime, kind: str = "delivered", before: str | None = "in_transit") -> UnitEvent:
    row = UnitEvent(
        unit_pk=order.units[0].id,
        order_pk=order.id,
        account_id=order.account_id,
        at=at,
        kind=kind,
        from_state=before,
        to_state=kind,
    )
    db.add(row)
    db.commit()
    return row


def _when(text: str) -> datetime:
    return datetime.fromisoformat(text)


# --- who may read it ---------------------------------------------------------------------
def test_the_feed_needs_a_token(api):
    assert api.get("/changes").status_code == 401
    assert api.post("/changes/seen").status_code == 401


def test_the_app_serves_the_feed(client):
    """Wired into the real app, behind its sign-in: refused, not missing."""
    assert client.get("/changes").status_code == 401
    assert client.post("/changes/seen").status_code == 401


# --- the window ----------------------------------------------------------------------------
def test_with_no_mark_the_feed_is_the_last_seven_days_newest_first(api, db):
    now = naive_utcnow()
    headers = _user(db, "ops@test.com")
    order = _order(db)
    older = _event(db, order, at=now - timedelta(days=2), kind="out_for_delivery")
    newer = _event(db, order, at=now - timedelta(hours=1), kind="delivered", before="out_for_delivery")
    _event(db, order, at=now - timedelta(days=8))  # before the window

    body = api.get("/changes", headers=headers).json()

    assert [item["id"] for item in body["items"]] == [newer.id, older.id]
    assert body["total"] == 2 and body["pages"] == 1
    assert body["seen_at"] is None and body["capped"] is False
    assert abs(_when(body["since"]) - (now - timedelta(days=7))) < timedelta(minutes=1)
    assert abs(_when(body["as_of"]) - now) < timedelta(minutes=1)
    first = body["items"][0]
    assert first["kind"] == "delivered" and first["from_state"] == "out_for_delivery" and first["to_state"] == "delivered"
    # What the dashboard names and links each change with.
    assert first["order_id"] == "OD100000000001"
    assert first["order_row_id"] == order.id
    assert first["unit_pk"] == order.units[0].id and first["unit_position"] == 1
    assert first["model"] == "vivo T5x 5G"
    assert first["account_email"] == "phones@test.com"
    assert abs(_when(first["at"]) - (now - timedelta(hours=1))) < timedelta(seconds=1)


def test_after_a_mark_only_what_came_after_it(api, db):
    now = naive_utcnow()
    headers = _user(db, "ops@test.com", seen_at=now - timedelta(hours=3))
    order = _order(db)
    fresh = _event(db, order, at=now - timedelta(hours=1))
    _event(db, order, at=now - timedelta(hours=5), kind="out_for_delivery")

    body = api.get("/changes", headers=headers).json()

    assert [item["id"] for item in body["items"]] == [fresh.id]
    assert _when(body["seen_at"]) == _when(body["since"])


def test_never_more_than_thirty_days_back(api, db):
    now = naive_utcnow()
    headers = _user(db, "away@test.com", seen_at=now - timedelta(days=60))
    order = _order(db)
    recent = _event(db, order, at=now - timedelta(days=10))
    _event(db, order, at=now - timedelta(days=40), kind="out_for_delivery")

    body = api.get("/changes", headers=headers).json()

    assert [item["id"] for item in body["items"]] == [recent.id]
    assert body["capped"] is True
    assert abs(_when(body["since"]) - (now - timedelta(days=30))) < timedelta(minutes=1)


def test_pages_and_the_counts_by_kind(api, db):
    now = naive_utcnow()
    headers = _user(db, "ops@test.com")
    order = _order(db)
    kinds = ["out_for_delivery", "delivered", "cancelled", "returned", "delivered"]
    made = [_event(db, order, at=now - timedelta(minutes=10 * (index + 1)), kind=kind) for index, kind in enumerate(kinds)]

    pages = [api.get("/changes", params={"page": page, "size": 2}, headers=headers).json() for page in (1, 2, 3)]

    assert [[item["id"] for item in page["items"]] for page in pages] == [
        [made[0].id, made[1].id],
        [made[2].id, made[3].id],
        [made[4].id],
    ]
    assert {(page["total"], page["pages"], page["size"]) for page in pages} == {(5, 3, 2)}
    assert pages[0]["by_kind"] == {"delivered": 2, "out_for_delivery": 1, "cancelled": 1, "returned": 1, "failed": 0}


def test_one_read_backs_changes_keep_a_steady_order(api, db):
    """One read-back stamps every change on an account with the same instant."""
    at = naive_utcnow() - timedelta(minutes=5)
    headers = _user(db, "ops@test.com")
    first = _event(db, _order(db, order_id="OD100000000001"), at=at)
    second = _event(db, _order(db, order_id="OD100000000002"), at=at)

    ids = [item["id"] for item in api.get("/changes", headers=headers).json()["items"]]

    assert ids == [second.id, first.id]


@pytest.mark.parametrize("params", [{"size": 0}, {"size": changes_api.MAX_SIZE + 1}, {"page": 0}])
def test_the_page_is_bounded(api, db, params):
    assert api.get("/changes", params=params, headers=_user(db, "ops@test.com")).status_code == 422


# --- marking seen ----------------------------------------------------------------------------
def test_marking_seen_empties_the_feed_until_the_next_change(api, db):
    headers = _user(db, "ops@test.com")
    order = _order(db)
    _event(db, order, at=naive_utcnow() - timedelta(hours=1))

    marked = api.post("/changes/seen", headers=headers)

    assert marked.status_code == 200
    assert abs(_when(marked.json()["seen_at"]) - naive_utcnow()) < timedelta(minutes=1)
    assert api.get("/changes", headers=headers).json()["total"] == 0

    later = _event(db, order, at=naive_utcnow() + timedelta(seconds=1), kind="returned", before="delivered")
    assert [item["id"] for item in api.get("/changes", headers=headers).json()["items"]] == [later.id]


def test_seen_is_per_user(api, db):
    one = _user(db, "one@test.com")
    two = _user(db, "two@test.com")
    _event(db, _order(db), at=naive_utcnow() - timedelta(hours=1))

    api.post("/changes/seen", headers=one)

    assert api.get("/changes", headers=one).json()["total"] == 0
    assert api.get("/changes", headers=two).json()["total"] == 1
    assert db.query(User).filter(User.email == "two@test.com").one().changes_seen_at is None


def test_a_change_recorded_after_the_page_loaded_stays_unseen(api, db):
    """The dashboard sends back the ``as_of`` of the list it showed, and the
    click can come long after that list was read. A change recorded in
    between stays unseen — even one older than the server's own now minus
    SEEN_GRACE, which only ``through`` keeps: a mark set up to now would hide
    it, so this fails if the server ignores ``through``."""
    headers = _user(db, "ops@test.com")
    order = _order(db)
    loaded = naive_utcnow() - timedelta(minutes=1)  # the list was read a minute ago
    _event(db, order, at=loaded - timedelta(minutes=10))  # it was on that list
    unseen = _event(db, order, at=loaded + timedelta(seconds=30), kind="returned", before="delivered")

    api.post("/changes/seen", json={"through": loaded.isoformat()}, headers=headers)

    assert [item["id"] for item in api.get("/changes", headers=headers).json()["items"]] == [unseen.id]


def test_the_mark_never_moves_back_nor_past_now(api, db):
    now = naive_utcnow()
    headers = _user(db, "ops@test.com", seen_at=now - timedelta(hours=1))

    earlier = api.post("/changes/seen", json={"through": (now - timedelta(hours=2)).isoformat()}, headers=headers)
    assert abs(_when(earlier.json()["seen_at"]) - (now - timedelta(hours=1))) < timedelta(seconds=1)

    future = api.post("/changes/seen", json={"through": (now + timedelta(days=2)).isoformat()}, headers=headers)
    assert abs(_when(future.json()["seen_at"]) - naive_utcnow()) < timedelta(minutes=1)


def test_the_mark_reads_a_zoned_stamp_as_utc(api, db):
    headers = _user(db, "ops@test.com")
    through = (naive_utcnow() - timedelta(hours=1)).replace(microsecond=0)
    ist = through + timedelta(hours=5, minutes=30)

    body = api.post("/changes/seen", json={"through": f"{ist.isoformat()}+05:30"}, headers=headers).json()

    assert _when(body["seen_at"]) == through - changes_api.SEEN_GRACE


def test_a_change_committed_just_after_the_list_was_read_shows_again(api, db):
    """A read-back stamps a change when it sees it and commits it a moment
    later: one stamped just before the list's moment, but not yet committed
    when the list was read, was never shown. Marking the list seen must not
    hide it for good."""
    headers = _user(db, "ops@test.com")
    order = _order(db)
    page = api.get("/changes", headers=headers).json()
    assert page["total"] == 0
    late = _event(db, order, at=_when(page["as_of"]) - timedelta(seconds=3))  # committed after the read

    api.post("/changes/seen", json={"through": page["as_of"]}, headers=headers)

    assert [item["id"] for item in api.get("/changes", headers=headers).json()["items"]] == [late.id]


def test_an_unknown_field_is_refused(api, db):
    response = api.post("/changes/seen", json={"until": naive_utcnow().isoformat()}, headers=_user(db, "ops@test.com"))
    assert response.status_code == 422


def test_a_sign_in_with_no_user_row_reads_the_default_window_but_cannot_mark(api, db):
    """A token the server minted itself (a worker, a script) has no row to keep a mark on."""
    headers = {"Authorization": f"Bearer {create_access_token('script', role='operator')}"}
    _event(db, _order(db), at=naive_utcnow() - timedelta(days=1))

    assert api.get("/changes", headers=headers).json()["total"] == 1
    assert api.post("/changes/seen", headers=headers).status_code == 409
