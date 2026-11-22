"""Tests for OTP extraction and reader selection."""

from types import SimpleNamespace

from app.automation.otp import (
    ImapOtpReader,
    NullOtpReader,
    get_otp_reader,
    otp_from_text,
)


def test_otp_from_text():
    assert otp_from_text("Your Flipkart OTP is 123456. Do not share.") == "123456"
    assert otp_from_text("Code: 987654") == "987654"
    assert otp_from_text("no digits here") is None
    assert otp_from_text("") is None
    assert otp_from_text(None) is None


def _settings(**over):
    base = dict(
        otp_imap_host="",
        otp_imap_port=993,
        otp_imap_user="",
        otp_imap_password="",
        otp_imap_mailbox="INBOX",
        otp_imap_use_ssl=True,
        otp_poll_interval=3,
        otp_sender_filter="flipkart",
        otp_since_days=1,
        otp_max_age_minutes=15,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_get_otp_reader_null_when_unconfigured():
    reader = get_otp_reader(_settings())
    assert isinstance(reader, NullOtpReader)


def test_get_otp_reader_imap_when_configured():
    reader = get_otp_reader(
        _settings(otp_imap_host="imap.example.com", otp_imap_user="catchall@x")
    )
    assert isinstance(reader, ImapOtpReader)


# --- HTML-only mail (found live 2026-10-23) ----------------------------------
def _msg(parts):
    """Build a message: parts is a list of (content_type, body)."""
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = "Your Flipkart verification"
    msg["To"] = "a@test.com"
    first_type, first_body = parts[0]
    msg.set_content(first_body, subtype=first_type.split("/")[1])
    for content_type, body in parts[1:]:
        msg.add_alternative(body, subtype=content_type.split("/")[1])
    return msg


def test_code_is_found_in_an_html_only_email():
    """The bug: only text/plain was read.

    Transactional mail like Flipkart's OTP is routinely HTML-only, so the
    reader saw nothing but the subject, reported "no OTP", and the login hung
    with the six boxes empty — looking like a browser problem.
    """
    from app.automation.otp import _message_text

    html = "<html><body><p>Your OTP is <b>483910</b></p></body></html>"
    msg = _msg([("text/html", html)])
    assert otp_from_text(_message_text(msg)) == "483910"


def test_digits_hidden_in_style_or_script_are_ignored():
    """A six-digit number in CSS must not be mistaken for the code."""
    from app.automation.otp import _message_text

    html = (
        "<html><head><style>.x{width:123456px}</style></head>"
        "<body>Your code: 778899</body></html>"
    )
    msg = _msg([("text/html", html)])
    assert otp_from_text(_message_text(msg)) == "778899"


def test_plain_text_still_works():
    from app.automation.otp import _message_text

    msg = _msg([("text/plain", "Your OTP is 112233, valid for 10 minutes.")])
    assert otp_from_text(_message_text(msg)) == "112233"


def test_html_entities_around_the_code_do_not_break_it():
    from app.automation.otp import _message_text

    html = "<div>OTP:&nbsp;<span>554433</span></div>"
    msg = _msg([("text/html", html)])
    assert otp_from_text(_message_text(msg)) == "554433"


# --- wrong-code protection (found live 2026-10-23) ---------------------------
# A real personal inbox matched 9,575 messages on "addressed to me", and the
# reader returned 128279 from a recruitment email as though it were the OTP.
RECRUITER = (
    "Capgemini Face-to-Face Interview Drive | Java Full Stack Developer "
    "Walk-in on 128279 Park Avenue, contact 9876543210. Ref 445566 applies."
)


def test_a_random_six_digit_number_is_not_treated_as_a_code():
    """require_context is the guard: no OTP wording, no code."""
    assert otp_from_text(RECRUITER, require_context=True) is None


def test_the_old_behaviour_would_have_returned_the_wrong_number():
    """Pins why require_context exists — without it, this is what happened."""
    assert otp_from_text(RECRUITER, require_context=False) == "128279"


def test_code_is_taken_from_otp_wording_not_the_first_number():
    text = "Order 998877 shipped. Your OTP is 483910. Ref 112233."
    assert otp_from_text(text, require_context=True) == "483910"


def test_code_before_the_wording_is_found_too():
    assert otp_from_text("483910 is your OTP", require_context=True) == "483910"
    assert otp_from_text("112233 is the code", require_context=True) == "112233"


def test_various_otp_phrasings():
    for text, expected in [
        ("Your verification code: 111111", "111111"),
        ("One time password 222222 for login", "222222"),
        ("Use security code 333333", "333333"),
        ("Your login code is 444444", "444444"),
        ("passcode 555555", "555555"),
    ]:
        assert otp_from_text(text, require_context=True) == expected, text


def test_stale_mail_is_ignored():
    """A code from an hour ago is not the one we just requested."""
    from datetime import datetime, timedelta, timezone
    from email.message import EmailMessage
    from email.utils import format_datetime

    from app.automation.otp import ImapOtpReader

    reader = ImapOtpReader("h", 993, "u", "p", max_age_minutes=15)

    fresh = EmailMessage()
    fresh["Date"] = format_datetime(datetime.now(timezone.utc))
    assert reader._too_old(fresh) is False

    stale = EmailMessage()
    stale["Date"] = format_datetime(datetime.now(timezone.utc) - timedelta(hours=1))
    assert reader._too_old(stale) is True

    undated = EmailMessage()
    assert reader._too_old(undated) is False  # cannot tell; let context decide


def test_a_code_sent_before_we_asked_is_ignored():
    """The mailbox already held three recent Flipkart codes.

    Polling starts the instant Continue is clicked, before the new mail
    arrives, so without this the newest OLD code gets typed and rejected —
    which looks like the boxes failing rather than a stale code.
    """
    from datetime import datetime, timedelta, timezone
    from email.message import EmailMessage
    from email.utils import format_datetime

    from app.automation.otp import _parsed_date

    now = datetime.now(timezone.utc)
    asked_at = now - timedelta(minutes=1)

    older = EmailMessage()
    older["Date"] = format_datetime(now - timedelta(minutes=9))
    assert _parsed_date(older) < asked_at  # would be skipped

    newer = EmailMessage()
    newer["Date"] = format_datetime(now)
    assert _parsed_date(newer) > asked_at  # accepted


def test_forwarded_email_matches_headers_forwarders_and_wrapped_body():
    """Forwarded accounts landing in one central Gmail inbox must match via
    forwarding headers, forwarder From/Reply-To headers, or wrapped body —
    without ever matching a similar address or unrelated sender."""
    from datetime import datetime, timezone
    from email.message import EmailMessage
    from email.utils import format_datetime

    from app.automation.otp import ImapOtpReader

    now_hdr = format_datetime(datetime.now(timezone.utc))

    def build_mail(*, sender="noreply@flipkart.com", to="single@gmail.com", extra=None, subject="Flipkart OTP", body="Your Flipkart OTP is 654321."):
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = to
        msg["Date"] = now_hdr
        msg["Subject"] = subject
        for k, v in (extra or {}).items():
            msg[k] = v
        msg.set_content(body)
        return msg

    cases = [
        ("X-Forwarded-To", build_mail(extra={"X-Forwarded-To": "earweeput@catbar.net"})),
        ("X-Forwarded-For", build_mail(extra={"X-Forwarded-For": "earweeput@catbar.net"})),
        ("Envelope-To", build_mail(extra={"Envelope-To": "earweeput@catbar.net"})),
        ("Reply-To", build_mail(sender="noreply@erinn.biz", extra={"Reply-To": "earweeput@catbar.net"})),
        (
            "Wrapped body",
            build_mail(
                sender="forwarder@kuku.lu",
                subject="Fwd: Flipkart Login",
                body="--- Forwarded message ---\nTo: earweeput@catbar.net\nYour Flipkart OTP is 654321.",
            ),
        ),
    ]

    for label, msg in cases:
        raw = msg.as_bytes()
        queries = []

        class FakeConn:
            def select(self, mailbox, readonly=True):
                return ("OK", [b"1"]) if mailbox == "INBOX" else ("NO", [])

            def search(self, charset, *criteria):
                queries.append(criteria)
                joined = " ".join(criteria)
                for h in ("X-Forwarded-To", "X-Forwarded-For", "Envelope-To", "Reply-To"):
                    if h in joined and msg.get(h):
                        return ("OK", [b"1"])
                if "TEXT" in joined and "FROM" not in joined:
                    return ("OK", [b"1"])
                return ("OK", [b""])

            def fetch(self, msg_id, what):
                return ("OK", [(b"1", raw)])

            def logout(self):
                return ("BYE", [])

        reader = ImapOtpReader("imap.gmail.com", 993, "single@gmail.com", "pw")
        reader._connect = lambda: FakeConn()
        assert reader._fetch_code("earweeput@catbar.net") == "654321", label
        assert reader._fetch_code("other@catbar.net") is None, label
        # Substring lookalike must never borrow the OTP even if IMAP TEXT returned it.
        assert reader._fetch_code("weeput@catbar.net") is None, label

    # An unrelated non-Flipkart email forwarded for the same address must be ignored.
    unrelated = build_mail(
        sender="alerts@example.com",
        subject="Security alert",
        body="To: earweeput@catbar.net — Your passcode is 999888.",
        extra={"X-Forwarded-To": "earweeput@catbar.net"},
    ).as_bytes()

    class UnrelatedConn:
        def select(self, mailbox, readonly=True):
            return ("OK", [b"1"]) if mailbox == "INBOX" else ("NO", [])

        def search(self, charset, *criteria):
            return ("OK", [b"1"]) if "X-Forwarded-To" in " ".join(criteria) else ("OK", [b""])

        def fetch(self, msg_id, what):
            return ("OK", [(b"1", unrelated)])

        def logout(self):
            return ("BYE", [])

    reader = ImapOtpReader("imap.gmail.com", 993, "single@gmail.com", "pw")
    reader._connect = lambda: UnrelatedConn()
    assert reader._fetch_code("earweeput@catbar.net") is None
