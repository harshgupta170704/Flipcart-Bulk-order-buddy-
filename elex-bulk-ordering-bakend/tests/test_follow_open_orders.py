"""Orders on their way beyond the recent list, and pages without a history
panel (26 Oct 2026).

Found live: Flipkart's order list shows one row per unit, and a read-back
takes its 20 newest rows — one ten-unit order fills half of them — so an
older order still on its way was never read again, and stayed "placed" after
its seller had cancelled it. A cancelled order's page has no "See All
Updates" panel; its own step lines carry the dates ("Cancelled, Today, Oct 26").
"""

import asyncio
import contextlib
from datetime import datetime, timedelta

from app.automation import sync
from app.db.models import (
    Account,
    AccountStatus,
    Job,
    JobItem,
    JobItemStatus,
    JobStatus,
    JobType,
    Order,
    OrderStatus,
    Session as SessionModel,
    naive_utcnow,
)
from app.jobs import schedule, sync_tasks
from tests.fakes import FakeContext, FakePage
from tests.test_schedule import _run_immediately

TODAY = datetime(2026, 10, 26)
REASON = "Sorry, your order was cancelled as per seller policy restrictions applicable to this brand."
#: A cancelled order's status block as the live page showed it (made-up product).
CANCELLED_PAGE = "\n".join(
    [
        "OD100000000000000001", "Order can be tracked by [number].", "Some Kurta", "3XL, Multicolor",
        "Seller: Some Seller", "₹235", "Order Confirmed, Sun Oct 25", "Cancelled, Today, Oct 26", REASON,
        "Chat with us", "Send Order Details", "Delivery details", "Price details",
    ]
)
#: An order on its way, as its page prints its steps without the history panel.
IN_TRANSIT_PAGE = "\n".join(
    [
        "Order Confirmed", "Seller has processed your order., Mon 26th Oct", "Shipped, Expected By Oct 27",
        "Out For Delivery", "Delivery, Sun Nov 01 By 11 PM", "Change Date", "See All Updates", "Cancel", "Chat with us",
    ]
)


# --- the page's own dated step lines ----------------------------------------------------------------------
def test_a_cancelled_pages_step_lines_give_the_days():
    assert sync.parse_page_steps(CANCELLED_PAGE, today=TODAY) == [
        ("Order Confirmed", datetime(2026, 10, 25)),
        ("Cancelled", datetime(2026, 10, 26)),
    ]


def test_another_items_step_lower_on_the_page_is_not_this_ones():
    page = CANCELLED_PAGE + "\nOther items in this order\nDelivered, Thu Oct 22"
    assert [name for name, _ in sync.parse_page_steps(page, today=TODAY)] == ["Order Confirmed", "Cancelled"]


def test_an_order_on_its_way_has_no_dated_step_lines():
    # The promise ("Delivery, Sun Nov 01 By 11 PM") and an expected step are never events.
    assert sync.parse_page_steps(IN_TRANSIT_PAGE, today=TODAY) == []


def test_a_delivered_step_line_is_one():
    assert sync.parse_page_steps("Delivered, Thu Oct 29\nChat with us", today=TODAY) == [("Delivered", datetime(2026, 10, 29))]


def test_an_order_read_by_id_takes_its_state_from_them():
    order = sync.ScrapedOrder(order_id="OD1")  # by id: no list row, so no status yet
    page = FakePage(inner_text_map={sync._DETAIL_TEXT: CANCELLED_PAGE})

    asyncio.run(sync._read_detail(page, order))

    assert order.platform_status == "Cancelled"
    assert order.timeline[-1]["status"] == "Cancelled" and order.timeline[-1]["date"] is not None
    assert order.cancel_reason == REASON


