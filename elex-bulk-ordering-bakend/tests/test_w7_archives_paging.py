"""The Download Compiled Invoices list, a page at a time (spec US3, FR-038:
the operator downloads the new archive "or any earlier archive").

``GET /invoices/archives`` answered the newest 200 and nothing past them: no
way to ask for the next ones and no count, so from the 201st archive on the
oldest could not be listed, downloaded or deleted from the dialog until the
age sweep took them (audit of 27 Oct 2026). Now it pages by a key —
``before=<created_at>,<id>``, the ``next_before`` of the page before — and
says how many archives there are in all.

A key and not an offset: Delete in the same dialog takes rows out of the list
already shown, and an offset counted after that would step over an archive.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import invoices as invoices_api
from app.core import archives
from app.core.security import create_access_token
from app.db.models import CompiledArchive

#: A made-up moment with microseconds, so the cursor has to carry them exactly.
T0 = datetime(2026, 10, 1, 9, 30, 0, 250_000)


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """A delete removes files from the test's own folder, never the configured store."""
    monkeypatch.setattr(archives, "archive_root", lambda: tmp_path / "archives")


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(invoices_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


def _archives(db, stamps: list[datetime]) -> None:
    """One archive row per stamp, added in the order given."""
    for n, at in enumerate(stamps):
        db.add(CompiledArchive(name=f"a{n:04d}.zip", prefix="a", path=f"a{n:04d}.zip", size=1, created_at=at))
        db.commit()


def _newest_first(db) -> list[int]:
    """Every archive's id in the list's own order — newest first, the higher
    id first between two of the same moment — read straight from the table."""
    rows = db.query(CompiledArchive).all()
    return [row.id for row in sorted(rows, key=lambda row: (row.created_at, row.id), reverse=True)]


def _page(api, **params) -> dict:
    response = api.get("/invoices/archives", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _walk(api, limit: int) -> tuple[list[int], list[dict]]:
    """Follow ``next_before`` from the first page to the last: the ids in the
    order they were listed, and every page as it came."""
    ids: list[int] = []
    pages: list[dict] = []
    before = None
    while True:
        page = _page(api, limit=limit) if before is None else _page(api, limit=limit, before=before)
        pages.append(page)
        ids += [item["id"] for item in page["items"]]
        before = page["next_before"]
        if before is None:
            return ids, pages
        assert len(pages) <= 50, "the cursor never moved on"


def test_every_archive_past_the_first_200_can_be_listed(api, db):
    _archives(db, [T0 + timedelta(minutes=n) for n in range(230)])

    first = _page(api)  # what the dialog asks for when it opens

    assert (len(first["items"]), first["total"]) == (200, 230)
    assert first["next_before"] is not None
    ids, pages = _walk(api, 200)
    assert [len(page["items"]) for page in pages] == [200, 30]
    assert ids == _newest_first(db)  # each once, newest first, none left out
    assert [page["total"] for page in pages] == [230, 230]


def test_archives_of_the_same_moment_are_split_by_id_with_none_skipped_or_repeated(api, db):
    # Five of one moment between others, some a microsecond apart, added out
    # of time order so the ids do not follow the stamps: with two to a page,
    # page boundaries fall inside the tie.
    tie = T0 + timedelta(seconds=1)
    _archives(
        db,
        [T0 + timedelta(seconds=2), tie, T0, tie, T0 + timedelta(microseconds=1), tie, tie, tie, T0 + timedelta(seconds=3)],
    )

    ids, pages = _walk(api, 2)

    assert ids == _newest_first(db)
    assert len(ids) == len(set(ids)) == 9
    assert [len(page["items"]) for page in pages] == [2, 2, 2, 2, 1]
    tied = [row.id for row in db.query(CompiledArchive).filter(CompiledArchive.created_at == tie)]
    assert [i for i in ids if i in tied] == sorted(tied, reverse=True)


def test_deleting_shown_archives_does_not_make_the_next_page_skip_any(api, db):
    _archives(db, [T0 + timedelta(minutes=n) for n in range(8)])
    order = _newest_first(db)
    first = _page(api, limit=3)
    assert [item["id"] for item in first["items"]] == order[:3]

    # The operator deletes two of the rows on screen, the page's last among them.
    for archive_id in (order[0], order[2]):
        assert api.delete(f"/invoices/archives/{archive_id}").status_code == 204

    second = _page(api, limit=3, before=first["next_before"])
    assert [item["id"] for item in second["items"]] == order[3:6]
    assert second["total"] == 6
    third = _page(api, limit=3, before=second["next_before"])
    assert [item["id"] for item in third["items"]] == order[6:]
    assert third["next_before"] is None


def test_the_cursor_names_the_last_archive_of_its_page(api, db):
    _archives(db, [T0 + timedelta(minutes=n) for n in range(3)])

    first = _page(api, limit=2)

    stamp, _, last_id = first["next_before"].rpartition(",")
    assert int(last_id) == first["items"][-1]["id"]
    assert datetime.fromisoformat(stamp) == datetime.fromisoformat(first["items"][-1]["created_at"])


def test_a_cursor_with_a_zone_is_read_as_the_utc_moment(api, db):
    _archives(db, [T0 + timedelta(minutes=n) for n in range(3)])
    order = _newest_first(db)
    middle = db.get(CompiledArchive, order[1])
    in_india = (middle.created_at + timedelta(hours=5, minutes=30)).isoformat() + "+05:30"

    page = _page(api, before=f"{in_india},{middle.id}")

    assert [item["id"] for item in page["items"]] == order[2:]


def test_no_archive_is_an_empty_page(api):
    assert _page(api) == {"items": [], "total": 0, "next_before": None}


@pytest.mark.parametrize(
    "before",
    [
        "",
        "57",
        "2026-10-01T09:30:00",
        "2026-10-01T09:30:00,",
        ",57",
        "yesterday,57",
        "2026-13-01T09:30:00,57",
        "2026-10-01T09:30:00,-1",
        "2026-10-01T09:30:00,5x",
        "2026-10-01T09:30:00," + "9" * 19,
        "0001-01-01T00:00:00+05:30,1",
        "2026-10-01T09:30:00," + "1" * 80,
    ],
)
def test_a_cursor_that_names_no_place_in_the_list_is_refused(api, db, before):
    _archives(db, [T0])

    response = api.get("/invoices/archives", params={"before": before})

    assert response.status_code == 422, response.text


def test_the_page_size_keeps_its_bounds(api, db):
    _archives(db, [T0])

    assert api.get("/invoices/archives", params={"limit": 0}).status_code == 422
    assert api.get("/invoices/archives", params={"limit": 1001}).status_code == 422
    page = _page(api, limit=1000)
    assert (len(page["items"]), page["total"], page["next_before"]) == (1, 1, None)
