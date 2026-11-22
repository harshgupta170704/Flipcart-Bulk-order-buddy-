"""Pydantic schemas for the jobs API and the live-progress WebSocket.

``JobItemOut`` carries the account *email* as well as its id because the UI
renders a per-account progress table and should not have to join accounts
client-side. Email lives on the related ``Account`` row, so it is filled by
:meth:`JobItemOut.from_item` rather than plain ``from_attributes`` mapping.

``Job.payload`` is echoed back as-is: it holds **non-sensitive run parameters
only** (coupon code, product link, qty, target, plus the account selection that
started the run). Card details never reach it (PLAN.md §5), so exposing it to
the client is safe by construction.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.account_filter import AccountFilter
from app.db.models import (
    AccountStatus,
    Job,
    JobItem,
    JobItemStatus,
    JobStatus,
    JobType,
    item_seconds,
)

# The accounts API's bounds on a selection, so a selection the Accounts page
# can tag can also run, and one it refuses is refused here too (audit F-40).
from app.schemas.account import (
    MAX_LISTED_IDS,
    MAX_LISTED_SUBSCRIPTIONS,
    MAX_LISTED_TAGS,
    MAX_SEARCH_CHARS,
    RowId,
)


class CardPairingOut(BaseModel):
    """One selected account, and the card that would pay for it.

    The card is named by its last four digits and nothing else; whether its
    row carries the bank's password is a yes or a no, never the password."""

    account_id: int
    email: str
    #: None: no card in the sheet pays for this account, and the run skips it.
    card_last4: str | None = None
    has_password: bool | None = None


class CardPreviewOut(BaseModel):
    """POST /jobs/order/card-preview: the pairing a card run would use."""

    accounts: list[CardPairingOut]
    #: Accounts a card pays for, and accounts the run will skip for want of one.
    paired: int
    without_card: int
    #: "email" when the sheet has an email column, else "position" (one account only).
    paired_by: str
    #: The sheet's rows that could not be a card: ``{"row", "reason"}``, by row
    #: number and field — never a value.
    rows_set_aside: list[dict] = Field(default_factory=list)


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    type: JobType
    status: JobStatus
    progress: int
    total_items: int
    done_items: int
    failed_items: int
    skipped_items: int
    #: Orders sent but never confirmed — may exist; check before re-running.
    unconfirmed_items: int = 0
    cancel_requested: bool
    payload: dict | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class BulkOrderRunOut(JobOut):
    """One order run as the Orders page lists it: the run's own record plus
    what its orders add up to. Non-sensitive by construction — every field is
    already on ``Job.payload`` or is a count over the run's ``Order`` rows."""

    platform: str = "flipkart"
    #: "real", "mock" or "ai".
    mode: str = "real"
    product_link: str | None = None
    product_name: str | None = None
    total_quantity: int | None = None
    qty_per_order: int | None = None
    orders_placed: int = 0
    units_placed: int = 0
    orders_unconfirmed: int = 0
    #: Orders that did not go through (failed, over the cap, never sent).
    orders_failed: int = 0
    amount_placed: float = 0.0
    #: The card the run paid with, masked: "54XX…824803".
    parent_card: str | None = None
    execution_version: str | None = None
    #: The run used Flipkart Minutes (the "Minutes" tag on the Orders page).
    flipkart_minutes: bool = False
    payment_mode: str | None = None
    card_type: str | None = None
    ref_code: str | None = None
    started_by: str | None = None
    address_label: str | None = None
    gst_label: str | None = None
    target: float | None = None


