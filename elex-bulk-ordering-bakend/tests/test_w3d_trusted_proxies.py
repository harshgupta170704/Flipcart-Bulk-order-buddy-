"""The sign-in throttle sees each browser's own address behind a TLS proxy or
a load balancer (audit F-30).

nginx tells the API the address it sees, and behind another hop that address
is the hop's: one throttle count for every browser, which fifty wrong
passwords from anywhere would lock for everyone. ORDERING_TRUSTED_PROXIES
names those hops. At every start
frontend/docker-entrypoint.d/40-trusted-proxies.sh renders nginx's config
from frontend/nginx.conf with a set_real_ip_from line for each, then
real_ip_header X-Forwarded-For and real_ip_recursive on, so nginx takes the
browser's address from the header they add. Here the script runs as the image runs it, on the shipped template,
under each POSIX shell this machine has: the image's own is busybox's, and
dash is the strictest of the common ones.
"""

import ipaddress
import os
import random
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests import layout

#: Every test here runs the web image's own start-up script on its nginx config.
pytestmark = layout.needs_web
FRONTEND = layout.web()
TEMPLATE = FRONTEND / "nginx.conf"
SCRIPT = FRONTEND / "docker-entrypoint.d" / "40-trusted-proxies.sh"
DOCKERFILE = FRONTEND / "Dockerfile"
COMPOSE = layout.COMPOSE
MARKER = "@ORDERING_TRUSTED_PROXIES@"
REALIP = ("set_real_ip_from", "real_ip_header", "real_ip_recursive")
#: The locations that pass plain HTTP to the API, each telling it one address:
#: the invoices, the two card-sheet routes, the account import, the sheet of
#: order ids, the order report, and the rest of /api/.
API_LOCATIONS = 7

#: How to run the script under each shell found here.
SHELLS = [command for command in (["sh"], ["dash"], ["busybox", "sh"]) if shutil.which(command[0])]
SHELL_IDS = [command[0] for command in SHELLS]
each_shell = pytest.mark.parametrize("shell", SHELLS, ids=SHELL_IDS)


def _render(shell: list[str], tmp_path: Path, value: str | None, *, template: Path = TEMPLATE, previous: str | None = None):
    """Run the start-up script as the image's entrypoint does, with
    ORDERING_TRUSTED_PROXIES set to ``value`` (None: not set at all). It
    writes into ``tmp_path``; ``previous`` is a config already there."""
    output = tmp_path / "default.conf"
    if previous is not None:
        output.write_text(previous)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if value is not None:
        env["ORDERING_TRUSTED_PROXIES"] = value
    done = subprocess.run(
        [*shell, str(SCRIPT), str(template), str(output)], env=env, capture_output=True, text=True, timeout=60
    )
    return done, output


def _with_lines_under_marker(lines: list[str]) -> list[str]:
    """The template's lines with ``lines`` inserted right under the marker."""
    out: list[str] = []
    for line in TEMPLATE.read_text(encoding="utf-8").splitlines():
        out.append(line)
        if MARKER in line:
            out.extend(lines)
    return out


def _realip_lines(*proxies: str) -> list[str]:
    """What the script writes for ``proxies``, at the marker's indentation."""
    return (
        [f"    set_real_ip_from {proxy};" for proxy in proxies]
        + ["    real_ip_header X-Forwarded-For;", "    real_ip_recursive on;"]
    )


def _statements(text: str) -> list[tuple[int, str]]:
    """Every directive with its depth: 0 at the http level the file is
    included at, 1 inside ``server``, 2 inside a ``location``. A quoted value
    (the Content-Security-Policy's) is one argument, its own ';' included."""
    flat = " ".join(raw.split("#", 1)[0] for raw in text.splitlines())
    out: list[tuple[int, str]] = []
    depth, current, quoted = 0, "", False
    for char in flat:
        if char == '"':
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


# --- unset: the config as shipped ---------------------------------------------


@each_shell
@pytest.mark.parametrize("value", [None, "", "   ", " , ,\t\n"], ids=["unset", "empty", "blanks", "separators"])
def test_unset_the_config_is_the_shipped_file_byte_for_byte(shell, value, tmp_path):
    """Unset, the image's own copy of the config (the Dockerfile's COPY) is
    left as it is: the script writes nothing at all, so a read-only
    /etc/nginx/conf.d cannot stop the container."""
    shipped = TEMPLATE.read_text(encoding="utf-8")
    done, output = _render(shell, tmp_path, value, previous=shipped)
    assert done.returncode == 0, done.stderr
    assert output.read_bytes() == TEMPLATE.read_bytes()
    assert not (tmp_path / "default.conf.rendering").exists()


