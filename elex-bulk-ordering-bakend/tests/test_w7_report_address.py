"""A unit's delivery address, from a report (review of 27 Oct 2026).

An address cell left empty in a later report blanked part of the held address
and set its pincode to None, raising a false "address change" and "address
mismatch"; and a row for a unit the read-back had seen counted as saying
otherwise than Flipkart's page whenever it gave an address at all, since the
read-back keeps the page's block ("Home · Tower 5, … 226013 · name · phone")
and a report its parts joined by commas. An address is now a place — its first
line and its pincode — as the alerts read it (spec C-5).
"""

from __future__ import annotations

from datetime import datetime

from app.core.alerts import evaluate
from app.db.models import Alert
from tests.w7_report_rows import UNITS, api, buyer, line, report, units_of, upload  # noqa: F401 — fixtures

ADDRESS = dict(
    delivery_name="Example Trading",
    delivery_line1="Tower 5, 1005",
    delivery_line2="Garden Bay",
    delivery_city="Lucknow",
    delivery_state="Uttar Pradesh",
    delivery_pincode="226013",
    delivery_phone="9999999999",
)
FULL = "Example Trading, Tower 5, 1005, Garden Bay, Lucknow, Uttar Pradesh, 226013, 9999999999"


def _alerts(db) -> list[str]:
    db.expire_all()
    return sorted(alert.key for alert in db.query(Alert).all())


def test_a_report_repeating_the_address_without_its_pincode_and_phone_moves_nothing(api, db, buyer):
    upload(api, report(line(**ADDRESS)))
    evaluate(db)
    before = _alerts(db)
    assert any(key.startswith("address_mismatch:") for key in before)  # the book is empty

    again = dict(ADDRESS, delivery_pincode="", delivery_phone="")
    out = upload(api, report(line(**again))).json()

    assert (out["updated"], out["unchanged"]) == (0, 1)
    (unit,) = units_of(db)
    assert (unit.delivery_address, unit.delivery_pincode) == (FULL, "226013")
    assert unit.address_changed_at is None
    evaluate(db)
    assert _alerts(db) == before


def test_the_same_place_in_fewer_words_keeps_the_fuller_address(api, db, buyer):
    upload(api, report(line(**ADDRESS)))

    out = upload(api, report(line(**dict(ADDRESS, delivery_phone="", delivery_name="")))).json()

    assert out["unchanged"] == 1
    (unit,) = units_of(db)
    assert (unit.delivery_address, unit.delivery_pincode, unit.address_changed_at) == (FULL, "226013", None)


def test_the_same_place_in_more_words_is_taken_without_a_change(api, db, buyer):
    upload(api, report(line(delivery_line1="Tower 5, 1005", delivery_pincode="226013")))

    out = upload(api, report(line(**ADDRESS))).json()

    assert out["updated"] == 1
    (unit,) = units_of(db)
    assert (unit.delivery_address, unit.address_changed_at) == (FULL, None)


def test_another_first_line_is_a_change(api, db, buyer):
    upload(api, report(line(**ADDRESS)))

    upload(api, report(line(**dict(ADDRESS, delivery_line1="Tower 9, 901"))))

    (unit,) = units_of(db)
    assert "Tower 9, 901" in unit.delivery_address and unit.delivery_pincode == "226013"
    assert unit.address_changed_at is not None


def test_a_row_without_a_first_line_gives_no_address(api, db, buyer):
    out = upload(api, report(line(**dict(ADDRESS, delivery_line1="")))).json()

    assert out["created"] == 1
    (unit,) = units_of(db)
    assert (unit.delivery_address, unit.delivery_pincode) == (None, None)


def test_the_ledgers_export_gives_no_address(api, db, buyer):
    """Its "Delivery State" column holds the unit's delivery reading
    ("in_transit"), and its name is the template's delivery_state: handed back,
    it gave every unit the address "in_transit"."""
    upload(api, report(line()))
    exported = api.get("/orders/export.csv").text.lstrip("﻿")
    assert "Delivery State" in exported.splitlines()[0]

    out = upload(api, exported).json()

    assert (out["updated"], out["unchanged"], out["rejected"]) == (0, 1, [])
    (unit,) = units_of(db)
    assert (unit.delivery_address, unit.delivery_pincode) == (None, None)


def _read_back(db, address: str, pincode: str | None) -> None:
    (unit,) = units_of(db)
    unit.synced_at = datetime(2026, 10, 26, 6, 0)
    unit.delivery_address, unit.delivery_pincode = address, pincode
    db.commit()


PAGE_BLOCK = "Home · Tower 5, 1005, Garden Bay, Lucknow, Uttar Pradesh 226013 · Example Trading · 9999999999"


def test_the_place_flipkarts_page_shows_is_not_counted_as_said_otherwise(api, db, buyer):
    upload(api, report(line()))
    _read_back(db, PAGE_BLOCK, "226013")

    out = upload(api, report(line(**ADDRESS))).json()

    assert (out["kept_platform"], out["unchanged"]) == (0, 1)
    (unit,) = units_of(db)
    assert unit.delivery_address == PAGE_BLOCK  # Flipkart's reading stands


def test_another_place_than_flipkarts_page_is_counted(api, db, buyer):
    upload(api, report(line()))
    _read_back(db, PAGE_BLOCK, "226013")

    out = upload(api, report(line(**dict(ADDRESS, delivery_line1="Tower 9, 901")))).json()

    assert out["kept_platform"] == 1
    (unit,) = units_of(db)
    assert unit.delivery_address == PAGE_BLOCK


def test_a_page_block_whose_pincode_was_not_read_is_matched_by_its_text(api, db, buyer):
    upload(api, report(line()))
    _read_back(db, PAGE_BLOCK, None)

    same = upload(api, report(line(**ADDRESS))).json()
    other = upload(api, report(line(**dict(ADDRESS, delivery_pincode="226010")))).json()

    assert (same["kept_platform"], other["kept_platform"]) == (0, 1)
