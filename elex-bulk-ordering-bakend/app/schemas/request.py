"""Pydantic schemas for the cancel / reschedule request log (spec FR-020, C-4).

A request is recorded for a person to action on the platform by hand; nothing
here is sent to Flipkart. ``unit_ids`` in the create body are the ledger's own
row ids (``OrderOut.id``), not the platform's unit numbers — those travel back
on each row as ``unit_id`` for display.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Ids the database can bind; a larger one is a mangled request, not a unit.
MAX_ID = 2**31 - 1
#: Units one bulk action may cover — a page of the ledger is at most 500.
MAX_UNITS = 500

RequestKind = Literal["cancel", "reschedule"]
RequestStatus = Literal["open", "done", "dropped"]


class RequestCreateIn(BaseModel):
    """``POST /requests``: the same request for every unit in the list."""

    model_config = ConfigDict(extra="forbid")

    unit_ids: list[Annotated[int, Field(ge=1, le=MAX_ID)]] = Field(min_length=1, max_length=MAX_UNITS)
    kind: RequestKind
    #: Why — required for a cancel, optional for a reschedule.
    reason: str | None = Field(default=None, max_length=2000)
    #: The wanted new delivery date — required for a reschedule.
    requested_for: date | None = None

    @model_validator(mode="after")
    def _what_each_kind_needs(self) -> "RequestCreateIn":
        reason = (self.reason or "").strip()
        self.reason = reason or None
        if self.kind == "cancel" and not reason:
            raise ValueError("a cancel request needs a reason")
        if self.kind == "reschedule" and self.requested_for is None:
            raise ValueError("a reschedule request needs the new delivery date")
        return self


class RequestEventOut(BaseModel):
    """One step of a request's life: the status it left the request at, who
    took it, when, and the note written with it (spec C-4's status log)."""

    status: str
    actor: str
    at: datetime
    note: str | None = None


class RequestOut(BaseModel):
    """One request, with enough of its unit alongside to read it on its own."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    #: The ledger row (``OrderOut.id``) the request is about.
    unit_pk: int
    kind: str
    reason: str | None = None
    requested_for: date | None = None
    status: str
    requested_by: str
    requested_at: datetime
    closed_by: str | None = None
    closed_at: datetime | None = None
    #: Written when the request was marked done or dropped.
    note: str | None = None
    #: Every step so far, oldest first: opened, marked done or dropped, opened
    #: again. ``closed_by``/``closed_at``/``note`` above say only the last.
    history: list[RequestEventOut] = Field(default_factory=list)

    # --- the unit's, from one joined query -----------------------------------
    order_row_id: int | None = None
    order_id: str | None = None
    #: The platform's unit number, when a read-back has seen it.
    unit_id: str | None = None
    #: The order's product name (``Order.model``).
    product: str | None = None
    platform_status: str | None = None
    delivery_state: str = "unknown"
    account_email: str | None = None


class RequestSkippedOut(BaseModel):
    unit_id: int
    why: str


class RequestCreateOut(BaseModel):
    """What a bulk action did: the rows it recorded, and each unit it passed over."""

    created: list[RequestOut] = Field(default_factory=list)
    skipped: list[RequestSkippedOut] = Field(default_factory=list)


class RequestPage(BaseModel):
    items: list[RequestOut] = Field(default_factory=list)
    total: int


class RequestPatchIn(BaseModel):
    """``PATCH /requests/{id}``: mark it done or dropped, or put it back to open.

    Repeating the row's current status changes nothing but the note, and the
    note only when one is written.
    """

    model_config = ConfigDict(extra="forbid")

    status: RequestStatus
    note: str | None = Field(default=None, max_length=2000)
