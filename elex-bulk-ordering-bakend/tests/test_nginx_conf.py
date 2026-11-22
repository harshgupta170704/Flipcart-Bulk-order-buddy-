"""The browser-hardening headers nginx adds to every answer (audit F-29),
logs that keep no query string (audit F-29, F-20): an access log in a format
of its own, and an error log that writes no request line; and card sheets,
account sheets and sheets of order ids passed to the API without nginx
writing them to disk."""

import re
from pathlib import Path

import pytest

from tests import layout

#: Every test here reads the web app's nginx config.
pytestmark = layout.needs_web
CONF = layout.web("nginx.conf")

#: Log variables that hold a query string, or a whole address that can carry
#: one: the request line as sent, the URI as sent, the arguments (also one by
#: one, as $arg_<name>), and the Referer.
QUERY_CARRIERS = {"request", "request_uri", "args", "query_string", "http_referer"}


def _directives() -> list[str]:
    """Every directive in the file, comments stripped, one per entry."""
    out: list[str] = []
    for raw in CONF.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def _policy() -> dict[str, list[str]]:
    """The Content-Security-Policy header's directives, by name."""
    line = next(d for d in _directives() if d.startswith("add_header Content-Security-Policy "))
    assert line.endswith(" always;"), line
    value = line.split('"')[1]
    return {part.split()[0]: part.split()[1:] for part in (p.strip() for p in value.split(";")) if part}


def test_the_security_headers_are_set_for_every_answer():
    directives = _directives()
    assert "add_header X-Content-Type-Options nosniff always;" in directives
    assert "add_header X-Frame-Options DENY always;" in directives
    assert "add_header Referrer-Policy no-referrer always;" in directives
    assert any(d.startswith("add_header Content-Security-Policy ") for d in directives)
    # nginx drops server-level add_header lines inside any location that sets
    # its own; none may, or the headers silently vanish on those paths.
    inside_location = False
    for line in directives:
        if line.startswith("location "):
            inside_location = True
        elif line == "}":
            inside_location = False
        elif inside_location and line.startswith("add_header"):
            raise AssertionError(f"a location sets its own header, dropping the server's: {line}")


def test_only_the_apps_own_scripts_run():
    """No inline script, no eval, no other site's script: a script slipped
    into a page cannot run, so it cannot read the sign-in token."""
    policy = _policy()
    assert policy["script-src"] == ["'self'"]
    assert policy["default-src"] == ["'self'"]
    assert policy["object-src"] == ["'none'"]
    assert policy["frame-ancestors"] == ["'none'"]
    assert policy["base-uri"] == ["'self'"] and policy["form-action"] == ["'self'"]
    # Pictures: the app's own, inline data, and Flipkart's image hosts only.
    assert set(policy["img-src"]) == {"'self'", "data:", "https://*.flixcart.com", "https://*.flipkart.com"}
    # The live run view's socket goes back to this same server.
    assert policy["connect-src"] == ["'self'"]


