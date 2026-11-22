"""Tests for the accounts API and health endpoints.

/accounts became token-protected when app authentication landed in Phase 4, so
these tests authenticate via the `auth_headers` fixture.
"""

import io

from openpyxl import Workbook, load_workbook

from app.io import excel

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _legacy_upload(rows) -> bytes:
    wb = Workbook()
    sheet = wb.active
    sheet.cell(row=1, column=excel.COL_EMAIL, value="Email")
    for i, (email, phone) in enumerate(rows):
        r = excel.FIRST_DATA_ROW + i
        sheet.cell(row=r, column=excel.COL_EMAIL, value=email)
        sheet.cell(row=r, column=excel.COL_PHONE, value=phone)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --- public endpoints --------------------------------------------------------
def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
    root = client.get("/").json()
    assert root["status"] == "ok"


# --- accounts now require a token -------------------------------------------
def test_accounts_requires_auth(client):
    for resp in (
        client.get("/accounts"),
        client.post("/accounts", json={"email": "x@test.com"}),
        client.get("/accounts/export"),
    ):
        assert resp.status_code in (401, 403), resp.status_code


def test_list_empty(client, auth_headers):
    resp = client.get("/accounts", headers=auth_headers)
    assert resp.status_code == 200
    # Paged envelope since Phase 7 — the list never comes back bare, because at
    # 37k accounts a bare list is a multi-megabyte response.
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 0


def test_create_and_list(client, auth_headers):
    resp = client.post(
        "/accounts", json={"email": "x@test.com", "phone": "123"}, headers=auth_headers
    )
    assert resp.status_code == 201
    assert resp.json()["email"] == "x@test.com"

    # duplicate -> 409
    dup = client.post("/accounts", json={"email": "x@test.com"}, headers=auth_headers)
    assert dup.status_code == 409

    listed = client.get("/accounts", headers=auth_headers).json()
    assert listed["total"] == 1
    assert len(listed["items"]) == 1


def test_import_then_export(client, auth_headers):
    payload = _legacy_upload([("a@test.com", "1"), ("b@test.com", "2")])
    files = {"file": ("inputs.xlsx", payload, _XLSX_MIME)}
    resp = client.post("/accounts/import", files=files, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["created"] == 2

    assert client.get("/accounts", headers=auth_headers).json()["total"] == 2

    export = client.get("/accounts/export", headers=auth_headers)
    assert export.status_code == 200
    assert export.headers["content-type"] == _XLSX_MIME
    wb = load_workbook(io.BytesIO(export.content))
    sheet = wb.active
    assert sheet.cell(row=excel.FIRST_DATA_ROW, column=excel.COL_EMAIL).value == "a@test.com"


def test_import_rejects_non_excel(client, auth_headers):
    files = {"file": ("notes.txt", b"hello", "text/plain")}
    resp = client.post("/accounts/import", files=files, headers=auth_headers)
    assert resp.status_code == 400
