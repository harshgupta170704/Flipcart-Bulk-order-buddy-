"""Processing stage and error code for every order attempt (parity with the
competitor's Orders screen: audit of 19-22 Nov 2026).

Until now an attempt kept only a free-text ``error``. The competitor records,
per attempt, **which of 25 pipeline stages it reached** and **one of a fixed
set of error codes**, so an operator can filter and count "how many failed at
payment, and why" across thousands of orders. This module is the taxonomy and
the classifier; it writes nothing and imports nothing from the app at runtime,
so it is pure and unit-testable on its own.

``classify(status_value, error)`` takes the order's status **as a string**
(``OrderStatus(...).value`` — kept out of this module so it stays import-light)
and the flow's error text, and returns ``(OrderStage, ErrorCode | None)``.

The error-code set below is seeded from the competitor's published codes and
from this flow's own failure strings. It is deliberately open: a code we have
not met yet simply is not listed, and its failure classifies to the closest
stage with ``SYSTEM_ERROR`` until the exact wording is added. The full list is
completed from the competitor's Error Codes filter as we confirm each string.
"""

from __future__ import annotations

import enum


class OrderStage(str, enum.Enum):
    """The 25 pipeline stages an attempt can reach, in order. Values mirror the
    competitor's stage names so the two tools read the same; the names are our
    own constants."""

    INITIATED = "Initiated"
    LOGIN = "Login"
    FETCHING_SKU = "Fetching SKU"
    REMOVING_SAVED_CARDS = "Removing Saved Cards"
    EMPTYING_CART = "Emptying Cart"
    MANAGING_ADDRESSES = "Managing Addresses"
    ADDING_TO_CART = "Adding to Cart"
    ADDRESS_SELECTION = "Address Selection"
    QUANTITY_SELECTION = "Quantity Selection"
    SETTING_SUPERCOIN = "Setting Supercoin"
    APPLYING_COUPON = "Applying Coupon"
    APPLYING_REWARD = "Applying Reward"
    VALIDATING_DELIVERY_SLA = "Validating Delivery SLA"
    MANAGING_GST = "Managing GST"
    REMOVING_GST = "Removing GST"
    VALIDATING_GST = "Validating GST"
    FINAL_CART_VALIDATION = "Final Cart Validation"
    PRELIMINARY_CARD_CHECK = "Preliminary card and amount check"
    PAYMENT_INITIATION = "Payment Initiation"
    VERIFYING_CART = "Verifying Cart"
    PROCESSING_CARD_DETAILS = "Processing Card Details"
    PROCESSING_PAYMENT = "Processing Payment"
    ORDER_CONFIRMATION = "Order Confirmation"
    COMPLETED = "Completed"
    FAILED = "Failed"


