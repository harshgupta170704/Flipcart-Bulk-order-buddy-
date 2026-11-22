"""The mailed reset link carries its token after the '#' (audit F-29).

A fragment is the one part of an address a browser never sends: not in the
request line, so no access log on the way (nginx's, uvicorn's) keeps it, and
never in a Referer. Behind a '?' the token used to land in both.
"""

from urllib.parse import urlsplit

import pytest

from app.config import get_settings
from app.core import mailer

TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJvcHNAdGVzdC5jb20ifQ.c2lnbmF0dXJl"


@pytest.fixture
def app_url(monkeypatch):
    monkeypatch.setattr(get_settings(), "app_url", "https://orders.example.com/")


def test_the_token_rides_in_the_fragment(app_url):
    link = mailer.reset_link(TOKEN)
    assert link == f"https://orders.example.com/reset-password#token={TOKEN}"
    parts = urlsplit(link)
    # What a browser puts on the wire for this link: the path, and no query.
    assert parts.path == "/reset-password" and parts.query == ""
    assert TOKEN not in parts._replace(fragment="").geturl()
    assert parts.fragment == f"token={TOKEN}"


def test_the_mail_holds_the_fragment_link(app_url, monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(mailer, "send_mail", lambda to, subject, body: sent.append(body))
    mailer.send_password_reset("ops@test.com", TOKEN)
    assert f"https://orders.example.com/reset-password#token={TOKEN}" in sent[0].splitlines()
    assert "?token=" not in sent[0]
