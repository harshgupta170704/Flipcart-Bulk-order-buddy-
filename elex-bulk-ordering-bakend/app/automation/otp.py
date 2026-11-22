"""OTP reading — pluggable, with an IMAP default.

Replaces the legacy kuku.lu browser script. You chose "you decide / not sure
yet", so the default is an IMAP reader against a centralized inbox, behind an
``OtpReader`` interface. Until the inbox is configured, ``get_otp_reader``
returns a ``NullOtpReader`` that the login flow tolerates (it simply won't
auto-fill the OTP), so the rest of Phase 2 works regardless.

Swapping to a web-inbox scraper or an email API later means adding one more
``OtpReader`` subclass — nothing else changes.
"""

from __future__ import annotations

import asyncio
import logging
import email
import imaplib
import os
import re
import ssl
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.header import decode_header

from app.config import get_settings

# Flipkart login OTPs are 6 digits.
log = logging.getLogger("ordering.otp")

_OTP_RE = re.compile(r"\b(\d{6})\b")


class OtpNotConfigured(RuntimeError):
    """Raised when an OTP is needed but no reader is configured."""


#: A 6-digit run sitting next to wording that marks it as a one-time code.
#: Preferred over "any 6 digits", because a real email is full of six-digit
#: numbers — reference ids, phone fragments, pin codes — and picking the wrong
#: one means typing a wrong code into the login rather than simply failing.
_OTP_IN_CONTEXT = re.compile(
    r"(?:otp|one[\s\-]*time(?:\s+password)?|verification\s+code|security\s+code"
    r"|login\s+code|passcode|code\s+is)\b[^0-9]{0,40}(\d{6})"
    r"|(\d{6})[^0-9]{0,40}\b(?:is\s+your|is\s+the)\b[^0-9]{0,20}"
    r"(?:otp|code|password)",
    re.IGNORECASE,
)


def otp_from_text(text: str | None, *, require_context: bool = False) -> str | None:
    """Extract a 6-digit code from an email body/subject.

    Prefers a code that appears beside OTP wording. ``require_context`` refuses
    to fall back to "the first six digits anywhere", which is what produced
    codes like 128279 out of a recruitment email.
    """
    if not text:
        return None

    match = _OTP_IN_CONTEXT.search(text)
    if match:
        return match.group(1) or match.group(2)

    if require_context:
        return None

    plain = _OTP_RE.search(text)
    return plain.group(1) if plain else None


class OtpReader(ABC):
    @abstractmethod
    async def get_otp(
        self,
        address: str,
        *,
        timeout: int = 60,
        not_before: datetime | None = None,
    ) -> str | None:
        """Return the OTP sent to ``address`` within ``timeout`` seconds.

        ``not_before`` discards codes that predate the request. A mailbox
        usually holds earlier OTPs, and the newest of those is still recent
        enough to look valid — returning one means typing a dead code.
        """
        raise NotImplementedError


class NullOtpReader(OtpReader):
    """Used when no inbox is configured. Signals the caller to skip auto-OTP."""

    async def get_otp(
        self,
        address: str,
        *,
        timeout: int = 60,
        not_before: datetime | None = None,
    ) -> str | None:
        raise OtpNotConfigured(
            "OTP inbox is not configured. Set ORDERING_OTP_IMAP_* in .env to "
            "enable automatic OTP reading."
        )


def imap_ssl_context() -> ssl.SSLContext:
    """TLS settings for an inbox connection that check the server's
    certificate and its name.

    ``imaplib.IMAP4_SSL`` checks neither unless it is handed a context like
    this, so anyone between the app and the mail server could pose as the
    server and collect the inbox password. The certificate authorities are
    the system's list plus certifi's (declared in requirements.txt for this
    reason): the python.org Python on macOS has no list of its own
    until its "Install Certificates" step is run — on the owner's laptop it
    had not been — so the system's list alone would refuse Gmail in a server
    started without ``SSL_CERT_FILE``, and every login would wait in vain for
    its code. Both the OTP reader and the Settings page's inbox test use it.
    """
    context = ssl.create_default_context()
    try:
        import certifi

        context.load_verify_locations(cafile=certifi.where())
    except (ImportError, OSError):  # no certifi, or a broken one: the system's list alone, still checked
        pass
    return context


