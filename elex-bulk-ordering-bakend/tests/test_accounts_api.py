"""Tests for the paged accounts API (Phase 7, ROADMAP §0).

Everything here is seeded with a few hundred accounts, because the bugs this
phase fixes only exist above one page: a filter that forgets its total, a page
boundary that repeats a row, a "tag everything matching" that quietly tags the
whole table. The seed is a fixed set of cycles (status / subscription / tag), so
every expectation is derived from the seed in Python rather than hard-coded —
if the seed changes, the assertions follow.

The router is mounted on a local FastAPI app: ``app/main.py`` is orchestrator
owned, and the routes, the auth dependency and the DB here are the real ones.
Verification reads use their own short-lived session so the test never holds a
SQLite read transaction open while the API writes.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from sqlalchemy import func, select

from app.api import accounts as accounts_api
from app.core.account_filter import MAX_SIZE, SORTABLE
from app.core.security import create_access_token
from app.db.base import SessionLocal
from app.db.models import Account, AccountStatus, Tag
from app.io import excel

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

SEED_COUNT = 240

# Cycle lengths 6 / 4 / 5 (lcm 60) over 240 rows, so every combination of
# status x subscription x tag actually occurs and AND-ing two filters is a real
# narrowing rather than a coincidence.
_STATUS_CYCLE = (
    AccountStatus.new,
    AccountStatus.session_valid,
    AccountStatus.blocked,
    AccountStatus.session_valid,
    AccountStatus.not_registered,
    AccountStatus.multiple_attempts,
)
_SUB_CYCLE = (None, "ACTIVE-PREMIUM-GOLD", "PLUS", "ACTIVE-PREMIUM-GOLD")
_TAG_CYCLE = ("VIP", "BULK", None, None, "VIP")
#: Created but never applied — a tag with no accounts must still be offered.
_UNUSED_TAG = "RETIRED"


# --- fixtures -----------------------------------------------------------------
@pytest.fixture
def api_app() -> FastAPI:
    application = FastAPI()
    application.include_router(accounts_api.router)
    return application


@pytest.fixture
def api(api_app: FastAPI) -> TestClient:
    token = create_access_token("tester", role="admin")
    return TestClient(api_app, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def anon(api_app: FastAPI) -> TestClient:
    return TestClient(api_app)


@dataclass(frozen=True)
class Seed:
    """The seeded rows as plain data, plus name -> id for the tags."""

    rows: list[dict]
    tags: dict[str, int]

    def ids_where(self, **criteria) -> set[int]:
        """Ids of seeded rows matching every given field value."""
        return {
            row["id"]
            for row in self.rows
            if all(row[field] == value for field, value in criteria.items())
        }

    def ids_in(self, field: str, values) -> set[int]:
        wanted = set(values)
        return {row["id"] for row in self.rows if row[field] in wanted}


@pytest.fixture
def seed(db) -> Seed:
    """A few hundred accounts across three tags, two subscriptions, five statuses."""
    tags = {
        name: Tag(name=name) for name in ("VIP", "BULK", _UNUSED_TAG)
    }
    db.add_all(tags.values())
    db.flush()

    accounts = []
    for i in range(SEED_COUNT):
        tag_name = _TAG_CYCLE[i % len(_TAG_CYCLE)]
        accounts.append(
            Account(
                # Email ids are 4-digit, phones are 7-digit and start with 5, so
                # a search for a phone can never accidentally match an email.
                email=f"acct{i:04d}@test.com",
                phone=f"5{i:06d}",
                status=_STATUS_CYCLE[i % len(_STATUS_CYCLE)],
                subscription_type=_SUB_CYCLE[i % len(_SUB_CYCLE)],
                tag=tags[tag_name] if tag_name else None,
            )
        )
    db.add_all(accounts)
    db.commit()

    rows = [
        {
            "id": account.id,
            "email": account.email,
            "phone": account.phone,
            "status": account.status.value,
            "subscription_type": account.subscription_type,
            "tag": account.tag.name if account.tag else None,
        }
        for account in accounts
    ]
    seeded = Seed(rows=rows, tags={name: tag.id for name, tag in tags.items()})
    db.commit()  # release the read transaction before the API writes
    return seeded


# --- helpers ------------------------------------------------------------------
def _tag_state() -> dict[int, int | None]:
    """{account id: tag id} straight from the DB, in its own session."""
    with SessionLocal() as session:
        return {
            int(account_id): (None if tag_id is None else int(tag_id))
            for account_id, tag_id in session.execute(
                select(Account.id, Account.tag_id)
            ).all()
        }


def _account_count() -> int:
    with SessionLocal() as session:
        return int(session.scalar(select(func.count()).select_from(Account)) or 0)


def _ids(body: dict) -> list[int]:
    return [item["id"] for item in body["items"]]


def _all_pages(api: TestClient, *, size: int, **params) -> list[dict]:
    """Walk every page of a listing and return the concatenated items."""
    first = api.get("/accounts", params={"size": size, "page": 1, **params})
    assert first.status_code == 200, first.text
    body = first.json()
    items = list(body["items"])
    for page in range(2, body["pages"] + 1):
        nxt = api.get("/accounts", params={"size": size, "page": page, **params})
        assert nxt.status_code == 200, nxt.text
        items.extend(nxt.json()["items"])
    return items


def _legacy_upload(rows: list[tuple[str, str]]) -> bytes:
    wb = Workbook()
    sheet = wb.active
    sheet.cell(row=1, column=excel.COL_EMAIL, value="Email")
    for i, (email, phone) in enumerate(rows):
        r = excel.FIRST_DATA_ROW + i
        sheet.cell(row=r, column=excel.COL_EMAIL, value=email)
        sheet.cell(row=r, column=excel.COL_PHONE, value=phone)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


# --- auth ---------------------------------------------------------------------
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/accounts"),
        ("get", "/accounts/filter-options"),
        ("get", "/accounts/export"),
        ("post", "/accounts"),
        ("post", "/accounts/tag"),
        ("post", "/accounts/import"),
        ("patch", "/accounts/1"),
    ],
)
def test_every_route_needs_a_token(anon: TestClient, method: str, path: str) -> None:
    assert getattr(anon, method)(path).status_code in (401, 403)


def test_a_bad_token_is_rejected(api_app: FastAPI) -> None:
    client = TestClient(api_app, headers={"Authorization": "Bearer not-a-jwt"})
    assert client.get("/accounts").status_code == 401


# --- paging -------------------------------------------------------------------
def test_pages_do_not_overlap_and_total_is_the_match_count(api, seed) -> None:
    first = api.get("/accounts", params={"page": 1, "size": 50}).json()
    second = api.get("/accounts", params={"page": 2, "size": 50}).json()

    assert len(first["items"]) == 50
    assert len(second["items"]) == 50
    # total is the whole match, not the page length — this is the number the UI
    # shows next to "select all matching".
    assert first["total"] == second["total"] == SEED_COUNT
    assert first["page"] == 1 and second["page"] == 2
    assert first["size"] == second["size"] == 50
    assert set(_ids(first)).isdisjoint(_ids(second))


@pytest.mark.parametrize(
    ("size", "expected_pages"),
    [(50, 5), (7, 35), (240, 1), (1000, 1)],
)
def test_pages_arithmetic(api, seed, size: int, expected_pages: int) -> None:
    body = api.get("/accounts", params={"size": size}).json()
    assert body["total"] == SEED_COUNT
    assert body["pages"] == expected_pages


def test_empty_table_reports_zero_pages(api) -> None:
    body = api.get("/accounts").json()
    assert body == {"items": [], "total": 0, "page": 1, "size": 50, "pages": 0}


def test_size_is_clamped_not_rejected(api, seed) -> None:
    body = api.get("/accounts", params={"size": MAX_SIZE + 100}).json()
    assert body["size"] == MAX_SIZE
    assert len(body["items"]) == SEED_COUNT  # 240 < MAX_SIZE, so all of them


def test_a_page_past_the_end_is_empty_but_still_counts(api, seed) -> None:
    body = api.get("/accounts", params={"page": 99, "size": 50}).json()
    assert body["items"] == []
    assert body["total"] == SEED_COUNT
    assert body["pages"] == 5


def test_order_is_stable_across_pages_on_a_non_unique_sort(api, seed) -> None:
    """Sorting by a column full of ties must not repeat or drop rows."""
    items = _all_pages(api, size=37, sort="status")
    ids = [item["id"] for item in items]
    assert len(ids) == SEED_COUNT
    assert len(set(ids)) == SEED_COUNT
    assert set(ids) == {row["id"] for row in seed.rows}


def test_paging_a_filtered_listing_keeps_the_filter_total(api, seed) -> None:
    expected = seed.ids_where(status=AccountStatus.blocked.value)
    body = api.get(
        "/accounts", params={"status": "blocked", "size": 5, "page": 2}
    ).json()
    assert body["total"] == len(expected)
    assert len(body["items"]) == 5
    assert set(_ids(body)) <= expected


# --- filters ------------------------------------------------------------------
def test_q_matches_email(api, seed) -> None:
    body = api.get("/accounts", params={"q": "acct0007"}).json()
    assert body["total"] == 1
    assert body["items"][0]["email"] == "acct0007@test.com"


def test_q_matches_phone(api, seed) -> None:
    body = api.get("/accounts", params={"q": "5000007"}).json()
    assert body["total"] == 1
    assert body["items"][0]["phone"] == "5000007"
    assert body["items"][0]["email"] == "acct0007@test.com"


def test_q_is_case_insensitive_and_trimmed(api, seed) -> None:
    body = api.get("/accounts", params={"q": "  ACCT0007  "}).json()
    assert body["total"] == 1


def test_blank_q_is_not_a_filter(api, seed) -> None:
    assert api.get("/accounts", params={"q": "   "}).json()["total"] == SEED_COUNT


def test_filter_by_one_status(api, seed) -> None:
    expected = seed.ids_where(status=AccountStatus.blocked.value)
    body = api.get("/accounts", params={"status": "blocked", "size": 500}).json()
    assert body["total"] == len(expected)
    assert set(_ids(body)) == expected


def test_filter_by_several_statuses(api, seed) -> None:
    wanted = [AccountStatus.blocked.value, AccountStatus.new.value]
    expected = seed.ids_in("status", wanted)
    body = api.get("/accounts", params={"status": wanted, "size": 500}).json()
    assert body["total"] == len(expected)
    assert set(_ids(body)) == expected


def test_unknown_status_is_a_422_naming_it(api, seed) -> None:
    response = api.get("/accounts", params={"status": "nonsense"})
    assert response.status_code == 422
    assert "nonsense" in response.text


def test_filter_by_tag_ids(api, seed) -> None:
    expected = seed.ids_where(tag="VIP")
    body = api.get(
        "/accounts", params={"tag_id": seed.tags["VIP"], "size": 500}
    ).json()
    assert body["total"] == len(expected)
    assert set(_ids(body)) == expected


def test_filter_by_two_tag_ids_is_an_or(api, seed) -> None:
    expected = seed.ids_in("tag", ["VIP", "BULK"])
    body = api.get(
        "/accounts",
        params={"tag_id": [seed.tags["VIP"], seed.tags["BULK"]], "size": 500},
    ).json()
    assert body["total"] == len(expected)
    assert set(_ids(body)) == expected


def test_filter_untagged(api, seed) -> None:
    expected = seed.ids_where(tag=None)
    body = api.get("/accounts", params={"untagged": "true", "size": 500}).json()
    assert body["total"] == len(expected)
    assert set(_ids(body)) == expected
    assert all(item["tag"] is None for item in body["items"])


def test_tag_ids_and_untagged_combine_as_an_or(api, seed) -> None:
    expected = seed.ids_where(tag="BULK") | seed.ids_where(tag=None)
    body = api.get(
        "/accounts",
        params={"tag_id": seed.tags["BULK"], "untagged": "true", "size": 500},
    ).json()
    assert body["total"] == len(expected)
    assert set(_ids(body)) == expected
    assert body["total"] < SEED_COUNT  # VIP accounts are excluded


def test_filter_by_subscription(api, seed) -> None:
    expected = seed.ids_where(subscription_type="PLUS")
    body = api.get("/accounts", params={"subscription": "PLUS", "size": 500}).json()
    assert body["total"] == len(expected)
    assert set(_ids(body)) == expected


def test_filter_by_several_subscriptions(api, seed) -> None:
    wanted = ["PLUS", "ACTIVE-PREMIUM-GOLD"]
    expected = seed.ids_in("subscription_type", wanted)
    body = api.get("/accounts", params={"subscription": wanted, "size": 500}).json()
    assert body["total"] == len(expected)
    assert set(_ids(body)) == expected


def test_two_filters_are_anded(api, seed) -> None:
    expected = seed.ids_where(status=AccountStatus.new.value, subscription_type="PLUS")
    assert expected, "seed must contain this combination for the test to mean anything"
    body = api.get(
        "/accounts",
        params={"status": "new", "subscription": "PLUS", "size": 500},
    ).json()
    assert body["total"] == len(expected)
    assert set(_ids(body)) == expected
    # A real narrowing, not an accidental OR.
    assert 0 < body["total"] < len(seed.ids_where(subscription_type="PLUS"))


def test_three_filters_are_anded(api, seed) -> None:
    expected = seed.ids_where(
        status=AccountStatus.session_valid.value, tag="VIP"
    ) & seed.ids_in("subscription_type", ["ACTIVE-PREMIUM-GOLD"])
    assert expected, "seed must contain this combination for the test to mean anything"
    body = api.get(
        "/accounts",
        params={
            "status": "session_valid",
            "tag_id": seed.tags["VIP"],
            "subscription": "ACTIVE-PREMIUM-GOLD",
            "size": 500,
        },
    ).json()
    assert body["total"] == len(expected)
    assert set(_ids(body)) == expected


# --- sorting ------------------------------------------------------------------
def _sort_values(items: list[dict], key: str) -> list:
    values = []
    for item in items:
        value = item[key]
        if key in ("created_at", "updated_at") and value is not None:
            value = datetime.fromisoformat(value)
        values.append(value)
    return values


@pytest.mark.parametrize("key", sorted(SORTABLE))
@pytest.mark.parametrize("descending", [False, True])
def test_sort_by_every_sortable_key(api, seed, key: str, descending: bool) -> None:
    body = api.get(
        "/accounts", params={"sort": key, "desc": str(descending).lower(), "size": 500}
    ).json()
    assert body["total"] == SEED_COUNT

    values = _sort_values(body["items"], key)
    # NULL placement is backend-specific, so only the present values are checked
    # for monotonicity — that is what "sorted by this column" has to mean.
    present = [v for v in values if v is not None]
    assert present == sorted(present, reverse=descending)


def test_desc_reverses_the_id_order(api, seed) -> None:
    asc = api.get("/accounts", params={"sort": "id", "size": 500}).json()
    desc = api.get("/accounts", params={"sort": "id", "desc": "true", "size": 500}).json()
    assert _ids(desc) == list(reversed(_ids(asc)))


def test_an_unknown_sort_is_a_422(api, seed) -> None:
    response = api.get("/accounts", params={"sort": "tag_id"})
    assert response.status_code == 422
    assert "tag_id" in response.text


def test_sort_cannot_reach_another_column(api, seed) -> None:
    assert api.get("/accounts", params={"sort": "id; drop table"}).status_code == 422


# --- filter options -----------------------------------------------------------
def test_filter_options_counts_match_reality(api, seed) -> None:
    body = api.get("/accounts/filter-options").json()

    assert body["total"] == SEED_COUNT
    assert body["untagged"] == len(seed.ids_where(tag=None))

    expected_statuses = {
        status: len(seed.ids_where(status=status))
        for status in {row["status"] for row in seed.rows}
    }
    assert {s["value"]: s["count"] for s in body["statuses"]} == expected_statuses

    assert body["subscriptions"] == sorted(
        {row["subscription_type"] for row in seed.rows if row["subscription_type"]}
    )

    expected_tags = {
        name: len(seed.ids_where(tag=name)) for name in seed.tags
    }
    assert {t["name"]: t["count"] for t in body["tags"]} == expected_tags
    # A tag nothing is tagged with is still offered, with count 0.
    assert expected_tags[_UNUSED_TAG] == 0
    assert {t["id"] for t in body["tags"]} == set(seed.tags.values())


def test_filter_options_on_an_empty_table(api) -> None:
    body = api.get("/accounts/filter-options").json()
    assert body == {
        "statuses": [],
        "subscriptions": [],
        "tags": [],
        "total": 0,
        "untagged": 0,
        "domains": [],
    }


# --- create / patch -----------------------------------------------------------
def test_create_account_and_reject_a_duplicate(api) -> None:
    created = api.post("/accounts", json={"email": "x@test.com", "phone": "123"})
    assert created.status_code == 201
    body = created.json()
    assert body["email"] == "x@test.com"
    assert body["status"] == AccountStatus.new.value
    assert body["tag"] is None and body["subscription_type"] is None

    assert api.post("/accounts", json={"email": "x@test.com"}).status_code == 409


def test_patch_updates_the_four_editable_fields(api, seed) -> None:
    account_id = sorted(seed.ids_where(tag=None))[0]
    response = api.patch(
        f"/accounts/{account_id}",
        json={
            "phone": "9998887777",
            "status": AccountStatus.blocked.value,
            "subscription_type": "PLUS-VIP",
            "tag_id": seed.tags["VIP"],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["phone"] == "9998887777"
    assert body["status"] == AccountStatus.blocked.value
    assert body["subscription_type"] == "PLUS-VIP"
    assert body["tag"] == {"id": seed.tags["VIP"], "name": "VIP"}
    assert _tag_state()[account_id] == seed.tags["VIP"]


def test_patch_leaves_absent_fields_alone(api, seed) -> None:
    row = next(r for r in seed.rows if r["tag"] == "VIP")
    response = api.patch(f"/accounts/{row['id']}", json={"phone": "111"})
    assert response.status_code == 200
    body = response.json()
    assert body["phone"] == "111"
    assert body["status"] == row["status"]
    assert body["subscription_type"] == row["subscription_type"]
    assert body["tag"]["name"] == "VIP"


def test_patch_with_a_null_tag_clears_it(api, seed) -> None:
    row = next(r for r in seed.rows if r["tag"] == "VIP")
    body = api.patch(f"/accounts/{row['id']}", json={"tag_id": None}).json()
    assert body["tag"] is None
    assert _tag_state()[row["id"]] is None


def test_patch_rejects_an_unknown_field(api, seed) -> None:
    account_id = seed.rows[0]["id"]
    response = api.patch(f"/accounts/{account_id}", json={"email": "new@test.com"})
    assert response.status_code == 422
    assert "email" in response.text


def test_patch_rejects_an_unknown_tag_and_status(api, seed) -> None:
    row = seed.rows[0]
    account_id = row["id"]
    assert api.patch(f"/accounts/{account_id}", json={"tag_id": 999_999}).status_code == 422
    assert api.patch(f"/accounts/{account_id}", json={"status": "nope"}).status_code == 422
    # A rejected patch must leave the row exactly as it was.
    assert _tag_state()[account_id] == (seed.tags[row["tag"]] if row["tag"] else None)


def test_patch_on_a_missing_account_is_404(api) -> None:
    assert api.patch("/accounts/4242", json={"phone": "1"}).status_code == 404


# --- bulk tagging -------------------------------------------------------------
def test_tag_by_ids(api, seed) -> None:
    before = _tag_state()
    chosen = sorted(seed.ids_where(tag=None))[:3]
    response = api.post(
        "/accounts/tag", json={"tag_id": seed.tags["BULK"], "account_ids": chosen}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"updated": len(chosen)}

    after = _tag_state()
    assert all(after[i] == seed.tags["BULK"] for i in chosen)
    assert {i: t for i, t in after.items() if i not in chosen} == {
        i: t for i, t in before.items() if i not in chosen
    }


def test_tag_by_filter_tags_exactly_the_matching_rows(api, seed) -> None:
    before = _tag_state()
    expected = seed.ids_where(status=AccountStatus.blocked.value)

    response = api.post(
        "/accounts/tag",
        json={"tag_id": seed.tags["BULK"], "filter": {"statuses": ["blocked"]}},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"updated": len(expected)}

    after = _tag_state()
    assert len(after) == SEED_COUNT  # tagging never deletes or creates rows
    for account_id, tag_id in after.items():
        if account_id in expected:
            assert tag_id == seed.tags["BULK"]
        else:
            assert tag_id == before[account_id], f"account {account_id} was touched"


def test_tag_by_a_filter_that_matches_a_single_page_boundary(api, seed) -> None:
    """The filter the table shows and the filter that tags must agree."""
    listing = api.get(
        "/accounts", params={"subscription": "PLUS", "untagged": "true", "size": 5}
    ).json()
    response = api.post(
        "/accounts/tag",
        json={
            "tag_id": seed.tags["VIP"],
            "filter": {"subscriptions": ["PLUS"], "include_untagged": True},
        },
    )
    assert 0 < listing["total"] < SEED_COUNT
    assert response.json()["updated"] == listing["total"]


def test_tag_by_filter_ids(api, seed) -> None:
    chosen = sorted(seed.ids_where(tag=None))[:4]
    response = api.post(
        "/accounts/tag", json={"tag_id": seed.tags["VIP"], "filter": {"ids": chosen}}
    )
    assert response.json() == {"updated": len(chosen)}
    after = _tag_state()
    assert all(after[i] == seed.tags["VIP"] for i in chosen)


def test_tag_by_filter_q(api, seed) -> None:
    response = api.post(
        "/accounts/tag", json={"tag_id": seed.tags["VIP"], "filter": {"q": "acct0007"}}
    )
    assert response.json() == {"updated": 1}


def test_tag_null_clears_the_tag(api, seed) -> None:
    expected = seed.ids_where(tag="VIP")
    response = api.post(
        "/accounts/tag",
        json={"tag_id": None, "filter": {"tag_ids": [seed.tags["VIP"]]}},
    )
    assert response.json() == {"updated": len(expected)}

    after = _tag_state()
    assert all(after[i] is None for i in expected)
    assert api.get(
        "/accounts", params={"tag_id": seed.tags["VIP"]}
    ).json()["total"] == 0


def test_tag_an_empty_id_list_changes_nothing(api, seed) -> None:
    before = _tag_state()
    response = api.post(
        "/accounts/tag", json={"tag_id": seed.tags["VIP"], "account_ids": []}
    )
    assert response.json() == {"updated": 0}
    assert _tag_state() == before


def test_tag_needs_exactly_one_of_ids_or_filter(api, seed) -> None:
    neither = api.post("/accounts/tag", json={"tag_id": seed.tags["VIP"]})
    both = api.post(
        "/accounts/tag",
        json={"tag_id": seed.tags["VIP"], "account_ids": [1], "filter": {}},
    )
    assert neither.status_code == 422, neither.text
    assert both.status_code == 422, both.text
    assert _tag_state() == {row["id"]: seed.tags[row["tag"]] if row["tag"] else None
                            for row in seed.rows}


def test_tag_rejects_an_unknown_tag_id(api, seed) -> None:
    before = _tag_state()
    response = api.post(
        "/accounts/tag", json={"tag_id": 999_999, "filter": {"statuses": ["blocked"]}}
    )
    assert response.status_code == 422
    assert _tag_state() == before


def test_tag_rejects_an_unknown_filter_field(api, seed) -> None:
    """A mistyped filter field must not widen the selection to everything."""
    before = _tag_state()
    response = api.post(
        "/accounts/tag",
        json={"tag_id": seed.tags["VIP"], "filter": {"status": ["blocked"]}},
    )
    assert response.status_code == 422
    assert _tag_state() == before


def test_tag_by_an_empty_filter_covers_every_account(api, seed) -> None:
    response = api.post(
        "/accounts/tag", json={"tag_id": seed.tags["BULK"], "filter": {}}
    )
    assert response.json() == {"updated": SEED_COUNT}
    assert set(_tag_state().values()) == {seed.tags["BULK"]}


# --- Excel (unchanged behaviour) ----------------------------------------------
def test_import_then_export_still_work(api) -> None:
    payload = _legacy_upload([("a@test.com", "1"), ("b@test.com", "2")])
    files = {"file": ("inputs.xlsx", payload, _XLSX_MIME)}
    imported = api.post("/accounts/import", files=files)
    assert imported.status_code == 200, imported.text
    assert imported.json() == {"created": 2, "updated": 0, "skipped": 0, "rejected": []}
    assert _account_count() == 2

    export = api.get("/accounts/export")
    assert export.status_code == 200
    assert export.headers["content-type"] == _XLSX_MIME
    sheet = load_workbook(io.BytesIO(export.content)).active
    assert (
        sheet.cell(row=excel.FIRST_DATA_ROW, column=excel.COL_EMAIL).value
        == "a@test.com"
    )


def test_import_rejects_a_non_excel_file(api) -> None:
    files = {"file": ("notes.txt", b"hello", "text/plain")}
    assert api.post("/accounts/import", files=files).status_code == 400


@pytest.mark.parametrize(
    ("kind", "value"),
    [("n", "4111 1111 1111 4242"), ("d", "4111-1111-1111-4242")],
    ids=["spaced-number", "number-as-date"],
)
def test_an_unreadable_sheet_is_refused_without_quoting_a_cell(api, caplog, kind, value) -> None:
    """The legacy layout's column T holds a card number, which the import
    leaves unread — but openpyxl reads every cell, and its error for a badly
    typed one quotes the cell's text. Neither the answer nor the log may hold
    it, even with its spaces and dashes taken out; and nothing is imported."""
    import re
    import zipfile

    sheet = _legacy_upload([("a@test.com", "1")])
    source = zipfile.ZipFile(io.BytesIO(sheet))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                cell = f'<c r="T{excel.FIRST_DATA_ROW}" t="{kind}"><v>{value}</v></c>'.encode()
                # Written into the row the account is in, after its last cell.
                row = f'<row r="{excel.FIRST_DATA_ROW}"'.encode()
                start = data.index(row)
                end = data.index(b"</row>", start)
                data = data[:end] + cell + data[end:]
            target.writestr(item, data)
    files = {"file": ("inputs.xlsx", out.getvalue(), _XLSX_MIME)}

    response = api.post("/accounts/import", files=files)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail.startswith("Could not read the file")
    bare = lambda text: re.sub(r"[\W_]+", "", text)  # noqa: E731
    logged = "\n".join(record.getMessage() for record in caplog.records)
    for text in (detail, logged):
        assert value not in text and bare(value) not in bare(text)
    assert _account_count() == 0
