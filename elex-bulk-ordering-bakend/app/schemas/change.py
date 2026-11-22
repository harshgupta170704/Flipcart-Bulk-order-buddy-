"""Pydantic schemas for the change feed: what the read-backs found since the
signed-in user last looked (app.api.changes, app.core.unit_events).

Every stamp here is UTC without an offset, like the rest of the API.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ChangeOut(BaseModel):
    """One change, with enough of its unit and order to read it on its own."""

    id: int
    #: When the read-back saw it.
    at: datetime
    #: delivered, out_for_delivery, cancelled, returned or failed.
    kind: str
    #: The unit's reading before (None for its first reading) and after.
    from_state: str | None = None
    to_state: str
    #: The ledger row (``OrderOut.id``) the change is about.
    unit_pk: int
    #: Its place in the order, and the platform's unit number once read back.
    unit_position: int | None = None
    unit_id: str | None = None
    #: The order row (``OrderOut.order_row_id``) and the platform's order id —
    #: what the dashboard links to the ledger with.
    order_row_id: int
    order_id: str | None = None
    model: str | None = None
    color: str | None = None
    account_id: int | None = None
    account_email: str | None = None


class ChangesPage(BaseModel):
    """One page of the feed, newest first, and what the whole window holds."""

    items: list[ChangeOut] = Field(default_factory=list)
    #: Every change in the window, across all pages.
    total: int
    page: int
    size: int
    pages: int
    #: The window's changes by kind, every kind of the feed listed (zeros included).
    by_kind: dict[str, int] = Field(default_factory=dict)
    #: Where the window starts: the caller's seen mark, else 7 days back —
    #: never more than 30 days back.
    since: datetime
    #: The caller's own mark; None until they first mark the feed as seen.
    seen_at: datetime | None = None
    #: True when the 30-day bound cut the window short.
    capped: bool = False
    #: The server's clock when this was read. "Mark all as seen" sends it back,
    #: so a change that arrives while the page is open stays unseen.
    as_of: datetime


class ChangesSeenIn(BaseModel):
    """Optional body of ``POST /changes/seen``: seen up to when.

    Absent means now. The dashboard sends the ``as_of`` of the page it showed,
    so a change recorded after that page loaded is not marked seen unread.
    """

    model_config = ConfigDict(extra="forbid")

    through: datetime | None = None


class ChangesSeenOut(BaseModel):
    seen_at: datetime
