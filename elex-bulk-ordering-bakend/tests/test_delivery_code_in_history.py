"""The delivery code is looked for in the "See All Updates" panel as well as
on the order page, and an out-for-delivery order with no code read keeps both
texts for mapping (26-27 Oct 2026: no order had yet been out for delivery, so
where Flipkart puts the code is still to be seen)."""

import asyncio
from pathlib import Path

from app.automation import sync
from tests.test_readback_history import _OrderPage

OFD_HISTORY = ["Out For DeliveryWed, 28th Oct \'26", "Your item is out for delivery", "Delivery OTP 4827"]


def test_a_code_shown_only_in_the_history_is_read():
    order = sync.ScrapedOrder(order_id="OD1", platform_status="Out For Delivery")
    page = _OrderPage("Order Confirmed\nShipped\nOut For Delivery", OFD_HISTORY)

    asyncio.run(sync._read_detail(page, order))

    assert order.delivery_otp == "4827"


def test_with_no_code_anywhere_both_texts_are_kept():
    order = sync.ScrapedOrder(order_id="OD2", platform_status="Out For Delivery")
    page = _OrderPage("Order Confirmed\nShipped\nOut For Delivery", ["Out For DeliveryWed, 28th Oct \'26", "Your item is out for delivery"])

    asyncio.run(sync._read_detail(page, order))

    assert order.delivery_otp is None
    [kept] = list(Path(sync.DIAGNOSTICS_DIR).glob("*-out-for-delivery-OD2.txt"))
    text = kept.read_text(encoding="utf-8")
    assert "Shipped" in text and "Your item is out for delivery" in text


def test_a_code_on_the_page_is_still_read_first():
    order = sync.ScrapedOrder(order_id="OD3", platform_status="Out For Delivery")
    page = _OrderPage("Out For Delivery\nDelivery OTP 1357", ["Delivery OTP 2468"])

    asyncio.run(sync._read_detail(page, order))

    assert order.delivery_otp == "1357"
