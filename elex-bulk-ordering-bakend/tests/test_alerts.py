"""Tests for the alerts engine (Phase 11, ROADMAP §2).

The queue is only worth having if two things hold, so most of this file is
about them rather than about any individual rule:

* **one problem is one row.** Raising the same key again must update that row,
  and a problem that comes back must reopen it — otherwise "443 open" counts
  job runs instead of real issues;
* **alerts clear themselves.** When the condition goes away the next
  ``evaluate`` resolves the key. A queue nobody can clear gets ignored, which
  is worse than no queue.

The third guarded rule is severity: an over-target order is the price cap doing
its job (PLAN.md §6a), so it is *info*. A healthy outcome that shouts like a
failure is how a queue loses its meaning.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core import alerts
from app.db.models import (
    Account,
    AccountStatus,
    Alert,
    AlertSeverity,
    Order,
    OrderStatus,
    utcnow,
)

KEY = "manual_note:order:412"  # a kind no rule owns: raised by hand, never resolved by a pass


# --- fixtures / helpers -------------------------------------------------------
@pytest.fixture
def account(db) -> Account:
    row = Account(email="one@test.com", phone="6000000001", status=AccountStatus.new)
    db.add(row)
    db.commit()
    return row


def _order(db, account: Account, **fields) -> Order:
    """A placed order ``days_ago`` old, overridable field by field."""
    defaults = {
        "account_id": account.id,
        "status": OrderStatus.placed,
        "model": "Realme 2",
        "qty": 1,
        "placed_at": utcnow(),
    }
    order = Order(**{**defaults, **fields})
    db.add(order)
    db.commit()
    return order


def _keys(findings) -> set[str]:
    return {finding.key for finding in findings}


def _all_alerts(db) -> list[Alert]:
    return list(db.scalars(select(Alert).order_by(Alert.id)).all())


def _open_keys(db) -> set[str]:
    return {alert.key for alert in _all_alerts(db) if not alert.resolved}


# --- raise_alert: one problem, one row ----------------------------------------
def test_the_order_outcome_rules_look_back_a_quarter_not_for_ever(db, account) -> None:
    """Audit F-43 (25 Oct 2026): an over-target, failed or unconfirmed order
    from months ago is history, not something to act on — and loading every
    such order ever on every pass grew with the ledger."""
    from datetime import timedelta

    from app.core import alerts as rules

    old = _order(
        db,
        account,
        status=OrderStatus.over_target,
        amount=18207.0,
        target=18000.0,
        created_at=(utcnow() - timedelta(days=rules.ORDER_RULE_WINDOW_DAYS + 10)).replace(tzinfo=None),
    )
    recent = _order(db, account, status=OrderStatus.over_target, amount=18207.0, target=18000.0)

    keys = _keys(rules.find_over_target_orders(db))
    assert f"order_over_target:order:{recent.id}" in keys
    assert f"order_over_target:order:{old.id}" not in keys


def test_raise_alert_creates_a_row(db) -> None:
    alert = alerts.raise_alert(
        db,
        key=KEY,
        kind="manual_note",
        message="Shipped to the wrong entity",
        order_id=None,
        details={"expected": "Delhi"},
    )
    assert alert.id is not None
    assert alert.key == KEY
    assert alert.kind == "manual_note"
    assert alert.severity == AlertSeverity.warning  # the default
    assert alert.resolved is False
    assert alert.resolved_at is None
    assert alert.details == {"expected": "Delhi"}
    assert len(_all_alerts(db)) == 1


def test_raising_the_same_key_updates_rather_than_duplicating(db) -> None:
    first = alerts.raise_alert(db, key=KEY, kind="manual_note", message="one")
    alert_id, created_at = first.id, first.created_at

    # Age the row so "updated_at moved" is a real assertion and not a
    # microsecond coincidence inside one test.
    first.updated_at = created_at - timedelta(hours=1)
    db.commit()
    earlier = first.updated_at

    second = alerts.raise_alert(
        db,
        key=KEY,
        kind="manual_note",
        message="two",
        severity=AlertSeverity.critical,
        details={"seen": 2},
    )
    assert second.id == alert_id  # the same row
    assert len(_all_alerts(db)) == 1
    assert second.message == "two"
    assert second.severity == AlertSeverity.critical
    assert second.details == {"seen": 2}
    assert second.created_at == created_at  # first sighting is kept
    assert second.updated_at > earlier  # last sighting moves


def test_raising_a_resolved_key_reopens_it_and_clears_resolved_at(db) -> None:
    """A problem that comes back is the same problem, not a new one."""
    first = alerts.raise_alert(db, key=KEY, kind="manual_note", message="one")
    alert_id = first.id
    assert alerts.resolve_alert(db, KEY) is True
    assert first.resolved is True
    assert first.resolved_at is not None

    again = alerts.raise_alert(db, key=KEY, kind="manual_note", message="back")
    assert again.id == alert_id
    assert again.resolved is False
    assert again.resolved_at is None
    assert len(_all_alerts(db)) == 1


def test_raise_alert_records_the_account_and_order_it_is_about(db, account) -> None:
    order = _order(db, account)
    alert = alerts.raise_alert(
        db,
        key=f"manual_note:order:{order.id}",
        kind="manual_note",
        message="Wrong entity",
        account_id=account.id,
        order_id=order.id,
    )
    assert (alert.account_id, alert.order_id) == (account.id, order.id)


# --- resolve / reopen ---------------------------------------------------------
def test_resolve_alert_closes_once_then_reports_nothing_to_do(db) -> None:
    alerts.raise_alert(db, key=KEY, kind="manual_note", message="one")
    assert alerts.resolve_alert(db, KEY) is True
    assert alerts.resolve_alert(db, KEY) is False  # already closed


def test_resolving_an_unknown_key_is_false_not_an_error(db) -> None:
    assert alerts.resolve_alert(db, "nope:order:1") is False


def test_reopen_alert_reopens_once_then_reports_nothing_to_do(db) -> None:
    alerts.raise_alert(db, key=KEY, kind="manual_note", message="one")
    alerts.resolve_alert(db, KEY)
    assert alerts.reopen_alert(db, KEY) is True
    assert alerts.reopen_alert(db, KEY) is False  # already open
    assert _open_keys(db) == {KEY}


def test_reopening_an_unknown_key_is_false_not_an_error(db) -> None:
    assert alerts.reopen_alert(db, "nope:order:1") is False


# --- rule 1: over target ------------------------------------------------------
def test_over_target_fires_and_names_the_amount_and_target(db, account) -> None:
    order = _order(
        db, account, status=OrderStatus.over_target, amount=10450.0, target=9900.0
    )
    findings = alerts.find_over_target_orders(db)
    assert _keys(findings) == {f"order_over_target:order:{order.id}"}
    finding = findings[0]
    assert "₹10,450.00 is over the ₹9,900.00 price cap" in finding.message
    assert finding.details["amount"] == 10450.0
    assert finding.details["target"] == 9900.0
    assert (finding.account_id, finding.order_id) == (account.id, order.id)


def test_alert_money_reads_as_on_every_screen(db, account) -> None:
    """NFR-010: Indian grouping and two decimals, as the ledger shows the same
    order — "₹1,29,900.00", not "₹129,900"."""
    order = _order(db, account, status=OrderStatus.over_target, amount=129900.0, target=125000.5)
    [finding] = alerts.find_over_target_orders(db)
    assert finding.message == (
        "The order (Realme 2) for one@test.com was not placed: "
        "₹1,29,900.00 is over the ₹1,25,000.50 price cap. Nothing was charged."
    )
    assert f"Order {order.id} " not in finding.message  # never the ledger's own row number
    unconfirmed = _order(db, account, status=OrderStatus.unconfirmed, amount=18207.0)
    [finding] = alerts.find_unconfirmed_orders(db)
    assert finding.order_id == unconfirmed.id
    assert "submitted for ₹18,207.00 but Flipkart never confirmed" in finding.message


def test_an_open_alert_written_the_old_way_reads_the_new_way_after_the_next_pass(db, account) -> None:
    """Each pass rewrites an open alert's message, so the alerts already open
    when the money format changed read the same as new ones after one pass."""
    order = _order(db, account, status=OrderStatus.over_target, amount=129900.0, target=125000.0)
    now = utcnow().replace(tzinfo=None)
    db.add(
        Alert(
            key=f"order_over_target:order:{order.id}",
            kind="order_over_target",
            severity=AlertSeverity.info,
            message=f"Order {order.id} was not placed: ₹129,900 is over the ₹125,000 target (one@test.com)",
            account_id=account.id,
            order_id=order.id,
            details={"amount": 129900.0, "target": 125000.0, "model": "Realme 2"},
            resolved=False,
            created_at=now,
            updated_at=now,
            last_seen_at=now,
        )
    )
    db.commit()

    alerts.evaluate(db)

    [row] = [alert for alert in _all_alerts(db) if alert.order_id == order.id]
    assert not row.resolved
    assert "₹1,29,900.00 is over the ₹1,25,000.00 price cap" in row.message


def test_over_target_is_info_not_critical(db, account) -> None:
    """The price cap working is a healthy outcome, not a failure (PLAN.md §6a)."""
    _order(db, account, status=OrderStatus.over_target, amount=10450.0, target=9900.0)
    severity = alerts.find_over_target_orders(db)[0].severity
    assert severity == AlertSeverity.info
    assert severity != AlertSeverity.critical


@pytest.mark.parametrize(
    "status", [OrderStatus.placed, OrderStatus.pending, OrderStatus.failed]
)
def test_over_target_does_not_fire_for_other_statuses(db, account, status) -> None:
    _order(db, account, status=status)
    assert alerts.find_over_target_orders(db) == []


# --- rule 2: failed orders ----------------------------------------------------
def test_failed_order_fires_as_a_warning(db, account) -> None:
    order = _order(db, account, status=OrderStatus.failed, placed_at=None)
    findings = alerts.find_failed_orders(db)
    assert _keys(findings) == {f"order_failed:order:{order.id}"}
    assert findings[0].severity == AlertSeverity.warning
    assert account.email in findings[0].message


@pytest.mark.parametrize(
    "status",
    [OrderStatus.placed, OrderStatus.pending, OrderStatus.over_target, OrderStatus.unconfirmed],
)
def test_failed_order_does_not_fire_for_other_statuses(db, account, status) -> None:
    _order(db, account, status=status)
    assert alerts.find_failed_orders(db) == []


# --- unconfirmed orders -------------------------------------------------------
def test_unconfirmed_order_fires_as_critical(db, account) -> None:
    """Submitted, never confirmed: money may be committed and nobody knows.

    The only order outcome above a warning, because ordering again on the
    account before someone checks can buy the same thing twice.
    """
    order = _order(db, account, status=OrderStatus.unconfirmed, amount=17707.0)
    findings = alerts.find_unconfirmed_orders(db)
    assert _keys(findings) == {f"order_unconfirmed:order:{order.id}"}
    assert findings[0].severity == AlertSeverity.critical
    assert account.email in findings[0].message
    assert "before ordering again" in findings[0].message


@pytest.mark.parametrize(
    "status",
    [OrderStatus.placed, OrderStatus.pending, OrderStatus.over_target, OrderStatus.failed],
)
def test_unconfirmed_order_does_not_fire_for_other_statuses(db, account, status) -> None:
    _order(db, account, status=status)
    assert alerts.find_unconfirmed_orders(db) == []


def test_unconfirmed_rule_is_registered() -> None:
    assert "order_unconfirmed" in {rule.kind for rule in alerts.RULES}


# --- rule 3: overdue delivery -------------------------------------------------
def test_overdue_delivery_fires_past_the_threshold(db, account) -> None:
    order = _order(
        db,
        account,
        placed_at=utcnow() - timedelta(days=14),
        platform_status="Out for Delivery",
    )
    findings = alerts.find_overdue_deliveries(db)
    assert _keys(findings) == {f"delivery_overdue:order:{order.id}"}
    finding = findings[0]
    assert finding.severity == AlertSeverity.warning
    assert "Out for Delivery" in finding.message
    assert finding.details["days"] == 14
    assert finding.details["overdue_days"] == alerts.OVERDUE_DAYS


def test_an_order_inside_the_threshold_is_not_overdue(db, account) -> None:
    _order(
        db,
        account,
        placed_at=utcnow() - timedelta(days=3),
        platform_status="Shipped",
    )
    assert alerts.find_overdue_deliveries(db) == []


@pytest.mark.parametrize("state", ["Delivered", "delivered", "Cancelled", "cancelled"])
def test_a_settled_order_is_never_overdue(db, account, state) -> None:
    """Matched case-insensitively: the platform's own wording is stored raw."""
    _order(db, account, placed_at=utcnow() - timedelta(days=40), platform_status=state)
    assert alerts.find_overdue_deliveries(db) == []