class ImapOtpReader(OtpReader):
    """Polls an IMAP mailbox for a recent message addressed to ``address`` and
    extracts the 6-digit code. Works with a catch-all inbox whose domain can
    change per run (we match on the recipient address, not a fixed domain).
    """

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        mailbox: str = "INBOX",
        use_ssl: bool = True,
        poll_interval: int = 3,
        senders: tuple[str, ...] = ("flipkart",),
        since_days: int = 1,
        max_age_minutes: int = 15,
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.mailbox = mailbox
        self.use_ssl = use_ssl
        self.poll_interval = poll_interval
        #: Only mail from these senders counts. The single most important
        #: filter on a personal mailbox: "addressed to me" matched 9,575
        #: messages there and a stray six-digit number in an unrelated email
        #: was being returned as the code.
        self.senders = tuple(x.strip().lower() for x in senders if x and x.strip())
        self.since_days = max(1, int(since_days or 1))
        self.max_age_minutes = max(1, int(max_age_minutes or 15))
        #: A certificate that fails the check is logged once per reader (one
        #: login), not on every poll.
        self._said_unverified = False

    def _too_old(self, message: email.message.Message) -> bool:
        """Ignore mail older than max_age_minutes.

        A code from an hour ago is not the one we just requested, and typing a
        stale code is a silent failure that looks like a browser problem.
        """
        sent = _parsed_date(message)
        if sent is None:
            return False  # cannot tell; let the context check decide
        age = datetime.now(timezone.utc) - sent
        return age > timedelta(minutes=self.max_age_minutes)

    async def get_otp(
        self,
        address: str,
        *,
        timeout: int = 60,
        not_before: datetime | None = None,
    ) -> str | None:
        loop = asyncio.get_event_loop()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            code = await loop.run_in_executor(
                None, self._fetch_code, address, not_before
            )
            if code:
                return code
            await asyncio.sleep(self.poll_interval)
        return None

    # --- blocking IMAP work (run in an executor) ----------------------------
    #: Searched in order after the configured mailbox turns up nothing. An OTP
    #: that lands in spam is otherwise invisible and the login just hangs —
    #: which is easy to hit, since a burst of identical code emails is exactly
    #: what spam filters react to. Names differ per provider, so we try the
    #: common ones and ignore the misses.
    _FALLBACK_MAILBOXES = (
        "[Gmail]/Spam",
        "[Google Mail]/Spam",
        "Spam",
        "Junk",
        "INBOX.Junk",
    )

    def _connect(self) -> imaplib.IMAP4:
        """Log in. The mailbox is selected per search, not here."""
        if self.use_ssl:
            # The server's certificate is checked before the password is sent.
            conn = imaplib.IMAP4_SSL(self.host, self.port, ssl_context=imap_ssl_context())
        else:
            conn = imaplib.IMAP4(self.host, self.port)
        conn.login(self.user, self.password)
        return conn

    def _mailboxes(self) -> list[str]:
        """Configured mailbox first, then the usual spam folders."""
        seen = {self.mailbox}
        return [self.mailbox] + [m for m in self._FALLBACK_MAILBOXES if m not in seen]

    #: Headers that can carry the real recipient. A catch-all that FORWARDS
    #: (e.g. Cloudflare Email Routing, InstAddr / m.kuku.lu -> Gmail) often
    #: leaves ``To:`` rewritten to the forwarding target; the delivery or
    #: forwarding headers below usually keep the original mailbox address.
    _RECIPIENT_HEADERS = (
        "Delivered-To",
        "X-Original-To",
        "X-Forwarded-To",
        "X-Forwarded-For",
        "Envelope-To",
        "X-Envelope-To",
        "Resent-To",
        "Original-Recipient",
        "X-Original-Recipient",
        "Cc",
    )
    #: Webmail forwarders sometimes put the forwarded address on sender headers
    #: instead of recipient headers.
    _FORWARDER_HEADERS = ("Reply-To", "Resent-From", "Return-Path")

    def _search_ids(
        self, conn: imaplib.IMAP4, address: str, *, deep: bool = False
    ) -> list[bytes]:
        """Recent message ids addressed to or forwarded for ``address``.

        Every search is scoped with ``SINCE`` to the last couple of days. That
        matters enormously: on a real mailbox (13k+ messages) an unscoped
        search takes minutes, and the login only waits 60 seconds for a code —
        so the poll would time out having never finished a single query, and
        report "no OTP" as though the inbox were empty.

        Forwarded copies are matched in three tiers, each strictly tied to
        ``address`` (audit F-12): ``TO``, forwarding headers / forwarded
        sender, and finally ``SINCE``-scoped ``TEXT`` for wrapped forwards
        whose headers omit the original recipient. Sender-only lookup stays
        behind ``deep=True`` for diagnostics only.
        """
        clean_address = (address or "").strip().replace('"', "")
        if not clean_address:
            return []
        since = (datetime.now() - timedelta(days=self.since_days)).strftime("%d-%b-%Y")
        found: list[bytes] = []
        seen: set[bytes] = set()

        def collect(*criteria: str) -> None:
            try:
                typ, data = conn.search(None, "SINCE", since, *criteria)
            except Exception:  # noqa: BLE001 — a server may reject a criterion
                return
            if typ != "OK" or not data or not data[0]:
                return
            for mid in data[0].split():
                if mid not in seen:
                    seen.add(mid)
                    found.append(mid)

        senders = self.senders or ("",)
        for sender in senders:
            sender_criteria = ("FROM", f'"{sender}"') if sender else ()
            collect(*sender_criteria, "TO", f'"{clean_address}"')
        if not found:
            # A forwarded copy may keep the original address in delivery /
            # forwarding headers or forwarder sender headers.
            for header in self._RECIPIENT_HEADERS:
                collect("HEADER", header, f'"{clean_address}"')
            collect("FROM", f'"{clean_address}"')
            for header in self._FORWARDER_HEADERS:
                collect("HEADER", header, f'"{clean_address}"')
        if not found:
            # Wrapped forwards (where both To: and forwarding headers hold only
            # the destination inbox) still mention the original address in the
            # subject or forwarded header block. Scoped by SINCE + exact
            # address verification in _fetch_code so parallel logins cannot mix.
            for sender in senders:
                if sender:
                    collect("FROM", f'"{sender}"', "TEXT", f'"{clean_address}"')
            if not found:
                collect("TEXT", f'"{clean_address}"')
        if not found and deep:
            # Sender only, any recipient: on the shared inbox this returns
            # whichever account's code arrived last, so with several logins
            # running at once account B would be handed account A's code (audit
            # F-12, 25 Oct 2026). Diagnostic only, never in the polling loop.
            for sender in senders:
                if sender:
                    collect("FROM", f'"{sender}"')
        return found

    def _fetch_code(
        self, address: str, not_before: datetime | None = None
    ) -> str | None:
        try:
            conn = self._connect()
        except ssl.SSLCertVerificationError as exc:
            # Not a passing hiccup: every poll would fail the same way, and
            # the login would only say it found no code.
            if not self._said_unverified:
                self._said_unverified = True
                log.error(
                    "the OTP inbox %s:%s was not logged in to: its certificate could not be verified (%s)",
                    self.host,
                    self.port,
                    exc,
                )
            return None
        except Exception:
            return None
        try:
            for mailbox in self._mailboxes():
                try:
                    typ, _ = conn.select(mailbox, readonly=True)
                except Exception:  # noqa: BLE001
                    continue  # folder does not exist on this provider
                if typ != "OK":
                    continue

                ids = self._search_ids(conn, address)
                # newest message first
                for msg_id in reversed(ids):
                    typ, msg_data = conn.fetch(msg_id, "(RFC822)")
                    if typ != "OK" or not msg_data:
                        continue
                    message = email.message_from_bytes(msg_data[0][1])
                    if self._too_old(message):
                        continue
                    if not_before is not None:
                        sent = _parsed_date(message)
                        if sent is not None and sent < not_before:
                            continue  # a code from before we asked
                    text = _message_text(message)
                    if not _message_targets_address(message, text, address):
                        continue
                    if not _message_matches_sender(message, text, self.senders):
                        continue
                    # require_context: a six-digit run must sit beside OTP
                    # wording. Without it, an unrelated email's reference
                    # number gets typed into the login as the code.
                    code = otp_from_text(text, require_context=True)
                    if code:
                        if mailbox != self.mailbox:
                            log.warning(
                                "OTP for %s was found in %r, not %r — add a filter "
                                "so these are never marked as spam.",
                                address,
                                mailbox,
                                self.mailbox,
                            )
                        return code
            return None
        finally:
            try:
                conn.logout()
            except Exception:
                pass


