"""ORM models — the unified data layer (PLAN.md §5).

Replaces the single overloaded Excel workbook and the per-repo ExcelHandlers.
Note: there is intentionally **no Card model** — card details are never stored
(PLAN.md §5, §8); they are uploaded per run and held in memory only. The one
exception is ``HdfcChildCard`` (19 Nov 2026): a CorpAssist child card's number
and balance, never its expiry or CVV.
"""

import enum
from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def naive_utcnow() -> datetime:
    """UTC now without a zone, for stamps that are compared with each other
    after a round trip through a plain DateTime column: PostgreSQL stores an
    aware value in such a column as the session's wall-clock time, a naive one
    as written, so two naive UTC stamps subtract correctly on any engine and
    under any session time zone."""
    return utcnow().replace(tzinfo=None)


class AccountStatus(str, enum.Enum):
    """Where an account stands.

    The first three are our own session states; the rest mirror the login
    states the operator already thinks in (and that the existing portal
    reports), so a 37k-account list can be filtered the same way.
    """

    new = "new"
    session_valid = "session_valid"
    session_invalid = "session_invalid"
    logged_out = "logged_out"
    not_registered = "not_registered"
    multiple_attempts = "multiple_attempts"
    blocked = "blocked"
    other = "other"


class OrderStatus(str, enum.Enum):
    pending = "pending"
    placed = "placed"
    failed = "failed"
    over_target = "failed_over_target"  # see PLAN.md §6a
    #: The order was submitted to Flipkart, but its confirmation never came, so
    #: it MAY exist. Never "failed": a real order recorded as failed hands its
    #: units back, and the next attempt buys them a second time. It counts
    #: against unit limits and is never retried automatically.
    #: (Stored by member name; must stay within the column's 11 characters.)
    unconfirmed = "unconfirmed"


class JobType(str, enum.Enum):
    login = "login"
    coupon = "coupon"
    order = "order"
    sync = "sync"  # pull order status back from the platform (Phase 10)
    compile = "compile"  # zip the invoice PDFs of a ledger selection (spec FR-032)
    balances = "balances"  # read each account's SuperCoins and gift card balance


class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"


