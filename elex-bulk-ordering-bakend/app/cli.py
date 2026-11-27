"""Manual runner for the automation flows (Phase 2).

Lets you exercise each ported flow against real accounts before the worker pool
(Phase 3) and API/UI (Phases 4–5) exist.

    python -m app.cli login                 # generate sessions for all accounts
    python -m app.cli coupon --code SAVE10   # apply a coupon to all sessions
    python -m app.cli order --product URL --target 1000 --email you@example.com

An order run needs ``--target`` (the price cap per order, in rupees) or, to
order at any price on purpose, ``--no-cap`` — one of the two, never neither and
never both. Accounts come from the database (import them first via the API).
A card run asks for the card at a hidden prompt, per run, and never stores it
(PLAN.md §5); the command line never carries it (audit F-21).
"""

from __future__ import annotations

import argparse
import email
import asyncio
import logging
import math

from sqlalchemy import select

from app.automation import coupons as coupon_flow
from app.automation import login as login_flow
from app.automation import ordering as order_flow
from app.automation.otp import get_otp_reader
from app.automation.sellers import MAX_SELLER_CHARS
from app.config import get_settings
from app.core import session_store
from app.core.browser import browser_page
from app.db.base import SessionLocal, init_db
from app.db.models import Account, AccountStatus, CouponRun, OrderStatus, utcnow
from app.jobs.tasks import (
    DestinationError,
    _effective_settings,
    account_billing,
    record_order_result,
    reserve_order,
    resolve_destination,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("ordering.cli")


async def cmd_login(args) -> None:
    settings = get_settings()
    reader = get_otp_reader(settings)
    db = SessionLocal()
    try:
        accounts = list(db.scalars(select(Account).order_by(Account.id)).all())
        for account in accounts:
            if not args.force and session_store.has_valid_session(db, account):
                log.info("skip %s (already has a session)", account.email)
                continue
            log.info("login: %s", account.email)
            began = utcnow()
            async with browser_page(headless=settings.browser_headless) as (_, page):
                cookies = await login_flow.run_login(
                    page,
                    account.email,
                    reader,
                    window_wait=settings.fk_window_wait,
                    otp_wait=settings.otp_fetch_timeout,
                )
            if not cookies:
                # run_login returns nothing when Flipkart still serves the
                # login page. Saving anyway would mark the account valid on a
                # logged-out browser — and print "session saved" one line
                # after "login did not complete", which is how this was missed.
                # Its cookie set goes too: no run is to load it
                # (session_store.forget_session).
                session_store.forget_session(db, account, since=began)
                log.error("NOT saving a session for %s — login unconfirmed", account.email)
                continue
            session_store.save_session(db, account, cookies)
            log.info("session saved for %s", account.email)
    finally:
        db.close()


async def cmd_coupon(args) -> None:
    settings = get_settings()
    db = SessionLocal()
    try:
        accounts = list(db.scalars(select(Account).order_by(Account.id)).all())
        for account in accounts:
            cookies = session_store.load_cookies(db, account)
            if not cookies:
                log.info("skip %s (no session)", account.email)
                continue
            async with browser_page(
                headless=settings.browser_headless, mobile=True, cookies=cookies
            ) as (_, page):
                result = await coupon_flow.apply_coupon(
                    page, args.code, result_wait=settings.coupon_wait
                )
            db.add(CouponRun(account_id=account.id, coupon_code=args.code, result=result))
            db.commit()
            log.info("coupon %s -> %s (%s)", args.code, result, account.email)
    finally:
        db.close()


#: The flags that once carried the card. The parser still knows them, unshown,
#: so a command line using them is refused by name: as unknown flags argparse
#: would print them back, values and all.
CARD_FLAGS = (("--card", "card"), ("--month", "month"), ("--year", "year"), ("--cvv", "cvv"))


def _card_from_args_or_prompt(args) -> "order_flow.CardInfo | None":
    """The card for a command-line card run, typed at a hidden prompt.

    Never from the command line: a card there stays in the shell's history
    and shows in the process list (audit F-21), and the flags were still
    taken whenever all four were given (review of 27 Oct 2026). A command
    line carrying any of them is refused, naming the flags and never their
    values. None when refused, or when the prompt was left blank."""
    import getpass

    given = [flag for flag, name in CARD_FLAGS if getattr(args, name, None)]
    if given:
        log.error(
            "card details are never taken on the command line (%s given): they stay in the shell's history "
            "and show in the process list — delete that line from the history. Run the command again "
            "without them and type the card at the hidden prompt. Nothing was ordered.",
            ", ".join(given),
        )
        return None
    log.info("asking for the card at the prompt (nothing typed is shown or kept)")
    number = getpass.getpass("Card number: ")
    month = getpass.getpass("Expiry month (MM): ")
    year = getpass.getpass("Expiry year (YY or YYYY): ")
    cvv = getpass.getpass("CVV: ")
    number = "".join(ch for ch in number if ch.isdigit())
    if not (number and month and year and cvv):
        log.error("a card run needs the card number, expiry month, expiry year and CVV; use --payment-mode cod to order without a card")
        return None
    password = ""
    if getattr(args, "ask_card_password", False):
        # The bank's page asks for it after Pay (HDFC's corporate cards); typed
        # here, never on the command line.
        password = getpass.getpass("Card password for the bank's page (blank if the bank asks for none): ")
    return order_flow.CardInfo(
        number=number, month=month.strip(), year=year.strip(), cvv=cvv.strip(), password=password.strip()
    )


def _order_bounds_problem(target: float | None, qty: int, *, no_cap: bool = False) -> str | None:
    """Why ``order`` must not start with these numbers, or None when they are fine.

    The bounds the portal's run has at its door (audit F-23, 25 Oct 2026):
    a cap of "inf" lets every price through, "nan" refuses every one, and 0 or
    less refuses every order — each a run that looks capped and is not — while
    a quantity below 1 is read by the flow as one unit. And, as in the portal
    since 26 Oct 2026, a run states its cap: ``--target``, or ``--no-cap`` to
    order at any price on purpose. Leaving ``--target`` out no longer means
    "no cap", and giving both is a contradiction, so either is refused.
    """
    if target is not None and no_cap:
        return "--target and --no-cap contradict each other: give a price cap or --no-cap, not both"
    if target is None and not no_cap:
        return "a price cap is required: give --target in rupees, or --no-cap to order at any price on purpose"
    if target is not None and not (math.isfinite(target) and target > 0):
        return f"--target must be a price above zero, in rupees — got {target}"
    if qty < 1:
        return f"--qty must be at least 1 — got {qty}"
    return None


async def cmd_order(args) -> None:
    settings = get_settings()
    # First of all: before the card is asked for, before the database is read
    # and long before a browser opens. Read with getattr, like the other
    # optional flags, so a caller that builds its own arguments without the
    # flag is refused for want of a cap rather than crashing.
    no_cap = bool(getattr(args, "no_cap", False))
    problem = _order_bounds_problem(args.target, args.qty, no_cap=no_cap)
    if problem:
        log.error("%s; nothing was ordered", problem)
        return
    if no_cap:
        log.warning("no price cap (--no-cap): every order is placed at whatever its final amount comes to")
    # Bounded as the portal bounds it, and read with getattr like the other
    # optional flags: a caller building its own arguments may leave it out.
    seller = " ".join((getattr(args, "seller", None) or "").split()) or None
    if seller and len(seller) > MAX_SELLER_CHARS:
        log.error("--seller is %d characters; a seller's name is at most %d; nothing was ordered", len(seller), MAX_SELLER_CHARS)
        return
    # Resolved exactly as the portal's job resolves it: the chosen Address Book
    # row, else the default, else refuse — and the GST chosen, linked or
    # default, unless the account has one of its own (per account, below).
    try:
        destination = resolve_destination(
            address_id=getattr(args, "address_id", None),
            gst_id=getattr(args, "gst_id", None),
        )
    except DestinationError as exc:
        log.error("%s", exc)
        return
    if not getattr(args, "email", None) and not getattr(args, "all_accounts", False):
        # Said in so many words, never by default: a run with no account named
        # once ordered on every account that had a session (audit F-21).
        log.error("name the account with --email <address>, or say --all to order on every account with a session")
        return
    mode = getattr(args, "payment_mode", order_flow.PAYMENT_CARD)
    card = None
    if mode == order_flow.PAYMENT_CARD:
        card = _card_from_args_or_prompt(args)
        if card is None:
            return
    db = SessionLocal()
    try:
        query = select(Account).order_by(Account.id)
        if getattr(args, "email", None):
            # Without this a run touches EVERY account that has a session —
            # which is how an order went out on an account nobody selected.
            query = query.where(Account.email == args.email)
        accounts = list(db.scalars(query).all())
        if not accounts:
            log.error("no matching account (--email %s)", getattr(args, "email", None))
            return
        for account in accounts:
            cookies = session_store.load_cookies(db, account)
            if not cookies:
                log.info("skip %s (no session)", account.email)
                continue
            # The account's billing phone and its own GST, as the portal's run
            # uses them (app.jobs.tasks.account_billing).
            phone, account_gst = account_billing(account)
            address = destination.address_for(phone)
            gst = destination.gst_for(account_gst)
            columns = dict(
                product_link=args.product,
                qty=args.qty,
                target=args.target,
                address=address,
                gst=gst,
            )
            reserved: list[int] = []

            def reserve(row: dict, _account_id: int = account.id) -> int:
                row_id = reserve_order(_account_id, row, **columns)
                reserved.append(row_id)
                return row_id

            result: dict | None = None
            try:
                async with browser_page(
                    headless=settings.browser_headless, cookies=cookies
                ) as (_, page):
                    result = await order_flow.run_order(
                        page,
                        product_link=args.product,
                        address=address,
                        gst=gst,
                        card=card,
                        qty=args.qty,
                        target=args.target,
                        address_match=args.address,
                        payment_mode=mode,
                        seller=seller,
                        reserve=reserve,
                        # Always an Address Book row: make Flipkart deliver there.
                        ensure_address=True,
                    )
            except Exception:
                if result is not None:
                    # The order ran and the browser then failed to close:
                    # losing the result here would leave a real order unrecorded.
                    log.warning("the browser failed to close after the order ran; keeping its result")
                elif reserved:
                    log.error(
                        "order %s -> UNCONFIRMED: the run broke after the order was "
                        "recorded and possibly sent; row %s stands — check the account",
                        account.email,
                        reserved[-1],
                    )
                    continue
                else:
                    raise
            # The portal's own saver, so a CLI run is recorded exactly like a
            # portal run. A copy of it here once dropped payment_mode and
            # placed_at, filing Cash on Delivery orders as card payments.
            record_order_result(account.id, result, **columns)
            _log_order_outcome(account.email, result)
    finally:
        db.close()


def _log_order_outcome(email: str, result: dict) -> None:
    """One line per account that says what happened to the money."""
    status = result.get("status") or OrderStatus.failed
    amount = result.get("amount")
    shown = f"₹{amount:,.2f}" if isinstance(amount, (int, float)) else "no amount"
    if status == OrderStatus.placed:
        log.info("order %s -> PLACED: %s for %s", email, result.get("order_id"), shown)
    elif status == OrderStatus.unconfirmed:
        log.error("order %s -> UNCONFIRMED (%s): %s", email, shown, result.get("error"))
    else:
        reason = result.get("error")
        log.info("order %s -> %s%s", email, status.value, f": {reason}" if reason else "")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.cli", description="Flipkart flows")
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="generate sessions")
    p_login.add_argument("--force", action="store_true", help="re-login even if a session exists")
    p_login.set_defaults(func=cmd_login)

    p_coupon = sub.add_parser("coupon", help="apply a coupon to all sessions")
    p_coupon.add_argument("--code", required=True)
    p_coupon.set_defaults(func=cmd_coupon)

    p_order = sub.add_parser("order", help="place orders (single card, for testing)")
    p_order.add_argument("--product", required=True)
    p_order.add_argument("--email", help="order on just this account")
    p_order.add_argument(
        "--all",
        dest="all_accounts",
        action="store_true",
        help="order on EVERY account that has a session (one of --email or --all is required)",
    )
    p_order.add_argument("--address", help="pick the saved address matching this text, e.g. Lucknow")
    p_order.add_argument(
        "--address-id",
        dest="address_id",
        type=int,
        help=(
            "deliver to this Address Book entry (default: the one marked default; "
            "with neither the run is refused), adding it to the Flipkart account "
            "if it is not saved there"
        ),
    )
    p_order.add_argument(
        "--gst-id",
        dest="gst_id",
        type=int,
        help="bill under this Address Book GST (default: the address's linked GST, else the default GST)",
    )
    p_order.add_argument(
        "--target",
        type=float,
        default=None,
        help="price cap per order in rupees, above zero (this or --no-cap is required)",
    )
    p_order.add_argument(
        "--no-cap",
        dest="no_cap",
        action="store_true",
        help="order at any price, on purpose: a run without --target needs this, and one with --target refuses it",
    )
    p_order.add_argument("--qty", type=int, default=1, help="units per order, at least 1")
    p_order.add_argument(
        "--seller",
        help=(
            "buy from this seller, as the product's sellers page names it (any case), instead of "
            "Flipkart's default; a seller the page does not list stops the order before anything is pressed"
        ),
    )
    p_order.add_argument(
        "--payment-mode",
        dest="payment_mode",
        choices=["card", "cod"],
        default="card",
        help="card (the card is asked for at a hidden prompt) or cod (no card at all)",
    )
    # A card run asks for the card at a hidden prompt: the shell's history and
    # the process list must not hold a card. These are known here, unshown,
    # only so _card_from_args_or_prompt can refuse them by name.
    for flag, _name in CARD_FLAGS:
        p_order.add_argument(flag, help=argparse.SUPPRESS)
    p_order.add_argument(
        "--ask-card-password",
        dest="ask_card_password",
        action="store_true",
        help="ask (hidden) for the password the bank's page wants after Pay, as HDFC's corporate cards do",
    )
    p_order.set_defaults(func=cmd_order)

    p_sess = sub.add_parser(
        "session-check", help="verify stored sessions are really logged in"
    )
    p_sess.add_argument("--email", help="check just this account")
    p_sess.add_argument(
        "--fix", action="store_true", help="mark unusable sessions invalid"
    )
    p_sess.set_defaults(func=cmd_session_check)

    p_otp = sub.add_parser("otp-check", help="show what the OTP reader sees for an address")
    p_otp.add_argument("--email", required=True)
    p_otp.add_argument("--limit", type=int, default=3)
    p_otp.set_defaults(func=cmd_otp_check)

    p_ia = sub.add_parser(
        "instaddr-connect",
        help="sign in to InstAddr (m.kuku.lu) in a browser window, once, so the app can read the accounts' mail",
    )
    p_ia.add_argument(
        "--name",
        default=None,
        help="keep it as a further InstAddr account under this name (e.g. vikramten) instead of replacing the first one",
    )
    p_ia.add_argument(
        "--domain",
        default=None,
        help="the account's own domain (e.g. apmail.org): the window closes by itself once the sign-in shows it",
    )
    p_ia.set_defaults(func=cmd_instaddr_connect)

    p_iar = sub.add_parser(
        "instaddr-refresh",
        help="re-read the addresses each further InstAddr account holds (after creating new ones there); no sign-in",
    )
    p_iar.set_defaults(func=cmd_instaddr_refresh)

    p_iac = sub.add_parser("instaddr-check", help="read-only: show what the saved InstAddr session sees")
    p_iac.add_argument("--url", default=None, help="a page of m.kuku.lu to open (default: the mailbox home)")
    p_iac.add_argument("--name", default=None, help="which saved InstAddr account (default: the first)")
    p_iac.set_defaults(func=cmd_instaddr_check)

    p_pw = sub.add_parser(
        "set-password",
        help="give a user a password from the server (the way in when no admin can sign in)",
    )
    p_pw.add_argument("email", help="the user's sign-in email, or the configured admin's name")
    p_pw.set_defaults(func=cmd_set_password)

    p_mail = sub.add_parser(
        "test-mail",
        help="send one test mail with the configured ORDERING_SMTP_* settings, to prove they work",
    )
    p_mail.add_argument("to", help="the address to send the test mail to")
    p_mail.set_defaults(func=cmd_test_mail)

    p_copy = sub.add_parser(
        "copy-to-postgres",
        help="copy this machine's database into an empty PostgreSQL one (Neon), read from ORDERING_NEW_DATABASE_URL",
    )
    p_copy.add_argument("--check", action="store_true", help="only check: reach it, see it is empty, count the rows; write nothing")
    p_copy.set_defaults(func=cmd_copy_to_postgres)

    return parser


