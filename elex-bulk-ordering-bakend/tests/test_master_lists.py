"""Gift Vouchers and Order Phone Numbers (Platform Accounts & Configs)."""

from app.db.models import Account, AccountStatus, GiftVoucher


def test_gift_voucher_register_keeps_only_the_last_four(client, auth_headers, db):
    db.add(Account(email="gv@test.com", status=AccountStatus.new))
    db.commit()
    resp = client.post(
        "/gift-vouchers",
        json={"label": "Diwali GV", "code": "ABCD 1234 5678 9XYZ", "value": 5000, "account_email": "GV@test.com"},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["code_last4"] == "9XYZ" and body["balance"] == 5000 and body["account_email"] == "gv@test.com"
    stored = db.get(GiftVoucher, body["id"])
    assert "ABCD" not in (stored.code_last4 or "")

    patched = client.patch(f"/gift-vouchers/{body['id']}", json={"balance": 1200, "account_email": None}, headers=auth_headers)
    assert patched.json()["balance"] == 1200 and patched.json()["account_id"] is None
    assert client.post("/gift-vouchers", json={"label": "x", "value": 1, "account_email": "nobody@x"}, headers=auth_headers).status_code == 422
    assert client.delete(f"/gift-vouchers/{body['id']}", headers=auth_headers).status_code == 204
    assert client.get("/gift-vouchers", headers=auth_headers).json() == []


def test_order_phone_numbers(client, auth_headers):
    first = client.post("/order-phones", json={"number": "+91 98765 43210", "label": "Ops 1"}, headers=auth_headers)
    assert first.status_code == 201 and first.json()["number"] == "9876543210"
    assert client.post("/order-phones", json={"number": "9876543210"}, headers=auth_headers).status_code == 409
    assert client.post("/order-phones", json={"number": "12345"}, headers=auth_headers).status_code == 422
    off = client.patch(f"/order-phones/{first.json()['id']}", json={"active": False}, headers=auth_headers)
    assert off.json()["active"] is False
    assert [p["number"] for p in client.get("/order-phones", headers=auth_headers).json()] == ["9876543210"]


def test_csv_exports_are_logged_for_the_downloads_tab(client, auth_headers):
    resp = client.get("/orders/export.csv?q=vivo", headers=auth_headers)
    assert resp.status_code == 200
    logged = client.get("/orders/exports", headers=auth_headers).json()
    assert logged[0]["kind"] == "order_units" and logged[0]["query"] == "q=vivo"
    assert logged[0]["file_name"].endswith(".csv")


def test_order_references_are_filled_in_and_cleared(client, auth_headers, db):
    from app.db.models import Order, OrderStatus

    account = db.query(Account).first() or Account(email="ref@test.com", status=AccountStatus.new)
    db.add(account)
    db.commit()
    order = Order(account_id=account.id, status=OrderStatus.placed, order_id="OD1")
    db.add(order)
    db.commit()
    body = client.patch(
        f"/orders/{order.id}/references",
        json={"po_number": " PO-9 ", "ewb_number": "1234567890"},
        headers=auth_headers,
    ).json()
    assert body["po_number"] == "PO-9" and body["ewb_number"] == "1234567890"
    cleared = client.patch(f"/orders/{order.id}/references", json={"po_number": ""}, headers=auth_headers).json()
    assert cleared["po_number"] is None and cleared["ewb_number"] == "1234567890"