class Tag(Base):
    """A custom label on an account — the operator's main way of segmenting.

    One tag per account (matching how the existing portal works): the column
    shows a single value per row, and a run includes or excludes whole tags.
    An account with no tag reads as "Untagged".
    """

    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    accounts: Mapped[list["Account"]] = relationship(back_populates="tag")


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[AccountStatus] = mapped_column(
        Enum(AccountStatus), default=AccountStatus.new, nullable=False, index=True
    )
    # e.g. "ACTIVE-PREMIUM-GOLD". Free text: the platform invents new tiers and
    # an enum would reject rows on import rather than storing what it was told.
    subscription_type: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    tag_id: Mapped[int | None] = mapped_column(
        ForeignKey("tags.id"), nullable=True, index=True
    )
    tag: Mapped["Tag | None"] = relationship(back_populates="accounts")
    #: The Address Book GST this account's orders bill under, when it has one
    #: of its own. It wins over the GST a run picks (app.jobs.tasks), so a run
    #: over the accounts of two entities bills each order under its own
    #: entity's registration rather than all of them under one. NULL means
    #: "the run's GST", as before. Deleting the GST unlinks the account and
    #: never the other way round (app.api.gsts.delete_gst, as for addresses);
    #: ON DELETE SET NULL says the same wherever the column has its foreign key
    #: (a column added at start-up by sync_missing_columns has none).
    gst_id: Mapped[int | None] = mapped_column(
        ForeignKey("gsts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    gst: Mapped["Gst | None"] = relationship()
    #: The phone this account's orders are placed with: the delivery address's
    #: phone at checkout. NULL means the account's own ``phone``, as before.
    #: As wide as the ledger's ``Order.billing_phone``, which records it.
    billing_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    sessions: Mapped[list["Session"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    coupon_runs: Mapped[list["CouponRun"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    orders: Mapped[list["Order"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )

    #: Read from Flipkart by a "Read balances" run (app.automation.balances):
    #: SuperCoins, the gift card balance in rupees, and when they were read.
    supercoins_balance: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    gift_voucher_balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    balances_read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: A parent account and the children that use its payment (family /
    #: corporate structure): a child names its parent's email here.
    is_parent: Mapped[bool | None] = mapped_column(default=False, nullable=True)
    parent_email: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)


class Session(Base):
    """Stored Flipkart cookies for an account (replaces cookes/*.pkl)."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    cookies: Mapped[list | dict | None] = mapped_column(JSON, nullable=True)
    valid: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    account: Mapped["Account"] = relationship(back_populates="sessions")


class CouponRun(Base):
    __tablename__ = "coupon_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    coupon_code: Mapped[str] = mapped_column(String(64))
    result: Mapped[str | None] = mapped_column(String(255), nullable=True)
    run_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    account: Mapped["Account"] = relationship(back_populates="coupon_runs")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    #: The run that attempted it, when a run did; None for rows a sync imported.
    #: Lets a finished run deep-link to its own rows once the order history
    #: lives on its own page.
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True, index=True)
    #: The run's Ref Code (the order form's free-text reference), on every
    #: order the run recorded, so the ledger and its export carry it.
    ref_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    #: Accounting references a person fills in on the ledger (or an import
    #: brings): the buyer's purchase order, the RTGS/IMPS transfer's source and
    #: beneficiary, and the e-way bill. Written to the export's own columns.
    po_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rtgs_source: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rtgs_beneficiary: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ewb_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: The payment page's SuperCoins toggle was turned on for this order.
    used_supercoins: Mapped[bool | None] = mapped_column(nullable=True)
    #: The rupees SuperCoins took off this order's total at checkout, read as
    #: the total before the toggle less the total after it. NULL when none
    #: were used or the figure could not be read.
    supercoins_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: When the row was recorded. ``placed_at`` is NULL for attempts that never
    #: became orders, so this is what "newest first" sorts on for them.
    created_at: Mapped[datetime | None] = mapped_column(DateTime, default=utcnow, index=True)
    product_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Classification used by UnitLimit. Recorded per order so a limit check is
    # a COUNT over past orders rather than a re-scrape of the product page.
    brand: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    category: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    color: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pin: Mapped[str | None] = mapped_column(String(16), nullable=True)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    target: Mapped[float | None] = mapped_column(Float, nullable=True)  # price cap §6a
    # "card" or "cod". Kept per order (not just per run) so the orders table can
    # show a Cash-on-Delivery column the way the operator expects to read it.
    # "unknown" for an order this tool did not place (read back, handed in by
    # id, or from an order report that does not say), and "prepaid" when an
    # order report says it was not Cash on Delivery without saying how it was
    # paid — never "card", which is a run's own card (27 Oct 2026).
    payment_mode: Mapped[str] = mapped_column(String(16), default="card", nullable=False)
    #: The number of the card the order was paid with — in full, at the owner's
    #: decision of 25 Oct 2026 (spec FR-004's card column), so the ledger can
    #: be matched against card statements. Card runs only: blank for Cash on
    #: Delivery. Never the expiry or the CVV, which no row holds.
    card_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # --- filled by a post-order sync (Phase 10) ---------------------------
    # The platform's own ids for this unit; the order page URL carries all
    # three, and the operator's existing portal reports them per order unit.
    # The ledger filters and sorts on these, so they are indexed; indexes a
    # model gains later are created at startup by sync_missing_indexes().
    item_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    unit_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    seller: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tracking_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    #: Flipkart's own wording ("Delivered", "Out for Delivery", ...), kept raw
    #: rather than mapped onto OrderStatus so a new platform state cannot be
    #: silently flattened into the wrong bucket. Read through
    #: app.core.order_status, never compared by hand.
    platform_status: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    #: The portal's one reading of ``platform_status`` (app.core.order_status):
    #: delivered / cancelled / returned / failed / in_transit / unknown. Kept
    #: in a column, set whenever platform_status is assigned, so the ledger's
    #: grouped status filter and the alert rules are plain indexed SQL and can
    #: never disagree with the Python reading. NULL means "not yet derived"
    #: (rows older than this column, until the startup backfill runs).
    delivery_state: Mapped[str | None] = mapped_column(
        String(16), nullable=True, index=True, default="unknown"
    )
    delivery_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    delivery_otp: Mapped[str | None] = mapped_column(String(16), nullable=True)
    invoice_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    #: Stored trimmed and upper-case by every writer, so a match is a plain
    #: equality on the indexed column.
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    gst_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: The GSTIN the order was billed under, recorded at order time: one
    #: business name can have a registration per state, so the name alone
    #: does not identify it.
    gstin: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: The phone the order was placed with — the delivery address's phone at
    #: checkout: the account's billing phone, else its own phone, else the
    #: Address Book address's. The ledger's and the CSV's "Billing Phone".
    #: NULL for rows a read-back or an id sheet created, and for older rows.
    billing_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus), default=OrderStatus.pending, nullable=False, index=True
    )
    #: Which of the 25 checkout stages the attempt reached, and the fixed error
    #: code when it did not complete (app.core.order_outcomes).
    processing_stage: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    placed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    account: Mapped["Account"] = relationship(back_populates="orders")
    #: One row per unit of this order — the grain the ledger shows and the
    #: platform reports on. Every order has at least one (app.core.order_units
    #: creates them at flush time for any writer that did not).
    units: Mapped[list["OrderUnit"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="OrderUnit.position"
    )

    @validates("platform_status")
    def _derive_delivery_state(self, _key: str, value: str | None) -> str | None:
        """Keep ``delivery_state`` in step with the platform's wording."""
        from app.core.order_status import classify  # local: core imports models

        self.delivery_state = classify(value)
        return value

    @validates("order_id")
    def _normalise_order_id(self, _key: str, value: str | None) -> str | None:
        """Trimmed and upper-cased on every write, by every writer.

        The sync matches orders by this column with a plain equality on its
        index; a stray space or a lower-case id written by any path would make
        a real order look like one the platform has never heard of.
        """
        if value is None:
            return None
        text = str(value).strip().upper()
        return text or None


class OrderUnit(Base):
    """One unit of an order, as the platform reports on it.

    ``Order`` keeps what this tool intended and paid for; a unit keeps what
    Flipkart later said about one piece of it — its own id, status, delivery
    date, tracking id, seller, invoice link. An order of three units is three
    rows here, which is how a partial delivery or one cancelled unit becomes
    visible at all. The writers live in app.core.order_units.
    """

    __tablename__ = "order_units"
    __table_args__ = (
        # A unit id names one unit on one platform, ever (spec FR-024). NULL is
        # a placeholder for a requested unit not yet seen; NULLs are distinct,
        # so an order's placeholders never collide with each other.
        UniqueConstraint("platform", "unit_id", name="uq_order_units_platform_unit_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_pk: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True, nullable=False)
    #: Constant "flipkart" for now (the owner's decision of 24 Oct 2026); a
    #: column so a second platform is an addition rather than a migration.
    platform: Mapped[str] = mapped_column(String(16), default="flipkart", nullable=False)
    #: 1-based place within the order. Placeholders are filled in this order.
    position: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    unit_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    item_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Who created the row: "run" (an order this tool placed), "sync" (first
    #: seen on the platform) or "upload" (an id list handed in to track).
    source: Mapped[str] = mapped_column(String(16), default="run", nullable=False, index=True)
    seller: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tracking_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    #: Flipkart's own wording, raw; read through app.core.order_status.
    platform_status: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    #: The portal's reading of ``platform_status``, kept in step by the validator.
    delivery_state: Mapped[str | None] = mapped_column(
        String(16), nullable=True, index=True, default="unknown"
    )
    delivery_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    #: The two dates ``delivery_date`` runs together, kept apart (26 Oct 2026)
    #: so "was it late?" has an answer: the day Flipkart promised ("Delivery
    #: expected by Nov 01", the latest promise read while the unit was on its
    #: way — kept once it is delivered) and the day it says the unit was
    #: delivered. NULL until a read-back sees each.
    promised_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    delivered_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: The first promise read, written once and never replaced (26 Oct 2026).
    #: Flipkart moves its promise when a delivery slips, and ``promised_date``
    #: follows it; a unit that then arrives on the moved day would read "on
    #: time" against that. Lateness is counted from this one. NULL until a
    #: read-back sees a promise (and on rows read before it existed, where the
    #: readers fall back to ``promised_date``).
    first_promised_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: How the unit was paid, as the order page's price details say it
    #: ("Cash On Delivery", a card), and that page's "Total amount" — paid, or
    #: to be paid on delivery. The unit's own page: an order's banner can show
    #: a larger figure for all its units. NULL until a read-back sees them.
    payment_method: Mapped[str | None] = mapped_column(String(100), nullable=True)
    amount_paid: Mapped[float | None] = mapped_column(Float, nullable=True)
    delivery_otp: Mapped[str | None] = mapped_column(String(16), nullable=True)
    invoice_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The order page's address block as the read-back saw it (type, address,
    #: name, phone on one line) and the pincode in it — the snapshot the
    #: address-mismatch alert compares with the Address Book (spec FR-044, C-5).
    delivery_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivery_pincode: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    #: When a read-back last replaced one snapshot with a different one — the
    #: address-change alert's trigger (spec FR-045). NULL until that happens.
    address_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: The GST the platform shows the order billed under ("GST details" on the
    #: order page: the GSTIN and the business name), read back per unit so it
    #: can be checked against the GST the run chose (``Order.gstin``) — an
    #: order invoiced to the wrong entity succeeds silently otherwise.
    billed_gstin: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    billed_business_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: When a person marked the unit's goods as in hand, and who (spec FR-001,
    #: FR-017: the ledger's "Received"). Set and cleared by hand only
    #: (app.api.orders, POST /orders/received); nothing the platform says
    #: changes it. NULL until someone marks it.
    received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    received_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Why the unit was cancelled, in the platform's own words: the line the
    #: order page prints under the timeline's "Cancelled, <date>" step ("Sorry,
    #: your order was cancelled as per seller policy restrictions applicable to
    #: this brand.", seen live 24–25 Oct 2026), at most 255 characters. NULL
    #: until a read-back sees one. It is what tells a cancellation by Flipkart
    #: from one the buyer asked for (app.core.cancellations).
    cancel_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: The order page's timeline as last read: a list of {"status", "date"}
    #: steps in page order, the date None for a step still to come — the
    #: order's history as the operator sees it (26 Oct 2026).
    timeline: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # --- what an order report says that no read-back reads (spec FR-022) ---
    # A later report updates a unit's refunds and cancellations; these were
    # read past as "not kept" until 27 Oct 2026. Written by app.io.order_report
    # alone, under its rules: a unit the read-back has seen has only the empty
    # ones filled, and an empty cell blanks none. NULL until a report gives each.
    #: The refund as the report words it ("Refund Completed"), its amount and
    #: how it was paid back ("Original payment method").
    refund_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    refund_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    refund_method: Mapped[str | None] = mapped_column(String(100), nullable=True)
    #: The courier carrying the unit ("Ekart Logistics"): the spec's Shipment Partner.
    shipment_partner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: The report's own "last tracking update", as written ("25-10-2026
    #: Cancelled"). The timeline's newest step stands over it once a read-back
    #: has dated one (app.io.orders_csv.last_tracking_cell).
    last_tracking_text: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Whether the user — the account that bought — cancelled the unit, as the
    #: report says: True, False, or NULL when no report said. Read beside
    #: Flipkart's own wording and the tool's cancel requests
    #: (app.core.cancellations.unit_cancelled_by_user).
    cancelled_by_user: Mapped[bool | None] = mapped_column(nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    order: Mapped["Order"] = relationship(back_populates="units")
    #: Cancel / reschedule requests recorded on this unit (spec FR-020); they
    #: go when the unit goes, since they are about nothing else.
    requests: Mapped[list["UnitRequest"]] = relationship(
        back_populates="unit", cascade="all, delete-orphan"
    )
    #: The changes the read-backs saw on this unit, oldest first (the
    #: dashboard's "Changes since you last looked"); they go when the unit goes.
    events: Mapped[list["UnitEvent"]] = relationship(
        back_populates="unit", cascade="all, delete-orphan", order_by="UnitEvent.id"
    )

    @validates("platform_status")
    def _derive_delivery_state(self, _key: str, value: str | None) -> str | None:
        from app.core.order_status import classify  # local: core imports models

        self.delivery_state = classify(value)
        return value

    @validates("unit_id")
    def _normalise_unit_id(self, _key: str, value: str | None) -> str | None:
        """Trimmed and upper-cased like an order id: matched by equality."""
        if value is None:
            return None
        text = str(value).strip().upper()
        return text or None

    @validates("item_id")
    def _trim_item_id(self, _key: str, value: str | None) -> str | None:
        """Trimmed only: never matched on, and the order row keeps it as read."""
        if value is None:
            return None
        text = str(value).strip()
        return text or None


class UnitRequest(Base):
    """A cancel or reschedule request on one unit, for a person to action.

    Spec FR-020, decided under C-4 (25 Oct 2026): the ledger records the
    request — kind, reason, the wanted date, who asked — and a person does it
    on the platform by hand, then marks it done or drops it here. Nothing in
    this app talks to Flipkart for it. A unit holds at most one open request
    per kind: checked in app.core.unit_requests before the write, and kept by
    the database with the partial unique index below — on open rows only, so
    a done request can be followed by a fresh one.
    """

    __tablename__ = "unit_requests"
    __table_args__ = (
        # Two operators submitting the same request at the same moment both
        # pass the read-then-write check; this is what stops the second write.
        # The WHERE makes it partial (SQLite and PostgreSQL both take one);
        # sync_missing_indexes() builds it on an existing table at start-up.
        Index(
            "uq_unit_requests_open_kind",
            "unit_pk",
            "kind",
            unique=True,
            sqlite_where=sql_text("status = 'open'"),
            postgresql_where=sql_text("status = 'open'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    unit_pk: Mapped[int] = mapped_column(ForeignKey("order_units.id"), index=True, nullable=False)
    #: "cancel" or "reschedule".
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    #: Why — required for a cancel, optional for a reschedule (checked by the API).
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The wanted new delivery date; required for a reschedule.
    requested_for: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: "open" until a person marks it "done" or "dropped".
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False, index=True)
    #: The signed-in user's email.
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    closed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: What the person wrote when closing it.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    unit: Mapped["OrderUnit"] = relationship(back_populates="requests")
    #: Every step of the request's life, in the order written: oldest first.
    events: Mapped[list["UnitRequestEvent"]] = relationship(
        back_populates="request",
        order_by="UnitRequestEvent.id",
        cascade="all, delete-orphan",
    )


class UnitRequestEvent(Base):
    """One step in a request's life: opened, marked done or dropped, opened
    again — by whom, when, and with what note (spec C-4: "a status log").

    The request row holds only where it stands now, so opening it again used
    to erase who had closed it and when (spec audit of 27 Oct 2026). These
    rows keep every step. Each is written with the change it records
    (app.core.unit_requests), and requests older than the table get theirs
    at start-up, from what their own row still says
    (app.core.unit_requests.backfill_history).
    """

    __tablename__ = "unit_request_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("unit_requests.id"), index=True, nullable=False)
    #: The request's status after this step: "open", "done" or "dropped".
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    #: Who took the step: the signed-in user's email.
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    #: What the person wrote with the step.
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    request: Mapped["UnitRequest"] = relationship(back_populates="events")


class UnitEvent(Base):
    """One change in a unit's delivery state, as a read-back saw it.

    The timer reads back every account with an order on its way, and until
    these rows a read-back that found a phone delivered or cancelled said so
    nowhere: the operator had to scan the ledger to notice (26 Oct 2026). Now
    the read-back writes a row at the moment it sees the change, and the
    dashboard lists the rows the signed-in user has not marked as seen
    (``User.changes_seen_at``). The rules for what counts as a change live in
    app.core.unit_events, the only writer. Rows are never edited: a unit that
    moves again gets another row.
    """

    __tablename__ = "unit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    unit_pk: Mapped[int] = mapped_column(ForeignKey("order_units.id"), index=True, nullable=False)
    #: The unit's order and account, kept on the row so the feed can name them
    #: and filter on them without going through the unit.
    order_pk: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True, nullable=False)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True, nullable=False)
    #: When the read-back saw the change. Naive UTC (:func:`naive_utcnow`), so
    #: it compares correctly with ``User.changes_seen_at`` on any database.
    at: Mapped[datetime] = mapped_column(DateTime, default=naive_utcnow, nullable=False, index=True)
    #: What happened: delivered, out_for_delivery, cancelled, returned or
    #: failed (app.core.unit_events.KINDS).
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The unit's reading before and after: a delivery state of
    #: app.core.order_status, or "out_for_delivery" for a unit on the
    #: courier's van. ``from_state`` is NULL for a unit's first reading.
    from_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_state: Mapped[str] = mapped_column(String(32), nullable=False)

    unit: Mapped["OrderUnit"] = relationship(back_populates="events")
    #: Declared so a flush that deletes an order deletes its events first.
    order: Mapped["Order"] = relationship()


class Invoice(Base):
    """An invoice PDF on file for an order.

    Fetched by the read-back with the account's own session when an order's
    page offers an invoice link, or uploaded by hand. The file itself lives in
    the invoice store (app.io.invoice_store); this row is what the ledger, the
    CSV and the order-details endpoints read to say "on file" and where.
    """

    __tablename__ = "invoices"
    __table_args__ = (
        # The same bytes for the same order are one invoice, however often
        # they are fetched or uploaded.
        UniqueConstraint("order_pk", "sha256", name="uq_invoices_order_sha256"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_pk: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True, nullable=False)
    platform: Mapped[str] = mapped_column(String(16), default="flipkart", nullable=False)
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    #: product / fee / credit_note / unknown — read off the PDF (app.io.invoice_pdf).
    kind: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    #: Path relative to the store's root.
    path: Mapped[str] = mapped_column(Text, nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: sync (fetched during a read-back) or upload.
    source: Mapped[str] = mapped_column(String(16), default="sync", nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # --- what the parser read off the PDF (spec FR-026..028) -----------------
    # All nullable: a row written before the parser, or one whose PDF could not
    # be read, keeps the file and says why in ``parse_error``. Written by
    # app.core.invoices.apply_parse; sync_missing_columns() adds them to an
    # existing table at start-up.
    #: The label the Extract Invoices Data dialog lists: "Product", the fee's
    #: own name ("Secure Packaging Fee"), "Credit Note".
    invoice_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    invoice_number: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    #: Dates as printed on the invoice (midnight, no clock to convert).
    invoice_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    order_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    seller_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    seller_gstin: Mapped[str | None] = mapped_column(String(32), nullable=True)
    buyer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Fee invoices carry none: they print a billing address, not a GSTIN.
    buyer_gstin: Mapped[str | None] = mapped_column(String(32), nullable=True)
    grand_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Credit notes only: the invoice being reversed, and why.
    original_invoice: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The invoice's lines, one dict per line in app.io.invoice_pdf.ParsedLine's
    #: shape; the CSV writes one row per entry.
    lines: Mapped[list | None] = mapped_column(JSON, nullable=True)
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: app.io.invoice_pdf.PARSER_VERSION at the time; a newer parser re-reads the row.
    parser_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parse_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class CompiledArchive(Base):
    """A zip of invoice PDFs built by a Compile Invoices run (spec FR-032/038).

    The file lives in the archive store (app.core.archives) under ``name``;
    the row is what the "Download compiled invoices" list reads, and what the
    retention sweep deletes after ``ORDERING_ARCHIVE_RETENTION_DAYS``.
    """

    __tablename__ = "compiled_archives"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: The file name, ``<prefix>.zip`` (numbered when the prefix was taken).
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(80))
    #: Path relative to the store's root — the name, today.
    path: Mapped[str] = mapped_column(Text)
    #: Bytes. BigInteger: a quarter's invoices pass 2 GiB, which PostgreSQL's
    #: 4-byte INTEGER cannot hold (a table made before the change is widened by
    #: Alembic revision 0002).
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    #: What went in: orders and units matched, PDFs written, units left out
    #: because their order had no invoice on file (spec FR-030), and invoice
    #: rows whose file was missing from the store when the zip was built.
    orders: Mapped[int] = mapped_column(Integer, default=0)
    units: Mapped[int] = mapped_column(Integer, default=0)
    invoices: Mapped[int] = mapped_column(Integer, default=0)
    left_out: Mapped[int] = mapped_column(Integer, default=0)
    missing_files: Mapped[int] = mapped_column(Integer, default=0)
    #: The ledger filter as it was asked for, so the archive says what it covers.
    filters: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class JobItemStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"
    skipped = "skipped"
    #: An order was sent for this account but never confirmed — it may exist.
    #: Kept apart from ``failed`` so the run is not marked failed over it, and
    #: so the operator sees "check this account" rather than "retry".
    unconfirmed = "unconfirmed"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[JobType] = mapped_column(Enum(JobType), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus), default=JobStatus.queued, nullable=False
    )
    progress: Mapped[int] = mapped_column(Integer, default=0)  # 0..100
    total_items: Mapped[int] = mapped_column(Integer, default=0)
    done_items: Mapped[int] = mapped_column(Integer, default=0)
    # Genuine failures only. An account that was passed over (no session, no
    # card, already done, cancelled) counts as skipped, not failed — otherwise
    # the UI reports a red "40 failed" for a batch where nothing went wrong.
    failed_items: Mapped[int] = mapped_column(Integer, default=0)
    skipped_items: Mapped[int] = mapped_column(Integer, default=0)
    #: Orders sent but never confirmed (see JobItemStatus.unconfirmed).
    unconfirmed_items: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    # Non-sensitive run parameters only (coupon code, product link, target, qty).
    # Card details are NEVER written here (PLAN.md §5).
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    log: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    items: Mapped[list["JobItem"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class JobItem(Base):
    """Per-account progress within a job — powers the live per-account view."""

    __tablename__ = "job_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    status: Mapped[JobItemStatus] = mapped_column(
        Enum(JobItemStatus), default=JobItemStatus.pending, nullable=False
    )
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: When the run began working on this account and when it stopped — the
    #: per-account timing the run history shows (plan §6). Both are None for an
    #: account the run never reached; ``finished_at`` alone is set for one the
    #: run passed over (skipped: no session, no card, already done), so a skip
    #: shows no time at all rather than "0s". Written as naive UTC
    #: (:func:`naive_utcnow`) so the difference survives any database zone.
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    job: Mapped["Job"] = relationship(back_populates="items")
    account: Mapped["Account"] = relationship()

    @property
    def seconds(self) -> float | None:
        """How long the run spent on this account, or None until both ends are known."""
        return item_seconds(self.started_at, self.finished_at)


def item_seconds(started_at: datetime | None, finished_at: datetime | None) -> float | None:
    """Seconds between two stamps, to a tenth; None unless both are known.

    Never negative: the two stamps come from the same process clock, but a
    clock set back mid-run must not show up as a negative duration.
    """
    if started_at is None or finished_at is None:
        return None
    # A stamp just assigned by utcnow() carries its zone; one read back from the
    # database does not (the columns are plain DateTime). Both are UTC, so put
    # them on the same footing before subtracting.
    return round(
        max(0.0, (_naive_utc(finished_at) - _naive_utc(started_at)).total_seconds()), 1
    )


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


class Gst(Base):
    """A GST registration the business orders under.

    Separate from Address because several addresses can bill under one GST, and
    the operator picks them as a pair at order time.
    """

    __tablename__ = "gsts"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    gstin: Mapped[str] = mapped_column(String(32))
    business_name: Mapped[str] = mapped_column(String(255))
    is_default: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    addresses: Mapped[list["Address"]] = relationship(back_populates="gst")


class Address(Base):
    """A delivery address, optionally linked to the GST it bills under.

    Replaces the single hard-set address in config: the business runs several
    entities, and shipping every order to one of them is wrong *quietly* —
    the order succeeds, it just goes to the wrong place under the wrong GST.
    """

    __tablename__ = "addresses"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    line1: Mapped[str] = mapped_column(Text)
    floor: Mapped[str | None] = mapped_column(String(64), nullable=True)
    city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    state: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pincode: Mapped[str] = mapped_column(String(16))
    gst_id: Mapped[int | None] = mapped_column(
        ForeignKey("gsts.id"), nullable=True, index=True
    )
    is_default: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    gst: Mapped["Gst | None"] = relationship(back_populates="addresses")


class CardFailure(Base):
    """A card run's order that failed with a card in hand (card pool rules).

    Holds no card number: ``card_key`` is a salted one-way fingerprint of it
    (app.core.card_pool.card_key), enough to recognise the same card in a
    later sheet and nothing more. Failed orders keep no card number on their
    own row (nothing was charged), so this is where "this card failed lately"
    is remembered.
    """

    __tablename__ = "card_failures"

    id: Mapped[int] = mapped_column(primary_key=True)
    card_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    failed_at: Mapped[datetime] = mapped_column(DateTime, default=naive_utcnow, nullable=False, index=True)
    #: Why the order failed, as the run reported it (redacted, shortened).
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class LimitScope(str, enum.Enum):
    """What a unit limit counts, mirroring how the operator thinks about it."""

    category = "category"  # e.g. "Mobile / Handset"
    brand = "brand"  # e.g. "vivo"
    model = "model"  # e.g. "T5x 5G"
    variant = "variant"  # a specific colour/storage combination
    seller = "seller"  # e.g. "KANUNGAKOURI": units bought from one seller


class UnitLimit(Base):
    """How many units one account may order of something, per rolling window.

    Platforms cap purchases per account (e.g. 20 handsets / 31 days, 16 vivo /
    31 days, 5 of one model / 30 days). Exceeding a cap gets the order rejected
    or the account flagged, so a run has to check before it spends an account's
    quota rather than discovering the limit by tripping it.
    """

    __tablename__ = "unit_limits"

    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[LimitScope] = mapped_column(Enum(LimitScope), nullable=False)
    #: The thing being limited, matched case-insensitively against an order's
    #: category/brand/model. Empty means "every value in this scope".
    value: Mapped[str] = mapped_column(String(128), default="")
    max_units: Mapped[int] = mapped_column(Integer, default=0)
    window_days: Mapped[int] = mapped_column(Integer, default=30)
    #: A disabled limit is kept for reference but not enforced — the operator's
    #: UI shows "Disabled" rather than making them delete and retype it.
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AlertSeverity(str, enum.Enum):
    info = "info"
    warning = "warning"
    critical = "critical"


class Alert(Base):
    """Something that needs a human to look at it.

    Modelled on the operator's existing "Needs attention" queue (443 open
    address mismatches there), so alerts are raised once, deduplicated by
    ``key``, and resolved rather than deleted — a problem that comes back
    should reopen the same row instead of silently appearing as new.
    """

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Stable identity for this problem, e.g. "address_mismatch:order:412".
    #: Raising the same key twice updates the existing row.
    key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[AlertSeverity] = mapped_column(
        Enum(AlertSeverity), default=AlertSeverity.warning, nullable=False
    )
    message: Mapped[str] = mapped_column(Text)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id"), nullable=True, index=True
    )
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id"), nullable=True, index=True
    )
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    resolved: Mapped[bool] = mapped_column(default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    #: When something about the alert last changed — reopened, closed, a new
    #: message. Set explicitly by the engine, never by ``onupdate``: a rules
    #: pass that only confirms the problem is still there touches
    #: ``last_seen_at`` alone, so the queue's order and ages survive the passes
    #: that run after every read-back.
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: What the operator wrote when closing it by hand (spec FR-046). A rule's
    #: close sets it to NULL and reopening clears it: a note describes one
    #: close, never a later one.
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Who closed it — "user" or "rule" — while ``resolved``. A user's close is
    #: an acceptance: the rules leave the row closed while the problem persists
    #: and reopen it only once the problem has gone away and come back.
    resolved_by: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: The email of the person who closed it, when ``resolved_by`` is "user".
    resolved_by_user: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: When a rules pass last reported the problem (see ``updated_at``).
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class GiftVoucher(Base):
    """A gift voucher on the register (app.api.master_lists): never its full
    code or PIN, only the last four characters, its value and what is left."""

    __tablename__ = "gift_vouchers"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    code_last4: Mapped[str | None] = mapped_column(String(8), nullable=True)
    value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    balance: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True, index=True)
    expires_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OrderPhone(Base):
    """A 10-digit number a run may put on the delivery address in turn."""

    __tablename__ = "order_phones"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ExportLog(Base):
    """One CSV export from Order Units History: the "CSV Downloads" tab lists
    them, and "Download again" runs the same query. The file itself is never
    kept — it holds card numbers — only the query that made it."""

    __tablename__ = "export_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    file_name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: The export's query string, filters and sheet, as the page sent it.
    query: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Agent(Base):
    """One process that does browser work (app.core.fleet): the API process,
    or an Arq queue worker. It registers itself and writes a heartbeat."""

    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    #: "api" (order runs) or "worker" (sign-in and coupon runs).
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="api")
    host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: A disabled agent takes no new work.
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    #: Takes only runs started with "Test on isolated agents", and only those.
    isolated: Mapped[bool] = mapped_column(default=False, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_test_ok: Mapped[bool | None] = mapped_column(nullable=True)


class HdfcUserId(Base):
    """An HDFC CorpAssist user id (app.api.corpassist): the login whose
    child-card emails arrive at ``email`` in the shared inbox."""

    __tablename__ = "hdfc_user_ids"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    #: The address HDFC mails this user's child cards to (lower case).
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    mobile: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: The corporate (parent) card the child cards hang off: last four only.
    parent_card_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    #: "Fetch all" passes over an inactive user id.
    active: Mapped[bool] = mapped_column(default=True, nullable=False)
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: Why the last fetch found nothing usable; None when it went through.
    last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: The parent card's "Alternate A/c No." on HDFC's Card Relationship page,
    #: digits only: the ``Aan`` column of HDFC's bulk virtual-card sheet.
    aan: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: HDFC's own "Virtual Card User ID" for this user (the portal's table).
    portal_user_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class HdfcCardBatch(Base):
    """A request for HDFC virtual child cards (app.core.hdfc_batches).

    ``rows`` is what was asked for, one entry per user id:
    ``{"user_id", "username", "amount", "count", "valid_days"}``. Sent to the
    CorpAssist portal (``send="portal"``) or downloaded as HDFC's bulk sheet to
    upload by hand (``send="download"``); either way Fetch then watches the
    inbox for the cards, and ``cards_received`` counts those that arrived.
    No card number is ever on a batch.
    """

    __tablename__ = "hdfc_card_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: sending, waiting, done, failed or cancelled.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="waiting")
    #: portal or download.
    send: Mapped[str] = mapped_column(String(16), nullable=False, default="portal")
    rows: Mapped[list] = mapped_column(JSON, nullable=False)
    card_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cards_received: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    #: The portal's own words on rows it refused (its output file), no card numbers.
    portal_report: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=naive_utcnow, index=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class HdfcChildCard(Base):
    """A virtual child card read from an HDFC email (app.core.corpassist).

    Keeps the number in full (the owner's decision of 19 Nov 2026, amending
    PLAN.md §5), so a run's sheet can be matched to it and its balance
    tracked. There is deliberately no expiry or CVV column: those still come
    from the run's card sheet, in memory only. What a card has paid for is
    read from ``Order.card_number`` (app.core.corpassist.used_by_number), never
    kept here, so "Used" cannot drift from the ledger.
    """

    __tablename__ = "hdfc_child_cards"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("hdfc_user_ids.id", ondelete="CASCADE"), index=True)
    #: Digits only.
    card_number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    parent_card_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    #: The amount the card was issued with, in rupees; None when the email did not say.
    opening_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: "active" or "cancelled" (a cancelled card pays for nothing).
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: When the email that carried it was sent.
    issued_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: The card batch it was asked for in, when one matched it.
    batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("hdfc_card_batches.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class OtpPhone(Base):
    """A phone running an SMS forwarder app (app.core.sms_relay): it posts
    each text it receives to ``/sms/inbound`` with its own token, so a flow
    that sends an OTP to ``phone_number`` can read it (Employee Phones).

    Only the token's SHA-256 is kept; ``token_hint`` is its last four
    characters, to tell tokens apart on the page.
    """

    __tablename__ = "otp_phones"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 10 digits, no country code.
    phone_number: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    employee_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    corporate_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    token_hint: Mapped[str] = mapped_column(String(8), nullable=False)
    active: Mapped[bool] = mapped_column(default=True, nullable=False)
    last_ping_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sms_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sender: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_otp: Mapped[str | None] = mapped_column(String(12), nullable=True)
    last_otp_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=naive_utcnow)


class SmsMessage(Base):
    """A text an OTP phone relayed. Kept for a day (``sms_relay.KEEP_HOURS``)
    so an OTP can be read and a phone's setup checked; then deleted."""

    __tablename__ = "sms_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone_id: Mapped[int] = mapped_column(ForeignKey("otp_phones.id", ondelete="CASCADE"), index=True)
    sender: Mapped[str | None] = mapped_column(String(64), nullable=True)
    body: Mapped[str] = mapped_column(String(1000), nullable=False)
    otp: Mapped[str | None] = mapped_column(String(12), nullable=True)
    #: When this server received it (naive UTC), which is what an OTP wait compares.
    received_at: Mapped[datetime] = mapped_column(DateTime, default=naive_utcnow, index=True)


class Setting(Base):
    """Key/value app settings editable from the Settings UI."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(
        JSON, nullable=True
    )


class UserRole(str, enum.Enum):
    """Spec FR-058 / C-7: admins manage users, addresses, GSTs, unit limits and
    settings; operators do everything that moves orders. Both read everything."""

    admin = "admin"
    operator = "operator"


class User(Base):
    """An app user: who may sign in, with which role (spec FR-055, FR-058).

    The configured admin gets a row at start-up (app.core.users); everyone else
    is added by an admin with a temporary password. There is no delete — a
    user who ran things stays on file as who ran them — only ``active``.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Lower-cased; the sign-in name.
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    #: Empty for the configured admin until they set a password of their own:
    #: the environment's credential is that row's key (and stays a second one).
    password_hash: Mapped[str] = mapped_column(Text, default="")
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.operator, nullable=False)
    active: Mapped[bool] = mapped_column(default=True, nullable=False)
    #: True while the password is a temporary one an admin set (spec FR-059).
    must_change_password: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )
    #: Up to when this user has seen the dashboard's "Changes since you last
    #: looked" (``UnitEvent.at``, naive UTC); NULL until they first mark them
    #: seen. Per user, so one operator clearing the list does not clear it for
    #: another (app.api.changes).
    changes_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ScreenerItem(Base):
    """One watchlist slot in the Product Screener (up to 20 slots, BOB parity)."""

    __tablename__ = "screener_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), default="flipkart", nullable=False)
    product_url: Mapped[str] = mapped_column(Text, nullable=False)
    product_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    target_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    listed_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    mrp: Mapped[float | None] = mapped_column(Float, nullable=True)
    best_offer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    offer_applied: Mapped[str | None] = mapped_column(String(255), nullable=True)
    offer_combos: Mapped[list | None] = mapped_column(JSON, nullable=True)
    net_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    in_stock: Mapped[bool | None] = mapped_column(nullable=True)
    stock_text: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=naive_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=naive_utcnow, onupdate=naive_utcnow)


class EmailSwap(Base):
    """One row per requested email change (old login email -> new login email).

    Fed by the Swap Email Address CSV. A background worker logs in with the
    account's stored session, drives Flipkart's profile email change, and reads
    the verification code(s) the modal asks for (both go to mailboxes we read for
    these accounts). ``status`` walks pending -> running -> done / failed.
    """

    __tablename__ = "email_swaps"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    old_email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    new_email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False, index=True)
    stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=naive_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=naive_utcnow, onupdate=naive_utcnow)


class InventoryBatch(Base):
    """Warehouse receiving / stock-count batch per platform + product (BOB Inventory)."""

    __tablename__ = "inventory_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    platform: Mapped[str] = mapped_column(String(32), default="flipkart", nullable=False, index=True)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    opening_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    received_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    approved_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=naive_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=naive_utcnow, onupdate=naive_utcnow)


class InventoryTransfer(Base):
    """Stock transfer note moving approved units out of warehouse (BOB Inventory)."""

    __tablename__ = "inventory_transfers"

    id: Mapped[int] = mapped_column(primary_key=True)
    reference: Mapped[str] = mapped_column(String(120), nullable=False)
    platform: Mapped[str] = mapped_column(String(32), default="flipkart", nullable=False, index=True)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    destination: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=naive_utcnow)

