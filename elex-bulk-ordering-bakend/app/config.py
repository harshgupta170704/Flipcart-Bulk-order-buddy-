"""Unified configuration.

Replaces the three scattered ``data`` JSON files and ``input_data.txt`` from the
old repos with one settings object. Values can be overridden via a ``.env`` file
or environment variables prefixed with ``ORDERING_`` (see ``.env.example``).
"""

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalise_database_url(url: str) -> str:
    """A PostgreSQL address as SQLAlchemy needs it to reach the driver this app
    installs (psycopg 3): ``postgresql+psycopg://``. Neon, and most hosts, hand
    out ``postgresql://…`` or ``postgres://…``, which SQLAlchemy would send to
    psycopg2 — not installed — and the app would not start. Every other
    address is kept as it is."""
    text = (url or "").strip()
    for prefix in ("postgresql://", "postgres://"):
        if text.startswith(prefix):
            return "postgresql+psycopg://" + text[len(prefix) :]
    return text


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="ORDERING_",
        extra="ignore",
    )

    app_name: str = "Flipkart Ordering Suite"

    # --- Database -------------------------------------------------------------
    # SQLite for local/dev; swap to PostgreSQL in production (PLAN.md §3).
    # A host's own "postgresql://" address works as given (normalise_database_url).
    database_url: str = "sqlite:///./ordering.db"

    @field_validator("database_url")
    @classmethod
    def _driver_for_postgres(cls, value: str) -> str:
        return normalise_database_url(value)

    # --- Automation timeouts (seconds) ---------------------------------------
    # Merged from login-otp-generator/data, add-coupons/data and
    # flipkart-ordering-tool/data. Used from Phase 2 onward.
    login_wait: int = 10
    otp_wait: int = 10
    fk_window_wait: int = 10
    refresh_time: int = 2
    coupon_window_wait: int = 10
    coupon_wait: int = 3
    paymethod_wait: int = 50
    card_select_wait: int = 3
    bank_otp_fill_wait: int = 300
    orderlist_wait: int = 300
    content_wait: int = 5

    # --- Browser (Playwright) ------------------------------------------------
    # Hidden (true) or shown (false) browser on THIS machine, decided by its
    # own .env (ORDERING_BROWSER_HEADLESS) and nothing else: a server with no
    # screen keeps true, a laptop where someone watches the runs may say
    # false. Not on the Settings page: until 24 Nov 2026 a switch there was
    # saved in the database, shared by every machine, and beat this line.
    browser_headless: bool = True
    proxy_server: str = ""  # e.g. "http://user:pass@host:port"; blank = no proxy
    # A run opens one Chromium and gives each account an isolated window in
    # it; past this many windows it opens another Chromium (PLAN.md §10 "One
    # browser per run"). The browser program is the heavy part, a window
    # costs a tab — so this is what lets more accounts run at once in the
    # same memory. Lower it when one browser dying with all its windows
    # would hurt more than the memory saved.
    contexts_per_browser: int = 8

    # --- OTP inbox (IMAP) ----------------------------------------------------
    # Blank host/user => OTP reader is disabled (NullOtpReader), which the other
    # flows tolerate. Fill these in to enable automatic OTP reading.
    otp_imap_host: str = ""
    otp_imap_port: int = 993
    otp_imap_user: str = ""
    otp_imap_password: str = ""
    otp_imap_mailbox: str = "INBOX"
    otp_imap_use_ssl: bool = True
    otp_poll_interval: int = 3  # seconds between inbox polls
    otp_fetch_timeout: int = 60  # seconds to wait for the OTP to arrive
    # Debug aid: write the OTP itself into the log so you can confirm the inbox
    # is being read. It is a short-lived credential, so this is OFF by default
    # and meant to be switched back off once the setup is proven.
    otp_log_codes: bool = False
    # Only mail from these senders is considered. Essential on a personal
    # mailbox, where "addressed to me" matches nearly everything and a stray
    # six-digit number in an unrelated email would be typed as the code.
    otp_sender_filter: str = "flipkart"
    # OTPs are minutes old. Searching further back is slow on a large mailbox
    # and only adds chances to match the wrong message.
    otp_since_days: int = 1
    # Ignore anything older than this many minutes, even if it matches.
    otp_max_age_minutes: int = 15
    # --- OTP inbox on Microsoft 365 (app.automation.graph_mail) -------------
    # Exchange Online refuses IMAP password logins, so a Microsoft 365 inbox
    # is read through Microsoft Graph with an app registration (Mail.Read,
    # application permission, admin-consented). Every account whose email is
    # on one of ``otp_m365_domains`` gets its code from ``otp_m365_mailbox``.
    # Blank client id or secret: not used.
    otp_m365_tenant_id: str = ""
    otp_m365_client_id: str = ""
    otp_m365_client_secret: str = ""
    otp_m365_mailbox: str = ""
    otp_m365_domains: str = ""
    # --- HDFC CorpAssist child-card emails (app.core.corpassist) -------------
    # Read from the first OTP inbox above (one shared mailbox). Only mail from
    # these senders (comma-separated, matched as IMAP FROM) is read.
    hdfc_sender_filter: str = "hdfc"
    # How far back Fetch looks for child-card emails.
    hdfc_since_days: int = 30
    # Fetch every active user id's cards this often, in minutes; 0 = only by hand.
    hdfc_auto_fetch_minutes: int = 0
    # How long a card batch watches the inbox for its cards before giving up.
    hdfc_batch_wait_minutes: int = 60
    # --- HDFC CorpAssist portal (app.automation.hdfc_portal) ----------------
    # Card batches sent with "Create on HDFC" sign in here and upload HDFC's
    # bulk virtual-card sheet. Empty user or password: the portal is not used
    # and batches are downloaded to upload by hand.
    hdfc_portal_url: str = "https://corpcardassist.hdfc.bank.in"
    hdfc_portal_user: str = ""
    hdfc_portal_password: str = ""
    # Where the portal's sign-in OTP is read: the Employee Phone registered for
    # this 10-digit mobile (SMS relay), else this address's OTP inbox.
    hdfc_portal_otp_mobile: str = ""
    hdfc_portal_otp_email: str = ""
    # Only texts from (or mentioning) this sender count as the portal's OTP.
    hdfc_portal_otp_sender: str = "HDFC"
    # The longest the portal may take over one page, in seconds.
    hdfc_portal_step_seconds: int = 60
    # When true, treat all active Employee Phones as online on the UI and portal check.
    employee_phones_assume_online: bool = False

    # --- Captchas (app.automation.challenges) ------------------------------
    #: Ask a person on the Captchas page when a flow meets a captcha; false
    #: fails that account at once with "captcha" as the reason.
    captcha_ask: bool = True
    #: How long a browser waits for someone to answer before the account is
    #: skipped (its card goes back to the pool).
    captcha_wait_seconds: int = 120
    #: A run stops starting new accounts once it has met this many captchas
    #: within ``captcha_stop_minutes`` (0 = never stop for captchas).
    captcha_stop_count: int = 5
    captcha_stop_minutes: int = 5

    # --- Concurrency / queue -------------------------------------------------
    max_concurrency: int = 4  # accounts processed in parallel per job
    #: How long one Mock-mode order takes, roughly, in seconds (each takes
    #: between half and one and a half times this). Only there so a mock run's
    #: live progress can be watched; 0 makes a mock run instant.
    mock_order_seconds: float = 0.5
    #: Read the accounts' recent orders back by themselves every N hours, so a
    #: delivery shows up without anyone pressing the button (0 = only by hand).
    #: Changeable from the Settings page like the other knobs.
    sync_every_hours: int = 0
    # Orders placed by this tool are read back on their own until they arrive
    # (the owner's ask of 26 Oct 2026), so the status — and the delivery code,
    # which Flipkart shows only once a unit is out for delivery — reach the
    # ledger without anyone pressing Sync. Only the accounts holding such
    # orders are read (app.jobs.schedule): every ``track_every_minutes`` while
    # an order is on its way, every ``out_for_delivery_minutes`` once it is out
    # for delivery. 0 switches either off.
    track_every_minutes: int = 60
    out_for_delivery_minutes: int = 30
    # Hard ceiling on how many accounts one run may touch. With tens of
    # thousands of accounts, "select all" is one mis-click away from launching
    # a run nobody intended, so the API refuses rather than starting it.
    max_accounts_per_run: int = 5000
    redis_url: str = ""  # blank => in-process runner only (no Arq/Redis needed)

    # Where invoice PDFs are kept: a directory for now (a mounted volume in
    # Docker — see docker-compose.yml), S3 later behind app.io.invoice_store.
    invoice_dir: str = "./data/invoices"
    # Compiled invoice archives (zips) live beside the invoices and are swept
    # after this many days (spec FR-039 / C-11); 0 keeps them for ever.
    archive_dir: str = "./data/archives"
    archive_retention_days: int = 90

    # --- App authentication --------------------------------------------------
    # The app is deployed on a public web server, so it has its own login.
    # Must be >= 32 bytes for HS256 (PyJWT warns below that). Override in
    # production with a long random value — see docs/DEPLOY.md §4.
    jwt_secret: str = "dev-only-insecure-secret-change-me-in-production"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 480  # a working day (spec C-13)
    # PBKDF2-SHA256 work factor for every password hashed by the app (user
    # rows, the hash command). The test suite turns it down.
    pbkdf2_iterations: int = 390_000
    # The interactive API docs (/docs, /openapi.json) are off unless asked for:
    # the app is internet-facing and they describe every route (DEPLOY.md §4).
    expose_docs: bool = False
    # The configured admin's sign-in name: an email, or any name (the shipped
    # default is "admin"). Everyone else signs in with the email an admin gave
    # them under Users.
    admin_username: str = "admin"
    # PBKDF2-SHA256 hash; generate with: python -m app.core.security hash <password>
    # Blank => falls back to admin_password (dev convenience only).
    admin_password_hash: str = ""
    # The shipped default is refused at sign-in unless dev_mode is on, so a
    # deployment that never set a password is not open to a guess of "admin".
    admin_password: str = "admin"
    # A local setup only: accepts the default admin password above.
    dev_mode: bool = False

    # --- Sign-in throttling (spec FR-056) ------------------------------------
    # Wrong passwords (or reset-link asks) tolerated inside fifteen minutes
    # before that door is locked for fifteen minutes: per email, and per
    # calling address. The address limit is far higher on purpose: an office
    # shares one address behind its NAT, and one person's typos must not lock
    # everyone out. The address is the browser's only when the proxy's
    # forwarded headers are trusted (FORWARDED_ALLOW_IPS in backend/Dockerfile).
    login_max_failures_per_email: int = 5
    login_max_failures_per_address: int = 50

    # --- Logging -------------------------------------------------------------
    # Level of the app's own lines ("ordering.*": DEBUG, INFO, WARNING, ERROR).
    # The audit trail ("ordering.audit", who changed what) stays at INFO
    # whatever this says. Applied by app.core.logging_setup.configure_logging.
    log_level: str = "INFO"

    # --- Mail (the password reset link, spec C-13) ----------------------------
    # Blank host => no mail is sent and the sign-in page offers no reset link;
    # an admin sets a temporary password under Users instead.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True
    # Where the app is reached from a browser, for the link in the mail
    # (e.g. https://orders.example.com). Blank => the reset link cannot be built.
    app_url: str = ""
    password_reset_minutes: int = 60

    # --- CORS ----------------------------------------------------------------
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # Business details (GSTIN, name, delivery address, pin) are not settings any
    # more: they live in the Address Book, and an ordering run ships to an
    # address chosen there. There is deliberately no config fallback — the one
    # that used to be here is how orders went to the wrong place quietly.


@lru_cache
def get_settings() -> Settings:
    return Settings()
