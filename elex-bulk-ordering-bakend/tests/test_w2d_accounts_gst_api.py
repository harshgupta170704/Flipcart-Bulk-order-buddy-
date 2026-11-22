"""Setting an account's own GST and billing phone (package D, 26 Oct 2026).

Per account (``PATCH /accounts/{id}``) and for a whole selection
(``POST /accounts/gst``, by ids or by the accounts filter, bounded like bulk
tagging), plus what deleting an Address Book GST now does to the accounts
that had it: they are unlinked and kept, exactly as its addresses are.

The routers are mounted on a local app, as the other API tests do; the auth
dependency and the database are the real ones.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text

from app.api import accounts as accounts_api
from app.api import gsts as gsts_api
from app.core.security import create_access_token
from app.db.base import SessionLocal, engine
from app.db.models import Account, AccountStatus, Gst, Tag
from app.schemas import account as account_schema

CAP = account_schema.MAX_LISTED_IDS
TOO_LARGE_ID = 2**31  # one past what the database binds


@pytest.fixture
def app_under_test() -> FastAPI:
    application = FastAPI()
    application.include_router(accounts_api.router)
    application.include_router(gsts_api.router)
    return application


@pytest.fixture
def api(app_under_test) -> TestClient:
    token = create_access_token("tester", role="admin")
    return TestClient(app_under_test, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def gsts(db) -> dict[str, int]:
    rows = {
        "DIGIDRIVE": Gst(label="DIGIDRIVE", gstin="27AADCD5678E1Z3", business_name="Digidrive LLP"),
        "SONIC": Gst(label="SONIC", gstin="33AAECS4321F1ZK", business_name="Sonic Kommerce"),
    }
    db.add_all(rows.values())
    db.commit()
    return {label: row.id for label, row in rows.items()}


@pytest.fixture
def accounts(db) -> list[int]:
    """Twelve accounts, every third one tagged VIP and blocked."""
    vip = Tag(name="VIP")
    db.add(vip)
    db.flush()
    rows = [
        Account(
            email=f"acct{i:02d}@test.com",
            phone=f"90000000{i:02d}",
            status=AccountStatus.blocked if i % 3 == 0 else AccountStatus.session_valid,
            tag=vip if i % 3 == 0 else None,
        )
        for i in range(12)
    ]
    db.add_all(rows)
    db.commit()
    return [row.id for row in rows]


def _gst_ids() -> dict[int, int | None]:
    """{account id: its gst_id}, read fresh."""
    with SessionLocal() as session:
        return dict(session.execute(select(Account.id, Account.gst_id)).all())


def _account(account_id: int) -> Account:
    with SessionLocal() as session:
        row = session.get(Account, account_id)
        session.expunge(row)
        return row


@contextmanager
def _captured_sql():
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


# --- one account: set, show, clear -------------------------------------------
def test_an_accounts_gst_and_billing_phone_are_set_and_shown(api, gsts, accounts):
    target = accounts[1]
    response = api.patch(
        f"/accounts/{target}", json={"gst_id": gsts["DIGIDRIVE"], "billing_phone": "  98765 43210  "}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["gst"] == {
        "id": gsts["DIGIDRIVE"],
        "label": "DIGIDRIVE",
        "gstin": "27AADCD5678E1Z3",
        "business_name": "Digidrive LLP",
    }
    assert body["billing_phone"] == "98765 43210"  # trimmed, as the Address Book trims a phone
    assert body["phone"] == "9000000001"  # the account's own phone is a different field, untouched

    listed = {row["id"]: row for row in api.get("/accounts", params={"size": 50}).json()["items"]}
    assert listed[target]["gst"]["label"] == "DIGIDRIVE"
    assert listed[target]["billing_phone"] == "98765 43210"
    assert listed[accounts[0]]["gst"] is None
    assert listed[accounts[0]]["billing_phone"] is None


def test_null_takes_them_off_and_a_blank_phone_is_no_phone(api, gsts, accounts):
    target = accounts[2]
    api.patch(f"/accounts/{target}", json={"gst_id": gsts["SONIC"], "billing_phone": "9811111111"})

    cleared = api.patch(f"/accounts/{target}", json={"gst_id": None, "billing_phone": "   "})

    assert cleared.status_code == 200, cleared.text
    assert (cleared.json()["gst"], cleared.json()["billing_phone"]) == (None, None)
    row = _account(target)
    assert (row.gst_id, row.billing_phone) == (None, None)


def test_a_patch_leaves_what_it_does_not_name_alone(api, gsts, accounts):
    target = accounts[3]
    api.patch(f"/accounts/{target}", json={"gst_id": gsts["SONIC"], "billing_phone": "9811111111"})

    api.patch(f"/accounts/{target}", json={"status": "logged_out"})
    api.patch(f"/accounts/{target}", json={"billing_phone": "9822222222"})

    row = _account(target)
    assert (row.gst_id, row.billing_phone) == (gsts["SONIC"], "9822222222")


@pytest.mark.parametrize("gst_id", [9999, 0, TOO_LARGE_ID])
def test_a_gst_that_is_not_in_the_address_book_is_a_422_and_changes_nothing(api, gsts, accounts, gst_id):
    target = accounts[4]

    response = api.patch(f"/accounts/{target}", json={"gst_id": gst_id})

    assert response.status_code == 422
    assert _account(target).gst_id is None


def test_the_billing_phone_is_held_to_its_columns_width(api, accounts):
    width = account_schema.MAX_BILLING_PHONE_LENGTH
    assert width == Account.__table__.c.billing_phone.type.length
    target = accounts[5]

    assert api.patch(f"/accounts/{target}", json={"billing_phone": "9" * (width + 1)}).status_code == 422
    assert _account(target).billing_phone is None
    fits = api.patch(f"/accounts/{target}", json={"billing_phone": "9" * width})
    assert fits.status_code == 200, fits.text
    assert fits.json()["billing_phone"] == "9" * width


def test_an_operator_may_not_set_them_but_still_tags(app_under_test, gsts, accounts):
    """An account's own GST and billing phone are setup (spec C-7): its orders
    bill under that GST whatever a run picks, so only an admin sets them, as
    only an admin creates, edits and deletes the GSTs. The rest of an account
    — its tag, phone and status — stays the operator's to arrange."""
    ops = TestClient(app_under_test, headers={"Authorization": f"Bearer {create_access_token('ops', role='operator')}"})

    assert ops.patch(f"/accounts/{accounts[0]}", json={"gst_id": gsts["SONIC"]}).status_code == 403
    assert ops.patch(f"/accounts/{accounts[0]}", json={"billing_phone": "9811111111"}).status_code == 403
    assert ops.patch(f"/accounts/{accounts[0]}", json={"gst_id": None}).status_code == 403  # taking it off changes it too
    assert ops.post("/accounts/gst", json={"gst_id": gsts["SONIC"], "account_ids": [accounts[1]]}).status_code == 403
    assert ops.post("/accounts/gst", json={"gst_id": None, "filter": {}}).status_code == 403
    assert ops.delete(f"/gsts/{gsts['SONIC']}").status_code == 403
    assert ops.patch("/accounts/999999", json={"gst_id": None}).status_code == 403  # refused before the lookup
    assert set(_gst_ids().values()) == {None}
    assert _account(accounts[0]).billing_phone is None

    # A body that mixes the two is refused whole: none of it is written.
    mixed = ops.patch(f"/accounts/{accounts[2]}", json={"phone": "9000000098", "billing_phone": "9811111111"})
    assert mixed.status_code == 403
    assert (_account(accounts[2]).phone, _account(accounts[2]).billing_phone) == ("9000000002", None)

    # What stays the operator's: a body naming neither.
    tagged = ops.patch(f"/accounts/{accounts[0]}", json={"tag_id": _tag_id("VIP"), "phone": "9000000099", "status": "logged_out"})
    assert tagged.status_code == 200, tagged.text
    assert (tagged.json()["tag"]["name"], tagged.json()["phone"], tagged.json()["status"]) == ("VIP", "9000000099", "logged_out")