def _app_is_running() -> bool:
    """Whether the app answers on this machine's usual port: a copy made while
    it runs could miss what it writes meanwhile."""
    import urllib.request

    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2) as answer:
            return answer.status == 200
    except Exception:  # noqa: BLE001 — nothing answering is the answer
        return False


async def cmd_copy_to_postgres(args) -> None:
    """Copy the laptop's SQLite database into an empty PostgreSQL database
    (app.db.copy_database). Its address comes from ORDERING_NEW_DATABASE_URL
    and is never printed — only its host."""
    from app.db import copy_database as tool
    from app.db.base import engine

    try:
        url = tool.target_url()
        host = tool.safe_host(url)
        if args.check:
            found = tool.check(engine, url)
            total = sum(found["rows"].values())
            print(f"reached {host} (PostgreSQL {found['server_version']}): it is empty, ready for the copy")
            print(f"this machine's database holds {total} rows in {sum(1 for n in found['rows'].values() if n)} tables")
            return
        if _app_is_running():
            raise tool.CopyRefused(
                "the app is running on this machine: stop it first (Ctrl+C in its window), "
                "so nothing it writes during the copy is left behind"
            )
        report = tool.copy(engine, url)
    except tool.CopyRefused as refused:
        print(f"NOT COPIED: {refused}")
        raise SystemExit(1) from None
    except Exception as exc:  # noqa: BLE001 — said in a line; the copy was undone whole
        said = str(getattr(exc, "orig", None) or exc).strip().splitlines()[0][:300]
        print(f"NOT COPIED: {type(exc).__name__}: {said}")
        print("nothing was kept in the new database; this machine's database is unchanged")
        raise SystemExit(1) from None
    print(f"copied {report.rows} rows in {sum(1 for n in report.tables.values() if n)} tables to {report.host}")
    print(f"the new database is recorded at migration {report.revision}")
    for column, count in sorted(report.cut.items()):
        print(f"  note: {count} value(s) of {column} were longer than the column and were cut to fit")
    if report.timezone_set:
        print("its time zone is set to UTC")
    elif report.timezone_note:
        print(f"  note: {report.timezone_note}")
    print("this machine's database file is unchanged; invoice PDFs and compiled zips stay in data/ here")
    if _target_is_apps_own():
        print("NEXT: the settings file already points the app at this database — start the app")
    else:
        print(
            "NEXT: in the .env beside the app, set ORDERING_DATABASE_URL to the same address as "
            "ORDERING_NEW_DATABASE_URL, then start the app"
        )


