"""Tests for DB-backed HDFC/runtime settings, Employee Phones bulk/online toggle,
Product Screener watchlist, Inventory, and Job Tasks (BOB parity)."""

from __future__ import annotations

from app.db.models import Account, Job, JobItem, JobItemStatus, JobStatus, JobType, Order, OrderStatus


def test_hdfc_portal_settings_stored_in_db_and_never_echoed(client, auth_headers):
    r = client.put(
        "/settings",
        headers=auth_headers,
        json={
            "hdfc_portal_url": "https://corpcardassist.hdfc.bank.in",
            "hdfc_portal_user": "CORPUSER01",
            "hdfc_portal_password": "super-secret-password",
            "hdfc_portal_otp_mobile": "9876543210",
            "hdfc_auto_fetch_minutes": 15,
            "hdfc_batch_wait_minutes": 45,
            "hdfc_since_days": 20,
            "employee_phones_assume_online": True,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["hdfc_portal_user"] == "CORPUSER01"
    assert body["hdfc_portal_password_set"] is True
    assert "hdfc_portal_password" not in body
    assert body["hdfc_portal_otp_mobile"] == "9876543210"
    assert body["hdfc_auto_fetch_minutes"] == 15
    assert body["employee_phones_assume_online"] is True

    # Portal status reads directly from DB-backed settings (no .env required)
    portal = client.get("/hdfc/portal", headers=auth_headers)
    assert portal.status_code == 200
    pdata = portal.json()
    assert pdata["configured"] is True
    assert pdata["user_hint"] == "CO…1"
    assert pdata["auto_fetch_minutes"] == 15


def test_employee_phones_assume_online_and_bulk_csv(client, auth_headers):
    res = client.post(
        "/otp-phones/bulk",
        headers=auth_headers,
        json={
            "corporate_id": "CORP-99",
            "csv_text": "employee_id,phone_number,label\nEMP01,9876543211,Alice\nEMP02,9876543212,Bob\n",
        },
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert len(data["added"]) == 2
    assert data["updated"] == 0
    assert data["skipped"] == []
    assert data["added"][0]["corporate_id"] == "CORP-99"
    assert data["added"][0]["token"]

    # Initially offline (no ping yet)
    listed = client.get("/otp-phones", headers=auth_headers).json()
    assert all(p["online"] is False for p in listed)

    # Toggle "Assume all numbers online" in DB
    t = client.put("/otp-phones/settings", headers=auth_headers, json={"assume_online": True})
    assert t.status_code == 200
    assert t.json()["assume_online"] is True

    listed2 = client.get("/otp-phones", headers=auth_headers).json()
    assert all(p["online"] is True for p in listed2)


def test_screener_watchlist_config_and_crud(client, auth_headers):
    cfg = client.put(
        "/screener/config",
        headers=auth_headers,
        json={
            "master_email": "master@example.com",
            "banks": ["HDFC", "ICICI"],
            "check_every_minutes": 10,
            "supercoin_offers": True,
            "coupon_offers": False,
            "enabled": True,
        },
    )
    assert cfg.status_code == 200, cfg.text
    assert cfg.json()["banks"] == ["HDFC", "ICICI"]
    assert cfg.json()["enabled"] is True

    add = client.post(
        "/screener/items",
        headers=auth_headers,
        json={
            "product_url": "https://www.flipkart.com/sample-phone/p/itm1234567890",
            "product_title": "Sample Phone 5G",
            "target_price": 14999,
        },
    )
    assert add.status_code == 201, add.text
    item_id = add.json()["id"]

    # Duplicate URL refused
    dup = client.post(
        "/screener/items",
        headers=auth_headers,
        json={"product_url": "https://www.flipkart.com/sample-phone/p/itm1234567890"},
    )
    assert dup.status_code == 409

    patched = client.patch(f"/screener/items/{item_id}", headers=auth_headers, json={"target_price": 13999})
    assert patched.status_code == 200
    assert patched.json()["target_price"] == 13999

    assert len(client.get("/screener/items", headers=auth_headers).json()) == 1
    assert client.delete(f"/screener/items/{item_id}", headers=auth_headers).status_code == 204
    assert client.get("/screener/items", headers=auth_headers).json() == []


def test_inventory_stock_batches_transfers_and_cod_cards(client, auth_headers, db):
    acct = Account(email="inv-test@example.com")
    db.add(acct)
    db.flush()
    db.add(
        Order(
            account_id=acct.id,
            order_id="OD9990001",
            model="Widget Pro",
            qty=5,
            status=OrderStatus.placed,
            delivery_state="delivered",
            payment_mode="cod",
        )
    )
    db.commit()

    b = client.post(
        "/inventory/batches",
        headers=auth_headers,
        json={
            "name": "Batch Nov-22",
            "platform": "flipkart",
            "product_name": "Widget Pro",
            "opening_qty": 2,
            "received_qty": 6,
            "approved_qty": 6,
        },
    )
    assert b.status_code == 201, b.text

    tr = client.post(
        "/inventory/transfers",
        headers=auth_headers,
        json={
            "reference": "TN-001",
            "platform": "flipkart",
            "product_name": "Widget Pro",
            "quantity": 4,
            "destination": "Main Warehouse",
        },
    )
    assert tr.status_code == 201, tr.text

    stock = client.get("/inventory/stock?tab=all", headers=auth_headers).json()
    assert len(stock) == 1
    row = stock[0]
    assert row["opening_qty"] == 2
    assert row["delivered_qty"] == 5
    assert row["expected_qty"] == 7
    assert row["received_qty"] == 6
    assert row["approved_qty"] == 6
    assert row["transferred_qty"] == 4
    assert row["mismatch_qty"] == 1
    assert row["to_be_transferred_qty"] == 2

    csv_resp = client.get("/inventory/mismatch-report.csv", headers=auth_headers)
    assert csv_resp.status_code == 200
    assert "Widget Pro" in csv_resp.text

    cod = client.post(
        "/inventory/cod-cards",
        headers=auth_headers,
        json={"csv_text": "order_id,card_number\nOD9990001,4111-2222-3333-4444\n"},
    )
    assert cod.status_code == 200
    assert cod.json()["updated"] == 1
    db.expire_all()
    order = db.query(Order).filter(Order.order_id == "OD9990001").one()
    assert order.card_number == "4111222233334444"


def test_job_tasks_endpoint(client, auth_headers, db):
    acct = Account(email="task-test@example.com")
    db.add(acct)
    db.flush()
    job = Job(type=JobType.order, status=JobStatus.completed, payload={"mode": "ai", "started_by": "admin"})
    db.add(job)
    db.flush()
    db.add(JobItem(job_id=job.id, account_id=acct.id, status=JobItemStatus.done, message="Placed OD123"))
    db.commit()

    resp = client.get("/jobs/tasks?email=task-test", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["email"] == "task-test@example.com"
    assert body["items"][0]["source"] == "ai"
    assert body["items"][0]["triggered_by"] == "admin"
