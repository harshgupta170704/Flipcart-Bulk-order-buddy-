#!/bin/sh
# vim:sw=4:ts=4:et
#
# Renders nginx's site config from its template at every start, adding the
# realip lines for the proxies ORDERING_TRUSTED_PROXIES names (audit F-30,
# 26 Oct 2026).
#
# Behind a TLS proxy or a load balancer, every connection nginx sees comes
# from that hop. nginx tells the API the address it sees (X-Forwarded-For, in
# frontend/nginx.conf), the sign-in throttle counts wrong passwords by it, and
# so every browser would share one count: fifty wrong passwords from anywhere
# would lock everyone out. For a connection from an address listed here,
# nginx takes the browser's address from the X-Forwarded-For header that hop
# added instead (ngx_http_realip_module), reading it from the right past every
# listed address, so an address a browser wrote into the header itself is
# never reached — provided each listed hop writes the header itself rather
# than passing on the browser's (DEPLOY.md §4 "TLS"). A connection from
# anywhere else keeps its own address.
#
# ORDERING_TRUSTED_PROXIES holds IPv4 or IPv6 addresses or CIDRs, separated by
# spaces or commas: "172.18.0.1", or "10.0.1.0/24, 10.0.2.0/24". Empty, the
# default, writes nothing: the config stays the image's own copy of the
# template, byte for byte. Anything
# else (a host name, a typo, a stray ";") stops the container here, before
# nginx starts: a value nginx skipped would quietly bring the one shared count
# back, and text copied into the config could add directives of its own. A /0
# is refused too: it would trust every address, and any browser could name
# whichever address it liked.
#
# The image's entrypoint (/docker-entrypoint.sh) runs every executable *.sh in
# /docker-entrypoint.d/ before nginx and starts nothing when one fails. The
# two arguments are for the tests (backend/tests/test_w3d_trusted_proxies.py):
# the template, then the file to write.

set -eu

ME=${0##*/}
TEMPLATE=${1:-/etc/nginx/ordering/nginx.conf.template}
OUTPUT=${2:-/etc/nginx/conf.d/default.conf}
# The comment line in the template that the realip lines are written under.
MARKER=@ORDERING_TRUSTED_PROXIES@

TAB=$(printf '\t')
NL='
'
BLANKS=" $TAB"

entrypoint_log() {
    if [ -z "${NGINX_ENTRYPOINT_QUIET_LOGS:-}" ]; then
        echo "$@"
    fi
}

# printf, not echo: the message quotes the refused value, and busybox's and
# dash's echo would act on a backslash in it ("\c" cuts the line short).
refuse() {
    printf '%s\n' "$ME: ERROR: $*; nginx is not started" >&2
    exit 1
}

# A decimal number from 0 to $2, written without a leading zero (some tools
# read 010 as octal; nobody means that here).
number_upto() {
    case $1 in
        '' | *[!0-9]* | 0?*) return 1 ;;
    esac
    [ "${#1}" -le 3 ] && [ "$1" -le "$2" ]
}

# Four decimal parts from 0 to 255: 172.18.0.1.
is_ipv4() {
    case $1 in
        *.*.*.*.*) return 1 ;;
        *.*.*.*) ;;
        *) return 1 ;;
    esac
    rest=$1
    for _part in 1 2 3 4; do
        number_upto "${rest%%.*}" 255 || return 1
        rest=${rest#*.}
    done
}

# Up to eight groups of one to four hex digits joined by ":", with at most one
# "::" standing for the groups left out: fc00::, 2001:db8:0:1::5. An IPv4 tail
# (::ffff:10.0.0.1) is refused; the IPv4 address itself is the way to write it.
is_ipv6() {
    case $1 in
        *:*) ;;
        *) return 1 ;;
    esac
    case $1 in
        *[!0-9A-Fa-f:]* | *:::* | *::*::*) return 1 ;;
        # A single ":" can neither open nor close an address; only "::" can.
        :[!:]* | *[!:]:) return 1 ;;
    esac
    groups=0
    rest=$1:
    while [ -n "$rest" ]; do
        group=${rest%%:*}
        rest=${rest#*:}
        [ "${#group}" -le 4 ] || return 1
        [ -z "$group" ] || groups=$((groups + 1))
    done
    case $1 in
        *::*) [ "$groups" -le 7 ] ;;
        *) [ "$groups" -eq 8 ] ;;
    esac
}

# An address, or an address with a prefix length: 172.18.0.1, 10.0.0.0/16,
# fc00::/7.
is_address_or_cidr() {
    case $1 in
        */*) address=${1%%/*} prefix=${1#*/} ;;
        *) address=$1 prefix= ;;
    esac
    if is_ipv4 "$address"; then
        longest=32
    elif is_ipv6 "$address"; then
        longest=128
    else
        return 1
    fi
    case $1 in
        */*) number_upto "$prefix" "$longest" ;;
    esac
}

# Spaces, tabs, line breaks and commas all separate entries. File-name
# expansion is off while they are split, so a "*" stays a "*" (and is
# refused) instead of turning into the names of the files here.
set -f
IFS=" ,$TAB$NL"
proxies=
for entry in ${ORDERING_TRUSTED_PROXIES:-}; do
    [ -n "$entry" ] || continue
    case $entry in
        */0) refuse "ORDERING_TRUSTED_PROXIES: \"$entry\" would trust every address, and any browser could name its own; list the proxy's own address or range" ;;
    esac
    is_address_or_cidr "$entry" ||
        refuse "ORDERING_TRUSTED_PROXIES: \"$entry\" is not an IP address or CIDR (for example 172.18.0.1 or 10.0.0.0/16)"
    proxies="$proxies $entry"
done
IFS=" $TAB$NL"
set +f

if [ -z "$proxies" ]; then
    entrypoint_log "$ME: info: ORDERING_TRUSTED_PROXIES is empty: every client's address is its connection's own"
    # Nothing to add: the image already holds the shipped config as
    # default.conf (the Dockerfile's COPY), so it is left exactly as it is.
    # Nothing is written, and /etc/nginx/conf.d need not even be writable.
    exit 0
fi
entrypoint_log "$ME: info: taking the browser's address from X-Forwarded-For on connections from:$proxies"

[ -r "$TEMPLATE" ] || refuse "cannot read the template $TEMPLATE"

# Written beside the config and then moved over it, so nginx never meets half
# a file; the template stays as it is, for the next start.
partial=$OUTPUT.rendering
markers=0
while IFS= read -r line || [ -n "$line" ]; do
    printf '%s\n' "$line"
    case $line in
        *"$MARKER"*)
            markers=$((markers + 1))
            # The marker's own indentation, so the lines line up beneath it.
            indent=${line%%[!$BLANKS]*}
            for proxy in $proxies; do
                printf '%sset_real_ip_from %s;\n' "$indent" "$proxy"
            done
            if [ -n "$proxies" ]; then
                printf '%sreal_ip_header X-Forwarded-For;\n' "$indent"
                printf '%sreal_ip_recursive on;\n' "$indent"
            fi
            ;;
    esac
done <"$TEMPLATE" >"$partial"

# With nowhere to put the lines, or two places, the setting would be dropped
# or doubled (nginx refuses a second real_ip_header): neither silently.
if [ -n "$proxies" ] && [ "$markers" -ne 1 ]; then
    rm -f "$partial"
    refuse "$TEMPLATE has $markers lines holding $MARKER, not exactly one, so there is no single place for the realip lines"
fi
mv -f "$partial" "$OUTPUT"