class ErrorCode(str, enum.Enum):
    """A fixed, filterable reason an attempt did not place. Grouped by stage."""

    # account / login
    ACCOUNT_NOT_REGISTERED = "Account not registered"
    ACCOUNT_BLOCKED = "Account blocked"
    MAX_LOGIN_ATTEMPTS = "Maximum login attempts reached"
    NOT_LOGGED_IN = "Account not logged in"
    # product
    PRODUCT_UNAVAILABLE = "Product unavailable or not serviceable"
    SIZE_UNAVAILABLE = "Size not available"
    SELLER_MISMATCH = "Seller mismatch"
    CART_ADD_FAILED = "Could not add to cart"
    # address / quantity
    ADDRESS_SELECTION_FAILED = "Address selection failed"
    PINCODE_NOT_ALLOWED = "Pincode not allowed"
    QUANTITY_FAILED = "Could not set quantity"
    UNIT_LIMIT_REACHED = "Maximum units limit reached"
    # offers
    SUPERCOIN_FAILED = "Failed to apply SuperCoins"
    COUPON_FAILED = "Coupon could not be applied"
    REWARD_FAILED = "Reward could not be applied"
    # delivery / gst / cart
    DELIVERY_ESTIMATE_UNREADABLE = "Delivery estimate unreadable"
    DELIVERY_SLA_NOT_MET = "Delivery SLA not met"
    GST_FAILED = "GST could not be set or validated"
    CART_AMOUNT_UNREADABLE = "Cart amount unreadable"
    CART_OVER_LIMIT = "Cart amount over the limit"
    PRICE_OVER_LIMIT = "Final amount over the limit"
    ORDER_TOTAL_UNREADABLE = "Order total unreadable"
    # payment plumbing
    PAYMENT_PAGE_UNREACHABLE = "Could not reach the payment page"
    PAYMENT_METHOD_UNAVAILABLE = "Payment method unavailable"
    COD_NOT_AVAILABLE = "Cash on Delivery not available"
    GIFT_CARD_ERROR = "Gift card / voucher error"
    WALLET_ERROR = "Wallet balance error"
    MCC_HALTED = "Order halted due to lower MCC"
    # card / bank
    CARD_NOT_SUPPLIED = "No card supplied"
    CARD_ENTRY_FAILED = "Card could not be entered"
    CARD_INVALID = "Card details invalid or expired"
    CARD_DECLINED = "Card declined by bank"
    INSUFFICIENT_FUNDS = "Insufficient funds or limit"
    OTP_FAILED = "Bank OTP failed"
    BANK_AUTH_FAILED = "Bank password / 3-D Secure failed"
    RISK_CHECK_FAILED = "Declined by risk checks"
    BANK_PAGE_ERROR = "Bank page / gateway error"
    # confirmation
    COD_CONFIRM_FAILED = "COD confirmation step failed"
    CONFIRMATION_UNVERIFIED = "Submitted but never confirmed"
    # platform / system
    PLATFORM_TRANSIENT = "Platform busy or rate-limited (retryable)"
    INTERNAL_RESERVE_FAILED = "Could not record the order before sending"
    SYSTEM_ERROR = "System error"