def test_the_listing_reads_every_gst_in_one_query_however_many_rows(api, db, accounts):
    """No query per row for the inlined GST, as for the tag. Each account has
    a GST of its own here: rows sharing one would hide a per-row load behind
    the session's memory of the first."""
    for n, account_id in enumerate(accounts):
        gst = Gst(label=f"G{n:02d}", gstin=f"27AADCD{n:04d}E1Z3", business_name=f"Entity {n}")
        db.add(gst)
        db.flush()
        db.get(Account, account_id).gst_id = gst.id
    db.commit()

    def selects(size: int) -> int:
        with _captured_sql() as statements:
            assert api.get("/accounts", params={"size": size}).status_code == 200
        return len([s for s in statements if s.lstrip().upper().startswith("SELECT")])

    assert selects(2) == selects(12)


# --- a selection at once ----------------------------------------------------------
def test_a_gst_for_ticked_accounts(api, gsts, accounts):
    picked = accounts[:3]

    response = api.post("/accounts/gst", json={"gst_id": gsts["DIGIDRIVE"], "account_ids": picked})

    assert response.status_code == 200, response.text
    assert response.json() == {"updated": 3}
    links = _gst_ids()
    assert {links[i] for i in picked} == {gsts["DIGIDRIVE"]}
    assert {links[i] for i in accounts[3:]} == {None}