def test_an_order_with_no_platform_status_can_go_overdue(db, account) -> None:
    """NULL is not "delivered" — after ten days, silence is the problem."""
    _order(db, account, placed_at=utcnow() - timedelta(days=14), platform_status=None)
    findings = alerts.find_overdue_deliveries(db)
    assert len(findings) == 1
    assert 'Flipkart still says "nothing yet"' in findings[0].message


@pytest.mark.parametrize("status", [OrderStatus.failed, OrderStatus.over_target])
def test_an_order_that_never_reached_the_platform_is_not_overdue(
    db, account, status
) -> None:
    """Nothing was placed, so nothing can be late — this would be unactionable."""
    _order(db, account, status=status, placed_at=utcnow() - timedelta(days=40))
    assert alerts.find_overdue_deliveries(db) == []


def test_an_unplaced_order_is_not_overdue(db, account) -> None:
    _order(db, account, placed_at=None, platform_status=None)
    assert alerts.find_overdue_deliveries(db) == []


def test_the_overdue_threshold_is_a_parameter(db, account) -> None:
    _order(db, account, placed_at=utcnow() - timedelta(days=3), platform_status="Shipped")
    assert alerts.find_overdue_deliveries(db) == []
    assert len(alerts.find_overdue_deliveries(db, overdue_days=2)) == 1