@each_shell
def test_unset_nothing_is_written_over_the_config_there(shell, tmp_path):
    done, output = _render(shell, tmp_path, "", previous="# the image's own copy\n")
    assert done.returncode == 0, done.stderr
    assert output.read_text(encoding="utf-8") == "# the image's own copy\n"


def test_the_shipped_file_trusts_no_proxy_of_its_own():
    """So the default render is today's config: every client's address is its
    connection's own, whatever X-Forwarded-For it sends, and nginx forwards
    exactly that one address."""
    statements = [statement for _, statement in _statements(TEMPLATE.read_text(encoding="utf-8"))]
    assert not [s for s in statements if s.startswith(REALIP)]
    assert [s for s in statements if s.startswith("proxy_set_header X-Forwarded-For")] == [
        "proxy_set_header X-Forwarded-For $remote_addr"
    ] * API_LOCATIONS


def test_the_template_has_one_marker_on_a_comment_line_inside_the_server_block():
    """The lines go in at server level: they then cover every location, and
    the access log's $remote_addr names the browser too."""
    lines = TEMPLATE.read_text(encoding="utf-8").splitlines()
    marked = [i for i, line in enumerate(lines) if MARKER in line]
    assert len(marked) == 1, f"{len(marked)} markers in nginx.conf"
    assert lines[marked[0]].strip().startswith("#"), "the marker must be a comment, harmless if nothing renders it"
    before = " ".join(line.split("#", 1)[0] for line in lines[: marked[0]])
    assert before.count("{") - before.count("}") == 1, "the marker must sit in the server block, outside any location"


# --- set: one line per proxy ---------------------------------------------------


@each_shell
def test_one_proxy_gets_its_line_and_the_two_header_settings_under_the_marker(shell, tmp_path):
    done, output = _render(shell, tmp_path, "172.18.0.1")
    assert done.returncode == 0, done.stderr
    assert output.read_text(encoding="utf-8").splitlines() == _with_lines_under_marker(_realip_lines("172.18.0.1"))


@each_shell
@pytest.mark.parametrize(
    "value",
    [
        "10.0.1.0/24,10.0.2.0/24,fc00::/7",
        "10.0.1.0/24 10.0.2.0/24 fc00::/7",
        " 10.0.1.0/24 ,, 10.0.2.0/24 ,fc00::/7, ",
        "10.0.1.0/24\t10.0.2.0/24\nfc00::/7\n",
    ],
    ids=["commas", "spaces", "untidy", "tabs-and-lines"],
)
def test_several_proxies_get_a_line_each_in_the_order_given(shell, value, tmp_path):
    done, output = _render(shell, tmp_path, value)
    assert done.returncode == 0, done.stderr
    assert output.read_text(encoding="utf-8").splitlines() == _with_lines_under_marker(
        _realip_lines("10.0.1.0/24", "10.0.2.0/24", "fc00::/7")
    )


@each_shell
def test_addresses_and_ranges_of_both_families_are_taken(shell, tmp_path):
    proxies = ["172.18.0.1", "10.0.0.0/8", "::1", "2001:db8:0:1::5/128", "FC00::/7", "1:2:3:4:5:6:7:8"]
    done, output = _render(shell, tmp_path, ", ".join(proxies))
    assert done.returncode == 0, done.stderr
    assert output.read_text(encoding="utf-8").splitlines() == _with_lines_under_marker(_realip_lines(*proxies))


def test_the_rendered_lines_sit_in_the_server_block_and_nginx_still_forwards_one_address(tmp_path):
    if not SHELLS:
        pytest.skip("no POSIX shell to run the start-up script with")
    done, output = _render(SHELLS[0], tmp_path, "172.18.0.1, 10.0.0.0/16")
    assert done.returncode == 0, done.stderr
    statements = _statements(output.read_text(encoding="utf-8"))
    realip = [(depth, s) for depth, s in statements if s.startswith(REALIP)]
    assert realip == [
        (1, "set_real_ip_from 172.18.0.1"),
        (1, "set_real_ip_from 10.0.0.0/16"),
        (1, "real_ip_header X-Forwarded-For"),
        (1, "real_ip_recursive on"),
    ]
    # $remote_addr is now the browser's, and it is still the one address the
    # API is told: never the appending form, which a browser could write into.
    forwarded = [s for _, s in statements if s.startswith("proxy_set_header X-Forwarded-For")]
    assert forwarded == ["proxy_set_header X-Forwarded-For $remote_addr"] * API_LOCATIONS
    assert not [s for _, s in statements if "proxy_add_x_forwarded_for" in s]


