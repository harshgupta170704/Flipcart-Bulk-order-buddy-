"""The post-order sync job (ROADMAP §10).

Until this phase the tool recorded what it *ordered* and never looked again. The
operator needs what actually **happened**: delivered or not, the tracking id, the
real delivery date, the delivery OTP, the seller, the invoice — the columns their
existing portal shows per order unit. This job reads those off Flipkart and folds
them into the ``Order`` rows we already have.

**Observation never overwrites intent.** ``amount``, ``target``, ``qty`` and
``status`` are what we meant to buy and what we paid; they are the record a
reconciliation is run *against*. A scrape is an observation of a page that can
be stale, truncated or simply misread, so it writes only the Phase 10 columns
(``_OBSERVED_FIELDS`` + ``synced_at``) and leaves the rest alone. The single
exception is a cancelled order: the platform saying "Cancelled" means the order
genuinely is not placed any more, so that one does flip ``Order.status``.

A field the scraper could not read is "not read", not "empty" — it is skipped
rather than written as ``None``. Otherwise one detail page that failed to render
would wipe a tracking id we already had.

Matching is **by platform order id within one account**. Scoping it to the
account is not a tidiness point: two accounts can both be syncing concurrently,
and a global match would let one account's scrape rewrite another's order row.
Flipkart lists one row per *unit*; so does the ledger. Observations are grouped
by order id, each becomes (or updates) its own unit row
(``app.core.order_units.apply_observations``), and the order row then mirrors
one representative unit decided from ALL the order's units — failed only when
every unit ever observed is cancelled — see ``_reconcile`` and
``_mirror_from_units``.

``tracking_id`` is wired up but not yet filled: no tracking id was visible on a
live order page, so the scraper does not surface one and the column stays None
until it does (``_OBSERVED_FIELDS``).

The browser and the scraper are bound to module-level names so tests can
monkeypatch ``app.jobs.sync_tasks.browser_page`` / ``scrape_orders`` and drive
the whole job with no browser at all.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.automation.ordering import PAYMENT_COD
from app.automation.sync import NotSignedIn, is_cancelled, is_delivered, scrape_order, scrape_orders
from app.core import alerts as alert_rules
from app.core import invoices, session_store
from app.core.order_units import SOURCE_SYNC, apply_observations, representative_unit, timeline_steps
from app.core.browser import browser_page
from app.db.models import Account, JobType, Order, OrderStatus, utcnow
from app.jobs import schedule
from app.jobs.account_guard import run_on_account
from app.jobs.tracker import JobTracker

# Shared with the login/coupon/order bodies rather than copied: one definition
# of "effective settings", of "a short-lived session per concurrent item", of
# what a worker reports back, and of how a settled item reaches the tracker. A
# second copy of any of these would be free to drift from the one the other
# three runs use.
from app.jobs.tasks import _Outcome, _effective_settings, _pool_for, _redact, _run_batch, _session

log = logging.getLogger("ordering.jobs.sync")

#: SQLite caps a statement at ~999 bound parameters, so an ``IN`` list is sent
#: in chunks: an account with thousands of orders would otherwise raise instead
#: of matching.
_MAX_BIND_PARAMS = 900

#: A read of one account's pages that took this long is said to be slow.
SLOW_READ_SECONDS = 5 * 60

#: How many orders on their way, beyond the recent list, a tracking
#: read-back reads by id per account: a few seconds each.
FOLLOW_OPEN_MAX = 10

#: The only ``Order`` columns a sync may write (plus ``synced_at``). Named once,
#: here, because this tuple *is* the "observation never overwrites intent" rule
#: — anything not in it keeps the value the ordering run put there.
#:
#: ``tracking_id`` is the courier's, read off the order's history once it has
#: shipped (``sync.parse_shipment``, mapped live 27 Oct 2026); the order row
#: takes the one of the unit it mirrors, as for the other fields.
_OBSERVED_FIELDS = (
    "item_id",
    "unit_id",
    "seller",
    "tracking_id",
    "platform_status",
    "delivery_date",
    "delivery_otp",
    "invoice_url",
)


# --- the run -----------------------------------------------------------------
async def run_sync_job(
    job_id: int,
    account_ids: list[int],
    *,
    limit_per_account: int = 20,
    import_unknown: bool = False,
    order_ids: Sequence[str] | None = None,
    follow_open: bool = False,
    invoices_only: bool = False,
) -> None:
    """Read each account's recent orders back off Flipkart and reconcile them.

    With ``invoices_only`` (the ledger's "Fetch invoices", with ``order_ids``)
    no page is read: each listed order's invoices are downloaded from
    Flipkart's invoice API under its account's session, stored and parsed —
    see :func:`_fetch_invoices_for`.

    Accounts run concurrently at ``settings.max_concurrency``, the same shape as
    every other job body: a ``JobTracker`` per run, ``run_bounded`` over the
    accounts, one outcome per account, and the batch carries on when one account
    fails.

    An account with no stored session is **skipped, not failed** — a sync is
    read-only, so "we have never logged this account in" is nothing to report in
    red. ``import_unknown`` additionally creates rows for orders this tool never
    placed; see ``_imported_order`` for why that is off by default.

    With ``order_ids`` the run reads exactly those orders, by id, under the
    account that holds each — for ids handed in to track, or older than the
    recent list's window — and the recent list is not read at all.

    With ``follow_open`` (the timer's tracking read-back) the orders still on
    their way that the recent list did not reach are read by id after it — at
    most :data:`FOLLOW_OPEN_MAX` an account. The list shows one row per unit,
    20 rows, so one ten-unit order fills half of it, and an older order on its
    way would otherwise never be read again (found live, 26 Oct 2026).
    """
    settings = _effective_settings()
    tracker = JobTracker(job_id)
    tracker.start(account_ids)
    # One browser for the whole run, one isolated window per account (PLAN.md
    # §10): it opens with the first account that asks and closes after the batch.
    pool = _pool_for(settings)
    listed = [key for key in (_key(value) for value in order_ids or ()) if key]

    async def worker(account_id: int, index: int) -> _Outcome:
        await tracker.item_started(account_id)

        # One account, one run (app.jobs.account_guard): an order run using
        # this account has it to itself, and it is read back after that run.
        ordering = run_on_account(account_id, JobType.order)
        if ordering is not None:
            # Only the timer reads an account again on its own, and it reads the
            # recent list, never a run's listed orders: those are said plainly.
            then = "start this read-back again once that run ends" if listed else "read back after that run"
            return _Outcome(False, f"order run #{ordering} is using this account — {then}", skipped=True)

        with _session() as db:
            account = db.get(Account, account_id)
            if account is None:
                return _Outcome(False, "account not found")
            # When this read-back took the account's cookies, for
            # _mark_session_invalid: a set saved after it is not the one used here.
            cookies_taken_at = utcnow()
            cookies = session_store.load_cookies(db, account)
            wanted = _listed_ids_for(db, account_id, listed) if listed else []
        if not cookies:
            return _Outcome(False, "no session", skipped=True)
        if listed and not wanted:
            return _Outcome(False, "none of the listed orders belongs to this account", skipped=True)

        #: The order run this read-back stopped for, once one starts here.
        gave_way: list[int] = []

        def give_way() -> bool:
            # Asked before each order page: an order run that starts on this
            # account meanwhile takes it over, and the rest waits for the next read.
            ordering = run_on_account(account_id, JobType.order)
            if ordering is not None:
                gave_way.append(ordering)
            return ordering is not None

        if invoices_only:
            return await _fetch_invoices_for(
                account_id, wanted, cookies=cookies, settings=settings, pool=pool, give_way=give_way, gave_way=gave_way
            )

        reading_since = time.monotonic()
        try:
            async with browser_page(
                headless=settings.browser_headless,
                # The session was made through the configured proxy, if any; a
                # read-back from the server's own address would look like a
                # different machine to Flipkart (audit F-13, 25 Oct 2026).
                proxy=settings.proxy_server or None,
                cookies=cookies,
                pool=pool,
            ) as (context, page):
                if wanted:
                    scraped = await _scrape_listed(page, wanted, should_stop=give_way)
                else:
                    scraped = await scrape_orders(
                        page, limit=limit_per_account, open_details=True, should_stop=give_way
                    )
                    if follow_open and not gave_way:
                        listed_now = {_key(_observed(observation, "order_id")) for observation in scraped}
                        with _session() as db:
                            beyond = [
                                order_id
                                for order_id in schedule.open_order_ids(db, account_id)
                                if _key(order_id) not in listed_now
                            ][:FOLLOW_OPEN_MAX]
                        if beyond:
                            scraped = [*scraped, *await _scrape_listed(page, beyond, should_stop=give_way)]
                # Still signed in: the invoice links the pages offered can be
                # fetched now. Outside this block the session is gone with it.
                # Not once the account is given over: the order run has it —
                # asked again before each order's download, as an order run
                # can start during the last page or a slow download.
                fetched: dict[str, list[bytes]] = {}
                for group in _with_invoice_links(scraped):
                    if gave_way or give_way():
                        break
                    fetched.update(await invoices.fetch_missing(context, account_id, group))
        except NotSignedIn as exc:
            # The session is dead: say so, and mark the account so the
            # session-invalid alert raises and the next login run picks it up
            # (audit F-8, 25 Oct 2026). Its cookie set goes with it, so no run
            # loads it again (review of 27 Oct 2026).
            log.error("sync: account %s is not signed in any more", account_id)
            _mark_session_invalid(account_id, since=cookies_taken_at)
            return _Outcome(False, f"not signed in: {exc} — run a login for this account")
        except Exception as exc:  # noqa: BLE001 - one account's failure is data
            # A traceback is safe here, unlike in an order run: a sync touches no
            # card data (PLAN.md §5), so there is nothing in this flow for a
            # driver message to echo. _redact still clamps the length.
            log.exception("sync failed for account %s", account_id)
            reason = _redact(str(exc) or exc.__class__.__name__)
            return _Outcome(False, f"sync failed: {reason}")

        # Deliberately outside the try: a failure here is a bug rather than a
        # flaky page, and ``run_bounded`` already captures it as this account's
        # failure without touching its siblings. The whole fold is one
        # transaction, so a half-applied account is never committed.
        tally = _reconcile(account_id, scraped, import_unknown=import_unknown)
        summary = tally.message()
        saved = invoices.record(account_id, fetched)
        if saved:
            summary += f", {saved} invoice(s) put on file"
        took = time.monotonic() - reading_since
        if took >= SLOW_READ_SECONDS:
            # The pages are a few seconds each; minutes mean Flipkart, or this
            # machine, was slow to answer — said on the item, which the run
            # history shows, as the server's own log is not always to hand.
            summary += f"; slow: Flipkart's pages took {took / 60:.0f} min to read (usually about a minute)"
        if gave_way:
            # What was read stands. Passed over, the account does not count as
            # read back (schedule._last_read_backs): the timer reads its recent
            # list after the run. A run's listed orders it never reads, so the
            # ones left are counted and the owner told to start it again.
            log.info("account %s: read-back stopped for order run #%s: %s", account_id, gave_way[0], summary)
            if wanted:
                read = {_key(_observed(observation, "order_id")) for observation in scraped}
                left = sum(1 for order_id in wanted if order_id not in read)
                rest = f"{left} listed order(s) not read — start this read-back again once that run ends"
            else:
                rest = "the rest is read back after that run"
            return _Outcome(
                False,
                f"{summary} — stopped early so order run #{gave_way[0]} could use this account; {rest}",
                skipped=True,
            )
        log.info("account %s synced: %s", account_id, summary)
        return _Outcome(True, summary)

    async with pool:
        await _run_batch(tracker, account_ids, worker, settings.max_concurrency)
    await tracker.finish()
    # What was read back may have raised or cleared alerts — a delivery that
    # arrived closes its overdue alert — so the rules run now, not when someone
    # next presses "Evaluate now".
    # Off the event loop: a pass waits on the engine's lock when "Evaluate now"
    # is running on a request thread, and the loop must not wait with it.
    await asyncio.to_thread(alert_rules.evaluate_in_own_session)


#: Orders asked per batch by "Fetch invoices": between batches the run asks
#: whether an order run wants the account, as a read-back does between pages.
INVOICE_BATCH = 10
#: Answers that mean Flipkart would not serve this session.
_REFUSED = ("Flipkart answered 401", "Flipkart answered 403", "a web page (the account may be signed out)")


@dataclass
class _InvoiceAsk:
    """One order to ask the invoice API for — the shape fetch_missing reads."""

    order_id: str
    invoice_url: str


async def _fetch_invoices_for(
    account_id: int,
    order_ids: Sequence[str],
    *,
    cookies: list[dict],
    settings,
    pool,
    give_way: Callable[[], bool],
    gave_way: list[int],
) -> _Outcome:
    """Download, store and parse the invoices of ``order_ids`` (all this
    account's) from Flipkart's invoice API — what "Download Invoice" on each
    order's page does, without opening the pages. An order whose invoices are
    already complete is left alone; one with none yet is said so."""
    asks = [_InvoiceAsk(order_id=o, invoice_url=invoices.invoice_api_url(o)) for o in order_ids]
    fetched: dict[str, list[bytes]] = {}
    problems: dict[str, str] = {}
    asked = 0
    try:
        async with browser_page(
            headless=settings.browser_headless,
            proxy=settings.proxy_server or None,
            cookies=cookies,
            pool=pool,
        ) as (context, _page):
            for start in range(0, len(asks), INVOICE_BATCH):
                if gave_way or give_way():
                    break
                batch = asks[start : start + INVOICE_BATCH]
                fetched.update(
                    await invoices.fetch_missing(context, account_id, batch, force=True, problems=problems)
                )
                asked += len(batch)
    except Exception as exc:  # noqa: BLE001 - one account's failure is data
        log.exception("invoice fetch failed for account %s", account_id)
        return _Outcome(False, f"invoice fetch failed: {_redact(str(exc) or exc.__class__.__name__)}")

    saved = invoices.record(account_id, fetched)
    complete = max(0, asked - len(fetched) - len(problems))
    parts = [f"invoices downloaded for {len(fetched)} of {len(order_ids)} order(s), {saved} new invoice(s) put on file"]
    if complete:
        parts.append(f"{complete} already complete")
    if problems:
        reason = next(iter(problems.values()))
        parts.append(f"{len(problems)} with none ({reason})")
    summary = ", ".join(parts)
    refused = [why for why in problems.values() if any(word in why for word in _REFUSED)]
    if problems and not fetched and len(refused) == len(problems):
        return _Outcome(False, f"{summary} — Flipkart refused this session; run a login for this account")
    if gave_way:
        left = len(order_ids) - asked
        return _Outcome(
            False,
            f"{summary} — stopped early so order run #{gave_way[0]} could use this account; "
            f"{left} order(s) not asked — fetch again once that run ends",
            skipped=True,
        )
    log.info("account %s invoices: %s", account_id, summary)
    return _Outcome(True, summary)


def _with_invoice_links(scraped: Sequence[object]) -> list[list[object]]:
    """The observations offering an invoice link, one list per order."""
    groups: dict[str, list[object]] = {}
    for observation in scraped:
        order_id = _key(_observed(observation, "order_id"))
        if order_id and getattr(observation, "invoice_url", None):
            groups.setdefault(order_id, []).append(observation)
    return list(groups.values())


def _representative(observations: Sequence[object]) -> object:
    """The unit whose wording the order row mirrors.

    The first unit still on its way, so an order with one unit outstanding is
    never read as delivered by the alert rules and the dashboard; then the
    first unit nothing is known about yet; then simply the first. Every unit
    keeps its own wording on its own row regardless.
    """
    from app.core import order_status

    states = [order_status.classify(_text(getattr(o, "platform_status", None))) for o in observations]
    for wanted in (order_status.IN_TRANSIT, order_status.UNKNOWN):
        for observation, state in zip(observations, states):
            if state == wanted:
                return observation
    return observations[0]


def _listed_ids_for(db: DbSession, account_id: int, listed: Sequence[str]) -> list[str]:
    """Which of the listed order ids this account holds in the ledger."""
    from sqlalchemy import select

    found: list[str | None] = []
    for chunk in _chunks(list(listed), _MAX_BIND_PARAMS):
        found.extend(
            db.scalars(
                select(Order.order_id).where(Order.account_id == account_id, Order.order_id.in_(chunk))
            ).all()
        )
    return sorted({key for key in (_key(value) for value in found) if key})


async def _scrape_listed(page, order_ids: Sequence[str], *, should_stop=None) -> list[object]:
    """One order page per listed id; one id failing costs only that order.
    ``should_stop`` is asked before each page, as ``scrape_orders`` asks it."""
    scraped: list[object] = []
    for order_id in order_ids:
        if should_stop is not None and should_stop():
            log.info("sync: stopped before order %s; the rest is read back later", order_id)
            break
        try:
            scraped.extend(await scrape_order(page, order_id))
        except Exception as exc:  # noqa: BLE001 — recorded as unread, the rest carry on
            log.error("sync: order %s could not be read: %s", order_id, exc)
    return scraped


# --- reconciliation ----------------------------------------------------------
@dataclass
class _Tally:
    """What one account's sync did, for the line the operator reads.

    ``matched`` and ``updated`` are deliberately separate: every matched row is
    stamped ``synced_at``, but only a row whose *observed* values actually moved
    counts as updated. "20 matched, 2 updated" says two orders progressed; a
    single count could not.
    """

    seen: int = 0
    matched: int = 0
    updated: int = 0
    unknown: int = 0
    imported: int = 0
    #: Unconfirmed rows (sent, never confirmed, no id) now matched to a real
    #: order the platform shows. See _adopt_unconfirmed.
    adopted: int = 0
    delivered: int = 0
    cancelled: int = 0
    #: Orders a read-back had failed on their cancelled units, put back to
    #: placed by a unit read for the first time — see _revive.
    revived: int = 0
    #: Further units of an order already reconciled in this pass — see _reconcile.
    extra_units: int = 0
    #: Observations whose row link carried no order id at all.
    unidentified: int = 0

    def message(self) -> str:
        parts = [
            f"{self.seen} order(s) seen",
            f"{self.matched} matched",
            f"{self.updated} updated",
            f"{self.unknown} unknown",
        ]
        if self.imported:
            parts.append(f"{self.imported} imported")
        if self.adopted:
            # Said out loud: an unconfirmed order got its id and is now settled.
            parts.append(f"{self.adopted} unconfirmed order(s) found on Flipkart and settled")
        if self.delivered:
            parts.append(f"{self.delivered} delivered")
        if self.cancelled:
            # Said out loud: this run changed our own status column, which no
            # other part of a sync does.
            parts.append(f"{self.cancelled} cancelled — marked failed")
        if self.revived:
            parts.append(f"{self.revived} back to placed: a unit read for the first time is still on its way")
        if self.extra_units:
            parts.append(f"{self.extra_units} further unit(s) of the same order recorded")
        if self.unidentified:
            parts.append(f"{self.unidentified} with no order id")
        return ", ".join(parts)


def _reconcile(
    account_id: int, scraped: Sequence[object], *, import_unknown: bool
) -> _Tally:
    """Fold one account's observations into its order and unit rows, in one
    transaction.

    Its own short-lived session, like every other DB touch in a worker: the
    accounts run concurrently and a ``Session`` is not task-safe.

    Flipkart's orders page lists one row per **unit**, so an order of three
    units arrives as three observations sharing an order id. Each becomes its
    own unit row (app.core.order_units.apply_observations). The order row
    mirrors the first unit — that is what the alert rules and the dashboard
    counters read — and is marked failed only when every unit is cancelled:
    one cancelled unit of three is a partial order, not a failed one.
    """
    tally = _Tally(seen=len(scraped))
    grouped: dict[str, list[object]] = {}
    for observation in scraped:
        key = _key(_observed(observation, "order_id"))
        if key is None:
            # Nothing to match on now or ever: with no id, importing it
            # would create a fresh duplicate on every single run.
            tally.unknown += 1
            tally.unidentified += 1
            continue
        grouped.setdefault(key, []).append(observation)

    with _session() as db:
        by_order_id = _orders_by_order_id(db, account_id, list(grouped))
        for key, observations in grouped.items():
            primary = _representative(observations)
            tally.extra_units += len(observations) - 1
            all_cancelled = all(_is_cancelled(o) for o in observations)
            if not all_cancelled and all(_is_delivered(o) for o in observations):
                tally.delivered += 1

            fresh = False
            rows = by_order_id.get(key)
            if not rows:
                adopted = _adopt_unconfirmed(db, account_id, primary, cancelled=all_cancelled)
                if adopted is not None:
                    rows = [adopted]
                    tally.matched += 1
                    tally.adopted += 1
                else:
                    tally.unknown += 1
                    if not import_unknown:
                        continue
                    row = _imported_order(
                        account_id, primary, cancelled=all_cancelled, units=len(observations)
                    )
                    db.add(row)
                    rows = [row]
                    fresh = True
                    tally.imported += 1
            else:
                tally.matched += 1

            # Counted only when this pass is what turns the order to failed: a
            # cancelled order stays in the recent list for weeks, and "4
            # cancelled — marked failed" at every hourly read-back read as if
            # four more orders were cancelled each time (seen 26 Oct 2026).
            already_failed = all(row.status == OrderStatus.failed for row in rows)

            # Every row carrying this order id is written, not just the first:
            # a duplicate record is a data problem of its own, and leaving one
            # half of it stale would hide the duplicate rather than expose it.
            for row in rows:
                # Which units had been read before this pass, for _revive.
                read_before = {id(unit) for unit in row.units if unit.synced_at is not None}
                # The units first; then the order row from ALL its units — never
                # from this pass alone, which may have listed only some of them.
                changed = apply_observations(
                    db, row, observations, source=SOURCE_SYNC, read=_observed
                ).changed
                if _payment_from_page(row):
                    changed = True
                if row.placed_at is None:
                    # An order handed in by id, or imported before the page
                    # was read: Flipkart's own "Order Confirmed" date is its
                    # order date, so it joins every order-date filter.
                    ordered = _ordered_on(primary)
                    if ordered is not None:
                        row.placed_at = ordered
                        changed = True
                if _fill_product(row, [primary, *observations]):
                    changed = True
                representative, cancelled = _mirror_from_units(row)
                if _apply(row, representative, cancelled=cancelled):
                    changed = True
                if _revive(row, cancelled=cancelled, read_before=read_before):
                    changed = True
                    tally.revived += 1
                if changed and not fresh:
                    tally.updated += 1
            # From the status change, not the wording: a refunded cancellation
            # reads "Refund Completed" and is failed all the same (_unit_state).
            if not already_failed and any(row.status == OrderStatus.failed for row in rows):
                tally.cancelled += 1
        db.commit()
    return tally


def _mirror_from_units(row: Order) -> tuple[object, bool]:
    """What the order row should say, decided from every unit it has.

    Not from the observations of one pass: the recent list is one row per
    unit cut at a row limit, and an unreadable row is skipped, so a pass can
    see one unit of two — the other unit's last reading still counts. The
    representative is :func:`app.core.order_units.representative_unit`'s, the
    rule the Order Details API shares: among the units the platform has ever
    shown, the first still on its way, then the first whose wording is
    unfamiliar, then the first delivered — so an order standing with a unit
    delivered and one cancelled reads delivered — then the first by position.
    A placeholder the platform never listed decides nothing: a requested unit
    that never existed (the quantity was capped) would otherwise hold a
    delivered order at "unknown" for ever. ``cancelled`` only when every
    observed unit is cancelled (a refunded cancellation included: _unit_state),
    and at least one has been.
    """
    from app.core import order_status

    units = sorted(row.units, key=lambda unit: unit.position)
    representative = representative_unit(row)
    observed = [_unit_state(u) for u in units if u.synced_at is not None]
    cancelled = bool(observed) and all(s == order_status.CANCELLED for s in observed)
    return representative, cancelled


#: An order whose payment the tool does not know: imported, or uploaded by id.
PAYMENT_UNKNOWN = "unknown"
#: "Paid By" wording that is cash on delivery (the order dialog's own test).
_PAID_ON_DELIVERY = re.compile(r"cash on delivery|pay on delivery", re.IGNORECASE)


def _payment_from_page(row: Order) -> bool:
    """An order of unknown payment is cash on delivery when a unit's page says
    so. True if it changed. A run's own card or COD is never overwritten, and
    no other wording — UPI, net banking, Pay Later — is taken for a card."""
    if row.payment_mode != PAYMENT_UNKNOWN:
        return False
    if any(_PAID_ON_DELIVERY.search(getattr(unit, "payment_method", None) or "") for unit in row.units):
        row.payment_mode = PAYMENT_COD
        return True
    return False


#: The product columns a read-back names an order by, with their widths:
#: PostgreSQL refuses a longer value (SQLite keeps it), and one long product
#: name must not fail an account's whole read-back.
_PRODUCT_COLUMNS = {"model": Order.__table__.c.model.type.length, "color": Order.__table__.c.color.type.length}


def _product_value(observation: object, field: str) -> str | None:
    """The page's product name or colour, cut to its column's width."""
    value = _observed(observation, field)
    return value[: _PRODUCT_COLUMNS[field]] if value else None


def _product_key(name: str) -> str:
    """A product name as two readings of it are compared: case and spacing aside."""
    return " ".join(name.split()).casefold()


def _fill_product(row: Order, observations: Sequence[object]) -> bool:
    """Name the product of an order that has none — one handed in by id — after
    its page, so To be delivered, the alerts and the exports show it under its
    model rather than as one unknown (review of 27 Oct 2026). True if it
    changed.

    An order has one name: when the units one pass shows name different
    products, it stays unnamed rather than count one product's units under the
    other's name. A colour is taken only from a unit of the product the order
    is named after, never another product's. What the row holds is kept: a
    run's own record, read off the product page, is never rewritten in the
    orders page's wording."""
    named = [(o, name) for o in observations if (name := _product_value(o, "model"))]
    if not named:
        return False
    changed = False
    if not (row.model or "").strip():
        if len({_product_key(name) for _, name in named}) > 1:
            log.info("sync: order %s shows more than one product; it is left unnamed", row.order_id)
            return False
        row.model = named[0][1]
        changed = True
    if not (row.color or "").strip():
        same = _product_key(row.model)
        for observation, name in named:
            colour = _product_value(observation, "color")
            if colour and _product_key(name) == same:
                row.color = colour
                changed = True
                break
    return changed


def _unit_state(unit) -> str:
    """A unit's state for mirroring its order: its wording's, except that a
    refunded cancellation is a cancellation
    (:func:`app.core.order_units.unit_state_for_order`, the one rule start-up
    shares when a reading changes)."""
    from app.core.order_units import unit_state_for_order

    return unit_state_for_order(unit)


def _revive(row: Order, *, cancelled: bool, read_before: set[int]) -> bool:
    """Put back to placed an order failed on the units a read-back had seen —
    all cancelled — once a unit read for the first time is on its way or
    delivered. True if it did.

    Found by review (26 Oct 2026): a pass that saw only the cancelled unit of a
    two-unit order (the list cut at its row limit) failed it, and the pass that
    then saw the other unit out for delivery never set it back — its code on no
    card, the timer no longer following it, its units no longer counted. A
    Flipkart order page opened by id links none of its units (seen live), so
    the other units cannot be fetched on purpose; this mends it whenever one is
    read. Only a unit never read before counts: a refund reads returned and
    never qualifies, and a unit already read as cancelled does not flip the
    order by being misread once. An order with no id — one a run of this tool
    failed — is never touched.
    """
    from app.core import order_status

    if cancelled or row.status != OrderStatus.failed or not row.order_id:
        return False
    live = (order_status.IN_TRANSIT, order_status.DELIVERED)
    for unit in row.units:
        if unit.synced_at is None or id(unit) in read_before:
            continue
        if order_status.classify(unit.platform_status) in live:
            row.status = OrderStatus.placed
            log.info("sync: order row %s back to placed: unit %s reads %r", row.id, unit.position, unit.platform_status)
            return True
    return False


def _apply(row: Order, observation: object, *, cancelled: bool | None = None) -> bool:
    """Write the observed fields onto one order row; True if anything moved.

    Only ``_OBSERVED_FIELDS`` are touched — see the module docstring on why a
    scrape must not rewrite ``amount``/``target``/``qty``/``status``. A cancelled
    order is the exception: it is not placed any more, so it is marked failed.
    """
    changed = False
    for field in _OBSERVED_FIELDS:
        value = _observed(observation, field)
        if value is None:
            # Not read, not empty. Writing None here would clear a tracking id
            # we already have whenever one detail page fails to render.
            continue
        if getattr(row, field) != value:
            setattr(row, field, value)
            changed = True

    if cancelled is None:
        cancelled = _is_cancelled(observation)
    if cancelled and row.status != OrderStatus.failed:
        row.status = OrderStatus.failed
        changed = True
    elif row.status == OrderStatus.unconfirmed and row.order_id:
        from app.core import order_status

        status_text = _observed(observation, "platform_status") or row.platform_status
        if (
            status_text
            and not order_status.payment_unconfirmed(status_text)
            and order_status.classify(status_text) in (order_status.IN_TRANSIT, order_status.DELIVERED)
        ):
            row.status = OrderStatus.placed
            changed = True

    # Stamped whether anything moved or not: "nothing changed" and "never
    # looked" are different answers, and this column is the only thing that can
    # tell them apart.
    row.synced_at = utcnow()
    return changed


def _adopt_unconfirmed(
    db: DbSession, account_id: int, observation: object, *, cancelled: bool | None = None
) -> Order | None:
    """Match an order the platform shows, which we hold no id for, to this
    account's most recent *unconfirmed* row — and settle that row.

    An unconfirmed order was sent but never confirmed, so it was recorded with
    no order id. When the platform later lists an order we cannot otherwise
    match, that row is the only candidate: importing the observation instead
    would create a second row for one order, and the unconfirmed one would
    stay "may exist" forever. The newest unconfirmed row is taken, preferring
    one whose model or amount agrees with what the platform shows.
    """
    candidates = (
        db.query(Order)
        .filter(
            Order.account_id == account_id,
            Order.status == OrderStatus.unconfirmed,
            Order.order_id.is_(None),
        )
        .order_by(Order.id.desc())
        .all()
    )
    if not candidates:
        return None

    model = (_text(getattr(observation, "model", None)) or "").lower()
    amount = _as_float(getattr(observation, "amount", None))

    def agreement(row: Order) -> int:
        score = 0
        if model and row.model and (model in row.model.lower() or row.model.lower() in model):
            score += 2
        if amount is not None and row.amount is not None and abs(row.amount - amount) < 1:
            score += 1
        return score

    row = max(candidates, key=agreement)  # ties keep the newest (first)
    if agreement(row) == 0:
        # Nothing about this order — not the model, not the amount — matches
        # the unconfirmed row. Filing it there would turn a stranger's order
        # (the owner's own purchase, an older one in the list) into a placed
        # order with a card number on it (audit F-9, 25 Oct 2026). Left for a
        # person to settle.
        log.info(
            "an unknown order on account %s matches no unconfirmed row by model or amount; not adopting",
            account_id,
        )
        return None
    from app.core import order_status

    row.order_id = _key(_observed(observation, "order_id"))
    if cancelled is None:
        cancelled = _is_cancelled(observation)
    status_text = _observed(observation, "platform_status")
    if cancelled:
        row.status = OrderStatus.failed
    elif order_status.payment_unconfirmed(status_text):
        row.status = OrderStatus.unconfirmed
    else:
        row.status = OrderStatus.placed
    # The session does not autoflush: without this the next observation's
    # query would still see this row as id-less and could adopt it again.
    db.flush()
    log.info(
        "sync: unconfirmed order row %s matched to platform order %s (%s)",
        row.id,
        row.order_id,
        row.status.value,
    )
    return row


def _mark_session_invalid(account_id: int, *, since: datetime | None = None) -> None:
    """The account's session no longer signs in: say so on the row, so the
    alerts raise it and the next login run re-creates it, and delete its
    cookie set, which no run is to load again (session_store.forget_session).
    ``since`` is when this read-back took the cookies: a login that finished
    after it saved a set of its own, and that one is kept."""
    with _session() as db:
        account = db.get(Account, account_id)
        if account is None:
            return
        if not session_store.forget_session(db, account, since=since):
            log.info("sync: account %s signed in again during its read-back; the new session is kept", account_id)


def _ordered_on(observation: object) -> datetime | None:
    """When Flipkart says the order was placed: the first dated step of the
    order page's timeline ("Order Confirmed, Oct 20") — the platform's own
    date, not one of ours. None when the page showed no dated step."""
    from datetime import datetime as _datetime

    for step in getattr(observation, "timeline", None) or []:
        when = step.get("date") if isinstance(step, dict) else None
        if not when:
            continue
        try:
            return _datetime.fromisoformat(str(when))
        except ValueError:
            continue
    return None


def _imported_order(
    account_id: int, observation: object, *, cancelled: bool | None = None, units: int = 1
) -> Order:
    """A row for an order this tool never placed (``import_unknown``).

    ``units`` is how many observations the platform listed for it — one per
    unit — and becomes the order's quantity; the caller folds the observations
    into unit rows once the order is in the session.

    Off by default, because on an account with years of personal history a sync
    would otherwise bury the orders this tool actually placed under everything
    else the account ever bought — and those invented rows then count towards
    the platform unit limits, which are a ``SUM`` over this table. Turned on, the
    point is the reverse: orders placed by hand or by the old portal should show
    up in the one table the operator reads.

    ``status`` is ``placed`` because the platform is showing a real order (or
    ``failed`` when it says cancelled, for the same reason a matched row flips).
    ``placed_at`` is Flipkart's own "Order Confirmed" date when the order page's
    timeline shows one, and unset otherwise: we did not place it, and inventing
    a timestamp would misreport when. ``payment_mode`` is "unknown", as for an
    id upload: the column's default, "card", would show a cash-on-delivery order
    placed by hand as paid by card. Its page's "Paid By" sets it to cash on
    delivery when it says so (:func:`_payment_from_page`).
    """
    observed = {field: _observed(observation, field) for field in _OBSERVED_FIELDS}
    if cancelled is None:
        cancelled = _is_cancelled(observation)
    # No product name or colour here: _reconcile names it from all the units
    # it was shown (_fill_product), as it names an order handed in by id.
    return Order(
        account_id=account_id,
        order_id=_key(_observed(observation, "order_id")),
        product_link=_observed(observation, "product_link"),
        qty=_as_int(getattr(observation, "qty", None)) or max(1, units),
        amount=_as_float(getattr(observation, "amount", None)),
        status=OrderStatus.failed if cancelled else OrderStatus.placed,
        payment_mode=PAYMENT_UNKNOWN,
        placed_at=_ordered_on(observation),
        synced_at=utcnow(),
        **observed,
    )


def _orders_by_order_id(
    db: DbSession, account_id: int, keys: Sequence[str | None]
) -> dict[str, list[Order]]:
    """This account's order rows, grouped by normalised platform order id.

    Stored ids are trimmed and upper-cased by the model as they are written, and
    ``keys`` are normalised the same way before the lookup, so case or stray
    whitespace on either side cannot cause a silent no-match — which would look
    exactly like "the platform has not heard of this order".
    """
    wanted = sorted({key for key in keys if key})
    found: dict[str, list[Order]] = {}
    if not wanted:
        return found
    # Every writer stores order_id trimmed and upper-cased, and ``wanted`` is
    # normalised the same way, so this is a plain match on the indexed column
    # rather than UPPER(TRIM(...)) over every row of the account.
    for chunk in _chunks(wanted, _MAX_BIND_PARAMS):
        stmt = select(Order).where(
            Order.account_id == account_id, Order.order_id.in_(chunk)
        )
        for row in db.scalars(stmt):
            key = _key(row.order_id)
            if key is not None:
                found.setdefault(key, []).append(row)
    return found


def _chunks(values: Sequence[str], size: int) -> Iterator[list[str]]:
    """Split an ``IN`` list so it stays under the driver's parameter cap."""
    for start in range(0, len(values), size):
        yield list(values[start : start + size])


# --- reading one observation -------------------------------------------------
#: Observed fields that are days, and the one that is an amount (26 Oct 2026).
_DATE_FIELDS = frozenset({"delivery_date", "promised_date", "delivered_date"})
_AMOUNT_FIELDS = frozenset({"amount_paid"})


def _observed(observation: object, field: str) -> str | datetime | float | None:
    """One field off a ``ScrapedOrder``, normalised. None means "not read".

    ``getattr`` with a default rather than attribute access: a scraped order is
    whatever one page happened to show, and a scraper that does not surface a
    column yet should leave that column alone instead of failing an account's
    whole sync.
    """
    value = getattr(observation, field, None)
    if field in _DATE_FIELDS:
        return _as_datetime(value)
    if field in _AMOUNT_FIELDS:
        return _as_float(value)
    if field == "timeline":
        # A list of steps, kept as one: run through _text it became the
        # list's Python text, which the order dialog then showed as one step
        # per character (found 26 Oct 2026).
        return timeline_steps(value)
    return _text(value)


def _text(value: object) -> str | None:
    """Trimmed text, or None for anything blank — "" is not an observation."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_datetime(value: object) -> datetime | None:
    """A delivery date the DateTime column will accept.

    A scraper handing back an ISO string instead of a ``datetime`` must not take
    the account's whole sync down with a type error on commit, and one
    unparseable date must not either — it is dropped with a warning, so the rest
    of the fields still land.
    """
    if isinstance(value, datetime):
        return value
    text = _text(value)
    if text is None:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        log.warning("ignoring an unparseable delivery_date %r", text)
        return None


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _key(value: object) -> str | None:
    """An order id as it is matched on: trimmed and upper-cased, or None."""
    text = _text(value)
    return text.upper() if text else None


# --- platform states ---------------------------------------------------------
# Decided by the scraper's own predicates (over DELIVERED_STATES /
# CANCELLED_STATES) rather than by a membership test written again here. That
# module owns the vocabulary and deliberately keeps it tight — an unrecognised
# state stays unrecognised instead of being guessed into a bucket — and it
# already strips the date tail, so ``platform_status`` arrives as clean wording
# ("Delivered", "Cancelled", "Out for Delivery"). Two copies of "what counts as
# cancelled" is exactly how one of them ends up wrong.
def _is_cancelled(observation: object) -> bool:
    return is_cancelled(_text(getattr(observation, "platform_status", None)))


def _is_delivered(observation: object) -> bool:
    return is_delivered(_text(getattr(observation, "platform_status", None)))
