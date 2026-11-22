"""The courier and its tracking id (mapped live 27 Oct 2026, on an order that
had shipped). The order page shows neither; its "See All Updates" history has
them on the line right under the "Shipped" header: the courier's name, a dash,
the id. The lines below are that history's, with a made-up id and nothing of
the owner's.
"""

import asyncio
from datetime import datetime

from app.automation import sync
from app.db.models import Account, AccountStatus, Order, OrderStatus
from app.jobs import sync_tasks
from tests.test_readback_history import LIVE_HISTORY, LIVE_PRICES, _OrderPage

TODAY = datetime(2026, 10, 27)
TRACKING = "FMPC0000000001"
EKART = ("Ekart Logistics", TRACKING)

#: What "See All Updates" showed on a live shipped order (27 Oct 2026).
SHIPPED_HISTORY = [
    "\u2715",
    "Order ConfirmedSun, 25th Oct \'26",
    "Your Order has been placed.",
    "Sun, 25th Oct \'26 - 10:00pm",
    "Seller has processed your order.",
    "Mon, 26th Oct \'26 - 10:09am",
    "Your item has been picked up by delivery partner.",
    "Tue, 27th Oct \'26 - 10:48am",
    "ShippedTue, 27th Oct \'26",
    f"Ekart Logistics - {TRACKING}",
    "Your item has been shipped.",
    "Tue, 27th Oct \'26 - 10:50am",
    "Your item has arrived at a Flipkart Facility",
    "Tue, 27th Oct \'26 - 10:42am- Jaipur",
    "Item yet to reach hub nearest to you.",
    "Item yet to be delivered.",
    "DeliveryExpected By Sun 1st Nov",
    "Item yet to be delivered.",
    "Expected by Sun, 1st Nov",
]


def _shipped(courier_line, *after):
    return ["ShippedWed, 28th Oct \'26", courier_line, "Your item has been shipped.", *after]


def test_the_live_history_gives_the_courier_and_its_tracking_id():
    assert sync.parse_shipment(SHIPPED_HISTORY, today=TODAY) == EKART


def test_a_courier_s_number_of_digits_alone_is_read_too():
    assert sync.parse_shipment(_shipped("Delhivery - 1490811234567"), today=TODAY) == ("Delhivery", "1490811234567")


def test_a_courier_named_with_a_hyphen_or_brackets_and_a_long_dash_are_read():
    assert sync.parse_shipment(_shipped("Xpress-Bees - 1234567890123"), today=TODAY) == ("Xpress-Bees", "1234567890123")
    assert sync.parse_shipment(_shipped("Delhivery (Surface) - 1490811234567"), today=TODAY) == (
        "Delhivery (Surface)",
        "1490811234567",
    )
    assert sync.parse_shipment(_shipped(f"Ekart Logistics \u2013 {TRACKING}"), today=TODAY) == EKART


def test_an_order_not_shipped_yet_has_none():
    assert sync.parse_shipment(LIVE_HISTORY, today=TODAY) is None


def test_only_the_line_right_under_shipped_is_taken():
    """Another line of the section shaped like one is never taken for it."""
    assert sync.parse_shipment(_shipped("Your item has been shipped.", "Seller - ABC1234567"), today=TODAY) is None
    assert sync.parse_shipment(_shipped(f"Ekart Logistics - {TRACKING}", "Seller - ABC1234567"), today=TODAY) == EKART


def test_a_return_s_pickup_is_not_the_delivery_s():
    lines = [
        "ShippedTue, 27th Oct \'26",
        "Your item has been shipped.",
        "DeliveredThu, 29th Oct \'26",
        "ReturnedMon, 2nd Nov \'26",
        "Ekart Logistics - FMPR0000000002",
    ]
    assert sync.parse_shipment(lines, today=TODAY) is None
    assert sync.parse_shipment([*SHIPPED_HISTORY, *lines[3:]], today=TODAY) == EKART


def test_lines_that_only_look_like_one_are_not_read():
    for line in (
        "Seller - SUPERCOMNET",
        "Tue, 27th Oct \'26 - 10:42am- Jaipur",
        "Your item has been shipped - 27 Oct",
        f"Ekart Logistics-{TRACKING}",
    ):
        assert sync.parse_shipment(_shipped(line), today=TODAY) is None, line


def test_the_order_page_reader_records_the_courier_and_the_tracking_id():
    order = sync.ScrapedOrder(order_id="OD1", platform_status="Shipped")
    page = _OrderPage("Order Confirmed\nShipped\nOut For Delivery\n" + LIVE_PRICES, SHIPPED_HISTORY)

    asyncio.run(sync._read_detail(page, order))

    assert (order.shipment_partner, order.tracking_id) == EKART


def test_the_read_back_puts_them_on_the_unit_and_never_blanks_them(db):
    account = Account(email="ship@test.com", status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    order = Order(account_id=account.id, order_id="OD1", qty=1, status=OrderStatus.placed)
    db.add(order)
    db.commit()
    shipped = sync.ScrapedOrder(
        order_id="OD1", unit_id="U1", platform_status="Shipped", tracking_id=TRACKING, shipment_partner="Ekart Logistics"
    )

    sync_tasks._reconcile(account.id, [shipped], import_unknown=False)
    # A later page that names neither leaves both standing.
    sync_tasks._reconcile(account.id, [sync.ScrapedOrder(order_id="OD1", unit_id="U1", platform_status="Shipped")], import_unknown=False)

    db.expire_all()
    row = db.get(Order, order.id)
    assert row.tracking_id == TRACKING
    assert [(unit.shipment_partner, unit.tracking_id) for unit in row.units] == [EKART]