#: Tags whose contents are never visible text — dropped before searching so a
#: six-digit number inside a style rule or script cannot be mistaken for a code.
_INVISIBLE = re.compile(
    r"<(script|style|head)\b.*?</\1>", re.IGNORECASE | re.DOTALL
)
_TAGS = re.compile(r"<[^>]+>")


def _html_to_text(html: str) -> str:
    """Crude tag strip — enough to find a code, no parser dependency."""
    text = _INVISIBLE.sub(" ", html)
    text = _TAGS.sub(" ", text)
    # The usual entities that show up around a code in a styled email.
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&#8203;", "")):
        text = text.replace(entity, char)
    return text


def _parsed_date(message: email.message.Message) -> datetime | None:
    from email.utils import parsedate_to_datetime

    try:
        parsed = parsedate_to_datetime(message.get("Date", ""))
    except Exception:  # noqa: BLE001 — malformed Date headers are common
        return None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


_ADDRESS_HEADERS = (
    ("To", "From", "Subject") + ImapOtpReader._RECIPIENT_HEADERS + ImapOtpReader._FORWARDER_HEADERS
)
_SENDER_HEADERS = ("From", "Sender", "Reply-To", "Return-Path", "Subject")


def _decode_header_value(raw_header: str | None) -> str:
    """Decode an RFC 2047 email header safely to Unicode text."""
    if not raw_header:
        return ""
    out: list[str] = []
    for raw, enc in decode_header(raw_header):
        out.append(raw.decode(enc or "utf-8", "ignore") if isinstance(raw, bytes) else raw)
    return "".join(out)


