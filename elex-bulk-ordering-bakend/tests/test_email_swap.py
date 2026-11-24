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
