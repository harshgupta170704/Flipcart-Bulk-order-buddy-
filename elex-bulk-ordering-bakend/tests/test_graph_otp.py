"""The Microsoft 365 (Graph) OTP reader: thousands of addresses delivering into
one mailbox (askcaayush.com, 26 Nov 2026), each login handed only the code
sent to its own address, and many logins sharing one poll of the inbox."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx

from app.automation import graph_mail
from app.automation.graph_mail import GraphOtpReader
from app.automation.otp import RoutedOtpReader, get_otp_reader


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _mail(to: str, code: str, *, minutes_ago: float = 0.5, sender: str = "noreply@nct.flipkart.com", html=False):
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    body = (
        f"<html><style>.x{{width:123456px}}</style><p>Your OTP is <b>{code}</b></p></html>"
        if html
        else f"Your Flipkart verification code is {code}"
    )
    return {
        "subject": "Flipkart verification",
        "from": {"emailAddress": {"name": "Flipkart", "address": sender}},
        "toRecipients": [{"emailAddress": {"name": "", "address": to}}],
        "receivedDateTime": _iso(when),
        "body": {"contentType": "html" if html else "text", "content": body},
    }


class _FakeGraph:
    def __init__(self, messages: list[dict]) -> None:
        self.messages = messages
        self.token_calls = 0
        self.list_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            self.token_calls += 1
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer tok"
        assert "/users/prem@askcaayush.com/messages" in request.url.path
        self.list_calls += 1
        newest_first = sorted(self.messages, key=lambda m: m["receivedDateTime"], reverse=True)
        return httpx.Response(200, json={"value": newest_first})


def _reader(fake: _FakeGraph, **over) -> GraphOtpReader:
    graph_mail._TOKENS.clear()
    graph_mail._LISTINGS.clear()
    kwargs = dict(
        tenant_id="tenant",
        client_id="client",
        client_secret="secret",
        mailbox="prem@askcaayush.com",
        poll_interval=1,
        transport=httpx.MockTransport(fake),
    )
    kwargs.update(over)
    return GraphOtpReader(**kwargs)


def test_each_address_gets_only_its_own_code_from_the_shared_inbox():
    fake = _FakeGraph(
        [
            _mail("a1@askcaayush.com", "111111", minutes_ago=1),
            _mail("b2@askcaayush.com", "222222", minutes_ago=0.2),
            # Same local part on another domain: must not count for a1.
            _mail("xa1@askcaayush.com", "999999", minutes_ago=0.1),
        ]
    )
    reader = _reader(fake)

    async def both():
        return await asyncio.gather(
            reader.get_otp("a1@askcaayush.com", timeout=3),
            reader.get_otp("B2@askcaayush.com", timeout=3),
        )

    assert asyncio.run(both()) == ["111111", "222222"]
    # Two waiters, one poll of the mailbox and one sign-in.
    assert fake.list_calls == 1
    assert fake.token_calls == 1


def test_a_code_from_before_the_request_is_not_returned():
    fake = _FakeGraph([_mail("a1@askcaayush.com", "111111", minutes_ago=5)])
    reader = _reader(fake)
    asked = datetime.now(timezone.utc) - timedelta(minutes=1)

    assert asyncio.run(reader.get_otp("a1@askcaayush.com", timeout=1, not_before=asked)) is None


def test_naive_utc_not_before_and_html_body_work():
    fake = _FakeGraph([_mail("a1@askcaayush.com", "123987", minutes_ago=0.1, html=True)])
    reader = _reader(fake)
    asked = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)

    assert asyncio.run(reader.get_otp("a1@askcaayush.com", timeout=2, not_before=asked)) == "123987"


def test_mail_from_another_sender_is_ignored():
    fake = _FakeGraph([_mail("a1@askcaayush.com", "111111", sender="promo@shop.example")])
    fake.messages[0]["subject"] = "Your order"
    fake.messages[0]["from"]["emailAddress"]["name"] = "Shop"
    fake.messages[0]["body"]["content"] = "Your verification code is 111111"
    reader = _reader(fake)

    assert asyncio.run(reader.get_otp("a1@askcaayush.com", timeout=1)) is None


def test_a_refused_sign_in_is_no_code_not_a_crash():
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error_description": "AADSTS7000215: Invalid client secret"})

    reader = _reader(_FakeGraph([]), transport=httpx.MockTransport(refuse))

    assert asyncio.run(reader.get_otp("a1@askcaayush.com", timeout=1)) is None


def _settings(**over):
    base = dict(
        otp_imap_host="",
        otp_imap_user="",
        otp_imap_password="",
        otp_poll_interval=3,
        otp_sender_filter="flipkart",
        otp_since_days=1,
        otp_max_age_minutes=15,
        otp_m365_tenant_id="tenant",
        otp_m365_client_id="client",
        otp_m365_client_secret="secret",
        otp_m365_mailbox="prem@askcaayush.com",
        otp_m365_domains="",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_accounts_on_the_m365_domain_are_routed_to_graph():
    reader = get_otp_reader(_settings(), extras=[], instaddr_accounts=[])

    assert isinstance(reader, RoutedOtpReader)
    chosen, label = reader.reader_for("Someone@AskCaAyush.com")
    assert isinstance(chosen, GraphOtpReader)
    assert "Microsoft 365" in label
    # Another domain is not sent there.
    other, _ = reader.reader_for("x@catbar.net")
    assert not isinstance(other, GraphOtpReader)


def test_not_set_up_means_no_graph_route():
    reader = get_otp_reader(_settings(otp_m365_client_secret=""), extras=[], instaddr_accounts=[])

    assert not isinstance(reader, RoutedOtpReader)


def test_probe_reports_a_refused_mailbox_and_passes_a_readable_one():
    def forbid(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return httpx.Response(403, json={"error": {"code": "ErrorAccessDenied"}})

    refused = _reader(_FakeGraph([]), transport=httpx.MockTransport(forbid))
    problem = asyncio.run(refused.probe("a1@askcaayush.com"))
    assert problem and "403" in problem

    readable = _reader(_FakeGraph([]))
    assert asyncio.run(readable.probe("a1@askcaayush.com")) is None


def test_routed_probe_names_the_inbox_and_null_inbox_is_a_problem():
    reader = get_otp_reader(_settings(otp_m365_client_secret=""), extras=[], instaddr_accounts=[])
    assert asyncio.run(reader.probe("x@catbar.net")) == "no OTP inbox is set up for this address"


def test_swap_stops_before_the_browser_when_a_code_cannot_be_read(db, monkeypatch):
    from app.api import email_swap as api
    from app.automation.otp import OtpReader
    from app.core import session_store
    from app.db.models import Account, EmailSwap

    account = Account(email="old1@apmail.org")
    db.add(account)
    db.commit()
    session_store.save_session(db, account, [{"name": "SN", "value": "x", "domain": ".flipkart.com", "path": "/"}])
    swap = EmailSwap(old_email="old1@apmail.org", new_email="new1@askcaayush.com", status="pending")
    db.add(swap)
    db.commit()

    class HalfReader(OtpReader):
        async def probe(self, address):
            return None if address.endswith("@apmail.org") else "cannot read prem@askcaayush.com: 403"

        async def get_otp(self, address, *, timeout=60, not_before=None):
            raise AssertionError("no code should be asked for")

    def no_browser(*args, **kwargs):
        raise AssertionError("the browser must not open")

    monkeypatch.setattr(api, "get_otp_reader", lambda settings: HalfReader())
    monkeypatch.setattr(api, "browser_page", no_browser)

    asyncio.run(api._process_one(swap.id))

    db.expire_all()
    done = db.get(EmailSwap, swap.id)
    assert (done.status, done.stage) == ("failed", "otp_unreadable")
    assert "new new1@askcaayush.com" in done.message and "old " not in done.message
    assert db.get(Account, account.id).email == "old1@apmail.org"


def test_group_alias_mail_is_matched_on_the_raw_to_header():
    # Live shape (27 Nov 2026): sent to bababi506@, a proxy address of group
    # otpgroup13; Graph's toRecipients shows the GROUP, the raw To: the alias.
    def group_mail(alias: str, code: str, minutes_ago: float) -> dict:
        mail = _mail("otpgroup13@askcaayush.com", code, minutes_ago=minutes_ago)
        mail["subject"] = f"Flipkart Account - {code} is your verification code for secure access"
        mail["internetMessageHeaders"] = [
            {"name": "Received", "value": f"from (10.17.22.187) by sendera38.sendclean.email for <{alias}>"},
            {"name": "To", "value": f'"null" <{alias}>'},
        ]
        return mail

    fake = _FakeGraph(
        [
            group_mail("bababi506@askcaayush.com", "893793", 0.5),
            # Same group, another account, newer: must not be handed to bababi506.
            group_mail("babive@askcaayush.com", "111222", 0.1),
        ]
    )
    reader = _reader(fake)

    async def both():
        return await asyncio.gather(
            reader.get_otp("bababi506@askcaayush.com", timeout=2),
            reader.get_otp("babive@askcaayush.com", timeout=2),
        )

    assert asyncio.run(both()) == ["893793", "111222"]
    # The group's own address is not an account's: no code for it.
    assert asyncio.run(reader.get_otp("otpgroup13@askcaayush.com", timeout=1)) is None
