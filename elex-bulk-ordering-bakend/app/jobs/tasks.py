"""The three job bodies: login, coupons, ordering (PLAN.md §7 phase 3).

Each one is the same shape — build a ``JobTracker``, fan the account list out
through ``run_bounded`` at ``settings.max_concurrency``, persist each outcome,
then close the job. A single account failing is recorded as that account's
failure and the batch carries on; in particular an over-target order
(PLAN.md §6a) is a per-order outcome, never a batch abort.

**Where an order ships (ROADMAP §8).** An ordering run resolves one address
and one GST up front — the explicitly chosen address, else the one flagged
``is_default``, and with neither the run is refused; the chosen GST, else the
address's linked GST, else the default, else none. A chosen row that does not
exist fails the run instead of falling back, because shipping to the wrong
entity under the wrong GST *succeeds* and so goes unnoticed. An account with a
GST of its own bills under that one instead, and one with a billing phone
orders with it (``account_billing``): a run over the accounts of two entities
then bills each order to its own entity, and says on the job how many did.

**How many units (ROADMAP §9).** An order run can also be given a *total*
quantity rather than "one order per account": it then keeps placing orders of
``qty_per_order`` units until the target is met, across as many accounts as it
needs. Because the accounts run concurrently, the target is enforced through a
``UnitBudget`` that each worker claims from before it opens a browser and
releases back when its order does not go through — counting placed orders
instead would let every in-flight worker see the same "one unit still wanted"
and place an order for it. Before claiming, each account's remaining platform
quota is checked (``app.core.unit_limits``), because exceeding a per-window cap
gets orders rejected and accounts flagged.

**Card handling (PLAN.md §5).** Cards arrive as an in-memory
``{account_id: CardInfo}`` map supplied by the caller for this run only. They
are passed straight to ``run_order`` and are never written to the DB, to
``Job.payload``, to ``Job.log``, to an event, or to a log line — there is no
Card table to write them to. Messages derived from exceptions additionally go
through ``_redact`` as a backstop.

The browser and automation entry points are imported as plain module-level
names so tests can monkeypatch ``app.jobs.tasks.browser_page`` /
``run_login`` / ``apply_coupon`` / ``run_order`` and drive the jobs with no
real browser.
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import TypeVar

from sqlalchemy.orm import Session as DbSession

# The "exactly one default" rule is enforced by the address-book routes, so the
# default rows are read back through their own helpers rather than re-derived
# here — one definition of "the default", not two that can drift apart.
from app.api.addresses import default_address
from app.api.gsts import default_gst
from app.automation.coupons import ERROR as COUPON_ERROR
from app.automation.coupons import apply_coupon
from app.automation.login import LoginRefused, NotRegistered, run_login
from app.automation.mock_ordering import mock_message, mock_order
from app.jobs.pacing import accounts_at_once
from app.automation.ordering import CARD_MODES, GIFT_CARD_MODES
from app.core.account_picking import least_recently_ordered_first, recently_ordered
from app.core.card_pool import CapCounter, CardRules, is_payment_failure
from app.jobs.pacing import Pacing, run_paced
from app.core.step_timer import timing
from app.core.card_pool import plan as plan_cards
from app.core.card_pool import record_failure as record_card_failure
from app.automation.ordering import (
    PAYMENT_CARD,
    PAYMENT_COD,
    QTY_MODE_MINIMUM,
    QTY_MODE_UPTO,
    AddressInfo,
    CardInfo,
    GstInfo,
    run_order,
)
from app.automation.otp import get_otp_reader
from app.config import get_settings
from app.core import app_settings, session_store
from app.core.browser import DEFAULT_CONTEXTS_PER_BROWSER, BrowserPool, browser_page
from app.db.base import SessionLocal
from app.db.models import (
    Account,
    AccountStatus,
    Address,
    CouponRun,
    Gst,
    Job,
    JobStatus,
    Order,
    OrderStatus,
    utcnow,
)
from app.jobs.account_guard import wait_for_read_back
# Not ``app.api.jobs``, which owns the stop flag: it imports this module. It
# answers through ``app.jobs.stopping``, which imports nothing of the app.
from app.jobs.stopping import is_stopping

#: What an account passed over because the API was stopping says.
STOPPED_NOT_STARTED = "not started — the server was stopping"
from app.jobs.budget import UnitBudget
from app.jobs.runner import ItemResult, run_bounded
from app.jobs.tracker import JobTracker

# The unit-limit engine (ROADMAP §9), bound to module-level names for the same
# reason as ``run_order`` above: a test monkeypatches
# ``app.jobs.tasks.unit_allowance`` to drive a quantity run without seeding
# limit rows, since what the run *does* with an allowance is separate from how
# the engine counts one.
from app.core.unit_limits import ProductFacts
from app.core.unit_limits import allowance as unit_allowance
from app.core.unit_limits import blocking_limit
from app.core.unit_limits import describe as describe_limit

log = logging.getLogger("ordering.jobs.tasks")

# Backstop for PLAN.md §5: scrub any long digit run (a PAN) out of text that
# came from somewhere we do not control, e.g. a driver exception message.
_LONG_DIGITS = re.compile(r"\d{12,}")
_MAX_MESSAGE_CHARS = 500


@dataclass
class _Outcome:
    """What a worker reports back for one account."""

    ok: bool
    message: str | None = None
    skipped: bool = False
    #: The order was sent but never confirmed, so it may exist. Neither a
    #: success nor a failure: the tracker keeps it apart from both.
    unconfirmed: bool = False


# --- login -------------------------------------------------------------------
async def run_login_job(
    job_id: int, account_ids: list[int], *, force: bool = False
) -> None:
    """Generate a Flipkart session per account and store its cookies."""
    settings = _effective_settings()
    tracker = JobTracker(job_id)
    tracker.start(account_ids)
    # One browser for the whole run, one isolated window per account (PLAN.md
    # §10): it opens with the first account that asks and closes after the batch.
    pool = _pool_for(settings)

    async def worker(account_id: int, index: int) -> _Outcome:
        await tracker.item_started(account_id)
        # When this login began: a session saved after it is another sign-in
        # that finished meanwhile, which a failure here must not undo.
        began = utcnow()

        with _session() as db:
            account = db.get(Account, account_id)
            if account is None:
                return _Outcome(False, "account not found")
            if not force and session_store.has_valid_session(db, account):
                # Re-logging in a good session wastes a browser and risks the
                # account; `force` is the explicit opt-out.
                return _Outcome(False, "already has session", skipped=True)
            email = account.email

        with timing() as timer:
            async with browser_page(
                headless=settings.browser_headless,
                proxy=settings.proxy_server or None,
                pool=pool,
            ) as (_context, page):
                try:
                    cookies = await run_login(
                        page,
                        email,
                        get_otp_reader(settings),
                        window_wait=settings.fk_window_wait,
                        otp_wait=settings.otp_fetch_timeout,
                    )
                except NotRegistered:
                    not_registered = True
                    cookies = None
                    refused = None
                except LoginRefused as exc:
                    # Blocked, or too many attempts: said by Flipkart, so the
                    # account says it too (audit of 19 Nov 2026).
                    not_registered = False
                    cookies = None
                    refused = exc
                else:
                    not_registered = False
                    refused = None
                timer.finish("confirm signed in")
                if not cookies and not not_registered:
                    # The page as the sign-in left it: why it did not finish
                    # (an error on Flipkart's page, a captcha, a phone prompt).
                    from app.automation.ordering import _snapshot

                    await _snapshot(page, f"login-failed-account{account_id}")
        steps = timer.summary()
        if steps:
            log.info("sign-in timing for account %s: %s", account_id, steps)

        if not_registered:
            with _session() as db:
                account = db.get(Account, account_id)
                if account is not None:
                    account.status = AccountStatus.not_registered
                    db.commit()
            return _Outcome(
                False,
                _with_steps(
                    "this email is not registered on Flipkart — no OTP is sent. Use the email the "
                    "Flipkart account was made with, or sign up first",
                    steps,
                ),
            )

        if refused is not None:
            with _session() as db:
                account = db.get(Account, account_id)
                # Unless another sign-in finished meanwhile (forget_session
                # then keeps it and says so).
                if account is not None and session_store.forget_session(db, account, since=began):
                    account.status = AccountStatus(refused.status)
                    db.commit()
            label = "blocked" if refused.status == "blocked" else "too many sign-in attempts"
            return _Outcome(False, _with_steps(f"{label} — {refused.reason}; no session saved", steps))

        if not cookies:
            # run_login returns nothing when Flipkart still serves the login
            # page afterwards. Saving anyway would mark the account
            # session_valid on a logged-out browser, so record the failure and
            # mark it invalid instead — a wrong green here hides itself until
            # a coupon or order run fails for no visible reason. A cookie set
            # it held goes too: no run is to load it (session_store.forget_session).
            with _session() as db:
                account = db.get(Account, account_id)
                if account is not None:
                    session_store.forget_session(db, account, since=began)
            return _Outcome(False, _with_steps("login not completed — no session saved", steps))

        with _session() as db:
            account = db.get(Account, account_id)
            if account is None:  # deleted mid-run
                return _Outcome(False, "account not found")
            session_store.save_session(db, account, cookies)
        return _Outcome(True, _with_steps(f"session saved ({len(cookies)} cookies)", steps))

    async with pool:
        await _run_batch(tracker, account_ids, worker, settings.max_concurrency)
    await tracker.finish()


# --- coupons -----------------------------------------------------------------
async def run_coupon_job(job_id: int, account_ids: list[int], coupon_code: str) -> None:
    """Claim one coupon on each account's existing session."""
    settings = _effective_settings()
    tracker = JobTracker(job_id)
    tracker.start(account_ids)
    # One browser for the whole run, one isolated window per account (PLAN.md
    # §10): it opens with the first account that asks and closes after the batch.
    pool = _pool_for(settings)

    async def worker(account_id: int, index: int) -> _Outcome:
        await tracker.item_started(account_id)

        with _session() as db:
            account = db.get(Account, account_id)
            if account is None:
                return _Outcome(False, "account not found")
            cookies = session_store.load_cookies(db, account)
        if not cookies:
            return _Outcome(False, "no session", skipped=True)

        failure: str | None = None
        try:
            async with browser_page(
                headless=settings.browser_headless,
                mobile=True,  # the legacy coupon flow is mobile-only
                proxy=settings.proxy_server or None,  # the address the session was made from (audit F-13)
                cookies=cookies,
                pool=pool,
            ) as (_context, page):
                result = await apply_coupon(
                    page, coupon_code, result_wait=settings.coupon_wait
                )
        except Exception as exc:  # noqa: BLE001 - still worth an audit row
            log.exception("coupon run failed for account %s", account_id)
            result = COUPON_ERROR
            failure = _redact(str(exc) or exc.__class__.__name__)

        with _session() as db:
            db.add(
                CouponRun(
                    account_id=account_id, coupon_code=coupon_code, result=result
                )
            )
            db.commit()

        ok = result != COUPON_ERROR
        message = f"coupon {coupon_code}: {result}"
        if failure:
            message = f"{message} ({failure})"
        return _Outcome(ok, message)

    async with pool:
        await _run_batch(tracker, account_ids, worker, settings.max_concurrency)
    await tracker.finish()


