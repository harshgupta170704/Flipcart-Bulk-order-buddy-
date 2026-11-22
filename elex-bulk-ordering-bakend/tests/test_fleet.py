"""The agent fleet: heartbeats, online/offline/disabled, test/enable/disable
all, and where an order run may go ("Test on isolated agents")."""

from datetime import timedelta

from app.core import fleet
from app.db.models import Agent, naive_utcnow


def test_heartbeat_registers_once_and_states_follow(db):
    first = fleet.heartbeat(db, "api", "api@one")
    again = fleet.heartbeat(db, "api", "api@one")
    assert first.id == again.id
    assert fleet.state(again) == "online"

    again.last_seen_at = naive_utcnow() - timedelta(seconds=fleet.STALE_SECONDS + 5)
    db.commit()
    assert fleet.state(again) == "offline"
    again.enabled = False
    db.commit()
    assert fleet.state(again) == "disabled"


def test_summary_test_enable_disable_all(db):
    fleet.heartbeat(db, "api", "api@one")
    fleet.heartbeat(db, "worker", "worker@one:1")
    stale = Agent(name="worker@gone:2", kind="worker", enabled=True, last_seen_at=naive_utcnow() - timedelta(hours=1))
    db.add(stale)
    db.commit()

    out = fleet.summary(db)
    assert (out["total"], out["online"], out["offline"], out["disabled"]) == (3, 2, 1, 0)

    tested = {row["name"]: row["last_test_ok"] for row in fleet.test_all(db)["agents"]}
    assert tested == {"api@one": True, "worker@one:1": True, "worker@gone:2": False}

    assert fleet.set_all_enabled(db, False)["disabled"] == 3
    assert fleet.set_all_enabled(db, True)["online"] == 2


def test_order_run_placement(db):
    # Before any heartbeat, the serving process is the agent.
    assert fleet.place_order_run(db, isolated=False).reason is None
    assert fleet.place_order_run(db, isolated=True).reason

    api = fleet.heartbeat(db, "api", "api@one")
    assert fleet.place_order_run(db, isolated=False).agent.id == api.id
    assert "isolated" in fleet.place_order_run(db, isolated=True).reason

    api.isolated = True
    db.commit()
    assert fleet.place_order_run(db, isolated=True).agent.id == api.id
    assert fleet.place_order_run(db, isolated=False).reason

    api.isolated = False
    api.enabled = False
    db.commit()
    assert "disabled" in fleet.place_order_run(db, isolated=False).reason


def test_fleet_api(client, auth_headers, db):
    fleet.heartbeat(db, "api", "api@one")
    assert client.get("/agents/fleet", headers=auth_headers).json()["online"] >= 1
    assert client.post("/agents/fleet/disable-all", headers=auth_headers).json()["online"] == 0
    body = client.post("/agents/fleet/enable-all", headers=auth_headers).json()
    agent_id = next(row["id"] for row in body["agents"] if row["name"] == "api@one")
    patched = client.patch(f"/agents/fleet/{agent_id}", json={"isolated": True}, headers=auth_headers).json()
    assert next(row for row in patched["agents"] if row["id"] == agent_id)["isolated"] is True
    assert client.patch("/agents/fleet/99999", json={"enabled": True}, headers=auth_headers).status_code == 404