def _message_targets_address(message: email.message.Message, text: str, address: str) -> bool:
    """Verify the message belongs to ``address`` with exact email boundaries.

    Prevents IMAP ``TEXT`` or substring header matches from confusing similar
    addresses (e.g. ``bob@catbar.net`` vs ``a.bob@catbar.net``) when several
    forwarded accounts log in concurrently.
    """
    wanted = (address or "").strip().lower()
    if not wanted:
        return False
    boundary = re.compile(
        rf"(?<![A-Za-z0-9._%+-]){re.escape(wanted)}(?![A-Za-z0-9._%+-])",
        re.IGNORECASE,
    )
    for header in _ADDRESS_HEADERS:
        for value in message.get_all(header) or ():
            if boundary.search(_decode_header_value(str(value))):
                return True
    return bool(boundary.search(text))


def _message_matches_sender(
    message: email.message.Message, text: str, senders: tuple[str, ...]
) -> bool:
    """Ensure forwarded messages match at least one configured sender keyword."""
    if not senders:
        return True
    header_text = " ".join(
        _decode_header_value(str(value))
        for header in _SENDER_HEADERS
        for value in (message.get_all(header) or ())
    )
    haystack = f"{header_text} {text}".lower()
    return any(sender in haystack for sender in senders)


def _message_text(message: email.message.Message) -> str:
    """Flatten subject and body into one searchable string.

    Reads text/plain AND text/html. Transactional mail like Flipkart's OTP is
    routinely HTML-only, so a plain-text-only reader finds nothing but the
    subject and silently reports "no OTP" — which looks like a broken mailbox
    rather than a parsing gap.
    """
    parts: list[str] = []

    subject = _decode_header_value(message.get("Subject", ""))
    if subject:
        parts.append(subject)

    def body_of(part: email.message.Message) -> None:
        content_type = part.get_content_type()
        if content_type not in ("text/plain", "text/html"):
            return
        payload = part.get_payload(decode=True)
        if not payload:
            return
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, "ignore")
        except LookupError:  # unknown charset name
            text = payload.decode("utf-8", "ignore")
        parts.append(_html_to_text(text) if content_type == "text/html" else text)

    if message.is_multipart():
        for part in message.walk():
            if not part.is_multipart():
                body_of(part)
    else:
        body_of(message)

    return " ".join(parts)