def test_a_gst_for_every_account_the_filter_matches_in_one_update(api, gsts, accounts):
    """The filter the page shows, never a list of ids, and one UPDATE however
    many it covers."""
    with _captured_sql() as statements:
        response = api.post(
            "/accounts/gst", json={"gst_id": gsts["SONIC"], "filter": {"statuses": ["blocked"]}}
        )

    assert response.status_code == 200, response.text
    blocked = [account_id for i, account_id in enumerate(accounts) if i % 3 == 0]
    assert response.json() == {"updated": len(blocked)}
    links = _gst_ids()
    assert {account_id for account_id, gst_id in links.items() if gst_id == gsts["SONIC"]} == set(blocked)
    assert len([s for s in statements if s.lstrip().upper().startswith("UPDATE")]) == 1


def test_a_filter_by_tag_and_search_narrows_as_it_does_on_the_page(api, gsts, accounts):
    vip_page = api.get("/accounts", params={"tag_id": _tag_id("VIP"), "q": "acct0"}).json()
    response = api.post(
        "/accounts/gst",
        json={"gst_id": gsts["DIGIDRIVE"], "filter": {"tag_ids": [_tag_id("VIP")], "q": "acct0"}},
    )

    assert response.json()["updated"] == vip_page["total"]
    links = _gst_ids()
    assert {i for i, g in links.items() if g == gsts["DIGIDRIVE"]} == {row["id"] for row in vip_page["items"]}


def _tag_id(name: str) -> int:
    with SessionLocal() as session:
        return session.scalar(select(Tag.id).where(Tag.name == name))


def test_null_takes_a_selections_own_gst_off(api, gsts, accounts):
    api.post("/accounts/gst", json={"gst_id": gsts["SONIC"], "account_ids": accounts})

    response = api.post("/accounts/gst", json={"gst_id": None, "account_ids": accounts[:5]})

    assert response.json() == {"updated": 5}
    links = _gst_ids()
    assert {links[i] for i in accounts[:5]} == {None}
    assert {links[i] for i in accounts[5:]} == {gsts["SONIC"]}


def test_an_empty_list_changes_nothing(api, gsts, accounts):
    assert api.post("/accounts/gst", json={"gst_id": gsts["SONIC"], "account_ids": []}).json() == {"updated": 0}
    assert set(_gst_ids().values()) == {None}


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"account_ids": [1], "filter": {}}, id="both"),
        pytest.param({}, id="neither"),
        pytest.param({"account_ids": [1], "label": "SONIC"}, id="an-unknown-field"),
        pytest.param({"filter": {"statuses": ["blocked"], "tag": "VIP"}}, id="an-unknown-filter-field"),
    ],
)
def test_exactly_one_selection_and_nothing_else(api, gsts, accounts, body):
    """A body that could be read as "every account" is refused, as bulk
    tagging refuses it."""
    response = api.post("/accounts/gst", json={"gst_id": gsts["SONIC"], **body})

    assert response.status_code == 422
    assert set(_gst_ids().values()) == {None}


