"""Speed is measured, not asserted: every response says how long it took."""

from fastapi.testclient import TestClient

from app.main import SLOW_REQUEST_MS, create_app


def test_every_response_carries_its_timing(db):
    with TestClient(create_app()) as client:
        response = client.get("/health")
    assert response.status_code == 200
    timing = response.headers["Server-Timing"]
    assert timing.startswith("app;dur=")
    assert float(timing.split("=", 1)[1]) >= 0


def test_a_slow_request_is_named_in_the_log(db, caplog, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "SLOW_REQUEST_MS", 0.0)  # everything is slow now
    with TestClient(create_app()) as client:
        client.get("/health")
    assert any("slow request: GET /health" in r.getMessage() for r in caplog.records)
    assert SLOW_REQUEST_MS == 1000.0  # the real threshold is untouched
