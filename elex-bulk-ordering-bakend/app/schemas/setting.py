"""Pydantic schemas for the Settings API (PLAN.md §6 "Settings").

``SettingsUpdate`` forbids extra keys: that is the first line of defence that
stops ``database_url``/``jwt_secret`` being set over HTTP (the second is
``app_settings.EDITABLE_FIELDS``), and it makes FastAPI answer 422 naming the
offending key instead of silently ignoring it.
"""

from pydantic import BaseModel, ConfigDict, Field


class ExtraInboxOut(BaseModel):
    """A numbered OTP inbox from the server's settings — never its password."""

    number: int
    user: str
    host: str
    #: The account emails (or "@domain"s) whose login codes are read from it.
    serves: list[str]


class UnusedInboxOut(BaseModel):
    """A numbered OTP inbox the server's settings name but the tool cannot use,
    and why — never its password."""

    number: int
    user: str
    problem: str


class SettingsOut(BaseModel):
    """Effective settings as shown in the UI.

    Note what is absent: ``otp_imap_password``. Secrets are reported only as a
    ``*_set`` boolean, and because they are not declared here FastAPI strips
    them from the response even if a dict slipped one in.
    """

    max_concurrency: int
    #: Read-only: this server's own ORDERING_BROWSER_HEADLESS (true = hidden).
    #: Each machine decides it in its .env; nothing saved on this page beats it.
    browser_headless: bool
    proxy_server: str
    #: Automatic read-backs every N hours; 0 = only when someone presses the button.
    sync_every_hours: int
    track_every_minutes: int = 60
    out_for_delivery_minutes: int = 30
    otp_imap_host: str
    otp_imap_port: int
    otp_imap_user: str
    otp_imap_mailbox: str
    otp_imap_use_ssl: bool
    otp_poll_interval: int
    otp_fetch_timeout: int
    otp_imap_password_set: bool
    otp_sender_filter: str = "flipkart"
    otp_since_days: int = 1
    otp_max_age_minutes: int = 15
    employee_phones_assume_online: bool = False
    hdfc_sender_filter: str = "hdfc"
    hdfc_since_days: int = 30
    hdfc_auto_fetch_minutes: int = 0
    hdfc_batch_wait_minutes: int = 60
    hdfc_portal_url: str = "https://corpcardassist.hdfc.bank.in"
    hdfc_portal_user: str = ""
    hdfc_portal_password_set: bool = False
    hdfc_portal_otp_mobile: str = ""
    hdfc_portal_otp_email: str = ""
    hdfc_portal_otp_sender: str = "HDFC"
    hdfc_portal_step_seconds: int = 60
    #: Inboxes 2, 3, … (app.automation.otp): shown so nobody wonders where a
    #: second account's code came from; set in the server's .env, not here.
    otp_extra_inboxes: list[ExtraInboxOut] = Field(default_factory=list)
    #: Numbered inboxes the server's settings name but cannot be used, and why:
    #: their accounts' login codes are looked for in the first inbox meanwhile.
    otp_unused_inboxes: list[UnusedInboxOut] = Field(default_factory=list)


class SettingsUpdate(BaseModel):
    """Partial update. Omitted fields keep their current value; send "" to
    clear a text field back to empty."""

    model_config = ConfigDict(extra="forbid")

    max_concurrency: int | None = Field(default=None, ge=1)
    #: Set aside, never saved: accepted only so a Settings page from before
    #: 24 Nov 2026, which still sends its browser switch, can save the rest.
    browser_headless: bool | None = None
    proxy_server: str | None = None
    sync_every_hours: int | None = Field(default=None, ge=0, le=168)
    track_every_minutes: int | None = Field(default=None, ge=0, le=1440)
    out_for_delivery_minutes: int | None = Field(default=None, ge=0, le=1440)
    otp_imap_host: str | None = None
    otp_imap_port: int | None = Field(default=None, ge=1, le=65535)
    otp_imap_user: str | None = None
    otp_imap_password: str | None = None
    otp_imap_mailbox: str | None = None
    otp_imap_use_ssl: bool | None = None
    otp_poll_interval: int | None = Field(default=None, ge=1)
    otp_fetch_timeout: int | None = Field(default=None, ge=1)
    otp_sender_filter: str | None = None
    otp_since_days: int | None = Field(default=None, ge=1, le=30)
    otp_max_age_minutes: int | None = Field(default=None, ge=1, le=120)
    employee_phones_assume_online: bool | None = None
    hdfc_sender_filter: str | None = None
    hdfc_since_days: int | None = Field(default=None, ge=1, le=365)
    hdfc_auto_fetch_minutes: int | None = Field(default=None, ge=0, le=1440)
    hdfc_batch_wait_minutes: int | None = Field(default=None, ge=1, le=1440)
    hdfc_portal_url: str | None = None
    hdfc_portal_user: str | None = None
    hdfc_portal_password: str | None = None
    hdfc_portal_otp_mobile: str | None = None
    hdfc_portal_otp_email: str | None = None
    hdfc_portal_otp_sender: str | None = None
    hdfc_portal_step_seconds: int | None = Field(default=None, ge=10, le=300)