def _target_is_apps_own() -> bool:
    """Whether this copy's target came from the app's own setting in the file
    (app.db.copy_database.target_url): no ORDERING_NEW_DATABASE_URL given."""
    import os

    from dotenv import dotenv_values

    from app.db import copy_database as tool

    if os.environ.get(tool.TARGET_SETTING, "").strip():
        return False
    return not any((dotenv_values(path).get(tool.TARGET_SETTING) or "").strip() for path in tool._settings_files(None))


def _mail_settings_summary(settings) -> str:
    """The mail settings as a line an operator can check — never the password."""
    user = settings.smtp_user or "(no sign-in)"
    tls = "STARTTLS on" if settings.smtp_starttls else "STARTTLS off"
    return (
        f"host {settings.smtp_host or '(not set)'}, port {settings.smtp_port}, {tls}, "
        f"user {user}, from {settings.smtp_from or '(not set)'}, app address {settings.app_url or '(not set)'}"
    )


def _explain_mail_failure(exc: BaseException) -> str:
    """The usual SMTP failures in the operator's words; the raw error follows."""
    text = str(exc)
    lower = text.lower()
    if "username and password not accepted" in lower or "authentication" in lower or "535" in lower:
        hint = (
            "the mailbox refused the sign-in — for Gmail, use an App Password "
            "(Google Account → Security → 2-Step Verification → App passwords), not the account password"
        )
    elif "connection refused" in lower or "nodename nor servname" in lower or "name or service not known" in lower:
        hint = "nothing answered at that host and port — check ORDERING_SMTP_HOST and ORDERING_SMTP_PORT (Gmail: smtp.gmail.com, 587)"
    elif "timed out" in lower:
        hint = "the connection timed out — a firewall or the network is blocking the port"
    elif "certificate" in lower or "ssl" in lower:
        hint = "the secure connection failed — the host name must be the mail server's own, as on its certificate"
    elif "starttls" in lower:
        hint = "the server does not offer STARTTLS on this port — port 587 does; set ORDERING_SMTP_STARTTLS=false only for a server that encrypts another way"
    else:
        hint = "the mail server refused the message"
    return f"{hint}. The server said: {text[:300]}"