# --- balances ----------------------------------------------------------------
async def run_balances_job(job_id: int, account_ids: list[int]) -> None:
    """Read each account's SuperCoins and gift card balance (read-only)."""
    from app.automation.balances import read_balances

    settings = _effective_settings()
    tracker = JobTracker(job_id)
    tracker.start(account_ids)
    pool = _pool_for(settings)

    async def worker(account_id: int, index: int) -> _Outcome:
        await tracker.item_started(account_id)
        with _session() as db:
            account = db.get(Account, account_id)
            if account is None:
                return _Outcome(False, "account not found")
            cookies = session_store.load_cookies(db, account)
        if not cookies:
            return _Outcome(False, "no session", skipped=True)
        try:
            async with browser_page(
                headless=settings.browser_headless,
                proxy=settings.proxy_server or None,
                cookies=cookies,
                pool=pool,
            ) as (_context, page):
                found = await read_balances(page)
        except Exception as exc:  # noqa: BLE001
            log.exception("balance read failed for account %s", account_id)
            return _Outcome(False, f"could not read balances ({_redact(str(exc) or exc.__class__.__name__)})")
        if found.supercoins is None and found.gift_card is None:
            return _Outcome(False, "the balance pages showed no balance (signed out?)")
        with _session() as db:
            account = db.get(Account, account_id)
            if account is not None:
                if found.supercoins is not None:
                    account.supercoins_balance = found.supercoins
                if found.gift_card is not None:
                    account.gift_voucher_balance = found.gift_card
                account.balances_read_at = utcnow()
                db.commit()
        coins = "?" if found.supercoins is None else str(found.supercoins)
        gift = "?" if found.gift_card is None else f"₹{found.gift_card:,.0f}"
        return _Outcome(True, f"SuperCoins {coins} · gift card {gift}")

    async with pool:
        await _run_batch(tracker, account_ids, worker, settings.max_concurrency)
    await tracker.finish()


# --- ordering ----------------------------------------------------------------
class DestinationError(RuntimeError):
    """The run cannot say where it ships: a chosen address/GST id is not in the
    address book, or no address was chosen and none is marked default.

    Fatal for the whole run on purpose (ROADMAP §8). The alternative — quietly
    using some other address — still places every order and still reports
    success, it just ships to the wrong entity and bills it under the wrong
    GST. That silent wrong answer is the failure this phase exists to prevent.
    """


@dataclass(frozen=True)
class _Destination:
    """Where one run ships and which GST it bills under.

    Resolved once per run rather than per account: every order in a batch must
    go to the same place, and re-reading the address book per account would let
    an edit mid-run split one batch across two entities.

    Always an Address Book row, which is precise enough to be added to a
    Flipkart account — so the run makes Flipkart deliver to it rather than to
    whatever address the account happened to have selected. ``phone`` is only
    the fallback — see ``address_for``. ``gst`` is the run's GST, which an
    account with a GST of its own does not use — see ``gst_for``.
    """

    name: str
    pin: str
    area: str
    phone: str
    gst: GstInfo
    line1: str = ""
    floor: str = ""
    city: str = ""
    state: str = ""

    def address_for(self, account_phone: str) -> AddressInfo:
        """Checkout address for one account: its own phone first, ours after.

        Pre-existing behaviour, kept deliberately: the account's phone is the
        number that receives the delivery OTP, so it wins when present. The
        account's phone is its billing phone when it has one, else its own
        (``account_billing``).
        """
        return AddressInfo(
            name=self.name,
            phone=account_phone or self.phone,
            pin=self.pin,
            area=self.area,
            line1=self.line1,
            floor=self.floor,
            city=self.city,
            state=self.state,
        )

    def gst_for(self, account_gst: GstInfo | None) -> GstInfo:
        """The GST one account's order bills under: its own when it has one,
        the run's otherwise.

        The account's wins over the run's, however the run's was reached
        (chosen, the address's, the default): the run's is one registration
        for every account in it, and an account of another entity ordered
        under it bills that entity's purchase to the wrong registration. This
        is also the GST ``run_order`` holds the checkout to before paying.
        """
        return account_gst if account_gst is not None and account_gst.gstin else self.gst


def account_billing(account: Account) -> tuple[str, GstInfo | None]:
    """The phone and the GST of the account's own that one order uses.

    The phone is the account's billing phone, else its own phone, else ""
    (``_Destination.address_for`` then falls back to the address's). The GST
    is the account's own as a ``GstInfo``, or None when it has none, which
    leaves the run's (``_Destination.gst_for``). Read while the account's
    session is open: the GST is a relationship, loaded on first use.
    """
    phone = account.billing_phone or account.phone or ""
    gst = account.gst
    own = None if gst is None else GstInfo(gstin=gst.gstin, business_name=gst.business_name)
    return phone, own


