"""The agent fleet: every process that does browser work, and whether it may.

An *agent* is one process that registers itself here and writes a heartbeat
every :data:`HEARTBEAT_SECONDS`:

- the API process (``kind="api"``) — every order run happens in it, because
  card data never goes through the queue (PLAN.md §5);
- each Arq queue worker (``kind="worker"``) — sign-in and coupon runs.

An agent is **online** when it is enabled and was heard from within
:data:`STALE_SECONDS`, **offline** when it was not, and **disabled** when a
person turned it off: a disabled agent takes no new work. An **isolated** agent
takes only runs started with "Test on isolated agents", and such a run takes
only isolated agents — so a test cannot disturb anyone else's work, and the
other way round.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.db.models import Agent, naive_utcnow

HEARTBEAT_SECONDS = 30
#: Not heard from for this long: offline.
STALE_SECONDS = 90


def agent_name(kind: str) -> str:
    """This process's agent name: "api@host" or "worker@host:pid"."""
    host = socket.gethostname()
    return f"api@{host}" if kind == "api" else f"worker@{host}:{os.getpid()}"


def heartbeat(db: DbSession, kind: str, name: str | None = None) -> Agent:
    """Register this process, or say it is still alive."""
    name = name or agent_name(kind)
    agent = db.scalar(select(Agent).where(Agent.name == name))
    now = naive_utcnow()
    if agent is None:
        agent = Agent(name=name, kind=kind, host=socket.gethostname(), enabled=True, isolated=False, last_seen_at=now)
        db.add(agent)
    else:
        agent.last_seen_at = now
    db.commit()
    db.refresh(agent)
    return agent


def state(agent: Agent, now=None) -> str:
    """online / offline / disabled."""
    if not agent.enabled:
        return "disabled"
    now = now or naive_utcnow()
    seen = agent.last_seen_at
    if seen is None or now - seen > timedelta(seconds=STALE_SECONDS):
        return "offline"
    return "online"


@dataclass
class Placement:
    """Where a run goes, or why it cannot."""

    agent: Agent | None
    reason: str | None = None


def place_order_run(db: DbSession, *, isolated: bool) -> Placement:
    """The API agent an order run will use. Order runs happen in the API
    process, so only an API agent can take one: it must be online, and its
    "isolated" flag must match the run's."""
    agents = db.scalars(select(Agent).where(Agent.kind == "api")).all()
    if not agents:
        # A fresh install before the first heartbeat: the process serving this
        # request is the agent, and it is not isolated.
        return Placement(None) if not isolated else Placement(
            None, "No isolated agent: mark an agent isolated on the Agents page first."
        )
    online = [agent for agent in agents if state(agent) == "online"]
    if not online:
        if all(not agent.enabled for agent in agents):
            return Placement(None, "Every order agent is disabled. Enable one on the Orders or Agents page.")
        return Placement(None, "No order agent is online.")
    matching = [agent for agent in online if bool(agent.isolated) == isolated]
    if not matching:
        return Placement(
            None,
            "No isolated agent is online for this test run."
            if isolated
            else "Every online order agent is isolated for tests. Turn off \"Test on isolated agents\" only after un-isolating one.",
        )
    local_name = agent_name("api")
    for agent in matching:
        if agent.name == local_name:
            return Placement(agent)
    return Placement(matching[0])


def summary(db: DbSession) -> dict:
    """The Orders page's "Agents Status" line and the Agents page's table."""
    now = naive_utcnow()
    agents = db.scalars(select(Agent).order_by(Agent.kind, Agent.name)).all()
    rows = [
        {
            "id": agent.id,
            "name": agent.name,
            "kind": agent.kind,
            "host": agent.host,
            "state": state(agent, now),
            "enabled": bool(agent.enabled),
            "isolated": bool(agent.isolated),
            "last_seen_at": agent.last_seen_at,
            "last_test_at": agent.last_test_at,
            "last_test_ok": agent.last_test_ok,
        }
        for agent in agents
    ]
    count = lambda name: sum(1 for row in rows if row["state"] == name)  # noqa: E731
    return {
        "agents": rows,
        "total": len(rows),
        "online": count("online"),
        "disabled": count("disabled"),
        "offline": count("offline"),
    }


def test_all(db: DbSession) -> dict:
    """Check every agent: one that answered its last heartbeat passes."""
    now = naive_utcnow()
    for agent in db.scalars(select(Agent)).all():
        agent.last_test_at = now
        agent.last_test_ok = state(agent, now) == "online"
    db.commit()
    return summary(db)


def set_all_enabled(db: DbSession, enabled: bool) -> dict:
    for agent in db.scalars(select(Agent)).all():
        agent.enabled = enabled
    db.commit()
    return summary(db)
