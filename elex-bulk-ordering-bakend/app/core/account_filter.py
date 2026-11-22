"""One place that turns "which accounts?" into a query.

At 37k accounts the old answer — fetch every row and let the client decide — is
not usable, and "select all" cannot mean posting 37,000 ids. So a selection is
expressed as a **filter**, and the same filter serves three callers:

* the Accounts table (one page of rows + a total count),
* bulk tagging ("tag everything matching this"),
* starting a run ("order on everything matching this"),

which is why it lives here rather than inside any one of them. Keeping the
query in a single module also keeps the semantics identical everywhere — a
filter that shows you 412 accounts starts a run on exactly those 412.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import Select, String, cast, func, or_, select
from sqlalchemy.orm import Session as DbSession, selectinload

from app.db.models import Account, AccountStatus

#: Columns the table may be sorted by, mapped to the model attribute. A
#: whitelist rather than getattr(), so a sort key can never reach another column.
SORTABLE = {
    "id": Account.id,
    "email": Account.email,
    "phone": Account.phone,
    # As text: PostgreSQL orders a native enum by its declaration, not its
    # spelling, so "blocked" would sort after "session_valid" there and before
    # it on SQLite. The cast makes both alphabetical.
    "status": cast(Account.status, String),
    "subscription_type": Account.subscription_type,
    "created_at": Account.created_at,
    "updated_at": Account.updated_at,
}

DEFAULT_SORT = "id"
DEFAULT_SIZE = 50
MAX_SIZE = 500


@dataclass(frozen=True)
class AccountFilter:
    """A selection of accounts. Every field is an AND; lists inside are an OR."""

    q: str | None = None
    statuses: tuple[AccountStatus, ...] = ()
    tag_ids: tuple[int, ...] = ()
    #: Include accounts with no tag at all ("Untagged" in the UI). Combines with
    #: tag_ids as an OR, so "Untagged + Verified" is expressible.
    include_untagged: bool = False
    subscriptions: tuple[str, ...] = ()
    ids: tuple[int, ...] = ()
    #: Email domains ("soniclip.in"): an account whose address ends in
    #: "@<domain>" for any of them, in any case.
    domains: tuple[str, ...] = ()
    #: Only accounts with at least this many SuperCoins (last read balance).
    min_supercoins: int | None = None

    def is_empty(self) -> bool:
        """True when this selects everything (no narrowing at all)."""
        return not (
            self.q
            or self.statuses
            or self.tag_ids
            or self.include_untagged
            or self.subscriptions
            or self.ids
            or self.domains
            or self.min_supercoins
        )


def apply(stmt: Select, flt: AccountFilter) -> Select:
    """Narrow a select over Account by the filter."""
    if flt.ids:
        stmt = stmt.where(Account.id.in_(flt.ids))
    if flt.min_supercoins:
        stmt = stmt.where(Account.supercoins_balance >= flt.min_supercoins)

    if flt.q:
        # Substring match on the two things an operator searches by, with the
        # text's own wildcards escaped (audit F-25): "_" finds underscores.
        from app.core.order_filter import like_pattern

        like = like_pattern(flt.q)
        stmt = stmt.where(or_(Account.email.ilike(like, escape="\\"), Account.phone.ilike(like, escape="\\")))

    if flt.statuses:
        stmt = stmt.where(Account.status.in_(flt.statuses))

    if flt.subscriptions:
        stmt = stmt.where(Account.subscription_type.in_(flt.subscriptions))

    if flt.domains:
        from app.core.order_filter import like_pattern

        # like_pattern wraps in %...%; a domain is matched at the end only.
        clauses = [
            Account.email.ilike("%@" + like_pattern(domain.strip().lower())[1:-1], escape="\\")
            for domain in flt.domains
            if domain.strip()
        ]
        if clauses:
            stmt = stmt.where(or_(*clauses))

    # Tags: explicit tags OR'd with "has no tag", so both can be asked for at once.
    tag_clauses = []
    if flt.tag_ids:
        tag_clauses.append(Account.tag_id.in_(flt.tag_ids))
    if flt.include_untagged:
        tag_clauses.append(Account.tag_id.is_(None))
    if tag_clauses:
        stmt = stmt.where(or_(*tag_clauses))

    return stmt


def count(db: DbSession, flt: AccountFilter) -> int:
    """How many accounts match — computed in SQL, never by loading rows."""
    stmt = apply(select(func.count()).select_from(Account), flt)
    return int(db.scalar(stmt) or 0)


def page(
    db: DbSession,
    flt: AccountFilter,
    *,
    page_number: int = 1,
    size: int = DEFAULT_SIZE,
    sort: str = DEFAULT_SORT,
    descending: bool = False,
) -> tuple[list[Account], int]:
    """One page of matching accounts plus the total match count."""
    size = max(1, min(int(size or DEFAULT_SIZE), MAX_SIZE))
    page_number = max(1, int(page_number or 1))
    column = SORTABLE.get(sort or DEFAULT_SORT, Account.id)
    order = column.desc() if descending else column.asc()

    stmt = apply(select(Account), flt)
    stmt = (
        # Avoids an N+1 on the tag name and on the account's own GST.
        stmt.options(selectinload(Account.tag), selectinload(Account.gst))
        .order_by(order, Account.id.asc())  # id breaks ties => stable paging
        .offset((page_number - 1) * size)
        .limit(size)
    )
    return list(db.scalars(stmt).all()), count(db, flt)


def ids(db: DbSession, flt: AccountFilter, *, cap: int | None = None) -> list[int]:
    """Every matching account id.

    Used when a filter is turned into real work (starting a run, bulk tagging).
    Only the id column is read, so this stays cheap even over the whole table.
    ``cap`` guards against a mis-click starting a run on everything.
    """
    stmt = apply(select(Account.id), flt).order_by(Account.id.asc())
    if cap is not None:
        stmt = stmt.limit(cap + 1)  # +1 so the caller can detect an overflow
    return [int(row) for row in db.scalars(stmt).all()]


@dataclass
class FilterOptions:
    """The values actually present in the data, for building the filter UI."""

    statuses: list[dict] = field(default_factory=list)
    subscriptions: list[str] = field(default_factory=list)
    total: int = 0
    untagged: int = 0
    #: Email domains present, most accounts first: [{"value": ..., "count": ...}].
    domains: list[dict] = field(default_factory=list)


def email_domain(email: str | None) -> str | None:
    """The part after the last "@", lower-cased; None without one."""
    if not email or "@" not in email:
        return None
    return email.rsplit("@", 1)[1].strip().lower() or None


def options(db: DbSession) -> FilterOptions:
    """Distinct values + counts, so the UI offers only filters that match something."""
    status_rows = db.execute(
        select(Account.status, func.count()).group_by(Account.status)
    ).all()
    subs = db.scalars(
        select(Account.subscription_type)
        .where(Account.subscription_type.is_not(None))
        .distinct()
        .order_by(Account.subscription_type.asc())
    ).all()
    untagged = int(
        db.scalar(
            select(func.count()).select_from(Account).where(Account.tag_id.is_(None))
        )
        or 0
    )
    return FilterOptions(
        statuses=[
            {"value": getattr(s, "value", s), "count": int(c)} for s, c in status_rows
        ],
        subscriptions=[s for s in subs if s],
        total=int(db.scalar(select(func.count()).select_from(Account)) or 0),
        untagged=untagged,
        domains=_domain_counts(db),
    )


def _domain_counts(db: DbSession) -> list[dict]:
    """Accounts per email domain. Counted over the email column alone: one
    string per account, which stays fast at tens of thousands of accounts and
    needs no database-specific string function."""
    from collections import Counter

    tally = Counter(email_domain(email) for email in db.scalars(select(Account.email)))
    tally.pop(None, None)
    return [{"value": domain, "count": count} for domain, count in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))]
