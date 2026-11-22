"""HDFC card automation: the SMS relay (Employee Phones), HDFC's bulk sheet,
card batches (portal or download, then matching the cards that arrive), the
portal endpoints, and pairing CorpAssist cards with accounts by balance.

The portal is a fake here: the real one (app.automation.hdfc_portal) drives a
browser against corpcardassist.hdfc.bank.in and is not run by the suite.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.api import corpassist as corpassist_api
from app.automation import hdfc_portal
from app.config import get_settings
from app.core import corpassist, hdfc_batches, sms_relay
from app.db.base import SessionLocal
from app.db.models import HdfcCardBatch, HdfcChildCard, HdfcUserId, OtpPhone, SmsMessage, naive_utcnow
from app.io import hdfc_bulk_csv

CARD_A = "4111111111111111"
CARD_B = "5555555555554444"
CARD_C = "4012888888881881"


# --- reading a code out of a text ------------------------------------------------


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("123456 is your OTP for login to HDFC Bank CorpAssist. Do not share. -HDFC Bank", "123456"),
        ("OTP for CorpCard Assist login is 4821. Valid 5 mins", "4821"),
        ("Your One Time Password is 90817263", "90817263"),
        ("Use verification code 556677 to continue", "556677"),
        ("Rs 1234 debited from card XX1111 on 21-11-26", None),
        ("Your order 123456 has shipped", None),
        ("", None),
    ],
)
def test_the_code_in_a_text(text, code):
    assert sms_relay.code_from_sms(text) == code


@pytest.mark.parametrize(
    ("raw", "number"),
    [("+91 98765 43210", "9876543210"), ("09876543210", "9876543210"), ("9876543210", "9876543210"), ("12345", None)],
)
def test_numbers_are_ten_digits(raw, number):
    assert sms_relay.normalise_number(raw) == number


# --- Employee Phones API and the relay ---------------------------------------------


def _add_phone(client, auth_headers, number="9876543210", label="Ops phone 1") -> dict:
    made = client.post("/otp-phones", json={"label": label, "phone_number": number}, headers=auth_headers)
    assert made.status_code == 201, made.text
    return made.json()


def test_a_phone_gets_a_token_once_and_only_its_hash_is_kept(client, auth_headers, db):
    made = _add_phone(client, auth_headers)
    assert made["phone_number"] == "9876543210"
    assert made["webhook_path"] == f"/api/sms/inbound?token={made['token']}"
    assert made["token_hint"] == made["token"][-4:]
    assert made["online"] is False
    stored = db.get(OtpPhone, made["id"])
    assert stored.token_hash == sms_relay.hash_token(made["token"])
    assert made["token"] not in stored.token_hash
    listed = client.get("/otp-phones", headers=auth_headers).json()
    assert "token" not in listed[0]
    assert client.post("/otp-phones", json={"label": "x", "phone_number": "+91 9876543210"}, headers=auth_headers).status_code == 409
    assert client.post("/otp-phones", json={"label": "x", "phone_number": "123"}, headers=auth_headers).status_code == 422


def test_the_phone_api_needs_a_signed_in_user(client):
    assert client.get("/otp-phones").status_code == 401


@pytest.mark.parametrize(
    ("kwargs", "body_key"),
    [
        ({"json": {"from": "VM-HDFCBK", "text": "123456 is your OTP. -HDFC Bank"}}, "json"),
        ({"json": {"sender": "VM-HDFCBK", "message": "123456 is your OTP. -HDFC Bank"}}, "json"),
        ({"data": {"phone": "VM-HDFCBK", "msg": "123456 is your OTP. -HDFC Bank"}}, "form"),
        ({"content": "123456 is your OTP. -HDFC Bank", "headers": {"content-type": "text/plain"}}, "plain"),
    ],
)
def test_a_forwarder_posts_a_text_in_any_shape(client, auth_headers, db, kwargs, body_key):
    phone = _add_phone(client, auth_headers)
    headers = kwargs.pop("headers", {})
    response = client.post(f"/sms/inbound?token={phone['token']}", headers=headers, **kwargs)
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "otp_found": True}
    row = client.get("/otp-phones", headers=auth_headers).json()[0]
    assert row["last_otp"] == "123456" and row["online"] is True
    texts = client.get(f"/otp-phones/{phone['id']}/messages", headers=auth_headers).json()
    assert texts[0]["otp"] == "123456"


def test_sms_forwarders_default_template_with_a_bearer_header(client, auth_headers, db):
    """GawkAr "SMS forwarder", set up as its guide says: URL sync, the app's
    own request body, and {"Authorization": "Bearer <token>"} (a stray space
    after the token included)."""
    phone = _add_phone(client, auth_headers)
    body = {
        "message": "17:05 - VM-HDFCBK (VM-HDFCBK) <br/>Message: OTP for CorpCard Assist login is 482913 (valid 5 mins). "
        "Do not share. -HDFC Bank (26.09.2026 17:05:12)",
        "subject": "You have a new SMS at the number 9876543210",
    }
    response = client.post("/sms/inbound", json=body, headers={"Authorization": f"Bearer {phone['token']} "})
    assert response.status_code == 200, response.text
    assert response.json()["otp_found"] is True
    text = client.get(f"/otp-phones/{phone['id']}/messages", headers=auth_headers).json()[0]
    assert text["sender"] == "VM-HDFCBK"
    assert text["otp"] == "482913"
    assert text["body"].startswith("OTP for CorpCard Assist") and "<br" not in text["body"]


def test_the_token_may_come_as_a_header(client, auth_headers):
    phone = _add_phone(client, auth_headers)
    ok = client.post("/sms/inbound", json={"text": "OTP is 4321"}, headers={"X-Phone-Token": phone["token"]})
    assert ok.status_code == 200
    bearer = client.post("/sms/inbound", json={"text": "OTP is 4321"}, headers={"Authorization": f"Bearer {phone['token']}"})
    assert bearer.status_code == 200


def test_a_wrong_inactive_or_replaced_token_is_refused(client, auth_headers, db):
    phone = _add_phone(client, auth_headers)
    assert client.post("/sms/inbound?token=nope", json={"text": "OTP 123456"}).status_code == 401
    assert client.post("/sms/inbound", json={"text": "OTP 123456"}).status_code == 401
    fresh = client.post(f"/otp-phones/{phone['id']}/token", headers=auth_headers).json()
    assert client.post(f"/sms/inbound?token={phone['token']}", json={"text": "x"}).status_code == 401
    assert client.post(f"/sms/inbound?token={fresh['token']}", json={"text": "hello"}).status_code == 200
    client.patch(f"/otp-phones/{phone['id']}", json={"active": False}, headers=auth_headers)
    assert client.post(f"/sms/inbound?token={fresh['token']}", json={"text": "hello"}).status_code == 401
    assert db.query(SmsMessage).count() == 1


def test_a_post_without_text_is_refused_and_ping_marks_the_phone(client, auth_headers):
    phone = _add_phone(client, auth_headers)
    assert client.post(f"/sms/inbound?token={phone['token']}", json={"from": "x"}).status_code == 422
    assert client.get(f"/sms/ping?token={phone['token']}").json() == {"ok": True}
    row = client.get("/otp-phones", headers=auth_headers).json()[0]
    assert row["last_ping_at"] is not None and row["online"] is True


def test_old_texts_are_dropped_and_deleting_a_phone_drops_its_texts(client, auth_headers, db):
    phone = _add_phone(client, auth_headers)
    db.add(SmsMessage(phone_id=phone["id"], body="old", received_at=naive_utcnow() - timedelta(hours=30)))
    db.commit()
    client.post(f"/sms/inbound?token={phone['token']}", json={"text": "new"})
    db.expire_all()
    assert [m.body for m in db.query(SmsMessage).all()] == ["new"]
    assert client.delete(f"/otp-phones/{phone['id']}", headers=auth_headers).status_code == 204
    assert db.query(SmsMessage).count() == 0


def test_an_otp_wait_reads_only_codes_sent_after_it_asked(db):
    phone = OtpPhone(label="p", phone_number="9876543210", token_hash="h", token_hint="abcd")
    db.add(phone)
    db.commit()
    asked = naive_utcnow()
    db.add(SmsMessage(phone_id=phone.id, sender="VM-HDFCBK", body="111111 is your OTP", otp="111111", received_at=asked - timedelta(minutes=1)))
    db.commit()
    assert asyncio.run(sms_relay.wait_for_otp(SessionLocal, "9876543210", not_before=asked, timeout=0)) is None
    db.add(SmsMessage(phone_id=phone.id, sender="AD-AMAZON", body="222222 is your OTP", otp="222222", received_at=asked))
    db.add(SmsMessage(phone_id=phone.id, sender="VM-HDFCBK", body="333333 is your OTP", otp="333333", received_at=asked))
    db.commit()
    got = asyncio.run(
        sms_relay.wait_for_otp(SessionLocal, "+919876543210", not_before=asked, timeout=0, sender_like=("HDFC",))
    )
    assert got == "333333"
    with pytest.raises(sms_relay.NoPhoneForNumber):
        asyncio.run(sms_relay.wait_for_otp(SessionLocal, "9000000000", not_before=asked, timeout=0))


# --- HDFC's bulk sheet ----------------------------------------------------------------


def test_the_bulk_sheet_has_hdfcs_headers_and_one_row_per_card():
    rows = [hdfc_bulk_csv.BulkRow("0001010370000166986", "manmohan", "earweeput@catbar.net", "7058969506", 1.0, 1)] * 2
    text = hdfc_bulk_csv.build(rows).decode()
    lines = text.split("\r\n")
    assert lines[0] == "Aan,VirtualCardUserName,EmailId,MobileNumber,Amount,ValidForDays"
    assert lines[1] == "0001010370000166986,MANMOHAN,earweeput@catbar.net,7058969506,1,1"
    assert lines[2] == lines[1] and lines[3] == ""
    assert "2500.50" in hdfc_bulk_csv.build([hdfc_bulk_csv.BulkRow("1" * 10, "a", "a@b.co", "9" * 10, 2500.5, 7)]).decode()
    with pytest.raises(ValueError):
        hdfc_bulk_csv.build([])


def test_the_portals_output_file_becomes_one_line_per_refused_row():
    raw = (
        "Aan,VirtualCardUserName,EmailId,MobileNumber,Amount,ValidForDays,Remarks\r\n"
        "0001,MANMOHAN,a@b.co,7058969506,1,1,Success\r\n"
        "0001,AYUSH,a@b.co,95829,1,1,Invalid mobile number\r\n"
        f"0001,HARSH,a@b.co,9009122239,1,1,Card {CARD_A} exists\r\n"
    ).encode()
    lines = hdfc_bulk_csv.read_output_file(raw)
    assert lines == ["row 3 (AYUSH): Invalid mobile number", "row 4 (HARSH): Card XXXX1111 exists"]


# --- batches ----------------------------------------------------------------------------


def _hdfc_user(db, name="manmohan", email="earweeput@catbar.net", aan="0001010370000166986", mobile="7058969506", active=True):
    user = HdfcUserId(username=name, email=email, aan=aan, mobile=mobile, active=active)
    db.add(user)
    db.commit()
    return user


class FakePortal:
    def __init__(self, result=None, error=None, users=()):
        self.result = result or hdfc_portal.BulkResult(True, "Request raised successfully")
        self.error = error
        self.users = list(users)
        self.sheets: list[bytes] = []
        self.emails: list[tuple[str, str]] = []

    async def test_login(self):
        if self.error:
            raise hdfc_portal.PortalError(self.error)
        return "signed in to the HDFC portal"

    async def bulk_create(self, sheet, file_name):
        if self.error:
            raise hdfc_portal.PortalError(self.error)
        self.sheets.append(sheet)
        return self.result

    async def list_users(self):
        return self.users

    async def set_user_email(self, username, email):
        self.emails.append((username, email))


@pytest.fixture
def portal_on(monkeypatch):
    """The portal configured, the fake in its place, and no background task."""
    settings = get_settings().model_copy(update={"hdfc_portal_user": "corpuser", "hdfc_portal_password": "pw"})
    monkeypatch.setattr(corpassist_api, "get_settings", lambda: settings)
    fake = FakePortal()
    monkeypatch.setattr(corpassist_api, "_portal", lambda: fake)
    sent: list[int] = []
    monkeypatch.setattr(corpassist_api, "_send_in_background", sent.append)
    return SimpleNamespace(portal=fake, sent=sent)


def test_a_batch_is_refused_for_a_user_without_an_aan_or_mobile(db):
    user = _hdfc_user(db, aan=None)
    with pytest.raises(hdfc_batches.BatchRefused, match="no Aan"):
        hdfc_batches.plan(db, [hdfc_batches.RowIn(user.id, 100, 1, 7)])
    with pytest.raises(hdfc_batches.BatchRefused, match="over the 1000"):
        hdfc_batches.plan(db, [hdfc_batches.RowIn(user.id, 100, 1001, 7)])


def test_a_portal_batch_is_sent_and_then_waits(db, portal_on, client, auth_headers):
    user = _hdfc_user(db)
    made = client.post(
        "/hdfc/batches",
        json={"rows": [{"user_id": user.id, "amount": 1, "count": 2, "valid_days": 1}], "send": "portal"},
        headers=auth_headers,
    )
    assert made.status_code == 201, made.text
    batch = made.json()
    assert batch["status"] == "sending" and batch["card_count"] == 2
    assert portal_on.sent == [batch["id"]]
    asyncio.run(hdfc_batches.send_to_portal(batch["id"], session_factory=SessionLocal, portal_factory=lambda: portal_on.portal))
    after = client.get(f"/hdfc/batches/{batch['id']}", headers=auth_headers).json()
    assert after["status"] == "waiting" and after["submitted_at"] is not None
    assert portal_on.portal.sheets[0].decode().count("MANMOHAN") == 2


def test_a_refused_sheet_fails_the_batch_with_the_portals_rows(db, portal_on, client, auth_headers):
    user = _hdfc_user(db)
    portal_on.portal.result = hdfc_portal.BulkResult(False, "the portal refused the sheet (Upload Failed)", ["row 2 (MANMOHAN): Invalid Aan"])
    batch = client.post("/hdfc/batches", json={"rows": [{"user_id": user.id, "amount": 5}]}, headers=auth_headers).json()
    asyncio.run(hdfc_batches.send_to_portal(batch["id"], session_factory=SessionLocal, portal_factory=lambda: portal_on.portal))
    failed = client.get(f"/hdfc/batches/{batch['id']}", headers=auth_headers).json()
    assert failed["status"] == "failed"
    assert "Upload Failed" in failed["error"] and "Invalid Aan" in failed["portal_report"]
    retried = client.post(f"/hdfc/batches/{batch['id']}/retry", headers=auth_headers).json()
    assert retried["status"] == "sending" and portal_on.sent == [batch["id"], batch["id"]]


def test_a_portal_error_fails_the_batch(db, portal_on, client, auth_headers):
    user = _hdfc_user(db)
    portal_on.portal.error = "the portal's sign-in asks for a captcha"
    batch = client.post("/hdfc/batches", json={"rows": [{"user_id": user.id, "amount": 5}]}, headers=auth_headers).json()
    asyncio.run(hdfc_batches.send_to_portal(batch["id"], session_factory=SessionLocal, portal_factory=lambda: portal_on.portal))
    assert "captcha" in client.get(f"/hdfc/batches/{batch['id']}", headers=auth_headers).json()["error"]


def test_portal_batches_need_the_portal_set_up_but_downloads_do_not(db, client, auth_headers):
    user = _hdfc_user(db)
    row = {"user_id": user.id, "amount": 100, "count": 3, "valid_days": 7}
    refused = client.post("/hdfc/batches", json={"rows": [row], "send": "portal"}, headers=auth_headers)
    assert refused.status_code == 409
    made = client.post("/hdfc/batches", json={"rows": [row], "send": "download"}, headers=auth_headers).json()
    assert made["status"] == "waiting" and made["submitted_at"] is not None
    sheet = client.get(f"/hdfc/batches/{made['id']}.csv", headers=auth_headers)
    assert sheet.status_code == 200
    assert sheet.headers["content-disposition"].endswith('"bulkVirtualCardCreation.csv"')
    assert sheet.text.count("0001010370000166986,MANMOHAN") == 3
    cancelled = client.post(f"/hdfc/batches/{made['id']}/cancel", headers=auth_headers).json()
    assert cancelled["status"] == "cancelled"
    assert client.post(f"/hdfc/batches/{made['id']}/cancel", headers=auth_headers).status_code == 409


def test_arriving_cards_are_tied_to_their_batch_and_close_it(db, monkeypatch):
    user = _hdfc_user(db)
    other = _hdfc_user(db, "ayush", "ayush@catbar.net")
    batch = hdfc_batches.create(db, [hdfc_batches.RowIn(user.id, 100, 2, 7)], send="download", started_by=None)
    old = HdfcChildCard(user_id=user.id, card_number=CARD_C, opening_amount=100, status="active",
                        issued_at=naive_utcnow() - timedelta(days=2))
    db.add(old)
    db.add(HdfcChildCard(user_id=user.id, card_number=CARD_A, opening_amount=100, status="active", issued_at=naive_utcnow()))
    db.add(HdfcChildCard(user_id=other.id, card_number=CARD_B, opening_amount=100, status="active", issued_at=naive_utcnow()))
    db.commit()
    fetched: list[list[str]] = []
    monkeypatch.setattr(hdfc_batches, "_fetch_users", lambda db_, users: fetched.append([u.username for u in users]))
    out = hdfc_batches.tick_once(db)
    assert fetched == [["manmohan"]] and out["closed"] == 0
    db.refresh(batch)
    assert batch.cards_received == 1 and batch.status == "waiting"
    assert db.get(HdfcChildCard, old.id).batch_id is None  # arrived before the batch
    db.add(HdfcChildCard(user_id=user.id, card_number="4242424242424242", opening_amount=100, status="active", issued_at=naive_utcnow()))
    db.commit()
    hdfc_batches.tick_once(db)
    db.refresh(batch)
    assert batch.cards_received == 2 and batch.status == "done"


def test_a_batch_whose_cards_never_come_fails_after_its_wait(db, monkeypatch):
    user = _hdfc_user(db)
    batch = hdfc_batches.create(db, [hdfc_batches.RowIn(user.id, 100, 1, 7)], send="download", started_by=None)
    monkeypatch.setattr(hdfc_batches, "_fetch_users", lambda db_, users: "No inbox is set up")
    hdfc_batches.tick_once(db, now=naive_utcnow() + timedelta(hours=3))
    db.refresh(batch)
    assert batch.status == "failed" and batch.error == "No inbox is set up"


def test_a_batch_left_sending_by_a_stopped_server_is_failed_not_resent(db):
    user = _hdfc_user(db)
    batch = hdfc_batches.create(db, [hdfc_batches.RowIn(user.id, 100, 1, 7)], send="portal", started_by=None)
    assert hdfc_batches.recover(db) == 1
    db.refresh(batch)
    assert batch.status == "failed" and "check Virtual Card Details" in batch.error


# --- portal endpoints -------------------------------------------------------------------


def test_portal_status_without_and_with_a_sign_in(client, auth_headers, portal_on, monkeypatch):
    off_settings = get_settings().model_copy(update={"hdfc_portal_user": "", "hdfc_portal_password": ""})
    monkeypatch.setattr(corpassist_api, "get_settings", lambda: off_settings)
    off = client.get("/hdfc/portal", headers=auth_headers).json()
    assert off["configured"] is False and off["otp_via"] is None
    on_settings = get_settings().model_copy(
        update={"hdfc_portal_user": "corpuser", "hdfc_portal_password": "pw", "hdfc_portal_otp_mobile": "9876543210"}
    )
    monkeypatch.setattr(corpassist_api, "get_settings", lambda: on_settings)
    on = client.get("/hdfc/portal", headers=auth_headers).json()
    assert on["configured"] is True and on["user_hint"] == "co…r" and on["otp_via"] == "sms"
    assert on["otp_ready"] is False and "no active Employee Phone" in on["otp_detail"]
    _add_phone(client, auth_headers)
    assert client.get("/hdfc/portal", headers=auth_headers).json()["otp_ready"] is True
    assert "pw" not in str(on)


def test_test_login_reports_the_portals_answer(client, auth_headers, portal_on):
    assert client.post("/hdfc/portal/test-login", headers=auth_headers).json() == {
        "ok": True,
        "detail": "signed in to the HDFC portal",
    }
    portal_on.portal.error = "wrong password"
    assert client.post("/hdfc/portal/test-login", headers=auth_headers).json() == {"ok": False, "detail": "wrong password"}


def test_sync_users_adds_new_ones_and_updates_known_ones(client, auth_headers, portal_on, db):
    known = _hdfc_user(db, "ayush", "old@catbar.net", mobile=None)
    portal_on.portal.users = [
        hdfc_portal.PortalUser("4574", "MANMOHAN", "earweeput@catbar.net", "7058969506", True),
        hdfc_portal.PortalUser("4535", "AYUSH", "ayush@catbar.net", "9582912283", True),
        hdfc_portal.PortalUser("4575", "HARSH", "not-an-email", None, True),
    ]
    out = client.post("/hdfc/portal/sync-users", headers=auth_headers).json()
    assert out == {"seen": 3, "added": 1, "updated": 1}
    db.expire_all()
    assert db.get(HdfcUserId, known.id).email == "ayush@catbar.net"
    assert db.get(HdfcUserId, known.id).portal_user_id == "4535"
    new = db.query(HdfcUserId).filter_by(username="manmohan").one()
    assert new.portal_user_id == "4574" and new.mobile == "7058969506"
    again = client.post("/hdfc/portal/sync-users", headers=auth_headers).json()
    assert again == {"seen": 3, "added": 0, "updated": 0}


def test_point_email_sets_the_users_email_on_the_portal(client, auth_headers, portal_on, db):
    user = _hdfc_user(db)
    assert client.post(f"/hdfc/user-ids/{user.id}/point-email", headers=auth_headers).status_code == 200
    assert portal_on.portal.emails == [("manmohan", "earweeput@catbar.net")]


def test_user_ids_keep_the_aan_and_portal_user_id(client, auth_headers):
    made = client.post(
        "/hdfc/user-ids",
        json={"username": "manmohan", "email": "e@catbar.net", "aan": "0001 0103 7000 0166 986", "portal_user_id": "4574"},
        headers=auth_headers,
    ).json()
    assert made["aan"] == "0001010370000166986" and made["portal_user_id"] == "4574"
    bad = client.patch(f"/hdfc/user-ids/{made['id']}", json={"aan": "12"}, headers=auth_headers)
    assert bad.status_code == 422
    cleared = client.patch(f"/hdfc/user-ids/{made['id']}", json={"aan": None}, headers=auth_headers).json()
    assert cleared["aan"] is None


def test_portal_table_rows_become_users():
    users = hdfc_portal.parse_users_table(
        [
            {"Virtual Card User ID": "4574", "Virtual Card Username": "MANMOHAN", "Contact Email": "EARWEEPUT@CATBAR.NET",
             "Mobile No.": "7058969506", "User Code": "-", "Status": "Active", "Action": "Edit"},
            {"Virtual Card User ID": "", "Virtual Card Username": "", "Contact Email": ""},
        ]
    )
    assert users == [hdfc_portal.PortalUser("4574", "MANMOHAN", "earweeput@catbar.net", "7058969506", True)]


# --- pairing by balance -----------------------------------------------------------------


def test_child_cards_pair_with_accounts_by_what_they_have_left(db):
    user = _hdfc_user(db)
    for number, opening, status in ((CARD_A, 500, "active"), (CARD_B, 2000, "active"), (CARD_C, 9000, "cancelled")):
        db.add(HdfcChildCard(user_id=user.id, card_number=number, opening_amount=opening, status=status))
    db.commit()
    cards = [SimpleNamespace(number=n) for n in (CARD_A, CARD_B, CARD_C)]
    accounts = [SimpleNamespace(id=1), SimpleNamespace(id=2), SimpleNamespace(id=3)]
    pairs = corpassist.pair_by_balance(db, cards, accounts)
    assert {k: v.number for k, v in pairs.items()} == {1: CARD_B, 2: CARD_A}
    assert {v.number for v in corpassist.pair_by_balance(db, cards, accounts, min_remaining=1000).values()} == {CARD_B}
    unknown = [SimpleNamespace(number=CARD_A), SimpleNamespace(number="4000056655665556")]
    assert corpassist.pair_by_balance(db, unknown, accounts) is None


def test_the_card_preview_pairs_corpassist_cards_by_balance_with_the_runs_minimum(client, auth_headers, db):
    from app.db.models import Account, AccountStatus

    user = _hdfc_user(db)
    for number, opening in ((CARD_A, 300), (CARD_B, 5000)):
        db.add(HdfcChildCard(user_id=user.id, card_number=number, opening_amount=opening, status="active"))
    accounts = [Account(email=f"a{i}@test.com", status=AccountStatus.session_valid) for i in (1, 2)]
    db.add_all(accounts)
    db.commit()
    ids = ",".join(str(a.id) for a in accounts)
    sheet = f"Card Number,Exp Month,Exp Year,CVV\n{CARD_A},08,2029,123\n{CARD_B},09,2030,456\n".encode()

    def preview(**fields):
        return client.post(
            "/jobs/order/card-preview",
            data={"account_ids": ids, **fields},
            files={"cards_file": ("cards.csv", sheet, "text/csv")},
            headers=auth_headers,
        )

    body = preview().json()
    assert body["paired_by"] == "balance" and body["paired"] == 2
    assert [row["card_last4"] for row in body["accounts"]] == [CARD_B[-4:], CARD_A[-4:]]  # most left first
    narrowed = preview(card_min_remaining="1000").json()
    assert narrowed["paired"] == 1 and narrowed["accounts"][0]["card_last4"] == CARD_B[-4:]
    refused = preview(card_min_remaining="9000")
    assert refused.status_code == 422 and "none has money left" in refused.json()["detail"]