async def cmd_test_mail(args) -> None:
    """Send one test mail with the configured settings and say what happened.

    Nothing is written anywhere; the password is read from the environment and
    never printed. A missing setting is named before anything is tried.
    """
    from app.config import get_settings
    from app.core import mailer

    settings = get_settings()
    missing = [
        name
        for name, value in (
            ("ORDERING_SMTP_HOST", settings.smtp_host),
            ("ORDERING_SMTP_FROM", settings.smtp_from),
            ("ORDERING_APP_URL", settings.app_url),
        )
        if not value
    ]
    print(f"mail settings: {_mail_settings_summary(settings)}")
    if missing:
        raise SystemExit(
            "not sending: "
            + ", ".join(missing)
            + " must be set (in backend/.env on a laptop, in the compose .env on a server); "
            "the sign-in page offers no reset link until all three are"
        )
    body = (
        "This is a test mail from the Ordering Suite.\n\n"
        f"It was sent through {settings.smtp_host}:{settings.smtp_port} as {settings.smtp_from}. "
        "If you can read it, password-reset mails will arrive the same way.\n"
    )
    try:
        mailer.send_mail(args.to, "Ordering Suite test mail", body)
    except Exception as exc:  # noqa: BLE001 — the point is to explain the failure
        raise SystemExit(f"not sent: {_explain_mail_failure(exc)}") from None
    print(f"sent a test mail to {args.to}; check that inbox (and its spam folder)")