#: Which stage each code is reported at.
STAGE_OF: dict[ErrorCode, OrderStage] = {
    ErrorCode.ACCOUNT_NOT_REGISTERED: OrderStage.LOGIN,
    ErrorCode.ACCOUNT_BLOCKED: OrderStage.LOGIN,
    ErrorCode.MAX_LOGIN_ATTEMPTS: OrderStage.LOGIN,
    ErrorCode.NOT_LOGGED_IN: OrderStage.LOGIN,
    ErrorCode.PRODUCT_UNAVAILABLE: OrderStage.FETCHING_SKU,
    ErrorCode.SIZE_UNAVAILABLE: OrderStage.QUANTITY_SELECTION,
    ErrorCode.SELLER_MISMATCH: OrderStage.ADDING_TO_CART,
    ErrorCode.CART_ADD_FAILED: OrderStage.ADDING_TO_CART,
    ErrorCode.ADDRESS_SELECTION_FAILED: OrderStage.ADDRESS_SELECTION,
    ErrorCode.PINCODE_NOT_ALLOWED: OrderStage.ADDRESS_SELECTION,
    ErrorCode.QUANTITY_FAILED: OrderStage.QUANTITY_SELECTION,
    ErrorCode.UNIT_LIMIT_REACHED: OrderStage.QUANTITY_SELECTION,
    ErrorCode.SUPERCOIN_FAILED: OrderStage.SETTING_SUPERCOIN,
    ErrorCode.COUPON_FAILED: OrderStage.APPLYING_COUPON,
    ErrorCode.REWARD_FAILED: OrderStage.APPLYING_REWARD,
    ErrorCode.DELIVERY_ESTIMATE_UNREADABLE: OrderStage.VALIDATING_DELIVERY_SLA,
    ErrorCode.DELIVERY_SLA_NOT_MET: OrderStage.VALIDATING_DELIVERY_SLA,
    ErrorCode.GST_FAILED: OrderStage.VALIDATING_GST,
    ErrorCode.CART_AMOUNT_UNREADABLE: OrderStage.FINAL_CART_VALIDATION,
    ErrorCode.CART_OVER_LIMIT: OrderStage.FINAL_CART_VALIDATION,
    ErrorCode.PRICE_OVER_LIMIT: OrderStage.FINAL_CART_VALIDATION,
    ErrorCode.ORDER_TOTAL_UNREADABLE: OrderStage.FINAL_CART_VALIDATION,
    ErrorCode.PAYMENT_PAGE_UNREACHABLE: OrderStage.PAYMENT_INITIATION,
    ErrorCode.PAYMENT_METHOD_UNAVAILABLE: OrderStage.PAYMENT_INITIATION,
    ErrorCode.COD_NOT_AVAILABLE: OrderStage.PAYMENT_INITIATION,
    ErrorCode.GIFT_CARD_ERROR: OrderStage.PROCESSING_PAYMENT,
    ErrorCode.WALLET_ERROR: OrderStage.PROCESSING_PAYMENT,
    ErrorCode.MCC_HALTED: OrderStage.PRELIMINARY_CARD_CHECK,
    ErrorCode.CARD_NOT_SUPPLIED: OrderStage.PROCESSING_CARD_DETAILS,
    ErrorCode.CARD_ENTRY_FAILED: OrderStage.PROCESSING_CARD_DETAILS,
    ErrorCode.CARD_INVALID: OrderStage.PROCESSING_CARD_DETAILS,
    ErrorCode.CARD_DECLINED: OrderStage.PROCESSING_PAYMENT,
    ErrorCode.INSUFFICIENT_FUNDS: OrderStage.PROCESSING_PAYMENT,
    ErrorCode.OTP_FAILED: OrderStage.PROCESSING_PAYMENT,
    ErrorCode.BANK_AUTH_FAILED: OrderStage.PROCESSING_PAYMENT,
    ErrorCode.RISK_CHECK_FAILED: OrderStage.PROCESSING_PAYMENT,
    ErrorCode.BANK_PAGE_ERROR: OrderStage.PROCESSING_PAYMENT,
    ErrorCode.COD_CONFIRM_FAILED: OrderStage.ORDER_CONFIRMATION,
    ErrorCode.CONFIRMATION_UNVERIFIED: OrderStage.ORDER_CONFIRMATION,
    ErrorCode.PLATFORM_TRANSIENT: OrderStage.FAILED,
    ErrorCode.INTERNAL_RESERVE_FAILED: OrderStage.PAYMENT_INITIATION,
    ErrorCode.SYSTEM_ERROR: OrderStage.FAILED,
}

