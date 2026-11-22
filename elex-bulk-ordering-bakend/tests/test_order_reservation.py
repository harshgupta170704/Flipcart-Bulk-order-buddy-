"""The record is written BEFORE the order is sent.

Everything after the send can be cut short — Ctrl+C in the CLI, an API restart
cancelling the job, a crash — and none of the handlers after it run. A record
written only afterwards might then never be written, and an order that exists
with no record is invisible to the unit limits, the alerts and the next run,
which buys it again.
"""

import asyncio

from app.automation import ordering as order_flow
from app.automation.ordering import PAYMENT_COD, AddressInfo, GstInfo
from app.core import selectors as S
from app.db.models import OrderStatus
from tests.fakes import FakePage

ADDR = AddressInfo(name="N", phone="", pin="226013", area="Lucknow")
GST = GstInfo(gstin="", business_name="")


def _run(page, reserve):
    return asyncio.run(
        order_flow.run_order(
            page,
            product_link="https://dl.flipkart.com/s/uFy91dNNNN",
            address=ADDR,
            gst=GST,
            card=None,
            qty=1,
            target=18_000.0,
            payment_mode=PAYMENT_COD,
            reserve=reserve,
        )
    )


def test_the_record_is_written_before_confirm_order_is_pressed():
    page = FakePage(inner_text_map={S.ORDER_TOTAL: "₹17,707"})
    seen: list[dict] = []

    def reserve(row: dict) -> int:
        seen.append({**row, "clicks_so_far": list(page.clicks)})
        return 41

    result = _run(page, reserve)

    assert result["status"] == OrderStatus.placed
    assert result["reservation"] == 41
    assert len(seen) == 1
    row = seen[0]
    assert row["status"] == OrderStatus.unconfirmed  # until Flipkart confirms
    assert row["amount"] == 17_707.0
    assert row["payment_mode"] == PAYMENT_COD
    # Written before anything was sent: neither order control had been pressed.
    assert S.COD_PLACE_ORDER_BTN not in row["clicks_so_far"]
    assert S.COD_CONFIRM_ORDER_BTN not in row["clicks_so_far"]


def test_a_record_that_cannot_be_written_stops_the_order_before_sending():
    page = FakePage(inner_text_map={S.ORDER_TOTAL: "₹17,707"})

    def reserve(_row: dict) -> int:
        raise RuntimeError("database is locked")

    result = _run(page, reserve)

    assert result["status"] == OrderStatus.failed
    assert "could not record the order before sending it" in result["error"]
    assert not page.submitted
    assert S.COD_PLACE_ORDER_BTN not in page.clicks


def test_nothing_is_reserved_when_the_gate_refuses():
    page = FakePage(inner_text_map={S.ORDER_TOTAL: "₹18,000.01"})
    calls: list[dict] = []
    result = _run(page, lambda row: calls.append(row) or 1)
    assert result["status"] == OrderStatus.over_target
    assert calls == []


def test_the_reservation_rides_on_every_outcome_after_the_send():
    page = FakePage(inner_text_map={S.ORDER_TOTAL: "₹17,707"})
    page.confirmation_url = None  # never confirmed
    result = _run(page, lambda row: 7)
    assert result["status"] == OrderStatus.unconfirmed
    assert result["reservation"] == 7


# --- a read-back that settles the row while the run is still waiting -------------
_COLUMNS = dict(
    product_link="https://www.flipkart.com/p/itm0c2cd3b721e22",
    qty=1,
    target=10_000.0,
    address=ADDR,
    gst=GST,
    card_number="4111111111111111",
)


def _reserved_row(db) -> tuple[int, int]:
    from app.db.models import Account
    from app.jobs import tasks

    account = Account(email="card@test.com")
    db.add(account)
    db.commit()
    row_id = tasks.reserve_order(
        account.id, {"amount": 759.0, "payment_mode": "card", "model": "Pigeon Special 3 L"}, **_COLUMNS
    )
    return account.id, row_id


def _adopt(db, row_id: int, order_id: str, status: OrderStatus) -> None:
    """What sync_tasks._adopt_unconfirmed does to the row."""
    from app.db.models import Order

    row = db.get(Order, row_id)
    row.order_id = order_id
    row.status = status
    db.commit()


def _settle(account_id: int, row_id: int, result: dict) -> None:
    from app.jobs import tasks

    tasks.settle_order_row(row_id, account_id, {"payment_mode": "card", "amount": 759.0, **result}, **_COLUMNS)


def _row(db, row_id: int):
    from app.db.models import Order

    db.expire_all()
    return db.get(Order, row_id)


def test_the_runs_unconfirmed_does_not_undo_a_read_back_that_found_the_order(db):
    """The timer's read-backs can run beside an order. One that finds the order
    on Flipkart first files its id on the reserved row; the run's own "never
    confirmed", written after, used to send the row back to "may exist"."""
    account_id, row_id = _reserved_row(db)
    _adopt(db, row_id, "OD338990000000000100", OrderStatus.placed)

    _settle(account_id, row_id, {"status": OrderStatus.unconfirmed, "order_id": None})

    row = _row(db, row_id)
    assert row.status == OrderStatus.placed
    assert row.order_id == "OD338990000000000100"
    assert row.card_number == "4111111111111111"  # the order was paid with it


def test_a_read_back_that_found_the_order_cancelled_is_kept_too(db):
    account_id, row_id = _reserved_row(db)
    _adopt(db, row_id, "OD338990000000000100", OrderStatus.failed)

    _settle(account_id, row_id, {"status": OrderStatus.unconfirmed, "order_id": None})

    row = _row(db, row_id)
    assert row.status == OrderStatus.failed and row.order_id == "OD338990000000000100"


def test_the_runs_own_confirmation_still_wins(db):
    """Flipkart's confirmation page, read by the run itself, is the proof."""
    account_id, row_id = _reserved_row(db)
    _adopt(db, row_id, "OD338990000000000100", OrderStatus.placed)

    _settle(account_id, row_id, {"status": OrderStatus.placed, "order_id": "OD338990000000000200"})

    row = _row(db, row_id)
    assert row.status == OrderStatus.placed and row.order_id == "OD338990000000000200"


def test_a_run_that_never_sent_anything_clears_a_wrong_adoption(db):
    """Nothing left the browser, so whatever a read-back filed on the row was
    some other order: the run's failure stands, and the id goes."""
    account_id, row_id = _reserved_row(db)
    _adopt(db, row_id, "OD338990000000000100", OrderStatus.placed)

    _settle(account_id, row_id, {"status": OrderStatus.failed, "order_id": None})

    row = _row(db, row_id)
    assert row.status == OrderStatus.failed and row.order_id is None
    assert row.card_number is None


def test_an_unsettled_reservation_takes_the_runs_unconfirmed_as_before(db):
    account_id, row_id = _reserved_row(db)

    _settle(account_id, row_id, {"status": OrderStatus.unconfirmed, "order_id": None})

    row = _row(db, row_id)
    assert row.status == OrderStatus.unconfirmed and row.order_id is None