async def cmd_set_password(args) -> None:
    """Set a user's password from the server's shell, typed twice and never echoed.

    For the configured admin this creates the row when it does not exist yet,
    so a deployment with no usable ORDERING_ADMIN_PASSWORD(_HASH) still has a
    way in. The password is checked by the same rule as in the app, and any
    session that user already had is ended (tokens older than the change are
    refused).
    """
    import getpass

    from app.core import users

    email = users.normalise_email(args.email)
    with SessionLocal() as db:
        user = db.scalar(select(users.User).where(users.User.email == email))
        if user is None and users.is_env_admin(email):
            user = users.ensure_env_admin(db)
        if user is None:
            raise SystemExit(f"no user with the email {email!r}; add them under Users in the app first")
        first = getpass.getpass(f"New password for {user.email}: ")
        second = getpass.getpass("Again: ")
        if first != second:
            raise SystemExit("the two passwords differ; nothing changed")
        problem = users.password_problem(first, email=user.email)
        if problem:
            raise SystemExit(problem)
        users.set_password(user, first)
        if not user.active:
            user.active = True
            log.warning("%s was deactivated; reactivated along with the new password", user.email)
        db.commit()
    log.info("password set for %s; their earlier sessions are ended", email)


def main() -> None:
    # The arguments first: ``--help``, or a mistyped command, ends here and
    # must never open the database. Until 11 Nov 2026 the database was set up
    # before the arguments were read, so asking for help took a backup and ran
    # the start-up repairs on the real data.
    args = build_parser().parse_args()
    init_db()
    asyncio.run(args.func(args))