async def run_order_job(
    job_id: int,
    account_ids: list[int],
    *,
    product_link: str,
    qty: int = 1,
    target: float | None = None,
    cards: dict[int, CardInfo] | None = None,
    payment_mode: str = PAYMENT_CARD,
    address_id: int | None = None,
    gst_id: int | None = None,
    total_quantity: int | None = None,
    qty_per_order: int = 1,
    total_attempts: int | None = None,
    cart_amount_limit: float | None = None,
    max_delivery_days: int | None = None,
    brand: str | None = None,
    category: str | None = None,
    # Needed for model-scoped caps ("5 of T5x 5G per 30 days"). Order.model is
    # only known after the page is scraped, so the caller must say up front what
    # is being bought or a model limit can never fire before the order is placed.
    model: str | None = None,
    # Apparel shows no buy control until a size is picked.
    size: str | None = None,
    address_match: str | None = None,
    # Buy from this seller rather than the product page's default; None leaves
    # the choice to Flipkart. Handed to run_order as it is.
    seller: str | None = None,
    # A person at the computer finishes the bank's step after Pay (POST
    # /jobs/order ``bank_step_by_hand``): see the docstring.
    bank_step_by_hand: bool = False,
    # Mock mode (app.automation.mock_ordering): every step up to the browser
    # runs as usual, then a made-up outcome stands in for Flipkart. No browser
    # opens, nothing is bought and no order row is written.
    mock: bool = False,
    # "upto" or "minimum" (app.automation.ordering.QTY_MODES).
    qty_mode: str = QTY_MODE_UPTO,
    # Refuse an order whose checkout does not confirm its GST (run_order).
    gst_mandatory: bool = False,
    # Phone numbers for the delivery address, used in turn by the run's
    # accounts in their order (the first account the first number, and so on,
    # starting over when they run out). Empty keeps the account's own phone.
    address_phones: list[str] | None = None,
    # Every order uses the address book's own phone, not the account's.
    use_address_phone: bool = False,
    # Pass over accounts with a bought order in the last N days (None/0: off).
    exclude_ordered_days: int | None = None,
    # Try never-ordered accounts first, then those that ordered longest ago.
    smart_selection: bool = False,
    # Card pool rules (app.core.card_pool): cooldown, least used first, a
    # hard cap per card, and skipping cards that failed lately. Card runs only.
    card_rules: CardRules | None = None,
    # AI mode (app.jobs.pacing): go in paced batches instead of all at once.
    pacing: Pacing | None = None,
    # The order form's Ref Code, written on every order row of the run.
    ref_code: str | None = None,
    # The order form's "Order execution version" (app.jobs.pacing).
    execution_version: str | None = None,
    # Use the account's SuperCoins (only accounts with at least
    # ``min_supercoins`` by their last read balance) and/or gift card balance.
    use_supercoins: bool = False,
    min_supercoins: int | None = None,
    use_wallet: bool = False,
    # "+ Add product URL": more products bought in the same order.
    extra_links: list[str] | None = None,
    # Gift cards per account for a Gift Voucher run, in memory only.
    gift_cards: dict[int, list] | None = None,
    # The order form's Coupon Codes, claimed on each account before its order.
    coupon_codes: list[str] | None = None,
) -> None:
    """Order across accounts, each order gated on ``amount <= target``.

    ``cards`` is per-run, in-memory only (PLAN.md §5). On a card run an account
    without a card is skipped rather than charged with somebody else's. On a
    Cash-on-Delivery run no card is needed or looked at.

    ``address_id``/``gst_id`` pick the delivery address and the GST to bill
    under; omitting them uses the Address Book's default rows (see
    ``resolve_destination``). An id that does not exist — or no address at
    all — fails the run before any account is touched.

    **How much to buy (ROADMAP §9).** With ``total_quantity`` unset this places
    exactly one order of ``qty`` units per account — the behaviour every caller
    had before this phase, unchanged. Set it, and the run instead keeps placing
    orders of ``qty_per_order`` units until that many units are placed, taking
    as many accounts as it needs and no more: ``qty`` is then ignored in favour
    of ``qty_per_order``. ``total_attempts`` caps how many accounts may be tried
    at all. ``brand``/``category`` classify what is being bought — recorded on
    every order, and matched against each account's platform unit limits before
    its quota is spent.

    ``cart_amount_limit`` and ``max_delivery_days`` ARE enforced: both are
    checked on the checkout page before any card is entered, and both fail
    closed — a value that cannot be read refuses the order rather than letting
    it through (see ``run_order``).

    ``bank_step_by_hand`` hands the bank's step to a person at the computer the
    tool runs on. This run's browser window is shown whatever the Settings page
    says, the accounts go one at a time so the person meets one bank page at
    once, and ``run_order`` gets each card without its bank password, so it
    types nothing on the bank's page: the person enters the password or the
    one-time code there, within the bank's allowance (``bank_otp_fill_wait``).
    """
    settings = _effective_settings()
    if mock:
        # No bank page in a mock run, so nothing for a person to finish.
        bank_step_by_hand = False
    if bank_step_by_hand:
        # This run only: every other run keeps the Settings page's choice.
        settings.browser_headless = False
    recent: set[int] = set()
    if smart_selection or exclude_ordered_days:
        with _session() as db:
            if smart_selection:
                account_ids = least_recently_ordered_first(db, account_ids)
            if exclude_ordered_days:
                recent = recently_ordered(db, account_ids, exclude_ordered_days)
    card_rules = card_rules or CardRules()
    card_blocked: dict[int, str] = {}
    card_caps: dict[str, int] = {}
    if cards and card_rules.any():
        with _session() as db:
            card_plan = plan_cards(db, account_ids, cards, card_rules)
        account_ids, card_blocked, card_caps = card_plan.order, card_plan.blocked, card_plan.caps
    card_cap = CapCounter(card_rules.max_uses, per_card=card_caps)
    tracker = JobTracker(job_id)
    tracker.start(account_ids)
    # One browser for the whole run, one isolated window per account (PLAN.md
    # §10): it opens with the first account that asks and closes after the batch.
    # A person looks at a by-hand run's windows, so their pictures stay on.
    pool = _pool_for(settings, lean=not bank_step_by_hand)
    cards = cards or {}

    try:
        destination = resolve_destination(address_id=address_id, gst_id=gst_id)
    except DestinationError as exc:
        # Before any browser, any card lookup and any order. Items stay pending
        # because no account was attempted — the reason belongs to the run.
        reason = str(exc)
        log.error("order job %s refused to start: %s", job_id, reason)
        _note_on_job(job_id, reason)
        await tracker.finish(JobStatus.failed)
        return

    if mock:
        _note_on_job(
            job_id,
            "MOCK run — no browser opens and nothing is bought; each order's outcome is made up.",
        )

    budget = (
        None if total_quantity is None else UnitBudget(total_quantity, qty_per_order)
    )
    # Built once per run: every account orders the same product, so what a limit
    # matches on cannot differ between accounts. Built for EVERY run, with or
    # without a total quantity: a run without one used to skip the unit limits
    # altogether (audit of 19 Nov 2026). ``model`` is the product's title as
    # the order form's preview read it; a model limit matches when it is inside.
    facts = ProductFacts(brand=brand, category=category, model=model, seller=seller)
    if not (brand or category or model):
        # Unclassified, so only limits that cover every value in their scope can
        # match. Said out loud because the run otherwise *looks* limit-checked
        # while a "vivo 16 / 31 days" cap sits there matching nothing.
        _note_on_job(
            job_id,
            "no brand or category given — only unit limits that cover every "
            "value in their scope can apply to this run.",
        )
    #: Why accounts were passed over, for the one summary line at the end.
    skips: Counter[str] = Counter()
    #: Units submitted but never confirmed. The budget keeps them spent, so the
    #: summary names them apart rather than calling them placed.
    unconfirmed: Counter[str] = Counter()
    #: Orders that used their account's own GST, by GSTIN: the run shows one
    #: GST, so the job says which orders did not use it.
    own_gst: Counter[str] = Counter()

    async def worker(account_id: int, index: int) -> _Outcome:
        # The hard cap per card holds a use while the order runs, and gives it
        # back when nothing was bought.
        held: list[str] = []
        outcome = await attempt(account_id, index, held)
        if held and not (outcome.ok or outcome.unconfirmed):
            card_cap.release(held[0])
        return outcome

    async def attempt(account_id: int, index: int, held: list[str]) -> _Outcome:
        await tracker.item_started(account_id)

        # The attempt cap is read off the account's position in the run, not off
        # a shared counter: the accounts run concurrently, so a counter would
        # make *which* accounts get tried depend on how the workers interleave,
        # and the same selection would cover different accounts each run.
        if account_id in recent:
            skips["ordered recently"] += 1
            return _Outcome(
                False, f"ordered in the last {exclude_ordered_days} day(s) — passed over", skipped=True
            )

        if total_attempts is not None and index >= total_attempts:
            skips["attempt limit"] += 1
            return _Outcome(
                False, f"attempt limit reached ({total_attempts} accounts)", skipped=True
            )

        with _session() as db:
            account = db.get(Account, account_id)
            if account is None:
                return _Outcome(False, "account not found")
            cookies = session_store.load_cookies(db, account)
            phone, account_gst = account_billing(account)
            coins = account.supercoins_balance
        if use_supercoins and min_supercoins and (coins is None or coins < min_supercoins):
            skips["below minimum SuperCoins"] += 1
            return _Outcome(
                False,
                f"SuperCoins {('not read yet' if coins is None else coins)} — below the minimum of {min_supercoins}",
                skipped=True,
            )
        if not cookies:
            skips["no session"] += 1
            return _Outcome(False, "no session", skipped=True)

        # Cash on Delivery needs no card, so a missing one must not skip the
        # account the way it does on a card run.
        card = None
        account_gift_cards = None
        if payment_mode in GIFT_CARD_MODES and not mock:
            account_gift_cards = (gift_cards or {}).get(account_id)
            if not account_gift_cards:
                skips["no gift card"] += 1
                return _Outcome(False, "no gift card supplied", skipped=True)
        if payment_mode in CARD_MODES and not (mock and not cards):
            card = cards.get(account_id)
            if card is None:
                skips["no card"] += 1
                return _Outcome(False, "no card supplied", skipped=True)
            if account_id in card_blocked:
                skips["card rule"] += 1
                return _Outcome(False, f"card not used: {card_blocked[account_id]}", skipped=True)
            if not card_cap.claim(card.number):
                skips["card cap"] += 1
                return _Outcome(
                    False,
                    f"card not used: it already pays for {card_cap.limit(card.number)} order(s) in this run",
                    skipped=True,
                )
            held.append(card.number)
            if bank_step_by_hand and card.password:
                # The person types it on the bank's page: run_order types a
                # password only when the card carries one. A copy, so the card
                # handed in is left as it was.
                card = replace(card, password="")

        # One account, one run (app.jobs.account_guard): a read-back reading
        # this account stops at its next page once it sees this item running,
        # and the order waits for it — before any claim, so a wait spends nothing.
        # The wait ends early for the operator's cancel and for the API
        # stopping: an order begun after a stop would be cut off on the way out.
        # A mock order opens no page, so it has nothing to wait for.
        note = None if mock else await wait_for_read_back(
            account_id, should_cancel=lambda: tracker.is_cancel_requested() or is_stopping()
        )
        if note is not None:
            _note_on_job(job_id, note)
            if tracker.is_cancel_requested():
                return _Outcome(False, "cancelled", skipped=True)
            if is_stopping():
                return _Outcome(False, STOPPED_NOT_STARTED, skipped=True)

        # How many units this one order carries, and the budget slot they came
        # from. The quota check comes first and the claim second, so an account
        # the platform has already capped never eats into the run's target; the
        # gates above come first still, so a claim is never spent on an account
        # that was never going to order.
        units = qty
        claimed: int | None = None
        if budget is None:
            # No total quantity: one order of ``qty`` per account, still inside
            # the account's unit limits (a run without a total used to skip
            # them altogether — audit of 19 Nov 2026).
            allowed = _unit_allowance_for(account_id, facts)
            if allowed is not None and allowed <= 0:
                skips["unit limit"] += 1
                return _Outcome(False, _limit_blocked_message(account_id, facts), skipped=True)
            if allowed is not None and allowed < qty:
                if qty_mode == QTY_MODE_MINIMUM:
                    skips["unit limit"] += 1
                    return _Outcome(
                        False,
                        f"unit limit leaves {allowed} of the {qty} unit(s) this order needs "
                        "(Minimum mode) — passed over",
                        skipped=True,
                    )
                units = allowed
        if budget is not None:
            if budget.remaining == 0:
                # Checked before the quota query, not after: a run for 10 units
                # over a 5000-account selection would otherwise spend a round
                # trip per account to be told the same thing ``claim`` below
                # tells it for free.
                skips["target reached"] += 1
                return _Outcome(False, "target quantity reached", skipped=True)
            allowed = _unit_allowance_for(account_id, facts)
            if allowed is not None and allowed <= 0:
                skips["unit limit"] += 1
                return _Outcome(
                    False, _limit_blocked_message(account_id, facts), skipped=True
                )
            claimed = budget.claim(limit=allowed)
            if claimed is None:
                skips["target reached"] += 1
                return _Outcome(False, "target quantity reached", skipped=True)
            units = claimed

        address = destination.address_for(
            pick_address_phone(index, phone, address_phones, use_address_phone)
        )
        # The account's own GST when it has one: it goes to run_order, whose
        # checkout check then holds the page to it, and onto the order row.
        gst = destination.gst_for(account_gst)
        if gst is account_gst:
            own_gst[gst.gstin] += 1

        columns = dict(
            product_link=product_link,
            qty=units,
            target=target,
            address=address,
            gst=gst,
            brand=brand,
            category=category,
            model=model,
            job_id=job_id,
            # The number only (the owner's decision of 25 Oct 2026): the
            # expiry and the CVV stay in memory and go nowhere.
            card_number=card.number if card is not None else None,
            ref_code=ref_code,
        )
        if mock:
            # Instead of the browser: a made-up outcome, and no order row (a
            # fake order in the ledger would count against unit limits, raise
            # alerts and be read back from Flipkart). The budget is settled as
            # for a real order, so a mock run stops where a real one would.
            result = await mock_order(
                job_id=job_id,
                account_id=account_id,
                qty=units,
                target=target,
                payment_mode=payment_mode,
                delay_seconds=float(getattr(settings, "mock_order_seconds", 0) or 0),
            )
            status = result["status"]
            _settle_claim(budget, claimed, result["qty"] if status == OrderStatus.placed else 0)
            return _Outcome(status == OrderStatus.placed, mock_message(result))

        #: The row written before the order was sent, if the run got that far.
        reserved: list[dict] = []

        def reserve(row: dict) -> int:
            row_id = reserve_order(account_id, row, **columns)
            reserved.append({**row, "id": row_id})
            return row_id

        result: dict | None = None
        # The order form's Coupon Codes: claimed on this account just before
        # its order (the same claim as the Coupons page), so the checkout
        # sees them. A coupon that does not take never stops the order.
        if coupon_codes and not mock:
            for code in coupon_codes:
                # Not ``claimed``: that name holds this order's budget claim,
                # and overwriting it lost the units (audit of 19 Nov 2026).
                coupon_result = COUPON_ERROR
                try:
                    async with browser_page(
                        headless=settings.browser_headless,
                        mobile=True,
                        proxy=settings.proxy_server or None,
                        cookies=cookies,
                        pool=pool,
                    ) as (_c, coupon_page):
                        coupon_result = await apply_coupon(coupon_page, code, result_wait=settings.coupon_wait)
                except Exception as exc:  # noqa: BLE001
                    log.warning("coupon %s on account %s failed: %s", code, account_id, exc.__class__.__name__)
                with _session() as db:
                    db.add(CouponRun(account_id=account_id, coupon_code=code, result=coupon_result))
                    db.commit()
        order_timer = timing()
        timer = order_timer.__enter__()
        try:
            async with browser_page(
                headless=settings.browser_headless,
                proxy=settings.proxy_server or None,  # the address the session was made from (audit F-13)
                cookies=cookies,
                pool=pool,
            ) as (_context, page):
                result = await run_order(
                    page,
                    product_link=product_link,
                    address=address,
                    gst=gst,
                    card=card,
                    qty=units,
                    target=target,
                    payment_mode=payment_mode,
                    cart_amount_limit=cart_amount_limit,
                    max_delivery_days=max_delivery_days,
                    size=size,
                    address_match=address_match,
                    seller=seller,
                    qty_mode=qty_mode,
                    gst_mandatory=gst_mandatory,
                    use_supercoins=use_supercoins,
                    use_wallet=use_wallet,
                    extra_links=extra_links,
                    gift_cards=account_gift_cards,
                    reserve=reserve,
                    # Always an Address Book row: make Flipkart deliver there.
                    ensure_address=True,
                )
                if (result.get("status") or OrderStatus.failed) == OrderStatus.failed:
                    # The page as it was when the flow gave up, beside the
                    # unconfirmed snapshots it already keeps: the one thing
                    # that explains a failure after the browser is gone.
                    from app.automation.ordering import _snapshot

                    log.error(
                        "order for account %s not placed at %s: %s",
                        account_id,
                        (page.url or "")[:120],
                        _redact(str(result.get("error") or "no reason given")),
                    )
                    await _snapshot(page, f"failed-account{account_id}")
        except Exception as exc:  # noqa: BLE001 - record the attempt, keep the batch alive
            # Deliberately NOT log.exception: a driver message could echo a
            # value we filled into the card form, and a traceback would put it
            # in the application log (PLAN.md §5). Log the redacted reason only.
            reason = _redact(str(exc) or exc.__class__.__name__)
            if result is not None:
                # run_order finished and the browser then failed to CLOSE. The
                # result stands: discarding it would record a placed order as
                # failed and hand its units to the next account.
                log.warning("browser close failed for account %s: %s", account_id, reason)
            elif reserved:
                # The order was recorded and then something broke before the
                # flow could say how it ended — possibly AFTER the order was
                # sent. The unconfirmed row stands, and its units stay spent.
                log.error("order for account %s ended unconfirmed: %s", account_id, reason)
                _settle_claim(budget, claimed, None)
                unconfirmed["orders"] += 1
                unconfirmed["units"] += units
                return _Outcome(
                    False,
                    _order_message(OrderStatus.unconfirmed, reserved[-1], target),
                    unconfirmed=True,
                )
            else:
                log.error("order failed for account %s: %s", account_id, reason)
                _note_card_failure(card, reason, job_id)
                _settle_claim(budget, claimed, 0)
                save_order_result(
                    account_id,
                    {"status": OrderStatus.failed, "payment_mode": payment_mode},
                    **columns,
                )
                return _Outcome(False, f"order failed: {reason}")

        timer.finish("place & confirm")
        order_timer.__exit__(None, None, None)
        steps = timer.summary()
        if steps:
            log.info("order timing for account %s: %s", account_id, steps)
        status = result.get("status") or OrderStatus.failed
        if status == OrderStatus.failed:
            _note_card_failure(card, _redact(str(result.get("error") or "")), job_id)
        # Units go back only when nothing was sent to Flipkart (over target, or
        # the flow stopped short). An unconfirmed order may exist, so it keeps
        # them: releasing would let another account buy the same units again.
        _settle_claim(
            budget,
            claimed,
            result.get("qty") if status in UNITS_SPENT_STATUSES else 0,
        )
        # Counted before the save: the units are already kept as spent above,
        # so a save that fails must not also hide them from the summary.
        if status == OrderStatus.unconfirmed:
            unconfirmed["orders"] += 1
            unconfirmed["units"] += int(result.get("qty") or units)
        try:
            record_order_result(account_id, result, **columns)
        except Exception as exc:  # noqa: BLE001 - the outcome is told all the same
            # Flipkart has the order or it has not, whatever the database did.
            # Raised from here, a placed order would be reported as a plain
            # failure with its number lost, and a failure invites the retry
            # that buys twice (review of 27 Oct 2026). The units stay spent
            # (above), and the next read-back records the order.
            saving = _redact(str(exc) or exc.__class__.__name__)
            log.error(
                "order for account %s ended %s, but saving it failed: %s",
                account_id,
                getattr(status, "value", status),
                saving,
            )
            if status == OrderStatus.placed:
                # Not written down as placed, so counted with the unconfirmed.
                unconfirmed["orders"] += 1
                unconfirmed["units"] += int(result.get("qty") or units)
            return _Outcome(
                False,
                _unsaved_order_message(status, result, target, saving),
                unconfirmed=status in UNITS_SPENT_STATUSES,
            )
        ok = status == OrderStatus.placed
        return _Outcome(
            ok,
            _with_steps(_order_message(status, result, target), steps),
            unconfirmed=status == OrderStatus.unconfirmed,
        )

    async with pool:
        # By hand, one bank page at a time: two windows waiting at once would
        # leave the person to work out which card each one is for.
        at_once = 1 if bank_step_by_hand else accounts_at_once(execution_version, settings.max_concurrency)
        if pacing is None:
            await _run_batch(tracker, account_ids, worker, at_once)
        else:
            await _run_paced_order_batches(
                tracker, account_ids, worker, at_once, pacing, budget, job_id
            )
    if budget is not None:
        _log_quantity_summary(job_id, budget, skips, unconfirmed["units"])
    elif unconfirmed["orders"]:
        # A run without a target quantity has no summary line, and its tracker
        # reads an unconfirmed order as a plain failure. Say what it really is.
        _note_on_job(
            job_id,
            f"{unconfirmed['orders']} order(s) UNCONFIRMED — submitted, but Flipkart "
            "never confirmed them, so they may exist. Check those accounts before "
            "ordering on them again.",
        )
    if own_gst:
        _note_own_gst(job_id, own_gst, destination.gst)
    await tracker.finish()


