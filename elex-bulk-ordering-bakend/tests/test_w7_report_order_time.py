"""The order's time beside a date not written DD-MM-YYYY (review of 27 Oct
2026): an ISO or slashed order_date was read whole by the id sheet's reader,
and the row's order_time dropped — the order filed at midnight IST."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.io import order_report
from tests.w7_report_rows import report, line

AT_10_10_15_IST = datetime(2026, 10, 24, 4, 40, 15)  # the stored clock is UTC
MIDNIGHT_IST = datetime(2026, 10, 23, 18, 30)


def _placed_at(**cells) -> datetime:
    (row,) = order_report.read_report(report(line(**cells)).encode("utf-8"), "report.csv").rows
    return row.placed_at


@pytest.mark.parametrize("day", ["24-10-2026", "2026-10-24", "24/10/2026"])
def test_the_order_time_goes_with_the_date_however_it_is_written(day):
    assert _placed_at(order_date=day, order_time="10:10:15") == AT_10_10_15_IST


def test_a_date_that_carries_its_own_time_keeps_it_when_order_time_is_empty():
    assert _placed_at(order_date="2026-10-24 10:10:15", order_time="") == AT_10_10_15_IST


def test_order_time_wins_over_a_time_in_the_date():
    assert _placed_at(order_date="24-10-2026 23:59:00", order_time="10:10:15") == AT_10_10_15_IST


def test_a_date_with_no_time_anywhere_is_midnight():
    assert _placed_at(order_date="2026-10-24", order_time="") == MIDNIGHT_IST


def test_an_unreadable_date_or_time_is_still_refused_by_row():
    text = report(line(order_date="24.10.2026"), line(unit_id="439123456789012345602", order_time="10h10"))
    out = order_report.read_report(text.encode("utf-8"), "report.csv")
    assert [(r.row_number, r.reason) for r in out.rejected] == [
        (2, "order_date is not a date: use DD-MM-YYYY"),
        (3, "order_time is not a time: use HH:MM:SS"),
    ]