async def cmd_instaddr_connect(args) -> None:
    """Open InstAddr in a visible window; the person signs in there (AccountID,
    password and the emailed code), then presses Enter here. The tool types
    nothing into InstAddr and prints no secret; it keeps the signed-in state."""
    from app.automation import instaddr

    async def wait_for_person() -> None:
        await asyncio.to_thread(
            input,
            "\nSign in to InstAddr in the browser window (AccountID, password, the emailed code).\n"
            "When your inbox shows, come back here and press Enter... ",
        )

    if args.domain:
        print(
            f"\nSign in to InstAddr in the browser window (AccountID, password, the emailed code).\n"
            f"It closes by itself once the account shows its @{args.domain.strip().lstrip('@')} addresses (15 minutes at most)."
        )
        state = await instaddr.connect_until_domain(args.domain)
        if not state:
            raise SystemExit(f"the window closed before the account showed any @{args.domain} address; nothing saved")
    else:
        state = await instaddr.connect_interactively(wait_for_person)
    if not state.get("cookies"):
        raise SystemExit("no InstAddr session was found in the window; nothing saved")
    addresses = await instaddr.fetch_addresses(state)
    domains = instaddr.domains_of(addresses)
    with SessionLocal() as db:
        try:
            domains = instaddr.save_account(db, args.name, state, domains, addresses)
        except ValueError as exc:
            raise SystemExit(f"{exc}; nothing saved") from None
    who = f" as {args.name!r}" if args.name else ""
    extra = f" Domains: {', '.join(domains)}." if domains else ""
    print(f"InstAddr session saved{who} ({len(state.get('cookies', []))} cookies).{extra} The app can now read the mailbox.")