@each_shell
def test_the_start_up_log_names_what_nginx_trusts(shell, tmp_path):
    """docs/DEPLOY.md sends the owner to `docker compose logs web` for this line."""
    done, _ = _render(shell, tmp_path, "172.18.0.1,10.0.0.0/16")
    assert "on connections from: 172.18.0.1 10.0.0.0/16" in done.stdout
    done, _ = _render(shell, tmp_path, None)
    assert "ORDERING_TRUSTED_PROXIES is empty" in done.stdout


# --- refused: anything that is not an address ----------------------------------

NOT_ADDRESSES = [
    "proxy",  # a host name: nginx would look it up at start
    "cafe",  # hex letters only, still a host name
    "172.18.0.1;",
    "172.18.0.1}",
    "172.18.0.1;include",
    "$(id)",
    "`id`",
    "*",
    "10.0.0.1\\c",  # an echo would stop printing at the \c: the error must come out whole
    "1.2.3",
    "1.2.3.4.5",
    "256.0.0.1",
    "010.0.0.1",  # read as octal by some tools
    "10.0.0.0/33",
    "10.0.0.0/",
    "10.0.0.0/8/9",
    "10.0.0.0/08",
    "10.0.0.0/255.0.0.0",
    "fc00::/129",
    "1::2::3",
    ":::1",
    ":1::",
    "12345::",
    "1:2:3:4:5:6:7",
    "1:2:3:4:5:6:7:8:9",
    "::ffff:10.0.0.1",
    "fe80::1%eth0",
    "0.0.0.0/0",  # every address: any browser could name its own
    "10.0.0.0/0",
    "::/0",
]


@each_shell
@pytest.mark.parametrize("bad", NOT_ADDRESSES)
def test_a_value_that_is_not_an_address_stops_the_start_and_writes_nothing(shell, bad, tmp_path):
    done, output = _render(shell, tmp_path, f"172.18.0.1, {bad}", previous="the config already here\n")
    assert done.returncode != 0
    assert f'"{bad}"' in done.stderr and "nginx is not started" in done.stderr
    # One bad entry refuses the lot: nothing half-applied, nothing left over.
    assert output.read_text(encoding="utf-8") == "the config already here\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["default.conf"]


@each_shell
def test_a_line_break_cannot_smuggle_a_directive_in(shell, tmp_path):
    """Line breaks separate entries like spaces do, so the directive after one
    is an entry of its own, and refused like any other word."""
    done, output = _render(shell, tmp_path, "172.18.0.1\nreal_ip_header X-Real-IP;", previous="before\n")
    assert done.returncode != 0
    assert '"real_ip_header"' in done.stderr
    assert output.read_text(encoding="utf-8") == "before\n"


@each_shell
def test_the_lines_need_exactly_one_marker(shell, tmp_path):
    text = TEMPLATE.read_text(encoding="utf-8")
    without = tmp_path / "without.conf"
    without.write_text(text.replace(MARKER, "(no marker)"))
    twice = tmp_path / "twice.conf"
    twice.write_text(text.replace(f"# {MARKER}", f"# {MARKER}\n    # {MARKER}"))
    for template in (without, twice):
        done, output = _render(shell, tmp_path, "172.18.0.1", template=template, previous="before\n")
        assert done.returncode != 0, template.name
        assert MARKER in done.stderr and "nginx is not started" in done.stderr
        assert output.read_text(encoding="utf-8") == "before\n"
        assert not (tmp_path / "default.conf.rendering").exists()
    # Unset needs no place for lines: the config already there is left as it is,
    # whatever the template holds.
    done, output = _render(shell, tmp_path, None, template=without)
    assert done.returncode == 0, done.stderr
    assert output.read_text(encoding="utf-8") == "before\n"


