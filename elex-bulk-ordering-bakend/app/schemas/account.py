"""Pydantic schemas for the accounts API.

The shape that matters most here is :class:`AccountFilterIn` — the one body
shape that means "which accounts?". It is deliberately shared by bulk tagging
and by every job-start endpoint, because at 37k accounts a selection cannot be
a list of ids: the client posts the *filter* it is looking at and the server
resolves it (``app.core.account_filter``). One shape, one meaning everywhere.

``extra="forbid"`` on the request bodies is a safety feature rather than
pedantry: a filter with a mistyped field name would silently widen to "every
account", which for tagging or for starting a run is the exact accident the
whole phase exists to prevent.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.core.account_filter import AccountFilter
from app.db.models import AccountStatus
from app.schemas.address import GstRef, Phone

#: Ids the database can bind. A larger one is a mangled request, not a row,
#: and SQLite answers it with an OverflowError — a 500 — rather than a 422
#: (audit F-40); the orders API bounds its ids the same way.
MAX_ID = 2**31 - 1
RowId = Annotated[int, Field(ge=1, le=MAX_ID)]
#: The most ids one selection may list, like the id sheet (app.io.order_ids):
#: a selection past this is a filter, not a list — and a list far past it
#: would outgrow the bound values one SQL statement may carry (a 500).
MAX_LISTED_IDS = 5_000
#: The most tags one filter may name.
MAX_LISTED_TAGS = 1_000
#: Subscription names one selection may list, and the longest search text:
#: each list item and the search go into the query (audit F-40).
MAX_LISTED_SUBSCRIPTIONS = 1_000
MAX_SEARCH_CHARS = 200
#: Lengths match the ORM column widths (``Account.phone``,
#: ``Account.subscription_type``), so an over-long value is a clean 422 here
#: rather than a database error on a backend that enforces length (audit F-40).
MAX_PHONE_LENGTH = 32
MAX_SUBSCRIPTION_LENGTH = 64
#: ``Account.billing_phone``'s width, which is the ledger's ``Order.billing_phone``.
MAX_BILLING_PHONE_LENGTH = 20
#: The phone an account's orders are placed with, held to the Address Book's
#: own phone rule (app.schemas.address.Phone: stripped, then measured) and to
#: its column's narrower width. Blank clears it: see ``_blank_to_none`` below.
BillingPhone = Annotated[Phone, StringConstraints(max_length=MAX_BILLING_PHONE_LENGTH)]


class TagRef(BaseModel):
    """An account's tag, inlined into the row so the table needs no second call."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str


class AccountCreate(BaseModel):
    email: EmailStr
    phone: str | None = Field(default=None, max_length=MAX_PHONE_LENGTH)


class AccountUpdate(BaseModel):
    """PATCH body — only these six fields, and ``gst_id`` and ``billing_phone``
    by an admin only: they are setup (spec C-7; app.api.accounts.ADMIN_FIELDS).

    Every field is optional *and* nullable, so "field absent" and "field set to
    null" must stay distinguishable (null clears the tag, absent leaves it
    alone); callers read ``model_fields_set`` rather than the values.

    ``gst_id`` names an Address Book GST for the account's orders to bill
    under; null puts the account back on the run's GST. ``billing_phone`` is
    the phone its orders are placed with; null or blank puts it back on the
    account's own phone.
    """

    model_config = ConfigDict(extra="forbid")

    phone: str | None = Field(default=None, max_length=MAX_PHONE_LENGTH)
    status: AccountStatus | None = None
    subscription_type: str | None = Field(default=None, max_length=MAX_SUBSCRIPTION_LENGTH)
    tag_id: int | None = None
    gst_id: RowId | None = None
    billing_phone: BillingPhone | None = None
    is_parent: bool | None = None
    parent_email: str | None = Field(default=None, max_length=255)

    @field_validator("billing_phone", mode="after")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        """Sending "" clears the billing phone, as it clears an address's phone."""
        return value or None


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    phone: str | None
    status: AccountStatus
    subscription_type: str | None = None
    tag: TagRef | None = None
    #: The account's own GST, inlined like the tag; null means its orders
    #: bill under whatever GST the run picks.
    gst: GstRef | None = None
    #: The phone its orders are placed with; null means its own ``phone``.
    billing_phone: str | None = None
    #: Last read from Flipkart by a "Read balances" run.
    supercoins_balance: int | None = None
    gift_voucher_balance: float | None = None
    balances_read_at: datetime | None = None
    #: Parent / child accounts: a child names its parent's email.
    is_parent: bool | None = False
    parent_email: str | None = None
    created_at: datetime
    updated_at: datetime


