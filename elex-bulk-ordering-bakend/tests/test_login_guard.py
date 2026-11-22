"""The sign-in throttle's counting (spec FR-056): every key carries its own
limit — an email's low, an address's high, because an office shares one — and
a lock lasts the window."""

from __future__ import annotations

from app.core.login_guard import WINDOW_SECONDS, Key, LoginGuard

EMAIL_LIMIT = 5
ADDRESS_LIMIT = 50


def _keys(email: str, address: str = "203.0.113.9") -> list[Key]:
    return [Key(f"login:email:{email}", EMAIL_LIMIT), Key(f"login:ip:{address}", ADDRESS_LIMIT)]


def test_an_email_locks_at_its_own_limit_while_the_address_it_shares_stays_open():
    guard = LoginGuard()
    alice = _keys("alice@example.com")
    for _ in range(EMAIL_LIMIT - 1):
        guard.failed(alice, now=100.0)
    assert guard.retry_after(alice, now=100.0) == 0
    guard.failed(alice, now=100.0)
    assert guard.retry_after(alice, now=100.0) == WINDOW_SECONDS
    # A colleague behind the same address: a fresh email, and no address lock.
    assert guard.retry_after(_keys("bob@example.com"), now=100.0) == 0


def test_an_address_locks_only_at_its_own_higher_limit():
    guard = LoginGuard()
    address = _keys("x")[1]
    for i in range(ADDRESS_LIMIT - 1):
        guard.failed(_keys(f"user{i}@example.com"), now=100.0)
    assert guard.retry_after([address], now=100.0) == 0
    guard.failed(_keys("one-more@example.com"), now=100.0)
    assert guard.retry_after([address], now=100.0) == WINDOW_SECONDS
    # ...and then even a never-seen email from that address must wait.
    assert guard.retry_after(_keys("innocent@example.com"), now=100.0) == WINDOW_SECONDS


def test_a_bare_key_name_counts_against_the_guards_own_limit():
    guard = LoginGuard(max_failures=2)
    guard.failed(["reset:email:x"], now=0.0)
    assert guard.retry_after(["reset:email:x"], now=0.0) == 0
    guard.failed(["reset:email:x"], now=0.0)
    assert guard.retry_after(["reset:email:x"], now=0.0) == WINDOW_SECONDS


def test_a_limit_below_one_still_allows_the_first_attempt_to_be_counted():
    """A misconfigured 0 must not lock a key nobody has tried yet."""
    guard = LoginGuard()
    key = Key("login:email:a@b.c", 0)
    assert guard.retry_after([key], now=0.0) == 0
    guard.failed([key], now=0.0)
    assert guard.retry_after([key], now=0.0) == WINDOW_SECONDS


def test_wrong_attempts_age_out_of_the_window_and_a_lock_ends_with_it():
    guard = LoginGuard()
    key = Key("login:email:a@b.c", 2)
    guard.failed([key], now=0.0)
    guard.failed([key], now=WINDOW_SECONDS + 1.0)  # the first one has aged out: one in the window
    assert guard.retry_after([key], now=WINDOW_SECONDS + 1.0) == 0
    guard.failed([key], now=WINDOW_SECONDS + 2.0)
    assert guard.retry_after([key], now=WINDOW_SECONDS + 2.0) == WINDOW_SECONDS
    assert guard.retry_after([key], now=2 * WINDOW_SECONDS + 3.0) == 0


def test_a_right_password_forgets_only_the_keys_it_is_told_to():
    guard = LoginGuard()
    email, address = _keys("a@b.c")
    for _ in range(EMAIL_LIMIT - 1):
        guard.failed([email, address], now=0.0)
    guard.succeeded([email])
    guard.failed([email, address], now=0.0)
    assert guard.retry_after([email], now=0.0) == 0  # the count started over at one