# --- More than one inbox (the owner's two accounts, 26 Oct 2026) ----------------
#: Beside the first inbox — the plain ``ORDERING_OTP_IMAP_*`` block, the one
#: the Settings page edits — further inboxes come from NUMBERED settings in the
#: environment or the ``.env`` file: ``ORDERING_OTP_IMAP_USER_2`` and
#: ``ORDERING_OTP_IMAP_PASSWORD_2`` are required; ``_HOST_2``, ``_PORT_2``,
#: ``_MAILBOX_2`` and ``_USE_SSL_2`` fall back to the first inbox's values;
#: ``ORDERING_OTP_IMAP_FOR_2`` lists the account emails that inbox serves
#: (commas; ``@domain`` for every account of a domain), the inbox's own address
#: when left out. Then ``_3``, ``_4`` and so on.
#:
#: Why: Flipkart mails an account's login code to that account's own address.
#: With one inbox configured, only the accounts whose mail lands there could be
#: logged in without a person — the owner's second account could be activated
#: only by swapping the lines in ``.env`` and restarting.
_EXTRA_INBOX_RE = re.compile(r"^ORDERING_OTP_IMAP_(USER|PASSWORD|HOST|PORT|MAILBOX|USE_SSL|FOR)_(\d{1,2})$")
_TRUE_WORDS = frozenset({"1", "true", "yes", "on", "t", "y"})
#: The ``.env`` the app itself reads (app.config.Settings), relative to the
#: working directory like every other setting. None reads the environment only.
ENV_FILE: str | None = ".env"


def _mask(address: str | None) -> str:
    """An address fit for a log line: ``ay…@gmail.com``."""
    text = (address or "").strip()
    local, at, domain = text.rpartition("@")
    if at and local and domain:
        return f"{local[:2]}…@{domain}"
    return f"<{len(text)} chars>"


@dataclass(frozen=True)
class InboxSpec:
    """One numbered inbox as the settings describe it. Holds the password:
    never log or return one whole — :meth:`summary` is the safe form."""

    number: int
    user: str
    password: str
    host: str
    port: int
    mailbox: str
    use_ssl: bool
    #: Lower-cased account emails, and ``@domain`` entries, the inbox serves.
    serves: tuple[str, ...]

    def summary(self) -> dict:
        return {"number": self.number, "user": self.user, "host": self.host, "serves": list(self.serves)}


def _env_values(environ: Mapping[str, str] | None, env_file: str | None) -> dict[str, str]:
    """The settings as the app sees them: the ``.env`` file, the environment
    over it — the precedence pydantic-settings applies to the first inbox."""
    values: dict[str, str] = {}
    if env_file and os.path.exists(env_file):
        try:
            from dotenv import dotenv_values

            values.update({key: value for key, value in dotenv_values(env_file).items() if value is not None})
        except Exception as exc:  # noqa: BLE001 — an unreadable file is no extra inbox, not a crash
            log.warning("could not read %s for numbered OTP inboxes: %s", env_file, exc)
    values.update(os.environ if environ is None else environ)
    return values


#: Gmail's IMAP server. A numbered inbox on a Gmail address that names no host
#: of its own is read here: the one server its App Password belongs to.
GMAIL_IMAP_HOST = "imap.gmail.com"
_GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})


def extra_otp_inboxes(
    settings, *, environ: Mapping[str, str] | None = None, env_file: str | None = None, use_env_file: bool = True
) -> list[InboxSpec]:
    """Inboxes 2, 3, … from the environment and the ``.env`` file.

    Each is read from the IMAP host it names. A Gmail address that names none
    is read from Gmail's, so a second Gmail needs nothing but its address and
    App Password; any other address borrows the first inbox's host from
    ``settings``, the server's own (never a host saved on the Settings page:
    :func:`get_otp_reader`). Port, mailbox and SSL come from the first inbox
    when its host is the one used, else they are 993, INBOX and SSL. An inbox
    that cannot be used is skipped with a warning naming the setting to fix,
    and :func:`settings_page_inboxes` lists it with the reason.
    """
    return _numbered_inboxes(settings, environ=environ, env_file=env_file, use_env_file=use_env_file)[0]


