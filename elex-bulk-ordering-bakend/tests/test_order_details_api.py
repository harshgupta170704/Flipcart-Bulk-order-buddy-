"""The order-details endpoints, in the shape of the owner's own API document
(docs/specs/order-unit-ledger/inputs/Flipkart Order Details Get API.pdf)."""

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import order_details
from app.api import orders as orders_api
from app.automation.sync import ScrapedOrder
from app.core import cancellations
from app.core.order_units import apply_observations
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Gst, Order, OrderStatus, UnitRequest
from app.jobs import sync_tasks


class _Seen:
    def __init__(self, **fields):
        for name in ("order_id", "unit_id", "item_id", "platform_status", "delivery_date", "seller", "tracking_id", "invoice_url", "delivery_otp"):
            setattr(self, name, None)
        self.__dict__.update(fields)


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(order_details.router)
    # The ledger page's own rows (GET /orders), to hold this API's answers against.
    application.include_router(orders_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def ledger(db) -> dict:
    account = Account(email="Buyer@test.com", status=AccountStatus.new)
    gst = Gst(label="Main", gstin="09AALCK2977Q1ZG", business_name="Kikaku Exim Private Limited")
    db.add_all([account, gst])
    db.commit()
    order = Order(
        account_id=account.id,
        order_id="OD334935538466132100",
        status=OrderStatus.placed,
        model="iPhone 15 128GB",
        color="Black",
        qty=2,
        amount=98300.0,
        gst_name="Kikaku Exim Private Limited",
        pin="226010",
        payment_mode="card",
        placed_at=datetime(2025, 4, 17, 9, 12, 34),
    )
    other = Order(
        account_id=account.id,
        order_id="OD334935538466132101",
        status=OrderStatus.placed,
        model="POCO M6",
        qty=1,
        amount=15339.0,
        payment_mode="cod",
        placed_at=datetime(2025, 4, 18, 12, 41, 10),
    )
    never = Order(account_id=account.id, status=OrderStatus.failed)  # no id: not an order
    db.add_all([order, other, never])
    db.commit()
    apply_observations(
        db,
        order,
        [
            _Seen(order_id=order.order_id, unit_id="u1", platform_status="Delivered", tracking_id="BD1"),
            _Seen(order_id=order.order_id, unit_id="u2", platform_status="Shipped"),
        ],
    )
    db.commit()
    return {"account": account, "order": order, "other": other}


def test_orders_by_email_within_a_date_range(api, ledger):
    out = api.post(
        "/orders/orderDetails",
        json={"emailid": "buyer@test.com", "offset": 0, "limit": "20", "fromDate": "2025-04-17", "toDate": "2025-04-17"},
    ).json()

    assert out["success"] is True
    assert out["pageInfo"] == {"offset": 0, "limit": 20, "count": 1, "total": 1, "hasMore": False, "nextOffset": 20}
    [entry] = out["data"]
    assert entry["orderId"] == "OD334935538466132100"
    assert entry["email"] == "Buyer@test.com"
    assert (entry["productModel"], entry["colour"], entry["quantity"], entry["totalAmount"]) == (
        "iPhone 15 128GB",
        "Black",
        2,
        98300.0,
    )
    assert (entry["gstNumber"], entry["gstHolder"]) == ("09AALCK2977Q1ZG", "Kikaku Exim Private Limited")
    assert entry["orderDate"] == "2025-04-17T09:12:34Z"
    assert entry["pincode"] == "226010"
    assert (entry["downloadStatus"], entry["invoiceFilePath"]) == ("PENDING", None)
    assert (entry["paymentMethod"], entry["paymentStatus"]) == ("CARD", "CAPTURED")
    # The document's vocabulary for the state, and Flipkart's own wording beside
    # it — of the unit still on its way, not the delivered one at position 1:
    # one unit of two delivered is not a delivered order.
    assert (entry["status"], entry["platformStatus"], entry["deliveryState"]) == ("SHIPPED", "Shipped", "in_transit")
    # The order stands: its stored status, and nobody cancelled it.
    assert (entry["outcome"], entry["cancelledBy"]) == ("placed", None)


def test_paging_and_no_email_means_every_account(api, ledger):
    first = api.post("/orders/orderDetails", json={"limit": 1}).json()
    assert first["pageInfo"]["total"] == 2 and first["pageInfo"]["hasMore"] is True
    assert first["data"][0]["orderId"] == "OD334935538466132101"  # newest first
    second = api.post("/orders/orderDetails", json={"limit": 1, "offset": 1}).json()
    assert second["data"][0]["orderId"] == "OD334935538466132100"
    assert second["pageInfo"]["hasMore"] is False


def test_one_order_in_full(api, ledger):
    out = api.post(
        "/orders/orderDetails", json={"orderId": "od334935538466132100", "emailid": "buyer@test.com"}
    ).json()
    data = out["data"]
    assert (data["orderId"], data["channel"], data["platform"], data["currency"]) == (
        "OD334935538466132100",
        "ECOM",
        "flipkart",
        "INR",
    )
    assert [(i["unitId"], i["status"], i["platformStatus"], i["deliveryState"]) for i in data["items"]] == [
        ("U1", "DELIVERED", "Delivered", "delivered"),
        ("U2", "SHIPPED", "Shipped", "in_transit"),
    ]
    assert data["shipments"] == [
        {
            "shipmentId": None,
            "unitId": "U1",
            "carrier": None,
            "trackingNo": "BD1",
            "shippedAt": None,
            "estimatedDelivery": None,
            "deliveredOn": None,
        }
    ]
    assert data["customer"] == {
        "name": None,
        "phone": None,
        "gstNumber": "09AALCK2977Q1ZG",
        "gstHolder": "Kikaku Exim Private Limited",
    }
    assert data["payments"] == [
        {"paymentId": None, "provider": None, "method": "CARD", "amount": 98300.0, "status": "CAPTURED", "paidAt": None}
    ]
    assert data["invoice"]["downloadStatus"] == "PENDING"
    assert data["totals"] == {
        "subtotal": None,
        "tax": None,
        "discount": None,
        "shipping": None,
        "grandTotal": 98300.0,
        "target": None,
    }
    # Every key the document shows is present, null where the ledger holds nothing.
    assert set(data["addresses"]["shipping"]) == {"name", "line1", "city", "state", "postalCode", "country"}
    assert data["addresses"]["billing"] == {"sameAsShipping": None}
    assert {"sku", "unitPrice", "tax", "discount"} <= set(data["items"][0])
    assert data["metadata"]["source"] == "RUN"


def test_a_cancelled_units_date_is_neither_a_promise_nor_a_delivery(api, db, ledger):
    """Flipkart's timeline for a cancelled unit ends on the cancellation date,
    and that is the date the read-back keeps. The document's estimatedDelivery
    must not show it — the same rule as the CSV's two date columns
    (orders_csv.delivery_dates): a promise until delivered, the delivery day
    after, nothing for a unit that will not arrive."""
    order = ledger["order"]
    apply_observations(
        db,
        order,
        [
            _Seen(order_id=order.order_id, unit_id="u1", platform_status="Delivered", delivery_date=datetime(2025, 4, 20)),
            _Seen(order_id=order.order_id, unit_id="u2", platform_status="Cancelled", tracking_id="BD2", delivery_date=datetime(2025, 4, 19)),
        ],
    )
    db.commit()

    data = api.post("/orders/orderDetails", json={"orderId": order.order_id}).json()["data"]
    shipments = {s["unitId"]: s for s in data["shipments"]}
    assert (shipments["U1"]["estimatedDelivery"], shipments["U1"]["deliveredOn"]) == (None, "2025-04-20")
    assert (shipments["U2"]["estimatedDelivery"], shipments["U2"]["deliveredOn"]) == (None, None)
    items = {i["unitId"]: i for i in data["items"]}
    assert (items["U1"]["deliveryDate"], items["U2"]["deliveryDate"]) == ("2025-04-20", None)

    # Back in transit, the date is the promise again.
    apply_observations(db, order, [_Seen(order_id=order.order_id, unit_id="u2", platform_status="Shipped", delivery_date=datetime(2025, 4, 21))])
    db.commit()
    data = api.post("/orders/orderDetails", json={"orderId": order.order_id}).json()["data"]
    shipments = {s["unitId"]: s for s in data["shipments"]}
    assert (shipments["U2"]["estimatedDelivery"], shipments["U2"]["deliveredOn"]) == ("2025-04-21", None)


def test_a_late_delivery_shows_its_promise_beside_the_day_it_came(api, db, ledger):
    """With the promise read apart (``promised_date``), the document's two days
    are the CSV's (orders_csv.delivery_days): on its way, the promise and no
    delivery; delivered, the promise beside the day it came — not the delivery
    day twice, which made every late delivery look on time — and the item's
    deliveryDate is the day it came; cancelled, neither, promise or not."""
    order = ledger["order"]
    nov_01, nov_03 = datetime(2026, 11, 1), datetime(2026, 11, 3)

    def read(unit_id: str, **page) -> dict:
        apply_observations(db, order, [_Seen(order_id=order.order_id, unit_id=unit_id, **page)])
        db.commit()
        data = api.post("/orders/orderDetails", json={"orderId": order.order_id}).json()["data"]
        shipment = next(s for s in data["shipments"] if s["unitId"] == unit_id.upper())
        item = next(i for i in data["items"] if i["unitId"] == unit_id.upper())
        return {"shipment": (shipment["estimatedDelivery"], shipment["deliveredOn"]), "deliveryDate": item["deliveryDate"]}

    on_its_way = read("u2", platform_status="Delivery expected by Nov 01", tracking_id="BD2", delivery_date=nov_01, promised_date=nov_01)
    assert on_its_way == {"shipment": ("2026-11-01", None), "deliveryDate": "2026-11-01"}

    delivered = read("u2", platform_status="Delivered", delivery_date=nov_03, delivered_date=nov_03)
    assert delivered == {"shipment": ("2026-11-01", "2026-11-03"), "deliveryDate": "2026-11-03"}

    read("u1", platform_status="Delivery expected by Nov 01", promised_date=nov_01)
    cancelled = read("u1", platform_status="Cancelled", delivery_date=datetime(2026, 10, 30))
    assert cancelled == {"shipment": (None, None), "deliveryDate": None}


def test_an_unknown_order_or_a_bad_request_is_refused(api, ledger):
    assert api.post("/orders/orderDetails", json={"orderId": "OD0000000000"}).status_code == 404
    assert api.post("/orders/orderDetails", json={"limit": "lots"}).status_code == 422
    assert api.post("/orders/orderDetails", json={"fromDate": "garbage"}).status_code == 422


# --- the same answers as the ledger page, through the read-back (review of 27 Oct 2026) ---
#: Flipkart's own reason, read back live on 24 and 25 Oct 2026.
SELLER_POLICY = "Sorry, your order was cancelled as per seller policy restrictions applicable to this brand."
#: How an order page words a cancellation the account asked for.
USER_ASKED = "You requested a cancellation because you changed your mind about this product."
#: A cancelled order's page: its "Cancelled" step stays once it is refunded.
CANCELLED_STEPS = [
    {"status": "Order Confirmed", "date": "2026-10-24T00:00:00"},
    {"status": "Cancelled", "date": "2026-10-24T00:00:00"},
]


@pytest.fixture
def buyer(db) -> Account:
    row = Account(email="agree@test.com", status=AccountStatus.session_valid)
    db.add(row)
    db.commit()
    return row


def _placed(db, account: Account, order_id: str, *, qty: int = 1, payment_mode: str = "card") -> Order:
    """An order a run placed and Flipkart confirmed, before any read-back.
    The id and the amount are made up."""
    row = Order(
        account_id=account.id,
        order_id=order_id,
        status=OrderStatus.placed,
        model="vivo T5 Lite 44W 5G",
        qty=qty,
        amount=18207.0 * qty,
        payment_mode=payment_mode,
        placed_at=datetime(2026, 10, 24, 10, 0),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _read_back(account: Account, *seen: ScrapedOrder) -> None:
    """One read-back of the account's recent orders, through the read-back's
    own fold (app.jobs.sync_tasks._reconcile), as a timed read-back runs it."""
    sync_tasks._reconcile(account.id, list(seen), import_unknown=False)


def _answers(api, account: Account, order_id: str) -> tuple[dict, dict]:
    """The order as this API answers for it: in full by its id, and as its
    entry in the account's list."""
    detail = api.post("/orders/orderDetails", json={"orderId": order_id}).json()["data"]
    listed = api.post("/orders/orderDetails", json={"emailid": account.email, "limit": 50}).json()["data"]
    (summary,) = [entry for entry in listed if entry["orderId"] == order_id]
    return detail, summary


def _ledger_rows(api, order_id: str) -> list[dict]:
    """The ledger page's rows for one order (GET /orders), by unit position."""
    items = api.get("/orders", params={"order_id": order_id}).json()["items"]
    return sorted(items, key=lambda item: item["unit_position"])


def _page_outcome(row: dict) -> str:
    """The Outcome the ledger page shows for a row: frontend/src/lib/orderOutcome.ts, line for line."""
    if row["status"] == "failed" and row["order_id"]:
        return "cancelled_by_user" if row["cancelled_by"] == "user" else "cancelled_by_flipkart"
    return row["status"]


def test_a_refunded_cancellation_still_reads_cancelled_as_on_the_ledger_page(api, db, buyer):
    """Flipkart cancels a card order under its seller policy and, once the
    money is back, words it "Refund Completed", which the portal reads as
    returned. The ledger page and the CSV go on calling it cancelled, by
    Flipkart; this API said RETURNED beside the outcome "failed" — the word
    for a run that sent nothing. Flipkart's wording and the portal's reading
    of it stay as read, as the CSV keeps its Delivery State."""
    order = _placed(db, buyer, "OD338967210239300021")
    _read_back(
        buyer,
        ScrapedOrder(
            order_id=order.order_id, unit_id="r1", platform_status="Cancelled", cancel_reason=SELLER_POLICY, timeline=CANCELLED_STEPS
        ),
    )
    refunded = [*CANCELLED_STEPS, {"status": "Refund Completed", "date": "2026-10-27T00:00:00"}]
    _read_back(buyer, ScrapedOrder(order_id=order.order_id, unit_id="r1", platform_status="Refund Completed", timeline=refunded))

    (row,) = _ledger_rows(api, order.order_id)
    assert (_page_outcome(row), row["cancelled_by"], row["delivery_state"]) == ("cancelled_by_flipkart", "flipkart", "returned")
    detail, summary = _answers(api, buyer, order.order_id)
    for answer in (detail, summary):
        assert (answer["status"], answer["outcome"], answer["cancelledBy"]) == ("CANCELLED", _page_outcome(row), row["cancelled_by"])
        assert (answer["platformStatus"], answer["deliveryState"]) == ("Refund Completed", "returned")
    assert [(i["unitId"], i["status"], i["platformStatus"], i["deliveryState"]) for i in detail["items"]] == [
        ("R1", "CANCELLED", "Refund Completed", "returned")
    ]


@pytest.mark.parametrize("asked_in_the_tool", [False, True], ids=["in-flipkarts-words", "by-a-cancel-request-here"])
def test_a_cancellation_the_account_asked_for_says_so_as_on_the_ledger_page(api, db, buyer, asked_in_the_tool):
    """The account's own cancellation: Flipkart's page says so ("You
    requested a cancellation…"), or someone asked for it in this tool — a
    cancel request, done on Flipkart by hand — whatever the page then says.
    The ledger page calls it the user's; this API said "failed"."""
    order = _placed(db, buyer, "OD338967210239300022")
    if asked_in_the_tool:
        db.add(
            UnitRequest(unit_pk=order.units[0].id, kind="cancel", reason="not needed", status="done", requested_by="ops@test.com")
        )
        db.commit()
    reason = SELLER_POLICY if asked_in_the_tool else USER_ASKED
    _read_back(buyer, ScrapedOrder(order_id=order.order_id, unit_id="r1", platform_status="Cancelled", cancel_reason=reason))

    (row,) = _ledger_rows(api, order.order_id)
    assert (_page_outcome(row), row["cancelled_by"]) == ("cancelled_by_user", "user")
    for answer in _answers(api, buyer, order.order_id):
        assert (answer["status"], answer["outcome"], answer["cancelledBy"]) == ("CANCELLED", _page_outcome(row), "user")


@pytest.mark.parametrize(
    ("reason", "who"), [(USER_ASKED, "user"), (SELLER_POLICY, "flipkart")], ids=["asked-for-by-the-account", "flipkarts-own"]
)
def test_a_unit_flipkart_never_listed_goes_with_its_cancelled_order(api, db, buyer, reason, who):
    """Two units asked for; Flipkart lists one and cancels it, and the order
    is failed. The other, never listed, has no wording at all: this API said
    PENDING, while the ledger page, the CSV and the owner's sheets say it was
    cancelled with its order — by whoever cancelled the order."""
    order = _placed(db, buyer, "OD338967210239300023", qty=2)
    _read_back(buyer, ScrapedOrder(order_id=order.order_id, unit_id="r1", platform_status="Cancelled", cancel_reason=reason))
    db.expire_all()
    assert db.get(Order, order.id).status == OrderStatus.failed

    rows = _ledger_rows(api, order.order_id)
    assert [(row["unit_position"], row["cancelled_by"], _page_outcome(row)) for row in rows] == [
        (1, who, f"cancelled_by_{who}"),
        (2, who, f"cancelled_by_{who}"),
    ]
    detail, summary = _answers(api, buyer, order.order_id)
    assert [(i["unitId"], i["status"], i["deliveryState"]) for i in detail["items"]] == [
        ("R1", "CANCELLED", "cancelled"),
        (None, "CANCELLED", "unknown"),  # nothing read of it, and cancelled with its order
    ]
    for answer in (detail, summary):
        assert (answer["status"], answer["outcome"], answer["cancelledBy"]) == ("CANCELLED", f"cancelled_by_{who}", who)


def test_who_cancelled_is_looked_up_only_for_orders_flipkart_cancelled(api, db, buyer, monkeypatch):
    """The order's verdict (app.core.cancellations.cancel_readings) is read
    only for the orders Flipkart took and then cancelled: a list or an order
    with none costs no query more. An order that stands says so."""
    standing = _placed(db, buyer, "OD338967210239300024")
    _read_back(buyer, ScrapedOrder(order_id=standing.order_id, unit_id="r1", platform_status="Shipped"))
    other = Account(email="other@test.com", status=AccountStatus.session_valid)
    db.add(other)
    db.commit()
    cancelled = _placed(db, other, "OD338967210239300025")
    _read_back(other, ScrapedOrder(order_id=cancelled.order_id, unit_id="r2", platform_status="Cancelled", cancel_reason=SELLER_POLICY))
    looked_up: list[list[int]] = []
    real = cancellations.cancel_readings

    def counted(session, order_pks):
        looked_up.append(sorted(order_pks))
        return real(session, order_pks)

    monkeypatch.setattr(cancellations, "cancel_readings", counted)

    for answer in _answers(api, buyer, standing.order_id):
        assert (answer["status"], answer["outcome"], answer["cancelledBy"]) == ("SHIPPED", "placed", None)
    assert looked_up == []

    _answers(api, other, cancelled.order_id)
    assert looked_up == [[cancelled.id], [cancelled.id]]  # by its id, then its account's list: that order alone


# --- the order's own status, from the unit that stands for it (review of 27 Oct 2026) ---
@pytest.mark.parametrize(
    ("passes", "expected", "items", "as_the_order_row"),
    [
        pytest.param(
            [[("r1", "Cancelled"), ("r2", "Delivery expected by Nov 01")]],
            ("SHIPPED", "Delivery expected by Nov 01", "in_transit"),
            ["CANCELLED", "SHIPPED"],
            True,
            id="a-one-cancelled-the-other-on-its-way",
        ),
        pytest.param(
            [[("r2", "Delivery expected by Nov 01"), ("r1", "Cancelled")]],
            ("SHIPPED", "Delivery expected by Nov 01", "in_transit"),
            ["SHIPPED", "CANCELLED"],
            True,
            id="b-the-same-listed-the-other-way-round",
        ),
        pytest.param(
            # The list cut at its row limit: one pass sees the cancelled unit
            # alone and fails the order; the next sees the other one out for
            # delivery and puts it back to placed.
            [[("r1", "Cancelled")], [("r2", "Out For Delivery")]],
            ("SHIPPED", "Out For Delivery", "in_transit"),
            ["CANCELLED", "SHIPPED"],
            True,
            id="c-the-cancelled-one-seen-alone-then-the-other-out-for-delivery",
        ),
        pytest.param(
            [[("r1", "Cancelled"), ("r2", "Delivered")]],
            ("DELIVERED", "Delivered", "delivered"),
            ["CANCELLED", "DELIVERED"],
            # The one step beyond the read-back's rule: the order row falls
            # back to the unit at position 1 here, the cancelled one.
            False,
            id="d-one-cancelled-the-other-delivered",
        ),
        pytest.param(
            [[("r1", "Delivered"), ("r2", "Delivery expected by Nov 01")]],
            ("SHIPPED", "Delivery expected by Nov 01", "in_transit"),
            ["DELIVERED", "SHIPPED"],
            True,
            id="e-one-delivered-the-other-on-its-way",
        ),
        pytest.param(
            # The quantity was capped: Flipkart never listed the second unit,
            # which must not hold a delivered order at PENDING for ever.
            [[("r1", "Delivered")]],
            ("DELIVERED", "Delivered", "delivered"),
            ["DELIVERED", "PENDING"],
            True,
            id="a-capped-quantity-whose-second-unit-was-never-listed",
        ),
    ],
)
def test_the_order_reads_as_the_unit_that_stands_for_it(api, db, buyer, passes, expected, items, as_the_order_row):
    """The order's status, wording and reading came from the unit at
    position 1 — whichever Flipkart happened to list first — so a standing
    card order with one unit cancelled and the other on its way read CANCELLED
    beside the outcome "placed" and payment CAPTURED. They now come from the
    unit that stands for the order (app.core.order_units.representative_unit):
    the read-back's own rule for the order row, which the overdue alert reads,
    and a delivered unit before a cancelled one. Each unit keeps its own
    reading in items."""
    order = _placed(db, buyer, "OD338975799606600000", qty=2)
    for listed in passes:
        _read_back(
            buyer,
            *(
                ScrapedOrder(
                    order_id=order.order_id,
                    unit_id=unit_id,
                    platform_status=wording,
                    cancel_reason=SELLER_POLICY if wording == "Cancelled" else None,
                )
                for unit_id, wording in listed
            ),
        )
    db.expire_all()
    stored = db.get(Order, order.id)
    assert stored.status == OrderStatus.placed  # it stands in every case
    if as_the_order_row:
        assert (stored.platform_status, stored.delivery_state) == expected[1:]

    detail, summary = _answers(api, buyer, order.order_id)
    for answer in (detail, summary):
        assert (answer["status"], answer["platformStatus"], answer["deliveryState"]) == expected
        assert (answer["outcome"], answer["cancelledBy"]) == ("placed", None)
    assert summary["paymentStatus"] == "CAPTURED"
    assert [item["status"] for item in detail["items"]] == items


# --- what was paid, for an order Flipkart took and then cancelled (review of 27 Oct 2026) ---
def _paid(api, account: Account, order_id: str) -> tuple[str, str]:
    """``(method, status)`` of the order's payment, the same in full and in the list."""
    detail, summary = _answers(api, account, order_id)
    (payment,) = detail["payments"]
    assert (summary["paymentMethod"], summary["paymentStatus"]) == (payment["method"], payment["status"])
    return payment["method"], payment["status"]


def test_a_card_order_flipkart_cancelled_is_owed_back_and_then_refunded(api, db, buyer):
    """Paid by card when Flipkart took it, so once Flipkart cancels it the
    money is owed back — the order dialog's "Paid ₹18,207.00 by Credit Card —
    refund due" — and it is back once Flipkart words it "Refund Completed"
    ("— refunded"). This API said NOT_CHARGED for both."""
    order = _placed(db, buyer, "OD338967210239300041")
    assert _paid(api, buyer, order.order_id) == ("CARD", "CAPTURED")

    _read_back(
        buyer,
        ScrapedOrder(
            order_id=order.order_id,
            unit_id="r1",
            platform_status="Cancelled",
            cancel_reason=SELLER_POLICY,
            timeline=CANCELLED_STEPS,
            payment_method="Credit Card",
            amount_paid=18207.0,
        ),
    )
    assert _paid(api, buyer, order.order_id) == ("CARD", "REFUND_DUE")

    _read_back(buyer, ScrapedOrder(order_id=order.order_id, unit_id="r1", platform_status="Refund Completed", timeline=CANCELLED_STEPS))
    assert _paid(api, buyer, order.order_id) == ("CARD", "REFUNDED")


def test_a_card_order_is_refunded_only_once_every_cancelled_unit_is(api, db, buyer):
    order = _placed(db, buyer, "OD338967210239300042", qty=2)
    _read_back(
        buyer,
        *(
            ScrapedOrder(order_id=order.order_id, unit_id=unit_id, platform_status="Cancelled", cancel_reason=SELLER_POLICY)
            for unit_id in ("r1", "r2")
        ),
    )
    _read_back(buyer, ScrapedOrder(order_id=order.order_id, unit_id="r1", platform_status="Refund Completed"))
    assert _paid(api, buyer, order.order_id) == ("CARD", "REFUND_DUE")  # the second unit's money is still owed

    _read_back(buyer, ScrapedOrder(order_id=order.order_id, unit_id="r2", platform_status="Refund Completed"))
    assert _paid(api, buyer, order.order_id) == ("CARD", "REFUNDED")


def test_a_cash_on_delivery_order_flipkart_cancelled_collected_nothing(api, db, buyer):
    order = _placed(db, buyer, "OD338967210239300043", payment_mode="cod")
    assert _paid(api, buyer, order.order_id) == ("COD", "COD_ON_DELIVERY")

    _read_back(buyer, ScrapedOrder(order_id=order.order_id, unit_id="r1", platform_status="Cancelled", cancel_reason=SELLER_POLICY))
    assert _paid(api, buyer, order.order_id) == ("COD", "NOT_CHARGED")


def test_cash_on_delivery_is_read_off_the_page_before_the_stored_mode(api, db, buyer):
    """A row the read-back imported before 27 Oct 2026 kept the column default
    "card" whatever the order was paid with. Its page says "Cash On
    Delivery", as the order dialog reads it, so once Flipkart cancels it the
    API must not report money owed back (recheck of 27 Oct 2026)."""
    order = _placed(db, buyer, "OD338967210239300045", payment_mode="card")
    _read_back(buyer, ScrapedOrder(order_id=order.order_id, unit_id="r1", platform_status="Order Confirmed", payment_method="Cash On Delivery"))
    assert _paid(api, buyer, order.order_id) == ("COD", "COD_ON_DELIVERY")

    _read_back(buyer, ScrapedOrder(order_id=order.order_id, unit_id="r1", platform_status="Cancelled", cancel_reason=SELLER_POLICY))
    assert _paid(api, buyer, order.order_id) == ("COD", "NOT_CHARGED")


def test_an_order_of_no_known_mode_flipkart_cancelled_asserts_nothing(api, db, buyer):
    """An order handed in by id carries no mode a run chose: the ledger never
    saw a charge, so it claims neither one nor its absence."""
    order = _placed(db, buyer, "OD338967210239300044", payment_mode="unknown")
    assert _paid(api, buyer, order.order_id) == ("UNKNOWN", "UNKNOWN")

    _read_back(buyer, ScrapedOrder(order_id=order.order_id, unit_id="r1", platform_status="Cancelled", cancel_reason=SELLER_POLICY))
    assert _paid(api, buyer, order.order_id) == ("UNKNOWN", "UNKNOWN")


def test_a_run_that_never_reached_flipkart_charged_nothing():
    """No order id: Flipkart never took it, whatever the mode. This API lists
    no such run; the rule is pinned so the cancelled-order branch never
    claims one."""
    for mode, method in (("card", "CARD"), ("cod", "COD"), ("unknown", "UNKNOWN")):
        never_sent = Order(status=OrderStatus.failed, order_id=None, payment_mode=mode)
        assert order_details._payment(never_sent) == (method, "NOT_CHARGED")
