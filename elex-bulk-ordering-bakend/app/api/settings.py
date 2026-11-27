"""Settings API — the editable knobs from PLAN.md §6 ("Settings" page).

Protection is declared on the router, not per route, so a route added later
cannot accidentally ship unauthenticated.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool
from sqlalchemy.orm import Session as DbSession

from app.automation.otp import imap_ssl_context, settings_page_inboxes
from app.config import get_settings
from app.core import app_settings
from app.core.security import require_admin, require_user
from app.db.base import get_db
from app.schemas.setting import SettingsOut, SettingsUpdate

log = logging.getLogger("ordering.api.settings")

router = APIRouter(
    prefix="/settings",
    tags=["settings"],
    dependencies=[Depends(require_user)],
)


@router.get("", response_model=SettingsOut)
def read_settings(db: DbSession = Depends(get_db)) -> dict:
    return _with_server_values(app_settings.public_view(app_settings.get_effective(db)))


@router.put("", response_model=SettingsOut, dependencies=[Depends(require_admin)])
def update_settings(
    payload: SettingsUpdate, db: DbSession = Depends(get_db)
) -> dict:
    # exclude_unset: only what the client actually sent is touched.
    # exclude_none: an explicit null is "no change" rather than an error.
    values = payload.model_dump(exclude_unset=True, exclude_none=True)
    # A Settings page from before 24 Nov 2026 still sends its browser switch.
    # Hidden or shown is each machine's own .env now, so the switch is set
    # aside rather than refusing the rest of that page's save.
    if values.pop("browser_headless", None) is not None:
        log.info("settings save: browser_headless set aside — each machine's .env decides it")
    _refuse_moving_the_inbox_without_its_password(db, values)
    try:
        app_settings.set_overrides(db, values)
    except app_settings.SettingsValidationError as exc:
        # Unknown keys are already rejected by SettingsUpdate(extra="forbid");
        # this covers values that pass the schema but not the domain rules.
        raise HTTPException(
            status_code=422,
            detail=[{"loc": ["body", exc.key], "msg": exc.message}],
        ) from exc
    return _with_server_values(app_settings.public_view(app_settings.get_effective(db)))


def _refuse_moving_the_inbox_without_its_password(db: DbSession, values: dict) -> None:
    """422 when a save would point the saved inbox password somewhere new.

    The password on file belongs to the host and user it was saved with. A
    save that changes either, or turns SSL off for an inbox saved with it,
    without a new password would hand the old one to the new place at the
    next login run — the same thing the inbox test refuses (audit F-42). A
    blank host or user points it nowhere, so that is let through.
    """
    if values.get("otp_imap_password"):
        return
    saved = app_settings.get_effective(db)
    if not saved.get("otp_imap_password"):
        return
    host = str(values.get("otp_imap_host") or "").strip().lower()
    user = str(values.get("otp_imap_user") or "").strip()
    moved = (
        (host and host != str(saved.get("otp_imap_host") or "").strip().lower())
        or (user and user != str(saved.get("otp_imap_user") or "").strip())
        or (values.get("otp_imap_use_ssl") is False and bool(saved.get("otp_imap_use_ssl", True)))
    )
    if moved:
        raise HTTPException(
            status_code=422,
            detail=[
                {
                    "loc": ["body", "otp_imap_password"],
                    "msg": (
                        "Type the inbox password again: the saved one is only used with the host and "
                        "user it was saved for, and over SSL when it was saved with SSL."
                    ),
                }
            ],
        )


def _with_server_values(view: dict) -> dict:
    """What the page shows read-only from this server's own .env: whether its
    browser is hidden (``ORDERING_BROWSER_HEADLESS``, nothing saved here beats
    it); and the numbered inboxes, without their passwords, beside the
    editable first inbox, with any it names but cannot use and the reason, so
    a skipped inbox is not a mystery."""
    settings = get_settings()
    view["browser_headless"] = bool(settings.browser_headless)
    view["otp_extra_inboxes"], view["otp_unused_inboxes"] = settings_page_inboxes(settings)
    return view


# --- OTP inbox connection test -----------------------------------------------
class OtpInboxTestRequest(BaseModel):
    """Optional overrides so the inbox can be tested BEFORE it is saved.

    Anything omitted falls back to the saved settings, so an operator can click
    Test with an empty body to check what is already configured.
    """

    model_config = ConfigDict(extra="forbid")

    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = None
    mailbox: str | None = None
    use_ssl: bool | None = None


class OtpInboxTestResult(BaseModel):
    ok: bool
    message: str
    #: Messages currently in the mailbox — a quick sanity signal that we are
    #: looking at the right place.
    message_count: int | None = None


def _probe_inbox(
    host: str, port: int, user: str, password: str, mailbox: str, use_ssl: bool
) -> OtpInboxTestResult:
    """Connect, log in, select the mailbox, count messages, log out.

    Deliberately reports WHICH step failed: "wrong password" and "host
    unreachable" need completely different fixes, and a single "could not
    connect" sends people hunting in the wrong place.
    """
    import imaplib
    import socket
    import ssl

    conn = None
    try:
        try:
            # With SSL the server's certificate and name are checked before
            # the password is sent (app.automation.otp.imap_ssl_context).
            conn = (
                imaplib.IMAP4_SSL(host, port, timeout=15, ssl_context=imap_ssl_context())
                if use_ssl
                else imaplib.IMAP4(host, port, timeout=15)
            )
        except ssl.SSLCertVerificationError as exc:
            # An OSError too, but "could not reach" would send people hunting
            # for a network problem.
            return OtpInboxTestResult(
                ok=False,
                message=(
                    f"Reached {host}:{port}, but its certificate could not be verified, so the "
                    f"password was not sent. Check that the host is the mail server's own name. ({exc})"
                ),
            )
        except (OSError, socket.timeout) as exc:
            return OtpInboxTestResult(
                ok=False,
                message=f"Could not reach {host}:{port} — check the host, the port, and that SSL is set correctly. ({exc})",
            )

        try:
            conn.login(user, password)
        except imaplib.IMAP4.error as exc:
            return OtpInboxTestResult(
                ok=False,
                message=(
                    "Connected, but the login was rejected. For Gmail this "
                    "almost always means an app password is needed rather than "
                    f"the account password. ({exc})"
                ),
            )

        typ, data = conn.select(mailbox, readonly=True)
        if typ != "OK":
            return OtpInboxTestResult(
                ok=False, message=f"Logged in, but mailbox {mailbox!r} could not be opened."
            )
        count = int(data[0]) if data and data[0] else 0
        return OtpInboxTestResult(
            ok=True,
            message=f"Connected to {host} as {user}. Mailbox {mailbox!r} opened.",
            message_count=count,
        )
    except Exception as exc:  # noqa: BLE001
        return OtpInboxTestResult(ok=False, message=f"Unexpected error: {exc}")
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass


@router.post("/otp-inbox/test", response_model=OtpInboxTestResult, dependencies=[Depends(require_admin)])
async def test_otp_inbox(
    payload: OtpInboxTestRequest | None = None, db: DbSession = Depends(get_db)
) -> OtpInboxTestResult:
    """Check the OTP inbox actually works, without waiting for a login run.

    Without this the only feedback is a run that silently never finds a code.
    The password is read but never echoed back.

    The saved password goes only to the saved host, for the saved user: a test
    that names another host or user must bring that inbox's password, or it
    is answered without connecting anywhere. Otherwise an admin — or whoever
    holds an admin's session — could have the saved password sent to a server
    of their choosing (audit F-42, 25 Oct 2026).
    """
    saved = app_settings.get_effective(db)
    sent = payload or OtpInboxTestRequest()

    saved_host = saved.get("otp_imap_host") or ""
    saved_user = saved.get("otp_imap_user") or ""
    host = sent.host or saved_host
    user = sent.user or saved_user
    password = sent.password or ""
    if not password and saved.get("otp_imap_password"):
        # Host names are not case-sensitive; the user is compared as saved. A
        # test that turns SSL off for an inbox saved with it is another inbox
        # too: the password would cross the network in clear.
        same_host = host.strip().lower() == saved_host.strip().lower()
        same_user = user.strip() == saved_user.strip()
        saved_ssl = bool(saved.get("otp_imap_use_ssl", True))
        same_ssl = sent.use_ssl is None or bool(sent.use_ssl) == saved_ssl
        if not (same_host and same_user and same_ssl):
            return OtpInboxTestResult(
                ok=False,
                message=(
                    "The saved password is only sent to the saved host and user, over SSL when it "
                    "was saved with SSL. To test another host, user or connection, type that "
                    "inbox's password too."
                ),
            )
        password = saved["otp_imap_password"]
    if not (host and user and password):
        return OtpInboxTestResult(
            ok=False,
            message="OTP inbox is not configured yet — host, user and password are all required.",
        )

    return await run_in_threadpool(
        _probe_inbox,
        host,
        int(sent.port or saved.get("otp_imap_port") or 993),
        user,
        password,
        sent.mailbox or saved.get("otp_imap_mailbox") or "INBOX",
        bool(saved.get("otp_imap_use_ssl", True) if sent.use_ssl is None else sent.use_ssl),
    )