# --- rule 4: invalid sessions -------------------------------------------------
def test_an_invalid_session_fires(db) -> None:
    row = Account(email="dead@test.com", status=AccountStatus.session_invalid)
    db.add(row)
    db.commit()
    findings = alerts.find_invalid_sessions(db)
    assert _keys(findings) == {f"session_invalid:account:{row.id}"}
    assert findings[0].severity == AlertSeverity.warning
    assert findings[0].account_id == row.id
    assert "dead@test.com" in findings[0].message


@pytest.mark.parametrize(
    "status",
    [
        AccountStatus.new,
        AccountStatus.session_valid,
        AccountStatus.logged_out,
        AccountStatus.blocked,
    ],
)
def test_other_account_statuses_do_not_fire(db, status) -> None:
    db.add(Account(email=f"{status.value}@test.com", status=status))
    db.commit()
    assert alerts.find_invalid_sessions(db) == []


# --- rule 5: stale sync -------------------------------------------------------
def test_a_never_synced_order_fires_as_info(db, account) -> None:
    order = _order(db, account, placed_at=utcnow() - timedelta(days=5), synced_at=None)
    findings = alerts.find_unsynced_orders(db)
    assert _keys(findings) == {f"stale_sync:order:{order.id}"}
    assert findings[0].severity == AlertSeverity.info
    assert findings[0].details["days"] == 5