class JobItemOut(BaseModel):
    """Per-account progress row within a job."""

    model_config = ConfigDict(from_attributes=True)

    account_id: int
    email: str | None = None
    status: JobItemStatus
    message: str | None = None
    updated_at: datetime | None = None
    #: Per-account timing (plan §6): when the run started on this account, when
    #: it stopped, and the seconds between — None until both ends are known.
    started_at: datetime | None = None
    finished_at: datetime | None = None
    seconds: float | None = None

    @classmethod
    def from_item(cls, item: JobItem) -> "JobItemOut":
        """Build from an ORM row, pulling the email off the related account.

        ``item.account`` may be unloaded/None (e.g. a deleted account), so the
        email is optional rather than a hard dependency on the relationship.
        """
        account = getattr(item, "account", None)
        started_at = getattr(item, "started_at", None)
        finished_at = getattr(item, "finished_at", None)
        return cls(
            account_id=item.account_id,
            email=getattr(account, "email", None),
            status=item.status,
            message=item.message,
            updated_at=item.updated_at,
            started_at=started_at,
            finished_at=finished_at,
            seconds=item_seconds(started_at, finished_at),
        )


class JobDetail(JobOut):
    items: list[JobItemOut] = Field(default_factory=list)

    @classmethod
    def from_job(cls, job: Job) -> "JobDetail":
        detail = cls.model_validate(job)
        detail.items = [JobItemOut.from_item(i) for i in job.items]
        return detail


# --- Request bodies -----------------------------------------------------------
# account_ids is nullable on purpose: null/empty means "every account", which is
# the common case from the UI's "run for all" button.


#: Email domains one filter may name.
MAX_LISTED_DOMAINS = 200


class AccountFilterIn(BaseModel):
    """A selection of accounts expressed as a *filter* rather than a list of ids.

    The portal holds 37k accounts, so "select all matching" cannot mean posting
    37,000 ids (ROADMAP §0). The client sends the same filter it is looking at
    and the server resolves it, which also guarantees the run covers exactly
    the rows the operator was shown.

    ``extra="forbid"`` is deliberate: a mistyped field would otherwise be
    dropped silently and the filter would widen to *every* account — at this
    scale that is the one failure worth being loud about.
    """

    model_config = ConfigDict(extra="forbid")

    q: str | None = Field(default=None, max_length=MAX_SEARCH_CHARS)
    statuses: list[AccountStatus] = Field(default_factory=list, max_length=len(AccountStatus))
    tag_ids: list[RowId] = Field(default_factory=list, max_length=MAX_LISTED_TAGS)
    #: OR'd with tag_ids, so "Untagged + Verified" is one selection.
    include_untagged: bool = False
    subscriptions: list[str] = Field(default_factory=list, max_length=MAX_LISTED_SUBSCRIPTIONS)
    ids: list[RowId] = Field(default_factory=list, max_length=MAX_LISTED_IDS)
    #: Email domains ("soniclip.in"); an account matches any of them.
    domains: list[str] = Field(default_factory=list, max_length=MAX_LISTED_DOMAINS)
    min_supercoins: int | None = Field(default=None, ge=0, le=10_000_000)

    def to_filter(self) -> AccountFilter:
        """Freeze into the core filter the query layer takes."""
        return AccountFilter(
            q=self.q,
            statuses=tuple(self.statuses),
            tag_ids=tuple(self.tag_ids),
            include_untagged=self.include_untagged,
            subscriptions=tuple(self.subscriptions),
            domains=tuple(d for d in (x.strip().lower() for x in self.domains) if d),
            ids=tuple(self.ids),
            min_supercoins=self.min_supercoins or None,
        )

    def as_provenance(self) -> dict:
        """What to record in ``Job.payload`` — only the fields actually set.

        JSON-mode so the dict survives the JSON column, and defaults dropped so
        a historical run reads as what the operator narrowed by rather than as
        six mostly-empty keys.
        """
        return self.model_dump(mode="json", exclude_defaults=True)


class LoginJobRequest(BaseModel):
    account_ids: list[RowId] | None = Field(default=None, max_length=MAX_LISTED_IDS)
    account_filter: AccountFilterIn | None = None
    force: bool = False


class CouponJobRequest(BaseModel):
    account_ids: list[RowId] | None = Field(default=None, max_length=MAX_LISTED_IDS)
    account_filter: AccountFilterIn | None = None
    coupon_code: str = Field(min_length=1)

    @field_validator("coupon_code")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        """A whitespace-only code would start a doomed browser run per account."""
        code = value.strip()
        if not code:
            raise ValueError("coupon_code must not be blank")
        return code
