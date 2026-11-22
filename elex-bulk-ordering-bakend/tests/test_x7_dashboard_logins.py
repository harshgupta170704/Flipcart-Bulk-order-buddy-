"""Step G: login health on the dashboard (GET /stats/logins)."""

from app.db.models import Account, AccountStatus


def test_login_health_counts_every_status_and_lists_empty_ones_at_zero(db, client, auth_headers):
    for n, status in enumerate(
        [AccountStatus.session_valid, AccountStatus.session_valid, AccountStatus.logged_out, AccountStatus.blocked]
    ):
        db.add(Account(email=f"l{n}@t.in", status=status))
    db.commit()

    body = client.get("/stats/logins", headers=auth_headers).json()

    assert body["total"] == 4 and body["logged_in"] == 2
    counts = {row["status"]: row["count"] for row in body["statuses"]}
    assert counts["session_valid"] == 2 and counts["logged_out"] == 1 and counts["blocked"] == 1
    assert counts["not_registered"] == 0
    assert body["statuses"][0]["status"] == "session_valid"
    assert len(body["statuses"]) == len(AccountStatus)


def test_login_health_needs_a_sign_in(client):
    assert client.get("/stats/logins").status_code == 401