def _numbered_inboxes(
    settings, *, environ: Mapping[str, str] | None = None, env_file: str | None = None, use_env_file: bool = True
) -> tuple[list[InboxSpec], list[dict]]:
    """:func:`extra_otp_inboxes`, and apart from them the inboxes that cannot be
    used: ``{"number", "user", "problem"}`` each, never a password."""
    numbered: dict[int, dict[str, str]] = {}
    source = (env_file or ENV_FILE) if use_env_file else None
    for key, value in _env_values(environ, source).items():
        match = _EXTRA_INBOX_RE.match(key)
        if match is None:
            continue
        number = int(match.group(2))
        if number < 2:
            continue
        numbered.setdefault(number, {})[match.group(1).lower()] = (value or "").strip()

    first_host = (getattr(settings, "otp_imap_host", "") or "").strip()
    specs: list[InboxSpec] = []
    unused: list[dict] = []

    def skip(number: int, user: str, problem: str) -> None:
        log.warning("OTP inbox %d is not used: %s", number, problem)
        unused.append({"number": number, "user": user, "problem": problem})

    for number in sorted(numbered):
        raw = numbered[number]
        user, password = raw.get("user", ""), raw.get("password", "")
        if not user and not password:
            # Named but blank — "ORDERING_OTP_IMAP_USER_2=" left in a .env, or a
            # compose file passing ${…:-} on a server with no second inbox:
            # nothing to read, and nothing to warn about.
            continue
        if not user or not password:
            skip(number, user, f"ORDERING_OTP_IMAP_USER_{number} and ORDERING_OTP_IMAP_PASSWORD_{number} are both needed")
            continue
        # The host, and whether the first inbox lends its port, mailbox and SSL
        # with it: only when its host is the one used.
        if raw.get("host"):
            host, lent = raw["host"], True
        elif user.rpartition("@")[2].lower() in _GMAIL_DOMAINS:
            host, lent = GMAIL_IMAP_HOST, first_host.lower() == GMAIL_IMAP_HOST
        elif first_host:
            host, lent = first_host, True
        else:
            skip(
                number,
                user,
                f"it names no IMAP host (ORDERING_OTP_IMAP_HOST_{number}) and the server's .env file has none for "
                "the first inbox to lend it; a host saved on the Settings page is never lent to another inbox",
            )
            continue
        try:
            port = int(raw.get("port") or (getattr(settings, "otp_imap_port", 993) if lent else 993) or 993)
        except (TypeError, ValueError):
            skip(number, user, f"ORDERING_OTP_IMAP_PORT_{number} is not a number")
            continue
        if raw.get("use_ssl"):
            use_ssl = raw["use_ssl"].lower() in _TRUE_WORDS
        else:
            use_ssl = bool(getattr(settings, "otp_imap_use_ssl", True)) if lent else True
        mailbox = raw.get("mailbox") or ((getattr(settings, "otp_imap_mailbox", "") or "INBOX") if lent else "INBOX")
        serves = tuple(
            dict.fromkeys(part.strip().lower() for part in (raw.get("for") or user).split(",") if part.strip())
        )
        specs.append(
            InboxSpec(
                number=number,
                user=user,
                password=password,
                host=host,
                port=port,
                mailbox=mailbox,
                use_ssl=use_ssl,
                serves=serves,
            )
        )
    return specs, unused


def settings_page_inboxes(settings) -> tuple[list[dict], list[dict]]:
    """For the Settings page, in one read: the numbered inboxes in use, without
    their passwords, and those that cannot be used, with the reason."""
    specs, unused = _numbered_inboxes(settings)
    return [spec.summary() for spec in specs], unused


def extra_inbox_summaries(settings) -> list[dict]:
    """The numbered inboxes in use without their passwords."""
    return settings_page_inboxes(settings)[0]


class RoutedOtpReader(OtpReader):
    """One reader per inbox, chosen by the account being logged in.

    The account's own address wins, then an ``@domain`` entry, then the first
    inbox — so every account that worked before still works, and an account
    named for another inbox is read from it. Two accounts logging in at once
    through their own inboxes can never be handed each other's code.
    """

    def __init__(self, default: OtpReader, inboxes: Sequence[tuple[InboxSpec, OtpReader]]) -> None:
        self.default = default
        self.inboxes = list(inboxes)

    def reader_for(self, address: str) -> tuple[OtpReader, str]:
        """The reader for ``address`` and, for the log, which inbox it is."""
        wanted = (address or "").strip().lower()
        for spec, reader in self.inboxes:
            if wanted and wanted in spec.serves:
                who = spec.user if spec.number == 0 else f"inbox {spec.number} ({_mask(spec.user)})"
                return reader, who
        domain = wanted.rpartition("@")[2]
        if domain:
            for spec, reader in self.inboxes:
                if f"@{domain}" in spec.serves:
                    who = (
                        f"{spec.user} (every @{domain} account)"
                        if spec.number == 0
                        else f"inbox {spec.number} ({_mask(spec.user)}, every @{domain} account)"
                    )
                    return reader, who
        return self.default, "the main inbox"

    async def get_otp(
        self,
        address: str,
        *,
        timeout: int = 60,
        not_before: datetime | None = None,
    ) -> str | None:
        reader, label = self.reader_for(address)
        log.info("the login code for %s is read from %s", _mask(address), label)
        return await reader.get_otp(address, timeout=timeout, not_before=not_before)