def test_a_synced_order_does_not_fire(db, account) -> None:
    _order(
        db,
        account,
        placed_at=utcnow() - timedelta(days=5),
        synced_at=utcnow() - timedelta(days=1),
    )
    assert alerts.find_unsynced_orders(db) == []


def test_a_fresh_order_is_not_stale_yet(db, account) -> None:
    _order(db, account, placed_at=utcnow() - timedelta(hours=6), synced_at=None)
    assert alerts.find_unsynced_orders(db) == []


def test_the_stale_threshold_is_a_parameter(db, account) -> None:
    _order(db, account, placed_at=utcnow() - timedelta(days=5), synced_at=None)
    assert len(alerts.find_unsynced_orders(db, stale_days=30)) == 0
    assert len(alerts.find_unsynced_orders(db, stale_days=2)) == 1


# --- evaluate: raising and clearing -------------------------------------------
def test_evaluate_reports_every_registered_kind(db) -> None:
    """Zeros included, so a caller can render stable counters."""
    counts = alerts.evaluate(db)
    assert set(counts) == set(alerts.RULE_KINDS)
    assert set(counts.values()) == {0}


def test_evaluate_raises_one_alert_per_finding(db, account) -> None:
    failed = _order(db, account, status=OrderStatus.failed, placed_at=None)
    stale = _order(db, account, placed_at=utcnow() - timedelta(days=5))

    counts = alerts.evaluate(db)
    assert counts["order_failed"] == 1
    assert counts["stale_sync"] == 1
    assert counts["order_over_target"] == 0
    assert _open_keys(db) == {
        f"order_failed:order:{failed.id}",
        f"stale_sync:order:{stale.id}",
    }