def _corpus(count: int) -> list[tuple[str, bool]]:
    """Random real addresses, as written in the usual ways (IPv6 shortened,
    written out in full, in capitals), some with a prefix length; about half
    then take a slip of the keyboard or two (a character dropped, doubled or
    stray). Each comes with whether it was left intact. Seeded, so a failure
    replays."""
    rng = random.Random(30)
    out: list[tuple[str, bool]] = []
    while len(out) < count:
        if rng.random() < 0.5:
            text, longest = str(ipaddress.IPv4Address(rng.getrandbits(32))), 32
        else:
            groups = [rng.getrandbits(16) for _ in range(8)]
            start = rng.randrange(8)
            for i in range(start, min(8, start + rng.choice([0, 1, 2, 4]))):  # zeros, for a "::"
                groups[i] = 0
            address = ipaddress.IPv6Address(sum(g << (16 * (7 - i)) for i, g in enumerate(groups)))
            text, longest = rng.choice([str(address), address.exploded, str(address).upper()]), 128
            if "." in text:  # an IPv4 tail, which the script refuses on purpose
                continue
        if rng.random() < 0.6:
            text += f"/{rng.randint(1, longest)}"
        slips = rng.choice([0, 0, 1, 2])
        for _ in range(slips):
            i = rng.randrange(len(text))
            text = rng.choice([text[:i] + text[i + 1 :], text[:i] + text[i] + text[i:], text[:i] + rng.choice("0aF.:/") + text[i:]])
        out.append((text, slips == 0))
    return out


def test_whatever_the_script_takes_python_reads_as_an_address_or_network_too(tmp_path):
    """The script's own parsing is plain shell; Python's ipaddress is the
    second opinion. Every intact address must get through, and nothing the
    script lets through may be garbage to Python."""
    if not SHELLS:
        pytest.skip("no POSIX shell to run the start-up script with")
    everyday = [
        ("172.18.0.1", True), ("10.0.0.0/8", True), ("192.168.0.0/16", True), ("255.255.255.255/32", True),
        ("::1", True), ("fc00::/7", True), ("2001:db8::/32", True), ("2600:1f18:abcd:1200::/56", True),
        ("1:2:3:4:5:6:7:8", True), ("1:2:3:4:5:6:7::", True), ("::1:2:3:4:5:6:7", True), ("::", True),
    ]
    taken = 0
    for value, intact in everyday + _corpus(150):
        done, _ = _render(SHELLS[0], tmp_path, value)
        if intact:
            assert done.returncode == 0, f"{value}: {done.stderr}"
        if done.returncode == 0:
            taken += 1
            ipaddress.ip_network(value, strict=False)  # raises ValueError on garbage
            assert not value.endswith("/0"), value
    assert taken > 75, "the corpus barely reached the accepting side"


# --- wired into the image and the stack ----------------------------------------


def _final_stage() -> list[str]:
    """The Dockerfile's serving stage (after its last FROM), comments and
    blank lines stripped."""
    lines = [line.strip() for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()]
    lines = [line for line in lines if line and not line.startswith("#")]
    last_from = max(i for i, line in enumerate(lines) if line.startswith("FROM "))
    return lines[last_from:]


def test_the_image_renders_its_config_from_the_template_it_ships():
    stage = _final_stage()
    assert stage[0].startswith("FROM nginx"), "the official nginx image runs /docker-entrypoint.d/ at start"
    script = SCRIPT.read_text(encoding="utf-8")
    assert script.startswith("#!/bin/sh\n")
    template = re.search(r"^TEMPLATE=\$\{1:-([^}]+)\}$", script, re.MULTILINE).group(1)
    output = re.search(r"^OUTPUT=\$\{2:-([^}]+)\}$", script, re.MULTILINE).group(1)
    # The config is installed where nginx reads it, as before, so the image
    # serves the shipped file even should the start-up scripts not run...
    assert "COPY nginx.conf /etc/nginx/conf.d/default.conf" in stage
    assert output == "/etc/nginx/conf.d/default.conf"
    # ...and kept as the template the script renders it from at every start,
    # outside the image's own envsubst folder.
    assert f"COPY nginx.conf {template}" in stage
    assert not template.startswith("/etc/nginx/templates/")
    # The entrypoint runs only executable *.sh files from that folder.
    assert f"COPY docker-entrypoint.d/{SCRIPT.name} /docker-entrypoint.d/" in stage
    assert SCRIPT.name.endswith(".sh")
    assert f"RUN chmod 755 /docker-entrypoint.d/{SCRIPT.name}" in stage


def _compose_service(name: str) -> list[str]:
    """One service's lines in docker-compose.yml, comments and blanks stripped."""
    lines: list[str] = []
    inside = False
    for raw in COMPOSE.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 2 and line.strip().endswith(":"):
            inside = line.strip() == f"{name}:"
            continue
        if indent < 2:
            inside = False
        if inside:
            lines.append(line.strip())
    return lines


def test_compose_hands_the_setting_to_nginx_empty_by_default():
    """Set in the project's .env; left out, nginx trusts no one, as before."""
    assert "ORDERING_TRUSTED_PROXIES: ${ORDERING_TRUSTED_PROXIES:-}" in _compose_service("web")
