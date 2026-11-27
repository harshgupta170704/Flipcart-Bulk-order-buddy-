"""Effective app settings: env defaults overlaid with what the Settings UI saved.

``app.config.Settings`` is read once from the environment at import time, which
is right for deployment-level values (database URL, JWT secret) but wrong for
the knobs the operator changes from the UI mid-flight — concurrency, proxy, OTP
inbox (PLAN.md §6 "Settings"). The business block (GSTIN, name, address, pin)
that once sat beside them moved to the Address Book; its keys are no longer
editable, and :func:`get_overrides` drops them from an older saved row. So did
``browser_headless`` on 24 Nov 2026: hidden or shown is each machine's own
choice, in its own .env (``ORDERING_BROWSER_HEADLESS``), never a value saved
here for every machine on the database at once.

Those live in one ``app_settings`` row keyed ``app`` as a JSON object, and
``get_effective`` merges them over the env values. Everything that needs a knob
(worker pool, OTP reader, ordering flow) reads it through here so there is a
single answer to "what is the current value".

Only ``EDITABLE_FIELDS`` can ever be stored or returned — deployment secrets
such as ``database_url`` and ``jwt_secret`` are deliberately not in that list
and must stay environment-only.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session as DbSession

from app.config import get_settings
from app.db.models import Setting

log = logging.getLogger("ordering.settings")

SETTINGS_KEY = "app"

#: The only keys the UI/API may write. Order is the display order in the UI.
EDITABLE_FIELDS: tuple[str, ...] = (
    "max_concurrency",
    "proxy_server",
    "sync_every_hours",
    "track_every_minutes",
    "out_for_delivery_minutes",
    "otp_imap_host",
    "otp_imap_port",
    "otp_imap_user",
    "otp_imap_password",
    "otp_imap_mailbox",
    "otp_imap_use_ssl",
    "otp_poll_interval",
    "otp_fetch_timeout",
    "otp_sender_filter",
    "otp_since_days",
    "otp_max_age_minutes",
    "employee_phones_assume_online",
    "hdfc_sender_filter",
    "hdfc_since_days",
    "hdfc_auto_fetch_minutes",
    "hdfc_batch_wait_minutes",
    "hdfc_portal_url",
    "hdfc_portal_user",
    "hdfc_portal_password",
    "hdfc_portal_otp_mobile",
    "hdfc_portal_otp_email",
    "hdfc_portal_otp_sender",
    "hdfc_portal_step_seconds",
)

#: Write-only fields: settable, never echoed back by the API (see public_view).
SECRET_FIELDS: tuple[str, ...] = ("otp_imap_password", "hdfc_portal_password")


class SettingsValidationError(ValueError):
    """Rejected setting. ``key`` lets the API name the offending field."""

    def __init__(self, key: str, message: str) -> None:
        super().__init__(f"{key}: {message}")
        self.key = key
        self.message = message


# --- Coercion / validation ---------------------------------------------------

_TRUE = {"1", "true", "yes", "on", "t", "y"}
_FALSE = {"0", "false", "no", "off", "f", "n", ""}


def _coerce_bool(key: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
    raise SettingsValidationError(key, "must be true or false")


def _coerce_int(key: str, value: Any, *, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool) or value is None:
        raise SettingsValidationError(key, "must be a whole number")
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    elif isinstance(value, str):
        try:
            number = int(value.strip())
        except ValueError:
            raise SettingsValidationError(key, "must be a whole number") from None
    else:
        raise SettingsValidationError(key, "must be a whole number")
    if number < minimum:
        raise SettingsValidationError(key, f"must be {minimum} or more")
    if maximum is not None and number > maximum:
        raise SettingsValidationError(key, f"must be {maximum} or less")
    return number


def _coerce_str(key: str, value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool) or isinstance(value, (dict, list)):
        raise SettingsValidationError(key, "must be text")
    if isinstance(value, (int, float)):
        # A number typed into a text setting is kept as text, not refused.
        return str(value)
    if isinstance(value, str):
        return value.strip()
    raise SettingsValidationError(key, "must be text")


#: The most accounts one run works at once. Far above what Flipkart tolerates
#: (it starts challenging sessions past about eight); the ceiling is there so
#: a slip of the finger — 50 typed as 5000 — cannot start thousands of
#: browsers on the machine.
MAX_CONCURRENCY_CEILING = 50
#: Seconds between two looks at the OTP inbox, and how long a code is waited
#: for. A poll every minute or more would miss codes that expire; a wait under
#: five seconds gives up before a mail can arrive, and one over ten minutes
#: holds a browser open on a code that is long dead.
OTP_POLL_RANGE = (1, 60)
OTP_WAIT_RANGE = (5, 600)
#: What a proxy address may start with (Chromium's own list).
PROXY_SCHEMES = ("http", "https", "socks5", "socks4")


def _coerce_proxy(key: str, value: Any) -> str:
    """A proxy address as the browser takes it — ``http://host:port``, with or
    without ``user:password@`` — or blank for none. Anything else is refused
    here, with words, rather than failing every run later with the browser's
    own error."""
    from urllib.parse import urlsplit

    text = _coerce_str(key, value)
    if not text:
        return ""
    try:
        # "host:port" alone is an HTTP proxy to the browser; read it as one.
        parts = urlsplit(text if "://" in text else f"http://{text}")
        host, _port = parts.hostname, parts.port  # .port raises on a port that is not a number
    except ValueError:
        host, parts = None, None
    if parts is None or parts.scheme.lower() not in PROXY_SCHEMES or not host or " " in text:
        raise SettingsValidationError(
            key, "must look like http://host:port (or http://user:password@host:port); leave it blank for no proxy"
        )
    return text


def _coerce(key: str, value: Any) -> Any:
    if key == "max_concurrency":
        # 0 would silently stall every job, so 1 is the floor.
        return _coerce_int(key, value, minimum=1, maximum=MAX_CONCURRENCY_CEILING)
    if key == "otp_imap_port":
        return _coerce_int(key, value, minimum=1, maximum=65535)
    if key == "sync_every_hours":
        # 0 turns the timer off; a week is the longest gap worth calling automatic.
        return _coerce_int(key, value, minimum=0, maximum=168)
    if key in ("track_every_minutes", "out_for_delivery_minutes", "hdfc_auto_fetch_minutes"):
        return _coerce_int(key, value, minimum=0, maximum=1440)
    if key == "hdfc_batch_wait_minutes":
        return _coerce_int(key, value, minimum=1, maximum=1440)
    if key == "hdfc_since_days":
        return _coerce_int(key, value, minimum=1, maximum=365)
    if key == "hdfc_portal_step_seconds":
        return _coerce_int(key, value, minimum=10, maximum=300)
    if key == "otp_since_days":
        return _coerce_int(key, value, minimum=1, maximum=30)
    if key == "otp_max_age_minutes":
        return _coerce_int(key, value, minimum=1, maximum=120)
    if key == "otp_poll_interval":
        return _coerce_int(key, value, minimum=OTP_POLL_RANGE[0], maximum=OTP_POLL_RANGE[1])
    if key == "otp_fetch_timeout":
        return _coerce_int(key, value, minimum=OTP_WAIT_RANGE[0], maximum=OTP_WAIT_RANGE[1])
    if key in ("otp_imap_use_ssl", "employee_phones_assume_online"):
        return _coerce_bool(key, value)
    if key == "proxy_server":
        return _coerce_proxy(key, value)
    if key == "hdfc_portal_otp_mobile":
        text = _coerce_str(key, value)
        if not text:
            return ""
        digits_only = "".join(ch for ch in text if ch.isdigit())
        if len(digits_only) == 12 and digits_only.startswith("91"):
            digits_only = digits_only[2:]
        if len(digits_only) != 10:
            raise SettingsValidationError(key, "must be a 10-digit mobile number or blank")
        return digits_only
    return _coerce_str(key, value)


def validate_overrides(values: dict) -> dict:
    """Whitelist + coerce a partial settings payload. Raises on anything else."""
    if not isinstance(values, dict):
        raise SettingsValidationError("body", "must be an object")
    cleaned: dict[str, Any] = {}
    for key, value in values.items():
        if key not in EDITABLE_FIELDS:
            raise SettingsValidationError(key, "is not an editable setting")
        cleaned[key] = _coerce(key, value)
    return cleaned


# --- Storage -----------------------------------------------------------------


def get_overrides(db: DbSession) -> dict:
    """Saved overrides only (no env defaults). Unknown keys are filtered out so
    a key removed from the whitelist can never leak back into effect."""
    row = db.get(Setting, SETTINGS_KEY)
    if row is None or not isinstance(row.value, dict):
        return {}
    return {k: v for k, v in row.value.items() if k in EDITABLE_FIELDS}


def set_overrides(db: DbSession, values: dict) -> dict:
    """Merge a partial payload into the saved overrides and return the result.

    The proxy address is shown with its sign-in masked (:func:`public_view`),
    and the Settings page sends the whole card back on Save. A proxy that
    comes back exactly as it was shown is therefore "unchanged", and the saved
    one — sign-in and all — is kept: until 11 Nov 2026 the mask itself was
    saved, so changing the concurrency silently replaced the proxy's password
    with "***" and every run after it failed at the proxy.
    """
    values = dict(values) if isinstance(values, dict) else values
    if isinstance(values, dict) and isinstance(values.get("proxy_server"), str):
        current = str(get_effective(db).get("proxy_server") or "")
        sent = values["proxy_server"].strip()
        if current and sent == mask_proxy(current):
            del values["proxy_server"]
        elif "***@" in sent:
            raise SettingsValidationError(
                "proxy_server",
                "still has *** where the proxy's sign-in goes: type the whole address again, or leave the field as it was",
            )
    cleaned = validate_overrides(values)
    merged = {**get_overrides(db), **cleaned}

    row = db.get(Setting, SETTINGS_KEY)
    if row is None:
        db.add(Setting(key=SETTINGS_KEY, value=merged))
    else:
        # Reassign rather than mutate: a plain JSON column isn't change-tracked,
        # so an in-place update would never be flushed.
        row.value = merged
    db.commit()
    log.info("settings updated: %s", ", ".join(sorted(cleaned)) or "(nothing)")
    return merged


def get_effective(db: DbSession) -> dict:
    """Env defaults for every editable field, overlaid with saved overrides.
    ``browser_headless`` is not among them: read ``get_settings()`` for it."""
    settings = get_settings()
    effective = {field: getattr(settings, field) for field in EDITABLE_FIELDS}
    effective.update(get_overrides(db))
    return effective


def public_view(effective: dict) -> dict:
    """Strip secrets for API responses, replacing each with a ``<name>_set`` flag.

    A password that has been saved must never travel back to the browser; the
    UI only needs to know whether one is configured.
    """
    public = {k: v for k, v in effective.items() if k not in SECRET_FIELDS}
    for secret in SECRET_FIELDS:
        public[f"{secret}_set"] = bool(effective.get(secret))
    # A proxy address may carry a sign-in ("http://user:pass@host:port"); the
    # host is what an operator needs to see, the sign-in is not (audit F-15).
    if public.get("proxy_server"):
        public["proxy_server"] = mask_proxy(public["proxy_server"])
    return public


def mask_proxy(value: str) -> str:
    """``http://user:pass@host:port`` shown as ``http://***@host:port``."""
    scheme, sep, rest = value.partition("://")
    head = f"{scheme}{sep}" if sep else ""
    body = rest if sep else value
    if "@" not in body:
        return value
    return f"{head}***@{body.rsplit('@', 1)[1]}"