def test_evaluate_twice_does_not_duplicate(db, account) -> None:
    _order(db, account, status=OrderStatus.failed, placed_at=None)
    alerts.evaluate(db)
    alerts.evaluate(db)
    assert len(_all_alerts(db)) == 1


def test_evaluate_resolves_an_alert_whose_condition_went_away(db, account) -> None:
    order = _order(db, account, status=OrderStatus.failed, placed_at=None)
    alerts.evaluate(db)
    key = f"order_failed:order:{order.id}"
    assert _open_keys(db) == {key}

    # The operator retried it by hand and it went through.
    order.status = OrderStatus.placed
    order.placed_at = utcnow()
    order.synced_at = utcnow()
    db.commit()

    counts = alerts.evaluate(db)
    assert counts["order_failed"] == 0
    assert _open_keys(db) == set()
    closed = _all_alerts(db)[0]
    assert closed.resolved is True
    assert closed.resolved_at is not None


def test_evaluate_re_raises_the_same_row_when_the_condition_returns(
    db, account
) -> None:
    order = _order(db, account, status=OrderStatus.failed, placed_at=None)
    alerts.evaluate(db)
    alert_id = _all_alerts(db)[0].id

    order.status = OrderStatus.placed
    order.placed_at = utcnow()
    order.synced_at = utcnow()
    db.commit()
    alerts.evaluate(db)

    order.status = OrderStatus.failed  # and it breaks again
    db.commit()
    counts = alerts.evaluate(db)

    assert counts["order_failed"] == 1
    rows = _all_alerts(db)
    assert len(rows) == 1  # not a brand-new row
    assert rows[0].id == alert_id
    assert rows[0].resolved is False
    assert rows[0].resolved_at is None


def test_evaluate_refreshes_the_message_of_a_still_open_alert(db, account) -> None:
    order = _order(db, account, placed_at=utcnow() - timedelta(days=14))
    alerts.evaluate(db)
    first_message = _all_alerts(db)[0].message

    order.platform_status = "Out for Delivery"
    db.commit()
    alerts.evaluate(db)

    row = _all_alerts(db)[0]
    assert row.message != first_message
    assert "Out for Delivery" in row.message