async def cmd_instaddr_refresh(args) -> None:
    """Re-read, with each saved sign-in, the addresses every further InstAddr
    account holds, so addresses created there since it was connected are
    routed to it. The first account is matched by domain and needs nothing."""
    from app.automation import instaddr

    with SessionLocal() as db:
        accounts = [a for a in instaddr.load_accounts(db) if a["name"] != instaddr.MAIN_ACCOUNT]
    if not accounts:
        raise SystemExit("no further InstAddr account saved: run  python -m app.cli instaddr-connect --name <name>  first")
    for account in accounts:
        addresses = await instaddr.fetch_addresses(account["state"])
        if not addresses:
            print(f"{account['name']}: could not read its addresses (signed out?); kept as it was")
            continue
        with SessionLocal() as db:
            domains = instaddr.save_account(
                db, account["name"], account["state"], instaddr.domains_of(addresses), addresses
            )
        print(f"{account['name']}: {len(addresses)} addresses on {', '.join(domains)}")


async def cmd_instaddr_check(args) -> None:
    """Read-only: what the saved InstAddr session sees (for mapping the page)."""
    import json as _json

    from app.automation import instaddr

    with SessionLocal() as db:
        state = instaddr.account_state(db, args.name)
    if not state:
        raise SystemExit("no InstAddr session saved: run  python -m app.cli instaddr-connect  first")
    found = await instaddr.dump_inbox(state, args.url or instaddr.HOME_URL)
    text = _json.dumps(found, ensure_ascii=False, indent=1)[:20000]
    try:
        print(text)
    except UnicodeEncodeError:
        print(_json.dumps(found, ensure_ascii=True, indent=1)[:20000])


async def cmd_otp_check(args) -> None:
    """Show what the OTP reader actually sees for an address.

    Added because "no OTP obtained" is not a diagnosis: the mailbox, the
    search, and the body parsing can each fail silently and they need
    different fixes. This prints each stage.
    """
    import imaplib

    from app.automation.instaddr import InstAddrOtpReader
    from app.automation.otp import ImapOtpReader, RoutedOtpReader, _message_text, otp_from_text

    settings = _effective_settings()
    address = args.email
    # The same choice a login run makes: the numbered inbox / InstAddr that
    # serves this address, else the main one.
    reader = get_otp_reader(settings)
    label = "the main inbox"
    if isinstance(reader, RoutedOtpReader):
        reader, label = reader.reader_for(address)
    if isinstance(reader, InstAddrOtpReader):
        print(f"inbox   : {label}")
        print(f"looking : InstAddr messages for {address}")
        mails = await reader.list_messages(address, include_body=True)
        print(f"\n[InstAddr recv.php] matched {len(mails)} message(s)")
        for mail in mails[: args.limit]:
            code = otp_from_text(mail.combined_text, require_context=True)
            print(f"  id      : {mail.mail_id}")
            print(f"  meta    : {mail.meta}")
            print(f"  subject : {mail.subject[:80]}")
            print(f"  CODE    : {code or '(none found)'}")
            print()
        return
    from app.automation.graph_mail import GraphOtpReader, message_code_for

    if isinstance(reader, GraphOtpReader):
        from datetime import datetime, timedelta, timezone

        print(f"inbox   : {label}")
        print(f"looking : Graph messages (all folders, last {reader.max_age_minutes} min) for {address}")
        problem = await reader.probe(address)
        if problem:
            print(f"\nCANNOT READ: {problem}")
            return
        messages = await reader._list_recent()
        oldest = datetime.now(timezone.utc) - timedelta(minutes=reader.max_age_minutes)
        print(f"\n[Microsoft Graph] {len(messages)} recent message(s) in the mailbox")
        shown = 0
        for message in messages:
            code = message_code_for(message, address, senders=reader.senders, not_before=None, oldest=oldest)
            # The raw To: header: Graph's toRecipients shows a group's own
            # address for mail sent to one of the group's aliases.
            to = ", ".join(
                str(h.get("value") or "")
                for h in message.get("internetMessageHeaders") or []
                if str(h.get("name") or "").lower() == "to"
            ) or ", ".join(
                (r.get("emailAddress") or {}).get("address", "") for r in message.get("toRecipients") or []
            )
            if address.lower() not in to.lower() and not code:
                continue
            print(f"  received: {message.get('receivedDateTime')}")
            print(f"  to      : {to}")
            print(f"  subject : {(message.get('subject') or '')[:80]}")
            print(f"  CODE    : {code or '(none found)'}")
            print()
            shown += 1
            if shown >= args.limit:
                break
        if not shown:
            print(f"  nothing addressed to {address} yet")
        return
    if not isinstance(reader, ImapOtpReader):
        log.error("OTP inbox is not configured — set ORDERING_OTP_IMAP_* in .env")
        return

    print(f"inbox   : {label}")
    print(f"host    : {reader.host}:{reader.port}  user: {reader.user}")
    print(f"looking : messages addressed to {address}")
    try:
        conn = reader._connect()
    except Exception as exc:  # noqa: BLE001
        print(f"CONNECT FAILED: {exc}")
        return

    try:
        for mailbox in reader._mailboxes():
            try:
                typ, _ = conn.select(mailbox, readonly=True)
            except Exception:
                continue
            if typ != "OK":
                continue
            ids = reader._search_ids(conn, address)
            print(f"\n[{mailbox}] matched {len(ids)} message(s)")
            if not ids:
                continue
            for msg_id in list(reversed(ids))[: args.limit]:
                typ, data = conn.fetch(msg_id, "(RFC822)")
                if typ != "OK" or not data:
                    continue
                msg = email.message_from_bytes(data[0][1])
                types = [
                    p.get_content_type() for p in msg.walk() if not p.is_multipart()
                ] or [msg.get_content_type()]
                text = _message_text(msg)
                code = otp_from_text(text)
                print(f"  from    : {msg.get('From','?')[:60]}")
                print(f"  subject : {msg.get('Subject','?')[:70]}")
                print(f"  date    : {msg.get('Date','?')[:40]}")
                print(f"  parts   : {types}")
                print(f"  chars   : {len(text)} readable")
                print(f"  CODE    : {code or '(none found)'}")
                print()
            return
        print("\nNo message matched that address in any mailbox.")
        print("Check: has an OTP actually arrived? Is the To: address exactly this?")
    finally:
        try:
            conn.logout()
        except Exception:
            pass


