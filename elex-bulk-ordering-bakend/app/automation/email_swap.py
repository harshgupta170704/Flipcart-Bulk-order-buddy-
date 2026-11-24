"""Change the login email on a signed-in Flipkart account.

The profile page (``account?link=my_account``) lets a signed-in account edit its
login email: click **Edit** beside *Email Address*, type the new address, press
**SAVE**, then a **Verify OTP** modal asks for a code at each of two places.

Confirmed live (2026-11-24) on a @catbar.net account: the modal shows **two
email boxes** — one code sent to the *new* address, one to the *old* address —
and **no phone box**, because the mobile Flipkart has on file for these accounts
was never verified. Each OTP ``<input>``'s ``name`` attribute *is* its
destination address (``name="new@catbar.net"``, ``name="old@catbar.net"``,
maxlength 6), so this flow reads the right mailbox for each box and fills it.

An account whose mobile *is* verified gets a phone box instead of the old-email
box; that code goes to a number we do not hold, so such a row ends ``needs_phone``
and the account is left unchanged. Nothing commits until SUBMIT, so any row that
cannot read a code leaves the email as it was.

The new email must be a mailbox the OTP reader can read (our @catbar.net /
@apmail.org inboxes) and must NOT already be registered on Flipkart.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.automation.otp import OtpReader

log = logging.getLogger("ordering.email_swap")

#: The signed-in profile page that holds the editable email field.
PROFILE_URL = "https://www.flipkart.com/account?link=my_account"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_email(dest: str) -> bool:
    return bool(_EMAIL_RE.match(dest.strip()))


@dataclass
class SwapResult:
    ok: bool
    stage: str                 # "done" on success, else the failing step
    message: str = ""
    destinations: list[str] = field(default_factory=list)   # what the OTP modal asked for
    session_dead: bool = False  # True when the change signed the old session out (re-login needed)


# --- page helpers (JS so they survive Flipkart's class-name churn) -----------

_CLICK_EMAIL_EDIT = """() => {
  const all = [...document.querySelectorAll('*')];
  for (const n of all) {
    if (n.childNodes.length === 1 && (n.textContent || '').trim() === 'Email Address') {
      const scope = n.parentElement || n;
      const edit = [...scope.querySelectorAll('*')].find(e => (e.textContent || '').trim() === 'Edit');
      if (edit) { edit.click(); return true; }
    }
  }
  return false;
}"""

_FILL_EMAIL = """(ne) => {
  const inp = [...document.querySelectorAll('input[name=email]')].find(i => !i.disabled && !i.readOnly);
  if (!inp) return false;
  const set = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  set.call(inp, ''); inp.dispatchEvent(new Event('input', {bubbles: true}));
  set.call(inp, ne); inp.dispatchEvent(new Event('input', {bubbles: true}));
  inp.dispatchEvent(new Event('change', {bubbles: true}));
  return inp.value === ne;
}"""

_CLICK_LABEL = """(labels) => {
  const btns = [...document.querySelectorAll('button,[role=button]')];
  for (const want of labels) {
    const b = btns.find(x => (x.innerText || '').trim().toUpperCase() === want && !x.disabled);
    if (b) { b.click(); return want; }
  }
  return '';
}"""

_READ_EMAIL_VALUE = """() => {
  const inp = document.querySelector('input[name=email]');
  return inp ? (inp.value || '') : '';
}"""

#: The OTP boxes: single inputs, maxlength 6, whose name is the destination
#: (an email address, or a phone like "91-9009122239"). Returns those names.
_OTP_DESTINATIONS = """() => [...document.querySelectorAll('input')]
  .filter(i => i.maxLength === 6 && /@|\\d{4,}/.test(i.name || ''))
  .map(i => i.name)"""

#: Fill each OTP box (matched by its destination name) with its code.
_FILL_OTPS = """(pairs) => {
  const set = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  let filled = 0;
  for (const [dest, code] of pairs) {
    const inp = [...document.querySelectorAll('input')].find(i => (i.name || '') === dest);
    if (!inp) continue;
    inp.focus();
    set.call(inp, String(code));
    inp.dispatchEvent(new Event('input', {bubbles: true}));
    inp.dispatchEvent(new Event('change', {bubbles: true}));
    filled++;
  }
  return filled;
}"""

#: Read the page state after the OTP codes are submitted. A successful email
#: change signs the old session out (Flipkart's security behaviour), so the
#: win is "the OTP boxes are gone and we've been bounced to the login wall"
#: — not a profile re-read, which the dead session can no longer load. Wrong
#: codes instead leave the OTP boxes up with an error, still signed in.
_POST_SUBMIT_STATE = r"""() => {
  const otpBoxes = [...document.querySelectorAll('input')]
    .filter(i => i.maxLength === 6 && /@|\d{4,}/.test(i.name || '')).length;
  const emailInput = document.querySelector('input[name=email]');
  const txt = document.body.innerText || '';
  const loginWall = /Enter your (phone number|mobile)|Login to access your orders|Use Email-ID/i.test(txt);
  const updated = /email\s*(ID|address)?\s*(has been\s*)?updated|successfully updated/i.test(txt);
  const otpError = [...document.querySelectorAll('span,div,p,label')]
    .map(e => (e.innerText || '').trim())
    .find(t => t && t.length < 90 && /invalid|incorrect|expired|wrong|did not match|didn.t match/i.test(t)) || '';
  return { otpBoxes, emailVal: emailInput ? (emailInput.value || '') : null, loginWall, updated, otpError };
}"""

#: Short visible text that explains why a new email was refused (no modal shown).
_REJECT_TEXT = """() => {
  const bad = [...document.querySelectorAll('span,div,p,label')]
    .map(e => (e.innerText || '').trim())
    .filter(t => t && t.length < 120 && /(already|registered|in use|exists|not valid|invalid)/i.test(t)
                 && /email|address|account/i.test(t));
  return bad[0] || '';
}"""


async def run_email_swap(
    page,
    *,
    old_email: str,
    new_email: str,
    otp_reader: OtpReader,
    otp_wait: int = 120,
    settle_ms: int = 4000,
) -> SwapResult:
    """Change the signed-in account's email from ``old_email`` to ``new_email``.

    ``page`` must already carry the account's session cookies. ``ok`` is true
    only once the profile re-reads as the new address.
    """
    old_email = old_email.strip()
    new_email = new_email.strip()

    await page.goto(PROFILE_URL, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(settle_ms)
    if "/login" in (page.url or ""):
        return SwapResult(False, "not_signed_in", "session bounced to login")

    current = (await page.evaluate(_READ_EMAIL_VALUE)).strip()
    if current and current.lower() == new_email.lower():
        return SwapResult(True, "done", "email already set to the new address")
    if current and old_email and current.lower() != old_email.lower():
        return SwapResult(
            False, "wrong_account",
            f"profile shows {current!r}, expected {old_email!r} — not swapping the wrong account",
        )

    if not await page.evaluate(_CLICK_EMAIL_EDIT):
        return SwapResult(False, "no_edit", "could not open the Email Address editor")
    await page.wait_for_timeout(1200)
    if not await page.evaluate(_FILL_EMAIL, new_email):
        return SwapResult(False, "no_field", "editable email field not found")

    not_before = _utcnow()
    if not await page.evaluate(_CLICK_LABEL, ["SAVE"]):
        return SwapResult(False, "no_save", "SAVE button not found / disabled")

    # Wait for the Verify OTP modal (its boxes name their destinations).
    destinations: list[str] = []
    for _ in range(12):  # up to ~12s
        await page.wait_for_timeout(1000)
        destinations = await page.evaluate(_OTP_DESTINATIONS)
        if destinations:
            break
    if not destinations:
        reason = (await page.evaluate(_REJECT_TEXT)) or "no verification step appeared after SAVE"
        stage = "new_email_rejected" if re.search(r"already|registered|in use|exists", reason, re.I) else "no_modal"
        return SwapResult(False, stage, reason)

    log.info("email-swap %s -> %s: modal asks codes for %s", old_email, new_email, destinations)

    # Every box must be a mailbox we can read. A phone box (verified-mobile
    # account) cannot be read, so we stop before committing.
    phone_boxes = [d for d in destinations if not _is_email(d)]
    if phone_boxes:
        return SwapResult(
            False, "needs_phone",
            f"Flipkart wants a code at {', '.join(phone_boxes)} (a phone we don't hold)",
            destinations=destinations,
        )

    # Read each destination's code.
    codes: dict[str, str] = {}
    for dest in destinations:
        code = await otp_reader.get_otp(dest, timeout=otp_wait, not_before=not_before)
        if not code:
            return SwapResult(
                False, "no_code",
                f"no verification code reached {dest} within {otp_wait}s",
                destinations=destinations,
            )
        codes[dest] = code

    filled = await page.evaluate(_FILL_OTPS, [[d, codes[d]] for d in destinations])
    if filled < len(destinations):
        return SwapResult(False, "fill_failed", f"filled {filled}/{len(destinations)} OTP boxes",
                          destinations=destinations)
    await page.wait_for_timeout(600)
    await page.evaluate(_CLICK_LABEL, ["SUBMIT", "VERIFY", "SAVE", "CONFIRM", "CONTINUE"])

    # A successful change signs the old session out (Flipkart security), so we
    # read the page in place rather than re-loading the profile with cookies
    # that are now dead. The login wall lands a few seconds *after* the OTP
    # boxes clear, so poll: explicit "updated" text, or the boxes clearing and
    # the login wall appearing, mean it took; boxes still up *with an error*
    # mean the codes were rejected.
    state: dict = {}
    for _ in range(12):
        await page.wait_for_timeout(1000)
        state = await page.evaluate(_POST_SUBMIT_STATE)
        if state.get("updated") or (not state.get("otpBoxes") and state.get("loginWall")):
            return SwapResult(
                True, "done",
                f"email changed to {new_email} — Flipkart signed the old session out, so re-login is needed",
                destinations=destinations, session_dead=True,
            )
        if state.get("otpBoxes") and state.get("otpError"):
            return SwapResult(False, "invalid_otp", state["otpError"], destinations=destinations)

    if state.get("otpBoxes"):
        return SwapResult(
            False, "invalid_otp",
            state.get("otpError") or "codes were submitted but the OTP step did not clear",
            destinations=destinations,
        )

    # OTP step cleared without a login bounce: the session may have survived,
    # so the profile field is the last word.
    await page.goto(PROFILE_URL, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(settle_ms)
    if "/login" in (page.url or ""):
        return SwapResult(
            True, "done",
            f"email changed to {new_email} — Flipkart signed the old session out, so re-login is needed",
            destinations=destinations, session_dead=True,
        )
    now = (await page.evaluate(_READ_EMAIL_VALUE)).strip()
    if now.lower() == new_email.lower():
        return SwapResult(True, "done", f"email changed to {new_email}", destinations=destinations)
    return SwapResult(
        False, "unconfirmed",
        f"after submitting the codes the profile still shows {now!r}",
        destinations=destinations,
    )