def test_the_page_carries_no_inline_script_the_policy_would_block():
    """index.html's scripts all name a file; an inline one would be refused
    by script-src 'self' and the theme would flash on every load."""
    page = (CONF.parent / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", page, flags=re.S)
    assert scripts, "index.html has no script at all"
    for attributes, body in scripts:
        assert "src=" in attributes and not body.strip(), f"an inline script: {attributes!r}"
    assert (CONF.parent / "public" / "theme.js").is_file()


def _statements() -> list[tuple[int, str]]:
    """Every directive with its depth: 0 at the http level this file is
    included at, 1 inside ``server``, 2 inside a ``location``. A directive
    spread over several lines (log_format's) comes back as one."""
    text = " ".join(raw.split("#", 1)[0] for raw in CONF.read_text(encoding="utf-8").splitlines())
    out: list[tuple[int, str]] = []
    depth, current, quoted = 0, "", False
    for char in text:
        if char == '"':
            # A double-quoted value (the Content-Security-Policy's) holds its
            # own ';' and is one argument, not the end of a directive.
            quoted = not quoted
            current += char
        elif quoted:
            current += char
        elif char == ";":
            out.append((depth, " ".join(current.split())))
            current = ""
        elif char in "{}":
            depth += 1 if char == "{" else -1
            current = ""
        else:
            current += char
    return out


def _log_formats() -> dict[str, tuple[int, str]]:
    """Each log_format by name: its depth, and the format with its quoted parts run together."""
    formats: dict[str, tuple[int, str]] = {}
    for depth, statement in _statements():
        if statement.startswith("log_format "):
            formats[statement.split()[1]] = (depth, "".join(re.findall(r"'([^']*)'", statement)))
    return formats


def test_the_log_format_leaves_out_the_query_string():
    """nginx's usual format logs $request, the request line query and all, and
    a query string can hold the archive download's token or the ledger's card
    filter. The format here logs the path ($uri) and nothing that holds one."""
    formats = _log_formats()
    assert formats, "no log_format: nginx would log in its usual format, query strings and all"
    for name, (depth, fmt) in formats.items():
        # nginx takes log_format in the http block only, and refuses to start otherwise.
        assert depth == 0, f"log_format {name} must sit outside the server block"
        variables = set(re.findall(r"\$\{?(\w+)", fmt))
        assert "uri" in variables, f"log_format {name} does not log the path"
        leaks = (variables & QUERY_CARRIERS) | {v for v in variables if v.startswith("arg_")}
        assert not leaks, f"log_format {name} logs a query string: {', '.join('$' + v for v in sorted(leaks))}"


def test_the_server_logs_in_that_format():
    """The format counts only where an access log uses it. That access_log
    sits in the server block: in the http block it would log alongside the
    image's own (format "main", the whole request line), not instead of it."""
    formats = _log_formats()
    logs = [(depth, statement.split()) for depth, statement in _statements() if statement.startswith("access_log ")]
    assert any(depth == 1 for depth, _ in logs), "the server sets no access_log of its own"
    for depth, words in logs:
        line = " ".join(words)
        assert depth >= 1, f"an access_log in the http block adds to the image's own: {line}"
        assert words[1] == "off" or (len(words) > 2 and words[2] in formats), f"an access_log in another format: {line}"


#: The error-log levels that write no failed request: nginx logs a request
#: that fails on its way upstream at "error", with its whole request line.
_QUIET_LEVELS = {"crit", "alert", "emerg"}


def test_the_error_log_writes_no_request_line():
    """nginx's error log cannot leave the query string out of a failed
    request's line, so the server keeps it to critical lines; a failure still
    shows in the access log, path only, as a 502 or a 504."""
    logs = [(depth, statement.split()) for depth, statement in _statements() if statement.startswith("error_log ")]
    assert any(depth == 1 for depth, _ in logs), "the server sets no error_log of its own: the image's logs failed requests whole"
    for depth, words in logs:
        level = words[2].rstrip(";") if len(words) > 2 else "error"
        assert level in _QUIET_LEVELS, f"an error log that writes failed requests, query strings and all: {' '.join(words)}"


def _blocks() -> dict[tuple[str, ...], list[str]]:
    """Every directive, by the blocks it sits in: ``("server",)`` for the
    server's own, ``("server", "location = /x")`` for a location's."""
    text = " ".join(raw.split("#", 1)[0] for raw in CONF.read_text(encoding="utf-8").splitlines())
    blocks: dict[tuple[str, ...], list[str]] = {}
    inside: list[str] = []
    current, quoted = "", False
    for char in text:
        if char == '"':
            quoted = not quoted
            current += char
        elif quoted:
            current += char
        elif char == ";":
            blocks.setdefault(tuple(inside), []).append(" ".join(current.split()))
            current = ""
        elif char == "{":
            inside.append(" ".join(current.split()))
            current = ""
        elif char == "}":
            inside.pop()
            current = ""
        else:
            current += char
    return blocks


def _location_for(uri: str) -> tuple[str, list[str]]:
    """The location nginx picks for ``uri``: its line, and its directives.

    nginx's order: an exact match (``=``) is taken at once; otherwise the
    longest matching prefix is remembered, and taken at once if marked
    ``^~``; otherwise the first regular expression (``~``, ``~*``) that
    matches, in the order of the file; otherwise that longest prefix."""
    locations = []
    for inside, directives in _blocks().items():
        if len(inside) == 2 and inside[0] == "server" and inside[1].startswith("location "):
            words = inside[1].split()
            modifier, path = (words[1], words[2]) if len(words) == 3 else ("", words[1])
            locations.append((modifier, path, inside[1], directives))
    for modifier, path, line, directives in locations:
        if modifier == "=" and path == uri:
            return line, directives
    prefixes = [location for location in locations if location[0] in ("", "^~") and uri.startswith(location[1])]
    longest = max(prefixes, key=lambda location: len(location[1]), default=None)
    if longest is None or longest[0] != "^~":
        for modifier, path, line, directives in locations:
            if modifier in ("~", "~*") and re.search(path, uri, re.IGNORECASE if modifier == "~*" else 0):
                return line, directives
    assert longest is not None, f"no location takes {uri}"
    return longest[2], longest[3]


def _setting(directives: list[str], name: str, default: str) -> str:
    """A one-value directive as nginx applies it in a location: its own, else
    the server's, else nginx's default (the image's http block sets neither
    of the two asked about here)."""
    for scope in (directives, _blocks()[("server",)]):
        found = [statement.split()[1] for statement in scope if statement.split()[0] == name]
        if found:
            return found[-1]
    return default


def _bytes(size: str) -> int:
    """An nginx size ("1m", "512k", "25m", "1048576") in bytes."""
    units = {"k": 1024, "m": 1024**2, "g": 1024**3}
    size = size.lower()
    return int(size[:-1]) * units[size[-1]] if size[-1] in units else int(size)


@pytest.mark.parametrize("uri", ["/api/jobs/order", "/api/jobs/order/card-preview"])
def test_a_card_sheet_is_streamed_to_the_api_and_capped_at_a_megabyte(uri):
    """The two routes a card sheet is posted to. nginx writes a request body
    bigger than its memory buffer (8-16 KB, a sheet of a hundred cards or so)
    to a temp file before passing it on, unless the location streams it; and
    the API's form reader writes a file part over 1 MiB to disk. So whichever
    location nginx picks for them streams the body, and refuses one over 1m
    (0 would be no limit at all), and still hands the API the same request
    the /api/ location would, with the same headers and timeout."""
    line, directives = _location_for(uri)

    assert _setting(directives, "proxy_request_buffering", "on") == "off", f"{line} buffers a card sheet to disk"
    cap = _bytes(_setting(directives, "client_max_body_size", "1m"))
    assert 0 < cap <= 1024**2, f"{line} lets a card upload of {cap} bytes through"
    passed_to = [statement.split()[1] for statement in directives if statement.startswith("proxy_pass ")]
    assert passed_to == ["http://api:8000" + uri.removeprefix("/api")], f"{line} does not pass {uri} to the API"
    api_line, api_directives = _location_for("/api/accounts")
    assert api_line == "location /api/"
    assert {d for d in api_directives if not d.startswith("proxy_pass ")} <= set(directives)


@pytest.mark.parametrize("uri", ["/api/accounts/import", "/api/orders/import-ids", "/api/orders/import-report"])
def test_an_account_or_id_sheet_is_streamed_to_the_api_and_capped_at_25_megabytes(uri):
    """Either route can be handed the owner's workbook in the legacy layout,
    whose columns T to W hold every account's card number, expiry and CVV,
    and the workbook of every account is about 2 MB: far over nginx's memory
    buffer. So whichever location nginx picks for them streams the body,
    refuses one over 25m (0 would be no limit at all), and still hands the
    API the same request the /api/ location would, with the same headers and
    timeout."""
    line, directives = _location_for(uri)

    assert _setting(directives, "proxy_request_buffering", "on") == "off", f"{line} buffers the sheet to disk"
    cap = _bytes(_setting(directives, "client_max_body_size", "1m"))
    assert cap == 25 * 1024**2, f"{line} caps the sheet at {cap} bytes, not 25m"
    passed_to = [statement.split()[1] for statement in directives if statement.startswith("proxy_pass ")]
    assert passed_to == ["http://api:8000" + uri.removeprefix("/api")], f"{line} does not pass {uri} to the API"
    api_line, api_directives = _location_for("/api/accounts")
    assert api_line == "location /api/"
    assert {d for d in api_directives if not d.startswith("proxy_pass ")} <= set(directives)


@pytest.mark.parametrize("uri", ["/api/orders/export.csv", "/api/orders/export", "/api/jobs/order", "/api/accounts/import"])
def test_a_big_answer_is_never_written_to_disk_on_its_way_out(uri):
    """nginx writes an answer bigger than its memory buffers to a temp file
    while a slow browser drains it. The ledger's CSV and workbook carry full
    card numbers, so no API location may: proxy_max_temp_file_size 0 keeps the
    answer in memory and paces the API instead (recheck of 27 Oct 2026)."""
    line, directives = _location_for(uri)
    assert _setting(directives, "proxy_max_temp_file_size", "1024m") == "0", f"{line} may write an answer to disk"