def test_an_unknown_gst_is_a_422_and_changes_nothing(api, gsts, accounts):
    response = api.post("/accounts/gst", json={"gst_id": 9999, "account_ids": accounts})

    assert response.status_code == 422
    assert "No GST with id 9999" in response.json()["detail"]
    assert set(_gst_ids().values()) == {None}


# --- the bound, like the other bulk actions -----------------------------------------
@pytest.mark.parametrize(
    "selection",
    [
        pytest.param({"account_ids": list(range(1, CAP + 2))}, id="ids-over-the-cap"),
        pytest.param({"filter": {"ids": list(range(1, CAP + 2))}}, id="filter-ids-over-the-cap"),
        pytest.param({"account_ids": [TOO_LARGE_ID]}, id="id-past-what-the-database-binds"),
        pytest.param({"filter": {"tag_ids": [TOO_LARGE_ID]}}, id="filter-tag-past-what-the-database-binds"),
        pytest.param({"gst_id": TOO_LARGE_ID, "account_ids": [1]}, id="gst-past-what-the-database-binds"),
    ],
)
def test_a_selection_past_its_bound_is_a_422_and_changes_nothing(api, gsts, accounts, selection):
    body = {"gst_id": gsts["SONIC"], **selection}

    response = api.post("/accounts/gst", json=body)

    assert response.status_code == 422, response.text
    assert set(_gst_ids().values()) == {None}


def test_a_list_exactly_at_the_cap_is_applied(api, gsts, accounts):
    """The cap is inclusive; ids that are not accounts are simply not there."""
    ids = accounts + [i for i in range(1, CAP + 1) if i not in accounts][: CAP - len(accounts)]
    assert len(ids) == CAP

    response = api.post("/accounts/gst", json={"gst_id": gsts["SONIC"], "account_ids": ids})

    assert response.status_code == 200, response.text
    assert response.json() == {"updated": len(accounts)}


# --- deleting a GST: its accounts are unlinked and kept, as its addresses are ------
def test_deleting_a_gst_unlinks_its_accounts_and_keeps_every_one(api, gsts, accounts):
    api.post("/accounts/gst", json={"gst_id": gsts["DIGIDRIVE"], "account_ids": accounts[:4]})
    api.post("/accounts/gst", json={"gst_id": gsts["SONIC"], "account_ids": accounts[4:6]})

    listed = {row["label"]: row for row in api.get("/gsts").json()}
    assert (listed["DIGIDRIVE"]["account_count"], listed["SONIC"]["account_count"]) == (4, 2)

    deleted = api.delete(f"/gsts/{gsts['DIGIDRIVE']}")
    assert deleted.status_code == 200
    assert deleted.json() == {"unlinked_addresses": 0, "unlinked_accounts": 4}

    links = _gst_ids()
    assert len(links) == len(accounts)  # no account was harmed
    assert {links[i] for i in accounts[:4]} == {None}
    assert {links[i] for i in accounts[4:6]} == {gsts["SONIC"]}  # the other GST's accounts untouched
    assert [row["label"] for row in api.get("/gsts").json()] == ["SONIC"]


def test_the_count_follows_an_edit_of_the_gst(api, gsts, accounts):
    api.post("/accounts/gst", json={"gst_id": gsts["SONIC"], "account_ids": accounts[:3]})

    edited = api.patch(f"/gsts/{gsts['SONIC']}", json={"business_name": "Sonic Kommerce Pvt Ltd"})

    assert edited.status_code == 200, edited.text
    assert edited.json()["account_count"] == 3


def test_the_database_sets_a_deleted_gsts_accounts_to_null_too(db, gsts, accounts):
    """ON DELETE SET NULL on the column's foreign key (where it has one): a
    GST row removed by any other means than the app still leaves no account
    naming it."""
    db.get(Account, accounts[0]).gst_id = gsts["SONIC"]
    db.commit()

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM gsts WHERE id = :id"), {"id": gsts["SONIC"]})

    assert _account(accounts[0]).gst_id is None
    assert len(_gst_ids()) == len(accounts)