def test_evaluate_leaves_an_unrelated_kind_alone(db) -> None:
    """A sync job's own alert is not a rule's to resolve (see the module doc)."""
    alerts.raise_alert(db, key=KEY, kind="manual_note", message="Wrong entity")
    alerts.evaluate(db)
    assert _open_keys(db) == {KEY}


def test_evaluate_adopts_a_row_whose_key_drifted_to_another_kind(db, account) -> None:
    """``key`` is unique, so a rule's key held under another kind would make the
    rule's INSERT fail and take the whole pass down with it."""
    order = _order(db, account, status=OrderStatus.failed, placed_at=None)
    key = f"order_failed:order:{order.id}"
    alerts.raise_alert(db, key=key, kind="hand_raised", message="by hand")

    counts = alerts.evaluate(db)  # must not raise

    assert counts["order_failed"] == 1
    rows = _all_alerts(db)
    assert len(rows) == 1  # adopted, not duplicated
    assert rows[0].kind == "order_failed"
    assert rows[0].resolved is False


def test_evaluate_handles_more_findings_than_one_in_clause(db) -> None:
    """A rule can report thousands of findings; the key lookup is chunked."""
    count = alerts._KEY_CHUNK + 5
    db.add_all(
        Account(email=f"dead{index}@test.com", status=AccountStatus.session_invalid)
        for index in range(count)
    )
    db.commit()
    assert alerts.evaluate(db)["session_invalid"] == count
    assert alerts.open_count(db) == count
    # And a second pass neither duplicates nor resolves any of them.
    assert alerts.evaluate(db)["session_invalid"] == count
    assert len(_all_alerts(db)) == count


def test_an_overdue_order_also_reads_as_unsynced(db, account) -> None:
    """Two different questions about one order, so two alerts on purpose:
    "the platform owes us a delivery" and "we know nothing about it"."""
    order = _order(db, account, placed_at=utcnow() - timedelta(days=14))
    counts = alerts.evaluate(db)
    assert counts["delivery_overdue"] == 1
    assert counts["stale_sync"] == 1
    assert _open_keys(db) == {
        f"delivery_overdue:order:{order.id}",
        f"stale_sync:order:{order.id}",
    }


# --- summary ------------------------------------------------------------------
def test_summary_counts_match_the_rows(db, account) -> None:
    _order(db, account, status=OrderStatus.failed, placed_at=None)
    _order(db, account, status=OrderStatus.over_target, amount=1.0, target=0.5)
    db.add(Account(email="dead@test.com", status=AccountStatus.session_invalid))
    db.commit()
    alerts.evaluate(db)

    rows = _all_alerts(db)
    summary = alerts.summary(db)
    assert summary["open"] == len([row for row in rows if not row.resolved]) == 3
    assert summary["resolved"] == 0
    assert summary["by_kind"] == {
        "order_failed": 1,
        "order_over_target": 1,
        "session_invalid": 1,
    }
    assert summary["by_severity"] == {"info": 1, "warning": 2, "critical": 0}
    assert sum(summary["by_kind"].values()) == summary["open"]


def test_summary_breakdowns_count_only_open_alerts(db) -> None:
    alerts.raise_alert(db, key="a:1", kind="manual", message="one")
    alerts.raise_alert(db, key="a:2", kind="manual", message="two")
    alerts.resolve_alert(db, "a:1")

    summary = alerts.summary(db)
    assert summary["open"] == 1
    assert summary["resolved"] == 1
    assert summary["by_kind"] == {"manual": 1}
    assert summary["by_severity"]["warning"] == 1


def test_summary_always_carries_every_severity(db) -> None:
    summary = alerts.summary(db)
    assert summary == {
        "open": 0,
        "resolved": 0,
        "by_severity": {"info": 0, "warning": 0, "critical": 0},
        "by_kind": {},
    }


