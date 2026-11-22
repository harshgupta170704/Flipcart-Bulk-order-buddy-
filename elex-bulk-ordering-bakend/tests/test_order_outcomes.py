"""The order-attempt stage + error-code taxonomy (parity with the competitor's
Orders screen). Pure classifier, so no fixtures or DB."""

from app.core.order_outcomes import (
    ErrorCode,
    OrderStage,
    STAGE_OF,
    classify,
    classify_error,
)


def test_there_are_the_competitors_twenty_five_stages():
    assert len(list(OrderStage)) == 25
    assert OrderStage.COMPLETED.value == "Completed"


def test_every_code_maps_to_a_stage():
    for code in ErrorCode:
        assert code in STAGE_OF, code


def test_a_placed_order_is_completed_with_no_code():
    assert classify("placed", None) == (OrderStage.COMPLETED, None)


def test_over_target_is_the_price_limit_at_cart_validation():
    stage, code = classify("failed_over_target", "over target: paid nothing")
    assert code == ErrorCode.PRICE_OVER_LIMIT
    assert stage == OrderStage.FINAL_CART_VALIDATION


def test_unconfirmed_lands_at_order_confirmation():
    stage, code = classify("unconfirmed", "submitted but Flipkart never confirmed it")
    assert stage == OrderStage.ORDER_CONFIRMATION
    assert code == ErrorCode.CONFIRMATION_UNVERIFIED


def test_specific_reasons_beat_generic_ones():
    # a page problem is not a card decline
    assert classify_error("could not reach the payment page — nothing was charged") == ErrorCode.PAYMENT_PAGE_UNREACHABLE
    # a refused gift card is not the bank card
    assert classify_error("Flipkart refused the gift card") == ErrorCode.GIFT_CARD_ERROR
    # a real bank decline is a decline
    assert classify_error("Payment declined by bank") == ErrorCode.CARD_DECLINED
    # the COD confirmation popup
    assert classify_error("the Cash on Delivery confirmation popup never appeared") == ErrorCode.COD_CONFIRM_FAILED


def test_flow_failure_strings_classify_and_carry_their_stage():
    cases = {
        "could not read the delivery date — nothing was charged": (OrderStage.VALIDATING_DELIVERY_SLA, ErrorCode.DELIVERY_ESTIMATE_UNREADABLE),
        "delivery is 9 days out, limit is 3": (OrderStage.VALIDATING_DELIVERY_SLA, ErrorCode.DELIVERY_SLA_NOT_MET),
        "card could not be entered (no card number box) — nothing was charged": (OrderStage.PROCESSING_CARD_DETAILS, ErrorCode.CARD_ENTRY_FAILED),
        "this email is not registered on Flipkart": (OrderStage.LOGIN, ErrorCode.ACCOUNT_NOT_REGISTERED),
        "maximum units limit reached": (OrderStage.QUANTITY_SELECTION, ErrorCode.UNIT_LIMIT_REACHED),
    }
    for reason, (stage, code) in cases.items():
        assert classify("failed", reason) == (stage, code), reason


def test_an_unknown_reason_is_a_system_error_at_failed():
    assert classify("failed", "something nobody has seen before") == (OrderStage.FAILED, ErrorCode.SYSTEM_ERROR)


def test_order_columns_and_ledger_filter_by_stage_and_error_code(db):
    from app.core import order_filter
    from app.db import base as db_base
    from app.db.models import Account, Order, OrderStatus
    from app.automation.ordering import AddressInfo, GstInfo
    from app.jobs.tasks import save_order_result

    acct = Account(email="stage-test@example.com")
    db.add(acct)
    db.commit()
    address = AddressInfo(name="Buyer", phone="9876543210", pin="462010", area="123 Road")
    gst = GstInfo(gstin="", business_name="")

    placed_id = save_order_result(
        acct.id,
        {"status": OrderStatus.placed, "order_id": "OD111111111111", "amount": 500.0, "qty": 1},
        product_link="https://www.flipkart.com/p/itm1",
        qty=1,
        target=600.0,
        address=address,
        gst=gst,
    )
    failed_id = save_order_result(
        acct.id,
        {
            "status": OrderStatus.failed,
            "error": "Payment declined by bank",
            "amount": 500.0,
            "qty": 1,
        },
        product_link="https://www.flipkart.com/p/itm1",
        qty=1,
        target=600.0,
        address=address,
        gst=gst,
    )

    db.expire_all()
    placed = db.get(Order, placed_id)
    failed = db.get(Order, failed_id)
    assert placed.processing_stage == OrderStage.COMPLETED.value
    assert placed.error_code is None
    assert failed.processing_stage == OrderStage.PROCESSING_PAYMENT.value
    assert failed.error_code == ErrorCode.CARD_DECLINED.value

    # Filtering by processing_stages and error_codes
    rows_completed, total_completed = order_filter.page(
        db, order_filter.OrderFilter(processing_stages=(OrderStage.COMPLETED.value,))
    )
    assert total_completed == 1 and rows_completed[0][1].id == placed_id

    rows_declined, total_declined = order_filter.page(
        db, order_filter.OrderFilter(error_codes=(ErrorCode.CARD_DECLINED.value,))
    )
    assert total_declined == 1 and rows_declined[0][1].id == failed_id

    opts = order_filter.options(db)
    assert {x["value"]: x["count"] for x in opts.processing_stages} == {
        OrderStage.COMPLETED.value: 1,
        OrderStage.PROCESSING_PAYMENT.value: 1,
    }
    assert {x["value"]: x["count"] for x in opts.error_codes} == {
        ErrorCode.CARD_DECLINED.value: 1,
    }

    # Backfill when processing_stage is NULL on older rows
    placed.processing_stage = None
    db.commit()
    assert db_base.sync_derived_columns() >= 1
    db.expire_all()
    assert db.get(Order, placed_id).processing_stage == OrderStage.COMPLETED.value