async def cmd_session_check(args) -> None:
    """Ask Flipkart whether a stored session is actually signed in.

    Exists because a cookie jar that LOOKS authenticated is not the same as
    being authenticated: Flipkart sets `at` and `T` before OTP verification, so
    a failed login produced a plausible-looking session that was saved and
    marked valid. The only trustworthy test is loading the cookies into a clean
    browser and asking for a page that requires a session.
    """
    from app.core.browser import browser_page

    db = SessionLocal()
    try:
        query = select(Account).order_by(Account.id)
        if args.email:
            query = query.where(Account.email == args.email)
        accounts = list(db.scalars(query).all())
        if not accounts:
            print("no accounts to check")
            return

        for account in accounts:
            # The stored set whatever the account's status: one a read-back
            # marked signed out is exactly what this check is for.
            cookies = session_store.stored_cookies(db, account)
            if not cookies:
                print(f"{account.email:40s} no stored session")
                continue

            async with browser_page(headless=True, cookies=cookies) as (_ctx, page):
                try:
                    await page.goto(
                        "https://www.flipkart.com/account/orders",
                        wait_until="domcontentloaded",
                    )
                    await page.wait_for_timeout(3000)
                    live = "/login" not in (page.url or "")
                except Exception as exc:  # noqa: BLE001
                    print(f"{account.email:40s} CHECK FAILED: {exc}")
                    continue

            verdict = "LOGGED IN" if live else "NOT logged in (bounced to login)"
            print(f"{account.email:40s} {verdict}")

            if args.fix and not live:
                # Stop the dashboard reporting a session that does not work,
                # and keep none of its cookies (spec NFR-003): a set that no
                # longer works here is of no use, and is deleted, not marked.
                for stored in list(account.sessions):
                    db.delete(stored)
                account.status = AccountStatus.session_invalid
                db.commit()
                print(f"{'':40s}  -> marked session_invalid; its cookies deleted")
    finally:
        db.close()

if __name__ == "__main__":
    main()
