"""Compose runs exactly one API container (audit F-19).

Runs are tracked inside the API process: a second replica would mark the
first one's live runs failed at its start-up and let two runs of a kind start
at once. A fixed container name makes ``docker compose up --scale api=2`` fail
instead, because Docker refuses two containers of one name.
"""

from pathlib import Path

from tests import layout

COMPOSE = layout.COMPOSE


def _service(name: str) -> list[str]:
    """The lines of one service's block, comments and blank lines stripped."""
    lines: list[str] = []
    inside = False
    for raw in COMPOSE.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 2 and line.strip().endswith(":"):  # a service starts
            inside = line.strip() == f"{name}:"
            continue
        if indent < 2:  # the next top-level key ends the services
            inside = False
        if inside:
            lines.append(line.strip())
    return lines


def test_the_api_has_a_fixed_container_name_and_no_replicas():
    api = _service("api")
    assert "container_name: ordering-api" in api
    assert not [line for line in api if line.startswith(("deploy:", "replicas:", "scale:"))]


def test_the_api_gets_its_grace_period_to_finish_orders_on_a_stop():
    """The API waits up to 80 s for the runs in flight; compose must not kill
    it sooner (its own default is 10 s)."""
    assert "stop_grace_period: 90s" in _service("api")


def test_the_scaled_services_keep_no_fixed_name():
    """The browser workers are the ones meant to scale."""
    assert not [line for line in _service("worker") if line.startswith("container_name:")]
