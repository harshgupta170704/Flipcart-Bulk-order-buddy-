"""Pydantic schemas for the alerts API (ROADMAP §2 Phase 11).

Everything here is a *response* shape: an alert is raised by a rule or by a job
that noticed something, never posted by the browser, so there is no write body
to validate. What the schemas do carry is the contract the queue UI renders
against — in particular that ``key`` travels to the client. The key is the
alert's identity ("delivery_overdue:order:412"), so a UI can show two rows for
the same order as one problem seen twice rather than two separate incidents.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import AlertSeverity


class AlertOut(BaseModel):
    """One row of the queue.

    ``created_at`` is the first sighting and ``updated_at`` the most recent, and
    they differ on purpose: an alert that has been reopened is an old problem
    that came back, which reads differently to the operator than a new one.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    key: str
    kind: str
    severity: AlertSeverity
    message: str
    account_id: int | None = None
    #: Filled by the list endpoint from one lookup over the page's account ids,
    #: so the queue names the account rather than numbering it.
    account_email: str | None = None
    order_id: int | None = None
    details: dict | None = None
    resolved: bool
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    #: The operator's reason when they closed it by hand; empty for a rule's
    #: close, and cleared when the alert reopens.
    resolution_note: str | None = None
    #: "user" or "rule" while resolved. A user's close is an acceptance: the
    #: rules leave it closed while the problem persists.
    resolved_by: str | None = None
    #: Who closed it by hand (their email), while ``resolved_by`` is "user".
    resolved_by_user: str | None = None
    #: When a rules pass last reported it; ``updated_at`` moves only on a change.
    last_seen_at: datetime | None = None


class ResolveIn(BaseModel):
    """Optional body of ``POST /alerts/{id}/resolve``: why it was closed."""

    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=1000)


class ResolveManyIn(BaseModel):
    """Body of ``POST /alerts/resolve``: the alerts to close in one go, and one
    reason for them all."""

    model_config = ConfigDict(extra="forbid")

    ids: list[int] = Field(min_length=1, max_length=1000)
    note: str | None = Field(default=None, max_length=1000)


class ResolveManyOut(BaseModel):
    """How many of the named alerts this call closed, and the queue afterwards."""

    resolved: int
    open: int


class AlertSummaryOut(BaseModel):
    """Counters for the queue header.

    The breakdowns describe **open** alerts only — see
    :func:`app.core.alerts.summary`. ``resolved`` is the lifetime count of
    closed rows, which is what makes "we cleared 40 today" visible.
    """

    open: int
    resolved: int
    by_severity: dict[str, int]
    by_kind: dict[str, int]


class EvaluateOut(BaseModel):
    """Result of a rules pass.

    ``counts`` is per rule kind (every registered kind, zeros included) and
    ``open`` is the queue total afterwards — the number the operator actually
    reacts to, which is not the sum of ``counts`` once an ad-hoc alert from a
    sync job is in the queue too.
    """

    counts: dict[str, int]
    open: int