class AccountPage(BaseModel):
    """One page of accounts plus the *full* match count.

    ``total`` is the size of the whole match, not of ``items`` — it is what the
    UI shows next to "select all matching", and what tells an operator that the
    filter they are about to run covers 412 accounts and not 37,062.
    """

    items: list[AccountOut] = Field(default_factory=list)
    total: int
    page: int
    size: int
    pages: int


#: Email domains one filter may name.
MAX_LISTED_DOMAINS = 200


class AccountFilterIn(BaseModel):
    """"Which accounts?", as posted by a client.

    Mirrors :class:`app.core.account_filter.AccountFilter` field for field;
    every field is optional and an all-default body means "every account".
    """

    model_config = ConfigDict(extra="forbid")

    q: str | None = Field(default=None, max_length=MAX_SEARCH_CHARS)
    statuses: list[AccountStatus] = Field(default_factory=list, max_length=len(AccountStatus))
    tag_ids: list[RowId] = Field(default_factory=list, max_length=MAX_LISTED_TAGS)
    include_untagged: bool = False
    subscriptions: list[str] = Field(default_factory=list, max_length=MAX_LISTED_SUBSCRIPTIONS)
    #: Bounded like the id sheet (audit F-40): a selection past this is a filter, not a list.
    ids: list[RowId] = Field(default_factory=list, max_length=MAX_LISTED_IDS)
    #: Email domains ("soniclip.in"); an account matches any of them.
    domains: list[str] = Field(default_factory=list, max_length=MAX_LISTED_DOMAINS)

    def to_filter(self) -> AccountFilter:
        """Convert to the frozen core filter the query layer takes."""
        return AccountFilter(
            q=(self.q or "").strip() or None,
            statuses=tuple(self.statuses),
            tag_ids=tuple(self.tag_ids),
            include_untagged=self.include_untagged,
            subscriptions=tuple(self.subscriptions),
            domains=tuple(d for d in (x.strip().lower() for x in self.domains) if d),
            ids=tuple(self.ids),
        )


class TagAssignRequest(BaseModel):
    """Body of ``POST /accounts/tag``.

    Exactly one of ``account_ids`` / ``filter`` is required. Accepting both, or
    neither, would make "tag everything I can see" ambiguous, and the ambiguous
    reading is the destructive one (tag all 37k), so it is a 422 instead.
    ``tag_id: null`` clears the tag.
    """

    model_config = ConfigDict(extra="forbid")

    tag_id: int | None = None
    account_ids: list[RowId] | None = Field(default=None, max_length=MAX_LISTED_IDS)
    filter: AccountFilterIn | None = None

    @model_validator(mode="after")
    def _exactly_one_target(self) -> "TagAssignRequest":
        if (self.account_ids is None) == (self.filter is None):
            raise ValueError("give exactly one of account_ids or filter")
        return self


class TagAssignResult(BaseModel):
    updated: int


class GstAssignRequest(BaseModel):
    """Body of ``POST /accounts/gst``: one GST for a selection of accounts.

    The same two ways to say "which accounts?" as bulk tagging, under the same
    rule — exactly one of ``account_ids`` / ``filter`` — and the same bounds: a
    list of more than MAX_LISTED_IDS ids is a 422 (send the filter instead).
    ``gst_id: null`` takes the accounts' own GST off, so they bill under the
    run's GST again.
    """

    model_config = ConfigDict(extra="forbid")

    gst_id: RowId | None = None
    account_ids: list[RowId] | None = Field(default=None, max_length=MAX_LISTED_IDS)
    filter: AccountFilterIn | None = None

    @model_validator(mode="after")
    def _exactly_one_target(self) -> "GstAssignRequest":
        if (self.account_ids is None) == (self.filter is None):
            raise ValueError("give exactly one of account_ids or filter")
        return self


class GstAssignResult(BaseModel):
    updated: int


class DomainOption(BaseModel):
    """An email domain present in the data, with how many accounts use it."""

    value: str
    count: int


class StatusOption(BaseModel):
    """A status that actually occurs in the data, with how many rows have it."""

    value: AccountStatus
    count: int


class TagOption(BaseModel):
    id: int
    name: str
    count: int


class FilterOptionsOut(BaseModel):
    """Everything the filter UI needs to build itself from real data."""

    statuses: list[StatusOption] = Field(default_factory=list)
    subscriptions: list[str] = Field(default_factory=list)
    tags: list[TagOption] = Field(default_factory=list)
    total: int = 0
    untagged: int = 0
    #: Email domains present, with how many accounts each.
    domains: list[DomainOption] = Field(default_factory=list)


class ImportRejectedRow(BaseModel):
    """A workbook row the import refused, and why (audit F-26)."""

    row: int
    reason: str


class ImportResult(BaseModel):
    created: int
    updated: int
    skipped: int
    #: The rows refused, each with its reason — the answer used to drop them,
    #: so a row with no email address was simply missing from every count.
    rejected: list[ImportRejectedRow] = Field(default_factory=list)