def test_summary_lists_the_worst_kind_first(db) -> None:
    for index in range(3):
        alerts.raise_alert(db, key=f"noisy:{index}", kind="noisy", message="x")
    alerts.raise_alert(db, key="quiet:1", kind="quiet", message="x")
    assert list(alerts.summary(db)["by_kind"]) == ["noisy", "quiet"]


def test_open_count_ignores_resolved_rows(db) -> None:
    alerts.raise_alert(db, key="a:1", kind="manual", message="one")
    alerts.raise_alert(db, key="a:2", kind="manual", message="two")
    assert alerts.open_count(db) == 2
    alerts.resolve_alert(db, "a:1")
    assert alerts.open_count(db) == 1


# --- the two address rules (spec FR-044 / FR-045, C-5 as decided) ---------------
def _unit_with_address(db, account, *, address: str, pincode: str, changed=None):
    from app.db.models import Order as _Order  # the fixture file already imports Order

    order = _order(db, account, order_id=f"OD{db.query(_Order).count() + 1}")
    db.refresh(order)
    unit = order.units[0]
    unit.delivery_address = address
    unit.delivery_pincode = pincode
    unit.address_changed_at = changed
    db.commit()
    return unit


def test_address_in_book_wants_the_same_pincode_and_first_line():
    from app.db.models import Address

    book = [Address(label="hq", name="HQ", line1="Tower 5, 1005, Garden Bay", pincode="226013")]
    page = "Home · Tower-5 1005 Garden Bay, Gomti Nagar Extension Lucknow Uttar Pradesh 226013 · A · 8957768866"
    assert alerts.address_in_book(page, "226013", book) is True
    assert alerts.address_in_book(page, "226010", book) is False  # another pincode
    assert alerts.address_in_book("Home · Flat 9 Some Other Road Lucknow 226013", "226013", book) is False
    assert alerts.address_in_book(None, "226013", book) is False
    assert alerts.address_in_book(page, "226013", []) is False


def test_a_short_first_line_matches_only_as_whole_words():
    """"A-1" is not the first line of "A-1234, Nucleus Software": the Address
    Book line must appear as a run of whole words, never inside a longer one,
    and a line too short to tell addresses apart matches none — otherwise a
    generic entry hides every mismatch in its pincode."""
    from app.db.models import Address

    book = [Address(label="a1", name="A1", line1="A-1", pincode="201307")]
    longer = "Work · A-1234, Nucleus Software Sector 62 Noida 201307 · N · 8957768866"
    assert alerts.address_in_book(longer, "201307", book) is False
    exact = "Work · A-1, Nucleus Software Sector 62 Noida 201307 · N · 8957768866"
    assert alerts.address_in_book(exact, "201307", book) is True

    tiny = [Address(label="one", name="One", line1="1", pincode="201307")]
    assert alerts.address_in_book("Work · 1 Nucleus Software Sector 62 Noida 201307", "201307", tiny) is False


def test_address_mismatch_is_raised_for_units_shipped_outside_the_book(db, account):
    from app.db.models import Address

    db.add(Address(label="hq", name="HQ", line1="Tower 5, 1005, Garden Bay", pincode="226013"))
    db.commit()
    ours = _unit_with_address(db, account, address="Work · Tower 5, 1005, Garden Bay Lucknow 226013 · A · 89", pincode="226013")
    theirs = _unit_with_address(db, account, address="Work · A-39, Nucleus Software Sector 62 Noida 201307 · A · 89", pincode="201307")
    _order(db, account)  # never read back: no address, no finding

    keys = _keys(alerts.find_address_mismatches(db))
    assert keys == {f"address_mismatch:unit:{theirs.id}"}
    assert f"address_mismatch:unit:{ours.id}" not in keys

    counts = alerts.evaluate(db)
    assert counts["address_mismatch"] == 1
    row = db.scalar(select(Alert).where(Alert.key == f"address_mismatch:unit:{theirs.id}"))
    assert row is not None and row.severity == AlertSeverity.warning and row.order_id == theirs.order_pk
    assert "not in the Address Book" in row.message

    # The address book catches up: the next pass closes it.
    db.add(Address(label="noida", name="Noida", line1="A-39, Nucleus Software Sector 62", pincode="201307"))
    db.commit()
    alerts.evaluate(db)
    db.refresh(row)
    assert row.resolved is True


