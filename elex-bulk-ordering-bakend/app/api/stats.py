"""Dashboard counters (PLAN.md §6 — "counts: accounts, sessions valid, coupons
applied, orders placed").

Every number is a SQL ``COUNT``. The dashboard polls this, and the tables it
counts grow with every run, so nothing here loads rows into Python — save one:
who cancelled an order Flipkart took is read from the platform's own wording,
which no portable SQL can match, so those orders (a handful next to the rest)
are read through app.core.cancellations, the alerts' own verdict.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.automation.coupons import CLAIMED
from app.core import cancellations
from app.core.security import require_user
from app.db.base import get_db
from app.db.models import (
    Account,
    AccountStatus,
    CouponRun,
    Job,
    JobStatus,
    Order,
    OrderStatus,
    OrderUnit,
    utcnow,
)
from app.io.orders_csv import IST, UTC_TZ

router = APIRouter(tags=["stats"], dependencies=[Depends(require_user)])


def _count(db: DbSession, column, *conditions) -> int:
    stmt = select(func.count(column))
    for condition in conditions:
        stmt = stmt.where(condition)
    return int(db.scalar(stmt) or 0)


def _cancelled(db: DbSession) -> tuple[int, int]:
    """``(by Flipkart, at the account's request)`` among the orders Flipkart
    took and then cancelled, by the alerts' verdict: an order every unit of
    which that says anything says the user's is the user's; a mix is Flipkart's."""
    pks = db.scalars(select(Order.id).where(cancellations.placed_then_cancelled_sql())).all()
    readings = cancellations.cancel_readings(db, pks)
    by_user = sum(1 for pk in pks if pk in readings and readings[pk].user_cancelled)
    return len(pks) - by_user, by_user


@router.get("/stats")
def get_stats(db: DbSession = Depends(get_db)) -> dict[str, int]:
    """Counters for the dashboard."""
    cancelled_by_flipkart, cancelled_by_user = _cancelled(db)
    return {
        "accounts": _count(db, Account.id),
        # By the account's status, as the Sessions page, the read-back timer
        # (app.jobs.schedule) and the "session invalid" alert read it — not
        # by cookie sets: a read-back that finds an account signed out marks
        # the account session_invalid and leaves its cookie set as the login
        # saved it, still valid=True, so counting sets kept calling it valid.
        "sessions_valid": _count(
            db, Account.id, Account.status == AccountStatus.session_valid
        ),
        # "Applied" means actually claimed — an already-used or errored attempt
        # is a run that happened, not a coupon the account now holds.
        "coupons_applied": _count(db, CouponRun.id, CouponRun.result == CLAIMED),
        "orders_placed": _count(db, Order.id, Order.status == OrderStatus.placed),
        # Counted apart from plain failures: over-target is the price cap doing
        # its job (PLAN.md §6a), not something that went wrong.
        "orders_over_target": _count(
            db, Order.id, Order.status == OrderStatus.over_target
        ),
        # The run's own failures only: stopped before anything reached
        # Flipkart, which is what the "Failed" tile says. An order Flipkart
        # took and later cancelled is failed too, but carries Flipkart's order
        # id; it is counted on its own, below (app.core.cancellations) — by
        # Flipkart, and apart from those the account itself asked to cancel.
        "orders_failed": _count(db, Order.id, cancellations.never_sent_sql()),
        "orders_cancelled": cancelled_by_flipkart,
        "orders_cancelled_by_user": cancelled_by_user,
        # Apart from both: submitted but never confirmed, so it may exist and
        # someone has to check the account before it is retried.
        "orders_unconfirmed": _count(
            db, Order.id, Order.status == OrderStatus.unconfirmed
        ),
        "jobs_running": _count(db, Job.id, Job.status == JobStatus.running),
    }


#: "Still to be delivered" is a unit whose order stands and that the platform
#: has not called delivered, cancelled, returned or failed — including one
#: never read back at all, which is honestly still on its way.
_PENDING_STATES = ("in_transit", "unknown")


def _spelt(text: str | None) -> str | None:
    """A model or brand as it is shown: runs of spaces closed up; blank is none."""
    return " ".join((text or "").split()) or None


def _model_key(model: str | None) -> str | None:
    """What makes two models the same model: their words, case aside.

    The same phone reaches the ledger spelt more than one way — the run keeps
    the operator's words, an order report the sheet's, a read-back the order
    page's — and an order handed in on the order-id sheet has no model until a
    read-back reads one. None is that one "model not known yet" group.
    """
    shown = _spelt(model)
    return shown.casefold() if shown else None


def _most_units(units_by_text: dict[str, int]) -> str | None:
    """The spelling carrying the most units; a tie goes to the first in plain
    sort order, so the row's name never changes between two refreshes."""
    if not units_by_text:
        return None
    return min(units_by_text, key=lambda text: (-units_by_text[text], text))


def _add(group: dict, brand: str | None, model: str | None, units: int, orders: int, oldest) -> None:
    """Count one stored (brand, model) row into its group."""
    group["units"] += units
    # An order has one brand and one model, so it is counted in exactly one
    # of the rows merged into a group: their distinct-order counts add up.
    group["orders"] += orders
    shown = _spelt(model)
    if shown is not None:
        group["models"][shown] = group["models"].get(shown, 0) + units
    shown_brand = _spelt(brand)
    if shown is not None and shown_brand is not None:
        group["brands"][shown_brand] = group["brands"].get(shown_brand, 0) + units
    if oldest is not None and (group["oldest"] is None or oldest < group["oldest"]):
        group["oldest"] = oldest


def _merge(into: dict, other: dict) -> None:
    """Fold one group's counts into another's."""
    into["units"] += other["units"]
    into["orders"] += other["orders"]
    for name in ("models", "brands"):
        for text, units in other[name].items():
            into[name][text] = into[name].get(text, 0) + units
    if other["oldest"] is not None and (into["oldest"] is None or other["oldest"] < into["oldest"]):
        into["oldest"] = other["oldest"]


def _start_of_day_before(days: int) -> datetime:
    """Midnight, India time, at the start of the day ``days`` days before
    today — in the stored clock (naive UTC).

    An order placed before it was placed MORE than ``days`` days before today
    (spec FR-041), counted in calendar days as the operator counts them and as
    the ledger's date filter reads a day (app.api.orders), not as ``days`` × 24
    hours back from this minute: that would split one day's orders by the
    time the dashboard happened to be opened.
    """
    today = utcnow().astimezone(IST).date()
    start = datetime.combine(today - timedelta(days=days), time.min, tzinfo=IST)
    return start.astimezone(UTC_TZ).replace(tzinfo=None)


@router.get("/stats/to-be-delivered")
def to_be_delivered(
    older_than_days: int | None = Query(
        default=None,
        ge=1,
        le=365,
        description="only units ordered more than this many days before today (India time)",
    ),
    db: DbSession = Depends(get_db),
) -> dict:
    """Units placed and not yet delivered, by model (spec FR-040/041).

    Counted in SQL over the unit grain, by the stored brand and model; the
    few rows that spell one model differently — or name no model at all — are
    then merged here, so each model is one row (:func:`_model_key`), named by
    the spelling and the brand most of its units carry. Orders that name no
    model are one row with neither. The dashboard shows the top of the list
    and a "View all". An order with no order date yet (handed in by id) counts
    from the day it was recorded.
    """
    ordered_when = func.coalesce(Order.placed_at, Order.created_at)
    stmt = (
        select(
            Order.brand,
            Order.model,
            func.count(OrderUnit.id),
            func.count(func.distinct(Order.id)),
            func.min(ordered_when),
        )
        .select_from(OrderUnit)
        .join(Order, Order.id == OrderUnit.order_pk)
        .where(
            Order.status == OrderStatus.placed,
            func.coalesce(OrderUnit.delivery_state, "unknown").in_(_PENDING_STATES),
        )
        .group_by(Order.brand, Order.model)
    )
    if older_than_days is not None:
        stmt = stmt.where(ordered_when < _start_of_day_before(older_than_days))
    # One group per model and brand, each spelt case and spaces aside: two
    # brands selling a model of one name (POCO and realme both sell a C75)
    # stay two rows.
    groups: dict[tuple[str | None, str | None], dict] = {}
    for brand, model, units, orders, oldest in db.execute(stmt):
        key = (_model_key(model), _model_key(brand) if _model_key(model) is not None else None)
        group = groups.setdefault(
            key, {"models": {}, "brands": {}, "units": 0, "orders": 0, "oldest": None}
        )
        _add(group, brand, model, int(units), int(orders), oldest)
    # An order that names its model but no brand — handed in by id, its name
    # read off the order page — joins the one branded row of that model; when
    # several brands carry the name, which one is not known, so it stays a row
    # of its own.
    for (model_key, brand_key), group in list(groups.items()):
        if model_key is None or brand_key is not None:
            continue
        branded = [key for key in groups if key[0] == model_key and key[1] is not None]
        if len(branded) == 1:
            _merge(groups[branded[0]], group)
            del groups[(model_key, brand_key)]
    models = [
        {
            "brand": _most_units(group["brands"]),
            "model": _most_units(group["models"]),
            "units": group["units"],
            "orders": group["orders"],
            "oldest_ordered_at": group["oldest"],
        }
        for group in groups.values()
    ]
    # Most units first; then by name, the row with no model after the named,
    # and a model's row with no brand after its branded ones: a fixed order
    # whatever order the database grouped in.
    models.sort(
        key=lambda item: (
            -item["units"],
            item["model"] is None,
            (item["model"] or "").casefold(),
            item["brand"] is None,
            (item["brand"] or "").casefold(),
        )
    )
    return {
        "total_units": sum(item["units"] for item in models),
        "total_orders": sum(item["orders"] for item in models),
        "models": models,
        "older_than_days": older_than_days,
    }


# --- out for delivery ----------------------------------------------------------------
@router.get("/stats/out-for-delivery")
def out_for_delivery(db: DbSession = Depends(get_db)) -> dict:
    """What the person at the door needs, in one place: every unit Flipkart
    says is out for delivery, with the delivery code the courier asks for, as
    of the last read-back.

    Flipkart shows the code only once a unit is out for delivery, so it is
    here only after a read-back made since — the timer's own every
    ``ORDERING_OUT_FOR_DELIVERY_MINUTES`` (app.jobs.schedule), or the card's
    "Read back now", which starts a read-back over ``account_ids``.
    """
    from app.core.order_units import out_for_delivery_rows

    rows = out_for_delivery_rows(db)
    units = [
        {
            "unit_pk": unit.id,
            "order_row_id": order.id,
            "order_id": order.order_id,
            "unit_id": unit.unit_id,
            "position": unit.position,
            "account_id": order.account_id,
            "account_email": email,
            "model": order.model,
            "color": order.color,
            "platform_status": unit.platform_status,
            "delivery_otp": unit.delivery_otp,
            "delivery_date": unit.delivery_date,
            "delivery_address": unit.delivery_address,
            "delivery_pincode": unit.delivery_pincode,
            "tracking_id": unit.tracking_id,
            "synced_at": unit.synced_at,
        }
        for unit, order, email in rows
    ]
    stamps = [unit.synced_at for unit, _order, _email in rows if unit.synced_at is not None]
    return {
        "total": len(units),
        "units": units,
        "account_ids": sorted({order.account_id for _unit, order, _email in rows}),
        "order_ids": sorted({order.order_id for _unit, order, _email in rows if order.order_id}),
        "last_read_back": max(stamps) if stamps else None,
    }


#: The order the login table lists statuses in: signed in first, then the
#: ways a sign-in goes wrong, then the accounts never signed in.
LOGIN_STATUS_ORDER = (
    AccountStatus.session_valid,
    AccountStatus.logged_out,
    AccountStatus.session_invalid,
    AccountStatus.not_registered,
    AccountStatus.multiple_attempts,
    AccountStatus.blocked,
    AccountStatus.other,
    AccountStatus.new,
)


def _scope(tag_id: list[int] | None, untagged: bool, domain: list[str] | None):
    """The dashboard's "Custom tags" and "Domains" filters, as an account filter."""
    from app.core.account_filter import AccountFilter

    return AccountFilter(
        tag_ids=tuple(tag_id or ()),
        include_untagged=untagged,
        domains=tuple(d.strip().lower() for d in (domain or ()) if d and d.strip()),
    )


#: The dashboard's login table columns, and the statuses each one counts.
LOGIN_COLUMNS = (
    ("logged_in", (AccountStatus.session_valid,)),
    ("logged_out", (AccountStatus.logged_out, AccountStatus.session_invalid)),
    ("not_registered", (AccountStatus.not_registered,)),
    ("multiple_attempts", (AccountStatus.multiple_attempts,)),
    ("blocked", (AccountStatus.blocked,)),
    ("other", (AccountStatus.other, AccountStatus.new)),
)


@router.get("/stats/balances")
def balances(
    tag_id: list[int] | None = Query(None),
    untagged: bool = Query(False),
    domain: list[str] | None = Query(None),
    platform: str | None = Query(None),
    db: DbSession = Depends(get_db),
) -> dict:
    """SuperCoins and gift card balances across the selected accounts, and the
    delivered orders whose checkout took SuperCoins off."""
    from app.core import account_filter
    from app.db.models import Order

    if platform and platform.lower() not in ("flipkart", "all"):
        return {
            "supercoins_balance": 0,
            "gift_voucher_balance": 0.0,
            "accounts_read": 0,
            "supercoins_orders_delivered": 0,
            "supercoins_applied_amount": 0.0,
        }
    scope = _scope(tag_id, untagged, domain)
    stmt = account_filter.apply(
        select(
            func.coalesce(func.sum(Account.supercoins_balance), 0),
            func.coalesce(func.sum(Account.gift_voucher_balance), 0),
            func.count(Account.balances_read_at),
        ),
        scope,
    )
    coins, gift, read = db.execute(stmt).one()
    delivered, applied = db.execute(
        account_filter.apply(
            select(func.count(Order.id), func.coalesce(func.sum(Order.supercoins_amount), 0))
            .join(Account, Account.id == Order.account_id)
            .where(Order.used_supercoins.is_(True), Order.delivery_state == "delivered"),
            scope,
        )
    ).one()
    return {
        "supercoins_balance": int(coins or 0),
        "gift_voucher_balance": float(gift or 0),
        "accounts_read": int(read or 0),
        "supercoins_orders_delivered": int(delivered or 0),
        # In rupees, as the competitor's card shows it (audit of 19 Nov 2026).
        "supercoins_applied_amount": round(float(applied or 0), 2),
    }


@router.get("/stats/logins")
def login_health(
    tag_id: list[int] | None = Query(None),
    untagged: bool = Query(False),
    domain: list[str] | None = Query(None),
    platform: str | None = Query(None),
    db: DbSession = Depends(get_db),
) -> dict:
    """How many accounts are signed in, and how many are in each other state.

    One GROUP BY over the accounts table. Every status is listed, at zero
    when no account has it, so the table on the dashboard keeps its shape.
    """
    from app.core import account_filter

    scope = _scope(tag_id, untagged, domain)
    rows = []
    if not platform or platform.lower() in ("flipkart", "all"):
        rows = db.execute(
            account_filter.apply(select(Account.status, func.count()), scope).group_by(Account.status)
        ).all()
    counts = {getattr(status, "value", status): int(count) for status, count in rows}
    total = sum(counts.values())
    columns = {name: sum(counts.get(s.value, 0) for s in statuses) for name, statuses in LOGIN_COLUMNS}
    return {
        "platform": "flipkart",
        "total": total,
        "logged_in": counts.get(AccountStatus.session_valid.value, 0),
        "statuses": [{"status": status.value, "count": counts.get(status.value, 0)} for status in LOGIN_STATUS_ORDER],
        # The video's table: one row per platform (Flipkart / Shopsy is the one
        # this suite runs on), these columns, and a total.
        "platforms": [{"platform": "Flipkart / Shopsy", **columns, "total": total}],
    }
