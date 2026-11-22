/**
 * Where to go after signing in (spec FR-055: the page named in the sign-in
 * link, `/login?from=%2Forders`). Only a path inside this app is accepted —
 * never another origin, a scheme, or a protocol-relative `//host` — and never
 * the sign-in page itself, which would loop.
 */
export function safeReturnTo(raw: string | null | undefined): string | null {
  if (typeof raw !== "string") return null;
  const value = raw.trim();
  if (value === "" || !value.startsWith("/") || value.startsWith("//") || value.startsWith("/\\")) return null;
  if (/[\u0000-\u001f]/.test(value)) return null;
  const path = value.split(/[?#]/, 1)[0];
  if (path === "/login") return null;
  return value;
}

/** The current location as a `from` value: path, query and hash. */
export function hereAsReturnTo(location: { pathname: string; search: string; hash: string }): string {
  return `${location.pathname}${location.search}${location.hash}`;
}
