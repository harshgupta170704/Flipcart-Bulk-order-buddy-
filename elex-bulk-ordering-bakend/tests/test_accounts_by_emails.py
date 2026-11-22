"""POST /accounts/by-emails: the Smart Bulk Order form's Upload CSV."""

from app.db.models import Account, AccountStatus


def test_emails_resolve_to_ids_in_file_order(client, auth_headers, db):
    db.add_all(
        [
            Account(email="One@test.com", status=AccountStatus.session_valid),
            Account(email="two@test.com", status=AccountStatus.new),
            Account(email="three@test.com", status=AccountStatus.session_valid),
        ]
    )
    db.commit()
    ids = {a.email.lower(): a.id for a in db.query(Account).all()}

    resp = client.post(
        "/accounts/by-emails",
        json={"emails": ["three@test.com", "one@TEST.com", "two@test.com", "ghost@test.com", "three@test.com"], "statuses": ["session_valid"]},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ids"] == [ids["three@test.com"], ids["one@test.com"]]
    assert body["not_eligible"] == ["two@test.com"]
    assert body["unmatched"] == ["ghost@test.com"]