async def _run_paced_order_batches(
    tracker: JobTracker,
    account_ids: list[int],
    worker,
    at_once: int,
    pacing: Pacing,
    budget: UnitBudget | None,
    job_id: int,
) -> None:
    """An AI run: the accounts in paced batches (app.jobs.pacing). Accounts
    the run never reached are closed as skipped, with the reason."""

    async def run_batch(batch: list[int], offset: int) -> int:
        results = await _run_batch(tracker, batch, lambda account_id, i: worker(account_id, offset + i), at_once)
        return sum(1 for r in results if r.ok and getattr(r.value, "ok", False))

    def should_stop() -> str | None:
        if tracker.is_cancel_requested():
            return "cancelled"
        if is_stopping():
            return STOPPED_NOT_STARTED
        if budget is not None and budget.remaining == 0:
            return "the target quantity was reached"
        return None

    _note_on_job(
        job_id,
        f"AI pacing: batches of {pacing.min_batch} to start, scaling up when "
        f"{pacing.scale_up_pct}% of a batch places, at most {pacing.max_interval_s / 60:g} min "
        f"between batches, for up to {pacing.run_seconds / 3600:g} h.",
    )
    outcome = await run_paced(account_ids, run_batch, pacing, should_stop=should_stop)
    _note_on_job(job_id, f"AI batches: {', '.join(str(n) for n in outcome.batches) or 'none'}")
    if outcome.started < len(account_ids):
        reason = outcome.stopped_because or "stopped"
        for account_id in account_ids[outcome.started :]:
            await tracker.item_finished(account_id, ok=False, message=f"not tried: {reason}", skipped=True)
        _note_on_job(
            job_id, f"AI run stopped ({reason}); {len(account_ids) - outcome.started} account(s) not tried."
        )