def _unit_billed_under(db, account, *, chosen: str | None, chosen_name: str | None = None, billed: str | None, business: str | None = None):
    from app.db.models import Order as _Order

    order = _order(db, account, order_id=f"OD{db.query(_Order).count() + 1}", gstin=chosen, gst_name=chosen_name)
    db.refresh(order)
    unit = order.units[0]
    unit.billed_gstin = billed
    unit.billed_business_name = business
    db.commit()
    return unit


def test_gst_mismatch_is_raised_only_when_both_gstins_are_known_and_differ(db, account):
    """The owner's addition of 26 Oct 2026: the order page names the GST the
    order was billed under; when it is not the one the run chose, the invoice
    names the wrong entity — quietly, since the order itself went through."""
    same = _unit_billed_under(db, account, chosen="09AAUFT2204P1ZI", chosen_name="Transparent Deals", billed="09aauft2204p1zi ")
    other = _unit_billed_under(db, account, chosen="27AAPFU0939F1ZV", chosen_name="Acme", billed="09AAUFT2204P1ZI", business="TRANSPARENT DEALS")
    _unit_billed_under(db, account, chosen=None, billed="09AAUFT2204P1ZI")  # the run chose no GST: nothing to compare
    _unit_billed_under(db, account, chosen="27AAPFU0939F1ZV", billed=None)  # the page showed no block: nothing to compare

    keys = _keys(alerts.find_gst_mismatches(db))
    assert keys == {f"gst_mismatch:unit:{other.id}"}
    assert f"gst_mismatch:unit:{same.id}" not in keys  # case and spacing are not a difference

    counts = alerts.evaluate(db)
    assert counts["gst_mismatch"] == 1
    row = db.scalar(select(Alert).where(Alert.key == f"gst_mismatch:unit:{other.id}"))
    assert row is not None and row.severity == AlertSeverity.warning and row.order_id == other.order_pk
    assert "09AAUFT2204P1ZI (TRANSPARENT DEALS)" in row.message and "27AAPFU0939F1ZV (Acme)" in row.message
    assert row.details["chosen_gstin"] == "27AAPFU0939F1ZV"

    # The run's record is corrected (or the page re-read agrees): the next pass closes it.
    other.billed_gstin = "27AAPFU0939F1ZV"
    db.commit()
    alerts.evaluate(db)
    db.refresh(row)
    assert row.resolved is True


def test_address_change_is_one_alert_per_change(db, account):
    first = utcnow() - timedelta(days=2)
    unit = _unit_with_address(db, account, address="Home · B-7 Lucknow 226013", pincode="226013", changed=first)
    keys = _keys(alerts.find_address_changes(db))
    assert len(keys) == 1 and next(iter(keys)).startswith(f"address_change:unit:{unit.id}:")
    alerts.evaluate(db)
    row = db.scalar(select(Alert).where(Alert.key.like(f"address_change:unit:{unit.id}:%")))
    assert row is not None and "changed on the platform" in row.message

    # Accepted by a person; a later change is a new row, not a quiet update.
    assert alerts.resolve_alert(db, row.key, note="we moved it", who="ops@test.com") is True
    unit.address_changed_at = utcnow()
    unit.delivery_address = "Home · C-1 Lucknow 226013"
    db.commit()
    alerts.evaluate(db)
    rows = list(db.scalars(select(Alert).where(Alert.key.like(f"address_change:unit:{unit.id}:%")).order_by(Alert.id)).all())
    assert len(rows) == 2
    assert rows[0].resolved is True and rows[1].resolved is False