# --- which orders are followed ------------------------------------------------------------------------------
def _account(db, email="follow@test.com"):
    account = Account(email=email, status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    db.add(SessionModel(account_id=account.id, cookies=[{"name": "SN", "value": email}], valid=True))
    db.commit()
    return account


def _order(db, account, order_id, *, days_ago=1, status=OrderStatus.placed, state=None):
    order = Order(account_id=account.id, product_link="https://www.flipkart.com/p", qty=1, status=status, order_id=order_id)
    db.add(order)
    db.commit()
    order.placed_at = naive_utcnow() - timedelta(days=days_ago)
    if state is not None:
        unit = order.units[0]
        unit.delivery_state, unit.platform_status, unit.synced_at = state, state.title(), naive_utcnow()
    db.commit()
    return order


def test_open_order_ids_are_the_placed_ones_still_on_their_way(db):
    account = _account(db)
    _order(db, account, "OD-NEVER-READ")
    _order(db, account, "OD-ON-ITS-WAY", state="in_transit")
    _order(db, account, "OD-DELIVERED", state="delivered")
    _order(db, account, "OD-OLD", days_ago=90)
    _order(db, account, "OD-FAILED", status=OrderStatus.failed)

    # Never read first, then the least recently read.
    assert schedule.open_order_ids(db, account.id) == ["OD-NEVER-READ", "OD-ON-ITS-WAY"]


def _fakes(monkeypatch, *, listed, by_id_status="Cancelled"):
    read_by_id: list[str] = []

    @contextlib.asynccontextmanager
    async def fake_browser_page(**kwargs):
        page = FakePage()
        page.context = FakeContext(kwargs.get("cookies"))
        yield page.context, page

    async def scrape_orders(page, *, limit=20, open_details=True, should_stop=None):
        return [sync.ScrapedOrder(order_id=order_id, unit_id=f"U-{order_id}", platform_status="Shipped") for order_id in listed]

    async def scrape_order(page, order_id):
        read_by_id.append(order_id)
        return [sync.ScrapedOrder(order_id=order_id, platform_status=by_id_status)]

    monkeypatch.setattr(sync_tasks, "browser_page", fake_browser_page)
    monkeypatch.setattr(sync_tasks, "scrape_orders", scrape_orders)
    monkeypatch.setattr(sync_tasks, "scrape_order", scrape_order)
    return read_by_id


def _run(db, account, **kwargs):
    job = Job(type=JobType.sync, status=JobStatus.queued)
    db.add(job)
    db.commit()
    asyncio.run(sync_tasks.run_sync_job(job.id, [account.id], **kwargs))
    return job


def test_a_tracking_read_back_reads_by_id_the_open_orders_its_list_missed(db, monkeypatch):
    account = _account(db)
    _order(db, account, "OD-LISTED")
    old = _order(db, account, "OD-BEYOND", days_ago=2)
    read_by_id = _fakes(monkeypatch, listed=["OD-LISTED"])

    _run(db, account, follow_open=True)

    assert read_by_id == ["OD-BEYOND"]
    db.expire_all()
    assert db.get(Order, old.id).status == OrderStatus.failed  # its seller had cancelled it


def test_other_read_backs_do_not(db, monkeypatch):
    account = _account(db)
    _order(db, account, "OD-BEYOND", days_ago=2)
    read_by_id = _fakes(monkeypatch, listed=[])

    _run(db, account)

    assert read_by_id == []


def test_no_more_than_the_cap_are_read_by_id(db, monkeypatch):
    account = _account(db)
    for number in range(sync_tasks.FOLLOW_OPEN_MAX + 3):
        _order(db, account, f"OD-{number:02d}", days_ago=2)
    read_by_id = _fakes(monkeypatch, listed=[], by_id_status="Shipped")

    _run(db, account, follow_open=True)

    assert len(read_by_id) == sync_tasks.FOLLOW_OPEN_MAX


def test_over_the_cap_the_passes_work_through_every_open_order(db, monkeypatch):
    account = _account(db)
    for number in range(sync_tasks.FOLLOW_OPEN_MAX + 2):
        _order(db, account, f"OD-{number:02d}", days_ago=2)
    read_by_id = _fakes(monkeypatch, listed=[], by_id_status="Shipped")

    _run(db, account, follow_open=True)
    first = list(read_by_id)
    read_by_id.clear()
    _run(db, account, follow_open=True)

    left_out = {f"OD-{n:02d}" for n in range(sync_tasks.FOLLOW_OPEN_MAX + 2)} - set(first)
    assert len(left_out) == 2
    assert set(read_by_id[:2]) == left_out  # the ones the first pass did not reach come first


def test_the_timers_tracking_read_back_follows_them(db, monkeypatch):
    calls: list = []
    _run_immediately(monkeypatch, calls)
    account = _account(db)

    schedule.start_tracking_read_back(db, [account.id])

    assert calls[0][2]["follow_open"] is True
