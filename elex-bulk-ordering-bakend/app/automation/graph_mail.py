"""OTP reading from a Microsoft 365 mailbox through Microsoft Graph.

Why not IMAP: Exchange Online refuses password (basic) logins over IMAP, so
:class:`app.automation.otp.ImapOtpReader` cannot open a Microsoft 365 inbox.
Graph with an app registration (client id + secret, ``Mail.Read`` application
permission) needs no person and no Authenticator prompt, and the secret lasts
up to two years.

The askcaayush.com set-up (26 Nov 2026): 6,823 addresses — aliases of
``prem@askcaayush.com`` and the proxy addresses of distribution groups
``otpgroup1..13`` whose only member is prem@ — all land in ONE inbox. The
``To:`` header still names the address Flipkart wrote to, so each login's code
is picked out by recipient, exactly like the shared IMAP inbox.

Many logins wait on that one inbox at once, so the inbox is read by ONE
shared poll per mailbox (cached for ``poll_interval`` seconds) and every
waiter picks its own message out of the same listing. Thirty logins at a
3-second interval are one Graph request per 3 seconds, not thirty — well
inside Graph's per-mailbox limit.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from app.automation.otp import OtpReader, _html_to_text, _mask, otp_from_text

log = logging.getLogger("ordering.otp.graph")

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"

#: Pages of 100 read per poll at most. A 15-minute window over 6,000 accounts
#: rarely holds more; past this the oldest mail is the least likely to matter.
_MAX_PAGES = 5
_SELECT = ",".join(
    (
        "subject",
        "from",
        "sender",
        "toRecipients",
        "ccRecipients",
        "bccRecipients",
        "receivedDateTime",
        "body",
        "internetMessageHeaders",
    )
)
#: Raw headers that name the address the mail was really sent to. Needed
#: because Exchange RESOLVES recipients: mail Flipkart sends to
#: ``bababi506@askcaayush.com`` — a proxy address of distribution group
#: ``otpgroup13`` — shows in Graph's ``toRecipients`` as
#: ``otpgroup13@askcaayush.com`` (seen live 27 Nov 2026). Only the raw
#: ``To:`` header and the ``Received: … for <address>`` line keep the
#: original, and matching on ``toRecipients`` alone would hand every account
#: in a group whichever code arrived last.
_RECIPIENT_HEADERS = frozenset(
    {
        "to",
        "cc",
        "delivered-to",
        "x-original-to",
        "envelope-to",
        "x-envelope-to",
        "resent-to",
        "original-recipient",
        "x-original-recipient",
        "received",
    }
)
#: Graph and our clock can disagree by a few seconds; a code Flipkart sent a
#: moment after we asked must not be dropped as "from before we asked".
_CLOCK_SKEW = timedelta(seconds=20)


class GraphError(RuntimeError):
    """Graph or the sign-in refused the request (bad secret, no consent, …)."""


@dataclass
class _Token:
    value: str = ""
    expires: float = 0.0


@dataclass
class _Listing:
    fetched: float = 0.0
    messages: list[dict] = field(default_factory=list)
    lock: asyncio.Lock | None = None


#: Shared across reader instances: tokens per (tenant, client), listings per
#: mailbox. A run builds a reader per account; they must still share one poll.
_TOKENS: dict[tuple[str, str], _Token] = {}
_LISTINGS: dict[str, _Listing] = {}


def _as_utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _parse_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return _as_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        return None


def _addresses(message: dict, *keys: str) -> list[str]:
    out: list[str] = []
    for key in keys:
        value = message.get(key)
        items = value if isinstance(value, list) else [value] if value else []
        for item in items:
            email_address = (item or {}).get("emailAddress") or {}
            out.append(f"{email_address.get('name') or ''} {email_address.get('address') or ''}")
    return out


def _boundary(address: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?<![A-Za-z0-9._%+-]){re.escape(address)}(?![A-Za-z0-9._%+-])",
        re.IGNORECASE,
    )


def message_code_for(
    message: dict,
    address: str,
    *,
    senders: tuple[str, ...],
    not_before: datetime | None,
    oldest: datetime,
) -> str | None:
    """The OTP in ``message`` when it is a fresh code sent to ``address``."""
    received = _parse_time(message.get("receivedDateTime"))
    if received is not None:
        if received < oldest:
            return None
        if not_before is not None and received < _as_utc(not_before) - _CLOCK_SKEW:
            return None

    body = message.get("body") or {}
    content = body.get("content") or ""
    if (body.get("contentType") or "").lower() == "html":
        content = _html_to_text(content)
    subject = message.get("subject") or ""
    text = f"{subject} {content}"

    wanted = _boundary(address.strip())
    raw = " ".join(
        str(header.get("value") or "")
        for header in message.get("internetMessageHeaders") or []
        if str(header.get("name") or "").lower() in _RECIPIENT_HEADERS
    )
    # The raw headers, when Graph returns them, name the real address; the
    # resolved toRecipients is only a fallback (it shows a group's own address).
    recipients = raw or " ".join(_addresses(message, "toRecipients", "ccRecipients", "bccRecipients"))
    # The recipient headers first; the body as a fallback for a wrapped copy
    # that names the address only in its text (same rule as the IMAP reader).
    if not (wanted.search(recipients) or wanted.search(text)):
        return None

    if senders:
        from_text = " ".join(_addresses(message, "from", "sender"))
        haystack = f"{from_text} {text}".lower()
        if not any(sender in haystack for sender in senders):
            return None

    return otp_from_text(text, require_context=True)


class GraphOtpReader(OtpReader):
    """Reads login codes for many addresses out of one Microsoft 365 mailbox."""

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        mailbox: str,
        poll_interval: int = 3,
        senders: tuple[str, ...] = ("flipkart",),
        max_age_minutes: int = 15,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.tenant_id = tenant_id.strip()
        self.client_id = client_id.strip()
        self.client_secret = client_secret
        self.mailbox = mailbox.strip()
        self.poll_interval = max(1, int(poll_interval or 3))
        self.senders = tuple(x.strip().lower() for x in senders if x and x.strip())
        self.max_age_minutes = max(1, int(max_age_minutes or 15))
        #: Tests hand in a mock transport; production uses the network.
        self._transport = transport
        self._said_error = False

    # --- Graph plumbing ------------------------------------------------------
    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=20, transport=self._transport)

    async def _token(self, client: httpx.AsyncClient) -> str:
        key = (self.tenant_id, self.client_id)
        token = _TOKENS.setdefault(key, _Token())
        if token.value and time.monotonic() < token.expires:
            return token.value
        response = await client.post(
            f"{LOGIN}/{self.tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
        )
        if response.status_code != 200:
            detail = response.json().get("error_description", response.text) if response.content else ""
            raise GraphError(f"Microsoft sign-in refused the app ({response.status_code}): {str(detail)[:300]}")
        payload = response.json()
        token.value = payload["access_token"]
        # Renew a minute early so a poll never carries an expiring token.
        token.expires = time.monotonic() + max(60, int(payload.get("expires_in", 3600)) - 60)
        return token.value

    async def _list_recent(self) -> list[dict]:
        """Every message in the mailbox (all folders, Junk included) received
        inside the freshness window, newest first."""
        since = datetime.now(timezone.utc) - timedelta(minutes=self.max_age_minutes)
        params = {
            "$filter": f"receivedDateTime ge {since.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "$orderby": "receivedDateTime desc",
            "$select": _SELECT,
            "$top": "100",
        }
        messages: list[dict] = []
        async with self._client() as client:
            token = await self._token(client)
            headers = {"Authorization": f"Bearer {token}", "Prefer": 'outlook.body-content-type="text"'}
            url: str | None = f"{GRAPH}/users/{self.mailbox}/messages"
            query: dict | None = params
            for _ in range(_MAX_PAGES):
                if not url:
                    break
                response = await client.get(url, params=query, headers=headers)
                if response.status_code == 401:
                    _TOKENS.pop((self.tenant_id, self.client_id), None)
                if response.status_code == 429:
                    break  # throttled: use what we have, the next poll retries
                if response.status_code != 200:
                    raise GraphError(f"Graph refused reading {self.mailbox} ({response.status_code}): {response.text[:300]}")
                payload = response.json()
                messages.extend(payload.get("value") or [])
                url, query = payload.get("@odata.nextLink"), None  # nextLink carries its own query
        return messages

    async def _listing(self) -> list[dict]:
        """The shared, briefly cached listing for this mailbox."""
        # Per event loop: an asyncio.Lock belongs to the loop it is used on,
        # and the CLI / tests run their own loops beside the server's.
        key = f"{self.mailbox.lower()}|{id(asyncio.get_running_loop())}"
        listing = _LISTINGS.setdefault(key, _Listing())
        if listing.lock is None:
            listing.lock = asyncio.Lock()
        async with listing.lock:
            if time.monotonic() - listing.fetched >= self.poll_interval:
                listing.messages = await self._list_recent()
                listing.fetched = time.monotonic()
            return listing.messages

    # --- OtpReader -----------------------------------------------------------
    def can_read(self, address: str) -> bool:
        return bool((address or "").strip()) and bool(self.tenant_id and self.client_id and self.client_secret)

    async def probe(self, address: str) -> str | None:
        """Really read the mailbox once: a wrong secret, a missing permission
        or a mailbox outside the app's scope shows here, before Flipkart is
        asked to send a code nobody could read."""
        if not self.can_read(address):
            return "the Microsoft 365 app (ORDERING_OTP_M365_*) is not fully set up"
        try:
            await self._listing()
        except (GraphError, httpx.HTTPError, ValueError, KeyError) as exc:
            return f"cannot read {self.mailbox}: {str(exc)[:200]}"
        return None

    async def find_code(self, address: str, not_before: datetime | None = None) -> str | None:
        oldest = datetime.now(timezone.utc) - timedelta(minutes=self.max_age_minutes)
        for message in await self._listing():  # newest first
            code = message_code_for(
                message, address, senders=self.senders, not_before=not_before, oldest=oldest
            )
            if code:
                return code
        return None

    async def get_otp(
        self,
        address: str,
        *,
        timeout: int = 60,
        not_before: datetime | None = None,
    ) -> str | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                code = await self.find_code(address, not_before)
            except (GraphError, httpx.HTTPError, ValueError, KeyError) as exc:
                if not self._said_error:
                    self._said_error = True
                    log.error("reading the Microsoft 365 inbox %s for %s failed: %s", self.mailbox, _mask(address), exc)
                code = None
            if code:
                return code
            await asyncio.sleep(self.poll_interval)
        return None
