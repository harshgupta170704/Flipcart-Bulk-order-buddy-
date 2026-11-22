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
    browser_headless: bool
    proxy_server: str
    #: Automatic read-backs every N hours; 0 = only when someone presses the button.
    sync_every_hours: int
    otp_imap_host: str
    otp_imap_port: int
    otp_imap_user: str
    otp_imap_mailbox: str
    otp_imap_use_ssl: bool
    otp_poll_interval: int
    otp_fetch_timeout: int
    otp_imap_password_set: bool
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
    browser_headless: bool | None = None
    proxy_server: str | None = None
    sync_every_hours: int | None = Field(default=None, ge=0, le=168)
    otp_imap_host: str | None = None
    otp_imap_port: int | None = Field(default=None, ge=1, le=65535)
    otp_imap_user: str | None = None
    otp_imap_password: str | None = None
    otp_imap_mailbox: str | None = None
    otp_imap_use_ssl: bool | None = None
    otp_poll_interval: int | None = Field(default=None, ge=1)
    otp_fetch_timeout: int | None = Field(default=None, ge=1)