def _with_steps(message: str, steps: str) -> str:
    """An account's outcome, with how long each step took after it."""
    return f"{message} — ⏱ {steps}" if steps else message


def _note_card_failure(card: CardInfo | None, reason: str, job_id: int) -> None:
    """Remember that a card's order failed (card pool rules), by fingerprint
    only. Never lets a bookkeeping error change the order's outcome."""
    if card is None or not is_payment_failure(reason):
        return
    try:
        with _session() as db:
            record_card_failure(db, card.number, reason, job_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not note a card failure: %s", exc.__class__.__name__)


def pick_address_phone(
    index: int, account_phone: str, phones: list[str] | None, use_address_phone: bool
) -> str:
    """The phone one order's delivery address carries.

    Numbers the run was given go first, one per account in turn. Otherwise
    "use the address's phone" gives "" (``address_for`` then falls back to the
    address book's number), and by default the account's own phone stays.
    """
    if phones:
        return phones[index % len(phones)]
    if use_address_phone:
        return ""
    return account_phone


# --- how many units (ROADMAP §9) ---------------------------------------------
def _unit_allowance_for(account_id: int, facts: ProductFacts) -> int | None:
    """Units this account may still buy under the run's limits; None = no cap.

    Opens its own short session like every other DB touch in a worker: the
    accounts run concurrently and a ``Session`` is not task-safe.
    """
    with _session() as db:
        return unit_allowance(db, account_id, facts)


def _limit_blocked_message(account_id: int, facts: ProductFacts) -> str:
    """Why an account was passed over, naming the cap that bit.

    The operator reads this in the job transcript next to the account, so
    "skipped" on its own is not an answer — they would have to go limit-hunting
    to find out whether the run was right. Worth the one extra query: it is only
    paid on the rare path where an account is actually blocked.
    """
    with _session() as db:
        limit = blocking_limit(db, account_id, facts)
        # Described inside the session: the row is detached once it closes.
        named = "" if limit is None else describe_limit(limit)
    if not named:
        # The allowance said 0 but no limit can be named — the row was edited
        # between the two reads. Fall back to what this run is buying, so the
        # line still means something.
        named = _facts_summary(facts) or "a unit limit"
    return f"unit limit reached — {named}, nothing left in its window"


def _facts_summary(facts: ProductFacts) -> str:
    """What this run is buying, in the operator's words."""
    return ", ".join(
        f"{scope} {value}"
        for scope, value in (
            ("brand", facts.brand),
            ("category", facts.category),
            ("model", facts.model),
        )
        if value
    )


#: Outcomes after which the order may exist on Flipkart, so its units stay spent.
UNITS_SPENT_STATUSES: tuple[OrderStatus, ...] = (OrderStatus.placed, OrderStatus.unconfirmed)


def _settle_claim(
    budget: UnitBudget | None, claimed: int | None, bought: int | None
) -> None:
    """Give back the part of a claim the order did not actually buy.

    ``bought`` is what the flow says went into the cart; ``None`` means it did
    not say, and we then assume the whole claim was used — guessing the other
    way would free units that *were* bought and let the run overshoot.
    """
    if budget is None or claimed is None:
        return
    used = claimed if bought is None else max(0, int(bought))
    if used < claimed:
        budget.release(claimed - used)


def _log_quantity_summary(
    job_id: int, budget: UnitBudget, skips: Counter, unconfirmed_units: int = 0
) -> None:
    """One line on whether the run hit its target, and what it passed over.

    On the job as well as in the log: a run that stops at 6 of 10 units is not
    a failure anywhere the tracker can show it, so without this the operator
    has no way to see that it fell short or why.

    Unconfirmed units stay spent in the budget (they may exist) but are not
    called placed: the line says how many, and that someone has to check.
    """
    reasons = (
        ", ".join(f"{count} {reason}" for reason, count in sorted(skips.items()))
        or "none"
    )
    unsure = (
        f"; {unconfirmed_units} more submitted but UNCONFIRMED — check those "
        "accounts before retrying"
        if unconfirmed_units
        else ""
    )
    line = (
        f"placed {budget.placed - unconfirmed_units} of {budget.total} unit(s) requested "
        f"({budget.per_order} per order){unsure}; accounts skipped: {reasons}"
    )
    log.info("order job %s: %s", job_id, line)
    _note_on_job(job_id, line)


def _note_own_gst(job_id: int, own_gst: Counter, run_gst: GstInfo) -> None:
    """One line on how many orders used their account's own GST, and which.

    The run page names one GST for the whole run ("Where this run ships"), so
    without this line nothing on the run says that some of its orders billed
    elsewhere — each order row records its own GST, but the run is where the
    operator reads what happened.
    """
    total = sum(own_gst.values())
    which = ", ".join(f"{gstin} ({count})" for gstin, count in sorted(own_gst.items()))
    line = f"{total} order(s) used their account's own GST: {which}. The run's GST: {run_gst.gstin or 'none'}."
    log.info("order job %s: %s", job_id, line)
    _note_on_job(job_id, line)


def _unenforced_note(
    cart_amount_limit: float | None, max_delivery_days: int | None
) -> str:
    """Say out loud that a recorded limit is not yet applied.

    A cap the operator set and we silently ignore is the worst kind of bug here:
    the run succeeds, so nothing looks wrong until the invoices arrive.
    """
    asked = ", ".join(
        f"{name} {value}"
        for name, value in (
            ("cart amount limit", cart_amount_limit),
            ("max delivery days", max_delivery_days),
        )
        if value is not None
    )
    return (
        f"recorded but NOT enforced: {asked} — reading the cart total and the "
        f"delivery promise off the page needs a selector pass."
    )


def _order_message(
    status: OrderStatus, result: dict, target: float | None
) -> str:
    """Human-readable outcome. Never includes card data. Amounts read as on
    every screen: "₹18,207.00" (app.core.money, NFR-010)."""
    from app.core.money import rupees

    amount = float(result.get("amount") or 0.0)
    effective_target = result.get("target", target)
    if status == OrderStatus.over_target:
        shown = "no target" if effective_target is None else rupees(float(effective_target))
        unit = result.get("unit_amount")
        if unit is not None and abs(float(unit) - amount) > 0.005:
            # Per unit, and before any SuperCoins / wallet (audit of 19 Nov 2026).
            return f"over target: paid nothing, {rupees(float(unit))} a unit > {shown} a unit (order total {rupees(amount)})"
        return f"over target: paid nothing, {rupees(amount)} > {shown}"
    if status == OrderStatus.placed:
        order_id = result.get("order_id") or "unknown"
        return f"placed: order {order_id}, amount {rupees(amount)}{_quantity_note(result)}"
    if status == OrderStatus.unconfirmed:
        # Leads with the word the Progress view keys on, and says what to do:
        # this is the one outcome where retrying blindly can buy twice. The
        # flow's own account of where the browser was left follows (the first
        # live card run of 26 Oct 2026 ended here with that account dropped).
        reason = _redact(str(result.get("why_unconfirmed") or result.get("error") or "").strip())
        return (
            f"unconfirmed: submitted for {rupees(amount)} but Flipkart never confirmed it, "
            f"so it MAY exist — check this account's orders before retrying{_quantity_note(result)}"
            + (f" ({reason})" if reason else "")
        )
    # The flow says WHY it stopped ("no saved address matched…", "Flipkart is
    # blocking checkout…"): that reason is the one thing the operator needs
    # from a failed run, and the live run of 26 Oct 2026 showed it was dropped.
    reason = _redact(str(result.get("error") or "").strip())
    suffix = f": {reason}" if reason else ""
    return f"not placed ({getattr(status, 'value', status)}){suffix}"


def _unsaved_order_message(status: OrderStatus, result: dict, target: float | None, saving: str) -> str:
    """The outcome of an order that ended but could not be written down.

    One that was sent reads as unconfirmed, whatever Flipkart said: this
    database does not hold it as placed, and the operator must not buy again
    before the next read-back has recorded it. Anything else is its usual
    message, with the failed save beside it. Never includes card data."""
    from app.core.money import rupees

    if status == OrderStatus.placed:
        order_id = result.get("order_id") or "unknown"
        amount = float(result.get("amount") or 0.0)
        return (
            f"unconfirmed: placed on Flipkart as order {order_id} for {rupees(amount)}{_quantity_note(result)}, "
            f"but saving it here failed ({saving}) — do NOT order again for this account; the next read-back "
            "records it"
        )
    return f"{_order_message(status, result, target)} (saving it here failed: {saving})"


def _quantity_note(result: dict) -> str:
    """"; 1 unit — You can only purchase 1 units…" when the checkout kept
    fewer units than the run asked for. Flipkart's own wording, so the
    operator reads the reason rather than a silent smaller number."""
    note = result.get("qty_note")
    if not note:
        return ""
    units = int(result.get("qty") or 1)
    return f"; {units} unit{'' if units == 1 else 's'} — {note}"


#: ``Order.billing_phone``'s width. The phone at checkout may come from a wider
#: column (an account's or an address's own phone holds 32 characters), and an
#: order must never fail to be recorded over a column kept for the ledger: a
#: longer value is cut to fit. A real phone is far shorter.
_BILLING_PHONE_WIDTH = Order.__table__.c.billing_phone.type.length


def _order_columns(
    account_id: int,
    result: dict,
    *,
    product_link: str,
    qty: int,
    target: float | None,
    address: AddressInfo,
    gst: GstInfo,
    brand: str | None = None,
    category: str | None = None,
    model: str | None = None,
    job_id: int | None = None,
    card_number: str | None = None,
    ref_code: str | None = None,
) -> dict:
    """The ``Order`` columns one outcome maps to.

    Of the card, the number alone is kept (``card_number``, the owner's
    decision of 25 Oct 2026, so the ledger matches a card statement); the
    expiry and the CVV have no column and never will.

    ``gst`` is the GST this order used — the account's own or the run's — and
    ``address.phone`` the phone it was placed with, recorded as the ledger's
    Billing Phone.

    ``brand``/``category``/``model`` are stored on every order, placed or not,
    so a later unit-limit check is a ``SUM`` over this table rather than a
    re-scrape of the product page. The RUN's words win: a limit is written in
    them ("T5x 5G", "vivo"), and a cap matched against the product page's full
    title would never fire. The page's own reading is the fallback for runs
    that were not told what they were buying.
    """
    from app.core.order_outcomes import classify as classify_outcome

    status = result.get("status") or OrderStatus.failed
    status_val = getattr(status, "value", str(status))
    raw_reason = result.get("error") or result.get("why_unconfirmed")
    stage, code = classify_outcome(status_val, str(raw_reason) if raw_reason else None)
    return dict(
        account_id=account_id,
        job_id=job_id,
        **({"ref_code": ref_code} if ref_code else {}),
        **({"used_supercoins": True} if result.get("used_supercoins") else {}),
        **(
            {"supercoins_amount": float(result["supercoins_amount"])}
            if result.get("supercoins_amount")
            else {}
        ),
        product_link=product_link,
        qty=int(result.get("qty") or qty or 1),
        model=model or result.get("model"),
        brand=brand or result.get("brand") or None,
        category=category or result.get("category") or None,
        color=result.get("color"),
        pin=result.get("pin") or address.pin or None,
        amount=result.get("amount"),
        target=result.get("target", target),
        order_id=result.get("order_id"),  # normalised by the model on assignment
        gst_name=gst.business_name or None,
        gstin=(gst.gstin or None) if gst.business_name else None,
        billing_phone=(address.phone or "")[:_BILLING_PHONE_WIDTH] or None,
        status=status,
        processing_stage=result.get("processing_stage") or stage.value,
        error_code=(
            result.get("error_code")
            if "error_code" in result
            else (code.value if code is not None else None)
        ),
        payment_mode=result.get("payment_mode") or PAYMENT_CARD,
        # The number of the card an order was PAID with (the owner's decision of
        # 25 Oct 2026, for matching against statements). An order that was
        # never sent — over target, stopped before the payment page, a failure
        # before Pay — charged nothing, so its row keeps no card number: there
        # is no statement line to match, only a number to protect.
        # Every mode a card pays in (card, gift voucher + card, COD + card
        # advance), not only "card": a GV + card order lost its number (audit
        # of 19 Nov 2026).
        card_number=(
            card_number
            if (result.get("payment_mode") or PAYMENT_CARD) in CARD_MODES and status in UNITS_SPENT_STATUSES
            else None
        ),
        # Unconfirmed too: unit-limit windows are keyed on this, and an order
        # that may exist must count from when it was submitted.
        placed_at=utcnow() if status in UNITS_SPENT_STATUSES else None,
        # The seller the run chose, on the row from its reservation on. Only
        # when the outcome names one: a run that left the choice to Flipkart
        # writes nothing here, so a settle never wipes the seller a read-back
        # has filled in meanwhile — and a run that chose one does not write it
        # over a row a read-back has looked at (``settle_order_row``).
        **({"seller": str(result["seller"])[:255]} if result.get("seller") else {}),
    )


def save_order_result(account_id: int, result: dict, **columns) -> int:
    """Persist one order outcome as a new row; returns its id."""
    with _session() as db:
        row = Order(**_order_columns(account_id, result, **columns))
        db.add(row)
        db.commit()
        return row.id


def reserve_order(account_id: int, row: dict, **columns) -> int:
    """Record an order BEFORE it is sent, as ``unconfirmed``; returns the row id.

    ``run_order`` calls this through its ``reserve`` hook. If the run is cut
    short after the send, this row is what remains: counted by the unit limits,
    raised by the alerts, and visible to the next run.
    """
    return save_order_result(account_id, {**row, "status": OrderStatus.unconfirmed}, **columns)


def settle_order_row(row_id: int, account_id: int, result: dict, **columns) -> None:
    """Complete a reserved row with how the order actually ended."""
    values = _order_columns(account_id, result, **columns)
    with _session() as db:
        row = db.get(Order, row_id)
        if row is None:
            # The reservation vanished (a database restored from backup, say);
            # a result still has to land somewhere.
            log.error("reserved order row %s is gone; recording the result as a new row", row_id)
            db.add(Order(**values))
        else:
            if row.placed_at is not None and values["status"] in UNITS_SPENT_STATUSES:
                # The order's time is when it was submitted (the reservation),
                # not when its outcome was written down a minute later.
                values["placed_at"] = row.placed_at
            if (
                values["status"] == OrderStatus.unconfirmed
                and row.order_id
                and row.status in (OrderStatus.placed, OrderStatus.failed)
            ):
                # A read-back running beside this order found it on Flipkart
                # and settled this very row (its id; placed, or failed when
                # cancelled) while the run was still waiting for Flipkart's
                # confirmation page. The run's own "never confirmed" knows less
                # than that, so it does not send the row back to "may exist".
                log.info(
                    "order row %s was settled by a read-back meanwhile (%s, %s); keeping that over the run's "
                    "unconfirmed",
                    row_id,
                    row.order_id,
                    row.status.value,
                )
                values.pop("order_id", None)
                values.pop("status", None)
                values.pop("processing_stage", None)
                values.pop("error_code", None)
            if row.synced_at is not None:
                # A read-back has looked at this row (it stamps ``synced_at``
                # on every row it touches) and wrote the seller the order's own
                # page shows, if any. The run's chosen seller is who it asked
                # for, not who Flipkart sold from — written over that reading
                # it would hide the very switch the choice is meant to catch.
                values.pop("seller", None)
            for name, value in values.items():
                setattr(row, name, value)
        db.commit()


def record_order_result(account_id: int, result: dict, **columns) -> None:
    """Persist an outcome: onto its reserved row when there is one, else as new."""
    reservation = result.get("reservation")
    if isinstance(reservation, int):
        settle_order_row(reservation, account_id, result, **columns)
    else:
        save_order_result(account_id, result, **columns)


# --- choosing the address and the GST (ROADMAP §8) ---------------------------
#: Only the two address-book models are ever looked up by a chosen id.
_Row = TypeVar("_Row", Address, Gst)


def resolve_destination(
    *,
    address_id: int | None = None,
    gst_id: int | None = None,
) -> _Destination:
    """Pick this run's address and GST, once, before any account is touched.

    The address is the Address Book row the caller chose, else the row flagged
    ``is_default``. There is no third rung: the flat config block that used to
    sit there is how orders shipped to the wrong place quietly, so a run that
    cannot name where it ships does not run.

    The GST is the row the caller chose, else the GST the resolved address is
    linked to, else the row flagged ``is_default``, else none — the orders are
    then placed without GST details, and the log says so. The linked GST comes
    before the default on purpose: an operator who picks one entity's address
    and leaves the GST unset means that entity, not whichever one happens to
    be the default.

    That is the run's GST. An account with a GST of its own bills under its
    own instead, whichever rung the run's came from (``_Destination.gst_for``,
    applied per account by the worker).

    Raises ``DestinationError`` if an id *was* given and no such row exists, or
    if no address could be resolved at all.
    """
    with _session() as db:
        address = _chosen(db, Address, address_id, "address_id")
        if address is None:
            address = default_address(db)
        if address is None:
            raise DestinationError(
                "no delivery address: pick an Address Book address for this run, "
                "or mark one as the default, and start again. Nothing was ordered."
            )

        gst_row = _chosen(db, Gst, gst_id, "gst_id")
        source = "chosen"
        if gst_row is None and address.gst is not None:
            gst_row, source = address.gst, "linked to the address"
        if gst_row is None:
            gst_row, source = default_gst(db), "the default"

        # Read every column inside the session: once it closes the rows are
        # detached and any unloaded attribute would raise.
        if gst_row is None:
            gst = GstInfo(gstin="", business_name="")
            source = "none"
            log.warning(
                "no GST chosen, linked to address %r or marked default: the orders "
                "will be placed without GST details",
                address.label,
            )
        else:
            gst = GstInfo(gstin=gst_row.gstin, business_name=gst_row.business_name)
        destination = _Destination(
            name=address.name,
            pin=address.pincode,
            area=_compose_area(address),
            phone=address.phone or "",
            gst=gst,
            line1=(address.line1 or "").strip(),
            floor=(address.floor or "").strip(),
            city=(address.city or "").strip(),
            state=(address.state or "").strip(),
        )

    log.info(
        "order run will ship to %r (pin %s) under GST %s (%s); accounts with a GST of their own use theirs",
        destination.name,
        destination.pin,
        gst.gstin or "<none>",
        source,
    )
    return destination


def _chosen(
    db: DbSession, model: type[_Row], row_id: int | None, field: str
) -> _Row | None:
    """The row the caller asked for, or None if they asked for nothing."""
    if row_id is None:
        return None
    row = db.get(model, row_id)
    if row is None:
        raise DestinationError(
            f"{field} {row_id} does not exist — no order was placed, because "
            f"falling back would ship to a different address and GST."
        )
    return row


def _compose_area(address: Address) -> str:
    """The parts of a stored address as the one free-text line checkout takes.

    Blank parts are dropped so the field never carries a dangling comma.
    """
    parts = (address.line1, address.floor, address.city, address.state)
    return ", ".join(part.strip() for part in parts if part and part.strip())


# --- shared plumbing ---------------------------------------------------------
async def _run_batch(
    tracker: JobTracker,
    account_ids: list[int],
    worker,
    limit: int,
) -> list[ItemResult]:
    """Fan out one batch, reporting every settled item to the tracker.

    No account starts once the job is cancelled or the API process has begun
    to stop (audit F-32): an account begun then would be cut off by the exit,
    mid-order perhaps. The accounts already started finish either way.
    """
    def should_cancel() -> bool:
        # The operator's cancel is read first, so a fault in the stop check
        # can never mask it.
        return tracker.is_cancel_requested() or is_stopping()

    async def on_result(result: ItemResult) -> None:
        account_id = result.item
        if result.skipped:  # runner-level skip: the job was cancelled, or the process is stopping
            message = STOPPED_NOT_STARTED if is_stopping() else result.error or "cancelled"
            await tracker.item_finished(account_id, ok=False, message=message, skipped=True)
            return
        if not result.ok:  # the worker raised
            await tracker.item_finished(
                account_id, ok=False, message=_redact(result.error or "failed")
            )
            return
        outcome: _Outcome = result.value
        await tracker.item_finished(
            account_id,
            ok=outcome.ok,
            message=outcome.message,
            skipped=outcome.skipped,
            unconfirmed=outcome.unconfirmed,
        )

    results = await run_bounded(
        list(account_ids),
        worker,
        limit=limit,
        should_cancel=should_cancel,
        on_result=on_result,
    )
    # Said on the run itself, not only per account: a run the stop cut short
    # still ends "completed", and a quantity run's summary counts only the
    # accounts its workers passed over.
    if is_stopping():
        untried = sum(
            1
            for result in results
            if result.skipped
            or (result.ok and getattr(result.value, "message", None) == STOPPED_NOT_STARTED)
        )
        if untried:
            _note_on_job(
                tracker.job_id,
                f"the server stopped: {untried} account(s) were not started — start them again",
            )
    return results


def _note_on_job(job_id: int, line: str) -> None:
    """Append one line to a job's transcript, leaving its status alone.

    For a reason that belongs to the run rather than to any one account: with
    no ``JobItem`` to carry it, ``Job.log`` is the only place the operator will
    see why nothing happened. The tracker still owns the final status.
    """
    with _session() as db:
        job = db.get(Job, job_id)
        if job is None:
            log.error("_note_on_job(): job %s does not exist", job_id)
            return
        job.log = f"{job.log or ''}{line}\n"
        db.commit()


@contextlib.contextmanager
def _session() -> Iterator[DbSession]:
    """Short-lived session: concurrent items must never share one."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _effective_settings() -> SimpleNamespace:
    """Deployment config overlaid with whatever the Settings UI saved.

    Jobs must honour the saved settings — concurrency, headless, proxy and the
    OTP inbox — without needing a restart, so the
    user-editable half comes from ``app_settings.get_effective`` (the single
    source of truth, keyed on ``app_settings.SETTINGS_KEY``) rather than from
    ``get_settings()`` alone. Deployment-only values (timeouts, database_url,
    jwt_secret) are not editable over HTTP and pass through untouched.
    """
    base = get_settings()
    merged = {name: getattr(base, name) for name in type(base).model_fields}
    with _session() as db:
        merged.update(app_settings.get_effective(db))
    return SimpleNamespace(**merged)


def _pool_for(settings, *, lean: bool = True) -> BrowserPool:
    """The run's browsers: one Chromium per ``contexts_per_browser`` accounts,
    each account in an isolated window of its own (PLAN.md §10). Nothing is
    launched until the first account asks for a window, so a run that passes
    every account over — or a test that stands in for ``browser_page`` —
    starts no browser at all. The run's settings are fixed here, once: a
    proxy or headless change saved mid-run applies to the next run.
    ``lean=False`` keeps the pictures (and the telemetry) on, for windows a
    person looks at, as ``browser_page`` says."""
    return BrowserPool(
        headless=settings.browser_headless,
        proxy=settings.proxy_server or None,
        lean=lean,
        contexts_per_browser=getattr(settings, "contexts_per_browser", DEFAULT_CONTEXTS_PER_BROWSER),
    )


def _redact(text: str) -> str:
    """Strip anything PAN-shaped and clamp length (PLAN.md §5 backstop)."""
    cleaned = _LONG_DIGITS.sub("[redacted]", text or "")
    return cleaned[:_MAX_MESSAGE_CHARS]
