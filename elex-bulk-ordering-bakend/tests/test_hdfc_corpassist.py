"""CorpAssist (HDFC): reading a child-card email, Fetch from the shared inbox,
the user ids and child cards API, balances from our own orders, and the
"minimum remaining" card rule a run applies.

The emails here are made up: no real HDFC child-card email had been seen
when this was written (19 Nov 2026), see app.io.hdfc_mail.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.core import corpassist
from app.core.card_pool import CardRules, plan
from app.db.models import Account, AccountStatus, HdfcChildCard, HdfcUserId, Order, OrderStatus
from app.io.hdfc_mail import luhn_ok, read_child_card

CARD_A = "4111111111111111"
CARD_B = "5555555555554444"

MAIL_A = (
    "Virtual card issued",
    "<p>Dear Customer,</p><p>Your virtual card <b>4111 1111 1111 1111</b> has been issued "
    "under parent card XXXX XXXX XXXX 9876.</p><p>Card limit: Rs. 25,000.00</p>"
    "<p>Expiry 12/29 CVV 123</p>",
)


# --- reading an email -------------------------------------------------------


def test_a_child_card_email_gives_the_number_amount_and_parent():
    mail = read_child_card(*MAIL_A)
    assert mail.number == CARD_A
    assert mail.opening_amount == 25000.0
    assert mail.parent_last4 == "9876"
    assert mail.reason is None


def test_the_expiry_and_cvv_are_never_read():
    mail = read_child_card(*MAIL_A)
    assert set(vars(mail)) == {"number", "opening_amount", "parent_last4", "reason"}


def test_a_printed_mail_shows_only_the_last_four():
    shown = repr(read_child_card(*MAIL_A))
    assert CARD_A not in shown and "1111" in shown


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("Your card XXXX XXXX XXXX 1111 is ready. Limit INR 5000", "masked"),
        ("Reference 1234 5678 9012 3456 for your request", "no card number"),  # fails Luhn
        (f"Cards {CARD_A} and {CARD_B}", "more than one"),
        ("Hello", "no card number"),
    ],
)
def test_an_email_without_one_clear_card_is_refused_with_a_reason(body, reason):
    mail = read_child_card("", body)
    assert mail.number is None
    assert reason in mail.reason


def test_luhn():
    assert luhn_ok(CARD_A) and luhn_ok(CARD_B)
    assert not luhn_ok("1234567890123456")


# --- fetch --------------------------------------------------------------------


class FakeInbox:
    def __init__(self, mails: dict[str, list[corpassist.Mail]], broken: set[str] = frozenset()) -> None:
        self.mails = mails
        self.broken = broken
        self.closed = False

    def mails_to(self, address: str) -> list[corpassist.Mail]:
        if address in self.broken:
            raise OSError("connection reset")
        return self.mails.get(address, [])

    def close(self) -> None:
        self.closed = True


def _user(db, name="hdfc1", email="hdfc1@corp.test", parent="9876") -> HdfcUserId:
    user = HdfcUserId(username=name, email=email, parent_card_last4=parent)
    db.add(user)
    db.commit()
    return user


def test_fetch_keeps_new_cards_once_and_records_why_others_failed(db):
    one = _user(db)
    two = _user(db, "hdfc2", "hdfc2@corp.test")
    three = _user(db, "hdfc3", "hdfc3@corp.test")
    sent = datetime(2026, 11, 18, 10, 0)
    inbox = FakeInbox(
        {
            "hdfc1@corp.test": [corpassist.Mail(*MAIL_A, sent_at=sent), corpassist.Mail(*MAIL_A, sent_at=sent)],
            "hdfc2@corp.test": [corpassist.Mail("", "card XXXX XXXX XXXX 4444")],
        },
        broken={"hdfc3@corp.test"},
    )
    results = {r.username: r for r in corpassist.fetch(db, [one, two, three], inbox)}

    assert inbox.closed
    assert (results["hdfc1"].emails, results["hdfc1"].added, results["hdfc1"].error) == (2, 1, None)
    assert "masked" in results["hdfc2"].error
    assert results["hdfc3"].error == "the inbox could not be read"
    card = db.query(HdfcChildCard).one()
    assert (card.card_number, card.opening_amount, card.parent_card_last4, card.issued_at) == (CARD_A, 25000.0, "9876", sent)
    db.refresh(two)
    assert two.last_fetched_at is not None and "masked" in two.last_error

    again = corpassist.fetch(db, [one], FakeInbox({"hdfc1@corp.test": [corpassist.Mail(*MAIL_A)]}))
    assert again[0].added == 0 and db.query(HdfcChildCard).count() == 1


def test_fetch_with_no_mail_says_so(db):
    user = _user(db)
    result = corpassist.fetch(db, [user], FakeInbox({}))[0]
    assert result.emails == 0 and "no HDFC email" in result.error


# --- balances and the card rule ------------------------------------------------


def _account(db, email="acc@test.com") -> Account:
    account = Account(email=email, phone="9100000000", status=AccountStatus.new)
    db.add(account)
    db.commit()
    return account


def _card(db, user, number, opening, status="active") -> HdfcChildCard:
    card = HdfcChildCard(user_id=user.id, card_number=number, opening_amount=opening, status=status)
    db.add(card)
    db.commit()
    return card


class _SheetCard:
    def __init__(self, number: str) -> None:
        self.number = number


def test_used_counts_only_bought_orders_paid_with_the_card(db):
    account = _account(db)
    db.add_all(
        [
            Order(account_id=account.id, status=OrderStatus.placed, card_number="4111 1111 1111 1111", amount=10000),
            Order(account_id=account.id, status=OrderStatus.unconfirmed, card_number=CARD_A, amount=2500),
            Order(account_id=account.id, status=OrderStatus.failed, card_number=CARD_A, amount=9999),
            Order(account_id=account.id, status=OrderStatus.placed, card_number=CARD_B, amount=100),
        ]
    )
    db.commit()
    assert corpassist.used_by_number(db, [CARD_A]) == {CARD_A: 12500.0}


def test_min_remaining_blocks_low_cancelled_and_unknown_amount_cards_only(db):
    user = _user(db)
    a, b, c, d, e = (_account(db, f"a{i}@test.com") for i in range(5))
    low, cancelled, no_amount, fine = "4012888888881881", "5105105105105100", "4222222222222", CARD_B
    _card(db, user, CARD_A, 1000)  # becomes low below
    _card(db, user, cancelled, 50000, status="cancelled")
    _card(db, user, no_amount, None)
    _card(db, user, fine, 50000)
    db.add(Order(account_id=a.id, status=OrderStatus.placed, card_number=CARD_A, amount=900))
    db.commit()
    cards = {
        a.id: _SheetCard(CARD_A),
        b.id: _SheetCard(cancelled),
        c.id: _SheetCard(no_amount),
        d.id: _SheetCard(fine),
        e.id: _SheetCard(low),  # not a CorpAssist card: untouched
    }
    result = plan(db, [a.id, b.id, c.id, d.id, e.id], cards, CardRules(min_remaining=500))

    assert "₹100.00 left" in result.blocked[a.id]
    assert "cancelled" in result.blocked[b.id]
    assert "no opening amount" in result.blocked[c.id]
    assert d.id not in result.blocked and e.id not in result.blocked
    assert CardRules(min_remaining=500).as_payload() == {"card_min_remaining": 500}


# --- API ----------------------------------------------------------------------


def test_user_ids_crud(client, auth_headers):
    made = client.post(
        "/hdfc/user-ids",
        json={"username": " ops1 ", "email": "Ops1@Corp.test", "mobile": "+91 98765 43210", "parent_card": "4321 0000 0000 9876"},
        headers=auth_headers,
    )
    assert made.status_code == 201, made.text
    body = made.json()
    assert (body["username"], body["email"], body["mobile"], body["parent_card_last4"]) == (
        "ops1",
        "ops1@corp.test",
        "9876543210",
        "9876",
    )
    clash = client.post("/hdfc/user-ids", json={"username": "ops2", "email": "ops1@corp.test"}, headers=auth_headers)
    assert clash.status_code == 409
    assert client.post("/hdfc/user-ids", json={"username": "x", "email": "nope"}, headers=auth_headers).status_code == 422

    off = client.patch(f"/hdfc/user-ids/{body['id']}", json={"active": False, "mobile": None}, headers=auth_headers)
    assert off.json()["active"] is False and off.json()["mobile"] is None
    assert client.delete(f"/hdfc/user-ids/{body['id']}", headers=auth_headers).status_code == 204
    assert client.get("/hdfc/user-ids", headers=auth_headers).json() == []


def test_the_api_needs_a_signed_in_user(client):
    assert client.get("/hdfc/user-ids").status_code == 401
    assert client.get("/hdfc/child-cards").status_code == 401


def test_fetch_without_an_inbox_is_refused(client, auth_headers):
    client.post("/hdfc/user-ids", json={"username": "ops1", "email": "ops1@corp.test"}, headers=auth_headers)
    response = client.post("/hdfc/fetch", json={}, headers=auth_headers)
    assert response.status_code == 409 and "inbox" in response.json()["detail"]


def test_fetch_through_the_api(client, auth_headers, monkeypatch):
    client.post("/hdfc/user-ids", json={"username": "ops1", "email": "ops1@corp.test"}, headers=auth_headers)
    client.post("/hdfc/user-ids", json={"username": "ops2", "email": "ops2@corp.test", "active": False}, headers=auth_headers)
    inbox = FakeInbox({"ops1@corp.test": [corpassist.Mail(*MAIL_A)]})
    monkeypatch.setattr(corpassist, "mail_source", lambda db: inbox)

    results = client.post("/hdfc/fetch", json={}, headers=auth_headers).json()
    assert [(r["username"], r["added"]) for r in results] == [("ops1", 1)]  # the inactive one is passed over


def test_child_cards_list_filter_csv_and_actions(client, auth_headers, db):
    user = _user(db)
    account = _account(db)
    first = _card(db, user, CARD_A, 25000)
    second = _card(db, user, CARD_B, 1000)
    db.add(Order(account_id=account.id, status=OrderStatus.placed, card_number=CARD_A, amount=5000))
    db.commit()

    rows = {r["card_number"]: r for r in client.get("/hdfc/child-cards", headers=auth_headers).json()}
    assert (rows[CARD_A]["used"], rows[CARD_A]["remaining"], rows[CARD_A]["username"]) == (5000.0, 20000.0, "hdfc1")
    over = client.get("/hdfc/child-cards", params={"min_remaining": 5000}, headers=auth_headers).json()
    assert [r["card_number"] for r in over] == [CARD_A]
    assert len(client.get("/hdfc/child-cards", params={"search": "4444"}, headers=auth_headers).json()) == 1

    csv_text = client.get("/hdfc/child-cards.csv", headers=auth_headers).text
    header = csv_text.splitlines()[0]
    assert "Card Number" in header and "CVV" not in header and "Expiry" not in header
    assert CARD_A in csv_text

    done = client.post("/hdfc/child-cards/action", json={"ids": [first.id], "action": "cancel"}, headers=auth_headers)
    assert done.json() == {"changed": 1}
    cancelled = client.get("/hdfc/child-cards", params={"status": "cancelled"}, headers=auth_headers).json()
    assert [r["id"] for r in cancelled] == [first.id]
    client.post("/hdfc/child-cards/action", json={"ids": [second.id], "action": "delete"}, headers=auth_headers)
    assert [r["id"] for r in client.get("/hdfc/child-cards", headers=auth_headers).json()] == [first.id]
    bad = client.post("/hdfc/child-cards/action", json={"ids": [1], "action": "nuke"}, headers=auth_headers)
    assert bad.status_code == 422


def test_dates_are_india_days_in_the_filter_and_the_csv(client, auth_headers, db):
    """22:00 UTC on 18 Nov is 03:30 on 19 Nov in India: it belongs to the 19th."""
    user = _user(db)
    card = _card(db, user, CARD_A, 100)
    card.created_at = datetime(2026, 11, 18, 22, 0)
    db.commit()
    on = lambda day: client.get(  # noqa: E731
        "/hdfc/child-cards", params={"created_from": day, "created_to": day}, headers=auth_headers
    ).json()
    assert len(on("2026-11-19")) == 1 and on("2026-11-18") == []
    assert "2026-11-19 03:30" in client.get("/hdfc/child-cards.csv", headers=auth_headers).text


def test_deleting_a_user_id_deletes_its_cards(client, auth_headers, db):
    user = _user(db)
    _card(db, user, CARD_A, 100)
    assert client.delete(f"/hdfc/user-ids/{user.id}", headers=auth_headers).status_code == 204
    db.expire_all()
    assert db.query(HdfcChildCard).count() == 0


def test_an_order_run_takes_the_min_remaining_rule(client, auth_headers):
    """The form field reaches the run's rules (refused for a COD run, like the other card rules)."""
    response = client.post(
        "/jobs/order",
        data={
            "product_link": "https://www.flipkart.com/x/p/itm1",
            "target": "1000",
            "payment_mode": "cod",
            "card_min_remaining": "500",
        },
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert "Card rules apply to card runs" in response.text