_UNSET = object()


def _saved_instaddr() -> tuple[dict | None, tuple[str, ...]]:
    """Read the saved InstAddr browser state and domains from app_settings."""
    try:
        from app.automation import instaddr
        from app.db.base import SessionLocal

        with SessionLocal() as db:
            state = instaddr.load_state(db)
            if not state:
                return None, ()
            return state, instaddr.load_domains(db)
    except Exception:  # noqa: BLE001 — unit tests without DB stay clean
        return None, ()


def get_otp_reader(
    settings,
    extras: Sequence[InboxSpec] | None = None,
    *,
    instaddr_state: dict | None = _UNSET,  # type: ignore[assignment]
    instaddr_domains: Sequence[str] | None = None,
) -> OtpReader:
    """Build an OTP reader from settings; Null when no inbox is configured.

    With numbered inboxes beside the first (:func:`extra_otp_inboxes`; pass
    ``extras`` to say which instead of reading the environment), or a saved
    InstAddr (m.kuku.lu) web-mailbox session, the reader sends each account's
    login to the inbox that serves its address/domain and falls back to the
    first IMAP inbox for everyone else. The polling, sender and age rules are
    the first inbox's for all of them.
    """
    shared = dict(
        poll_interval=settings.otp_poll_interval,
        senders=tuple(
            x.strip()
            for x in (getattr(settings, "otp_sender_filter", "") or "").split(",")
            if x.strip()
        ),
        since_days=getattr(settings, "otp_since_days", 1),
        max_age_minutes=getattr(settings, "otp_max_age_minutes", 15),
    )
    primary: OtpReader
    if settings.otp_imap_host and settings.otp_imap_user:
        primary = ImapOtpReader(
            host=settings.otp_imap_host,
            port=settings.otp_imap_port,
            user=settings.otp_imap_user,
            password=settings.otp_imap_password,
            mailbox=settings.otp_imap_mailbox,
            use_ssl=settings.otp_imap_use_ssl,
            **shared,
        )
    else:
        primary = NullOtpReader()

    routed: list[tuple[InboxSpec, OtpReader]] = [
        (
            spec,
            ImapOtpReader(
                host=spec.host,
                port=spec.port,
                user=spec.user,
                password=spec.password,
                mailbox=spec.mailbox,
                use_ssl=spec.use_ssl,
                **shared,
            ),
        )
        for spec in (list(extras) if extras is not None else extra_otp_inboxes(get_settings()))
    ]

    if instaddr_state is _UNSET:
        ia_state, ia_domains = _saved_instaddr()
    else:
        ia_state = instaddr_state
        ia_domains = tuple(
            dict.fromkeys(str(d).strip().lower().lstrip("@") for d in (instaddr_domains or ("catbar.net",)) if str(d).strip())
        )
    if instaddr_domains is not None and ia_state:
        ia_domains = tuple(
            dict.fromkeys(str(d).strip().lower().lstrip("@") for d in instaddr_domains if str(d).strip())
        )

    if ia_state and ia_domains:
        from app.automation.instaddr import InstAddrOtpReader

        routed.append(
            (
                InboxSpec(
                    number=0,
                    user="InstAddr (m.kuku.lu)",
                    password="",
                    host="m.kuku.lu",
                    port=443,
                    mailbox="Inbox",
                    use_ssl=True,
                    serves=tuple(f"@{d}" for d in ia_domains),
                ),
                InstAddrOtpReader(
                    ia_state,
                    poll_interval=shared["poll_interval"],
                    senders=shared["senders"],
                    max_age_minutes=shared["max_age_minutes"],
                ),
            )
        )

    if not routed:
        return primary
    return RoutedOtpReader(primary, routed)
