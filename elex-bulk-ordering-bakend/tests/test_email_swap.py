"""Swap Email Address: CSV parsing + the upload/list/summary API."""

from app.api.email_swap import _parse_pairs


def test_parser_skips_header_and_blank_lines_and_keeps_pairs():
    text = "old email,new email\nold1@catbar.net,new1@catbar.net\n\nold2@apmail.org,new2@apmail.org\n"
    pairs, errors = _parse_pairs(text)
    assert pairs == [
        ("old1@catbar.net", "new1@catbar.net"),
        ("old2@apmail.org", "new2@apmail.org"),
    ]
    assert errors == []


def test_parser_flags_rows_that_are_not_email_pairs():
    text = "old email,new email\njust-one-column@x.com\nnotanemail,alsonot\n"
    pairs, errors = _parse_pairs(text)
    assert pairs == []
    # one row has a single column, one row has non-email values
    assert len(errors) == 2


def test_parser_lowercases_addresses():
    pairs, _ = _parse_pairs("Old@Catbar.NET,New@Catbar.NET\n")
    assert pairs == [("old@catbar.net", "new@catbar.net")]


def test_upload_queues_rows_and_summary_counts_them(client, auth_headers):
    csv = "old email,new email\na@catbar.net,b@catbar.net\nc@catbar.net,d@catbar.net\n"
    resp = client.post("/email-swap/upload", json={"csv": csv}, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] == 2
    assert body["skipped"] == 0
    batch = body["batch"]

    summary = client.get("/email-swap/summary", headers=auth_headers).json()
    assert summary["total"] >= 2
    assert summary["pending"] >= 2

    items = client.get(f"/email-swap/items?batch={batch}", headers=auth_headers).json()
    assert {r["old_email"] for r in items} == {"a@catbar.net", "c@catbar.net"}
    assert all(r["status"] == "pending" for r in items)


def test_upload_skips_a_pair_already_queued_for_the_same_old_email(client, auth_headers):
    first = client.post(
        "/email-swap/upload",
        json={"csv": "x@catbar.net,y@catbar.net\n"},
        headers=auth_headers,
    )
    assert first.json()["created"] == 1
    again = client.post(
        "/email-swap/upload",
        json={"csv": "x@catbar.net,z@catbar.net\n"},
        headers=auth_headers,
    )
    assert again.json()["created"] == 0
    assert again.json()["skipped"] == 1


def test_upload_rejects_a_csv_with_no_pairs(client, auth_headers):
    resp = client.post("/email-swap/upload", json={"csv": "old email,new email\n"}, headers=auth_headers)
    assert resp.status_code == 422


# --- Swap one account picked on the page (/check, /one) ----------------------

def _fake_reader(monkeypatch, unreadable: set[str] = frozenset()):
    from app.api import email_swap as api
    from app.automation.otp import InboxSpec, NullOtpReader, OtpReader, RoutedOtpReader

    class Reader(OtpReader):
        async def probe(self, address):
            return "403 from the inbox" if address in unreadable else None

        async def get_otp(self, address, *, timeout=60, not_before=None):
            return None

    spec = InboxSpec(0, "Microsoft 365 prem@askcaayush.com", "", "graph", 443, "all", True, ("@askcaayush.com", "@apmail.org"))
    monkeypatch.setattr(api, "get_otp_reader", lambda settings: RoutedOtpReader(NullOtpReader(), [(spec, Reader())]))


def _account_with_session(db, email):
    from app.core import session_store
    from app.db.models import Account

    account = Account(email=email)
    db.add(account)
    db.commit()
    session_store.save_session(db, account, [{"name": "SN", "value": "x", "domain": ".flipkart.com", "path": "/"}])
    return account


def test_check_says_ready_when_both_codes_are_readable(client, auth_headers, db, monkeypatch):
    _fake_reader(monkeypatch)
    account = _account_with_session(db, "pick1@apmail.org")

    body = client.get(
        "/email-swap/check",
        params={"old_email": "Pick1@apmail.org", "new_email": "bababi506@askcaayush.com"},
        headers=auth_headers,
    ).json()

    assert body["ok"] is True, body
    assert body["account_id"] == account.id and body["has_session"] is True
    assert body["new"]["readable"] is True and "Microsoft 365" in body["new"]["inbox"]


def test_check_lists_every_reason_a_swap_would_fail(client, auth_headers, db, monkeypatch):
    _fake_reader(monkeypatch, unreadable={"new9@askcaayush.com"})
    _account_with_session(db, "pick2@apmail.org")
    _account_with_session(db, "taken@askcaayush.com")

    unreadable = client.get(
        "/email-swap/check", params={"old_email": "pick2@apmail.org", "new_email": "new9@askcaayush.com"}, headers=auth_headers
    ).json()
    assert unreadable["ok"] is False
    assert any("new address cannot be read" in r for r in unreadable["reasons"])

    taken = client.get(
        "/email-swap/check", params={"old_email": "pick2@apmail.org", "new_email": "taken@askcaayush.com"}, headers=auth_headers
    ).json()
    assert taken["new_in_use"] is True and taken["ok"] is False

    # An address no inbox serves (no NullOtpReader fallback configured).
    nobody = client.get(
        "/email-swap/check", params={"old_email": "pick2@apmail.org", "new_email": "x@gmail.com"}, headers=auth_headers
    ).json()
    assert nobody["new"]["readable"] is False

    missing = client.get(
        "/email-swap/check", params={"old_email": "nobody@apmail.org", "new_email": "n@askcaayush.com"}, headers=auth_headers
    ).json()
    assert missing["account_id"] is None and missing["ok"] is False


def test_swap_one_queues_a_checked_pair_and_refuses_a_bad_one(client, auth_headers, db, monkeypatch):
    _fake_reader(monkeypatch, unreadable={"bad@askcaayush.com"})
    _account_with_session(db, "pick3@apmail.org")

    refused = client.post(
        "/email-swap/one", json={"old_email": "pick3@apmail.org", "new_email": "bad@askcaayush.com"}, headers=auth_headers
    )
    assert refused.status_code == 422
    assert "cannot be read" in refused.json()["detail"]

    queued = client.post(
        "/email-swap/one", json={"old_email": "pick3@apmail.org", "new_email": "good@askcaayush.com"}, headers=auth_headers
    )
    assert queued.status_code == 200, queued.text
    row = queued.json()
    assert (row["old_email"], row["new_email"], row["status"]) == ("pick3@apmail.org", "good@askcaayush.com", "pending")
    assert row["batch"].startswith("single-")

    again = client.post(
        "/email-swap/one", json={"old_email": "pick3@apmail.org", "new_email": "good@askcaayush.com"}, headers=auth_headers
    )
    assert again.status_code == 422 and "already queued" in again.json()["detail"]