#: Ordered keyword rules: the FIRST whose any-substring matches the lowercased
#: error text wins. Order matters — specific before generic (e.g. "gift card"
#: before a bare "card", a page/COD reason before a bank "declined").
_RULES: list[tuple[tuple[str, ...], ErrorCode]] = [
    (("not registered", "new here"), ErrorCode.ACCOUNT_NOT_REGISTERED),
    (("too many attempts", "maximum number of attempts", "maximum login attempts"), ErrorCode.MAX_LOGIN_ATTEMPTS),
    (("blocked", "suspended", "deactivated"), ErrorCode.ACCOUNT_BLOCKED),
    (("no session", "not logged in", "login failed", "account not active"), ErrorCode.NOT_LOGGED_IN),
    (("could not pick size", "size "), ErrorCode.SIZE_UNAVAILABLE),
    (("out of stock", "unavailable", "not serviceable", "product not found"), ErrorCode.PRODUCT_UNAVAILABLE),
    (("names the seller", "seller mismatch"), ErrorCode.SELLER_MISMATCH),
    (("could not place the cart order", "add to cart", "cart add"), ErrorCode.CART_ADD_FAILED),
    (("pincode", "account has address saved"), ErrorCode.PINCODE_NOT_ALLOWED),
    (("saved-address picker", "first delivery address", "no saved address matched", "address"), ErrorCode.ADDRESS_SELECTION_FAILED),
    (("maximum units limit", "unit limit"), ErrorCode.UNIT_LIMIT_REACHED),
    (("could not set quantity", "update quantity", "quantity"), ErrorCode.QUANTITY_FAILED),
    (("supercoin",), ErrorCode.SUPERCOIN_FAILED),
    (("coupon",), ErrorCode.COUPON_FAILED),
    (("reward",), ErrorCode.REWARD_FAILED),
    (("could not read the delivery", "delivery estimate", "not able to get delivery"), ErrorCode.DELIVERY_ESTIMATE_UNREADABLE),
    (("delivery is", "delivery sla", "days out"), ErrorCode.DELIVERY_SLA_NOT_MET),
    (("gst",), ErrorCode.GST_FAILED),
    (("could not read the cart amount", "cart amount"), ErrorCode.CART_AMOUNT_UNREADABLE),
    (("exceeds the cart limit", "cart limit"), ErrorCode.CART_OVER_LIMIT),
    (("over target", "exceeds target", "over the limit"), ErrorCode.PRICE_OVER_LIMIT),
    (("could not read the order total", "order total"), ErrorCode.ORDER_TOTAL_UNREADABLE),
    (("could not reach the payment page", "payment page did not load", "reach the payment"), ErrorCode.PAYMENT_PAGE_UNREACHABLE),
    (("cash on delivery is not", "cod is not", "cash on delivery not"), ErrorCode.COD_NOT_AVAILABLE),
    (("gift card", "gift voucher", "voucher"), ErrorCode.GIFT_CARD_ERROR),
    (("wallet",), ErrorCode.WALLET_ERROR),
    (("lower mcc", "mcc"), ErrorCode.MCC_HALTED),
    (("no card supplied",), ErrorCode.CARD_NOT_SUPPLIED),
    (("card could not be entered", "card number box"), ErrorCode.CARD_ENTRY_FAILED),
    (("cvv", "expired", "invalid card"), ErrorCode.CARD_INVALID),
    (("insufficient", "limit exceeded", "credit limit"), ErrorCode.INSUFFICIENT_FUNDS),
    (("otp",), ErrorCode.OTP_FAILED),
    (("password", "3-d", "3d secure", "authenticat"), ErrorCode.BANK_AUTH_FAILED),
    (("risk",), ErrorCode.RISK_CHECK_FAILED),
    (("card driver", "gateway", "icici", "pin limit", "bank page", "bank end"), ErrorCode.BANK_PAGE_ERROR),
    (("declin", "not approved", "transaction failed", "payment failed", "payment did not"), ErrorCode.CARD_DECLINED),
    (("confirmation popup never appeared", "confirmation call"), ErrorCode.COD_CONFIRM_FAILED),
    (("never confirmed", "may exist", "unconfirmed"), ErrorCode.CONFIRMATION_UNVERIFIED),
    (("rate limit", "server busy", "503", "timed out", "timeout", "page fetch", "internet", "network"), ErrorCode.PLATFORM_TRANSIENT),
    (("could not record the order", "before sending"), ErrorCode.INTERNAL_RESERVE_FAILED),
    (("payment method", "could not select the payment"), ErrorCode.PAYMENT_METHOD_UNAVAILABLE),
]


def classify_error(error: str | None) -> ErrorCode:
    """The code a failure's reason falls under; SYSTEM_ERROR if nothing matches."""
    text = (error or "").lower()
    for needles, code in _RULES:
        if any(n in text for n in needles):
            return code
    return ErrorCode.SYSTEM_ERROR


def classify(status_value: str, error: str | None) -> tuple[OrderStage, ErrorCode | None]:
    """The (stage, code) to store for an attempt.

    ``status_value`` is ``OrderStatus(...).value``: "placed", "unconfirmed",
    "failed_over_target", "failed", "pending". A placed order has no error code;
    everything else classifies from the error text to a code and its stage.
    """
    if status_value == "placed":
        return OrderStage.COMPLETED, None
    if status_value == "unconfirmed":
        return OrderStage.ORDER_CONFIRMATION, ErrorCode.CONFIRMATION_UNVERIFIED
    if status_value == "failed_over_target":
        return OrderStage.FINAL_CART_VALIDATION, ErrorCode.PRICE_OVER_LIMIT
    code = classify_error(error)
    return STAGE_OF.get(code, OrderStage.FAILED), code
