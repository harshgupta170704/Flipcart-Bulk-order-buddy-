/** Small, dependency-free display helpers. */

/** What we render in place of a missing value. */
export const DASH = "—";

const inr = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const plain = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 });

const intFmt = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });

/** "₹1,234.56" — or an em dash when there is nothing to show. */
export function formatCurrency(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH;
  return inr.format(value);
}

/** Grouped number without a currency symbol. */
export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH;
  return plain.format(value);
}

/** Grouped integer — for counts and stat tiles. */
export function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH;
  return intFmt.format(Math.round(value));
}

export function formatPercent(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH;
  return `${value.toFixed(digits)}%`;
}

/** A date-time with no offset: what the API writes for every stored timestamp. */
const OFFSET_LESS = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/;

/**
 * The API stores and serialises UTC without an offset, and ECMAScript reads
 * such a string as the browser's LOCAL time — 5h30 early in India, which put
 * a late-evening order on the previous day while the CSV and the date filter
 * said otherwise. So an offset-less timestamp is read as UTC here, once, for
 * every page.
 */
function toDate(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const d = new Date(OFFSET_LESS.test(iso) ? `${iso}Z` : iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

/**
 * Every date and time on a page is India Standard Time, written one way —
 * "Oct 21, 2026" and a 24-hour "15:20", as the spec's screens show them —
 * whatever the viewer's browser is set to. Left to the browser, an American
 * English machine in India wrote "Oct 20, 2026, 10:15 AM"; one set to a zone
 * west of India put an order placed soon after midnight in India on the day
 * before (and, west of UTC, every delivery day too); one set to another
 * language spelt the month in that language. The locale only fixes the order
 * and spelling: American English is the one that writes "Oct 21, 2026"
 * (Indian English writes "21 Oct 2026").
 */
const DISPLAY_LOCALE = "en-US";
const DISPLAY_ZONE = "Asia/Kolkata";

const dayFormat = new Intl.DateTimeFormat(DISPLAY_LOCALE, {
  timeZone: DISPLAY_ZONE,
  month: "short",
  day: "2-digit",
  year: "numeric",
});

// hourCycle "h23" rather than hour12: false, which some browsers render as
// "24:05" for five past midnight.
const clockFormat = new Intl.DateTimeFormat(DISPLAY_LOCALE, {
  timeZone: DISPLAY_ZONE,
  hourCycle: "h23",
  hour: "2-digit",
  minute: "2-digit",
});

const clockWithSecondsFormat = new Intl.DateTimeFormat(DISPLAY_LOCALE, {
  timeZone: DISPLAY_ZONE,
  hourCycle: "h23",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});

/**
 * "Oct 23, 2026 19:35" — the spec's own form ("last modified Oct 21, 2026
 * 15:20"): the day and the clock joined by a space, not the comma a browser
 * puts between them when asked for both at once.
 */
export function formatDateTime(iso: string | null | undefined): string {
  const d = toDate(iso);
  if (!d) return DASH;
  return `${dayFormat.format(d)} ${clockFormat.format(d)}`;
}

/** "Oct 23, 2026" — for a value that is a day, not a moment (a delivery date). */
export function formatDate(iso: string | null | undefined): string {
  const d = toDate(iso);
  if (!d) return DASH;
  return dayFormat.format(d);
}

/** "19:35:02" — for live event logs. */
export function formatTime(iso: string | null | undefined): string {
  const d = toDate(iso);
  if (!d) return DASH;
  return clockWithSecondsFormat.format(d);
}

/** "just now" / "4m ago" / "2d ago" */
export function formatRelative(iso: string | null | undefined, now: number = Date.now()): string {
  const d = toDate(iso);
  if (!d) return DASH;
  const seconds = Math.round((now - d.getTime()) / 1000);
  if (seconds < 0) return "just now";
  if (seconds < 45) return "just now";
  if (seconds < 90) return "1m ago";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days}d ago`;
  return formatDateTime(iso);
}

/** "1m 42s" between two timestamps (end defaults to now). */
export function formatDuration(startIso: string | null | undefined, endIso?: string | null): string {
  const start = toDate(startIso);
  if (!start) return DASH;
  const end = toDate(endIso) ?? new Date();
  return formatSeconds((end.getTime() - start.getTime()) / 1000);
}

/** "14s", "1m 42s", "1h 2m" from a number of seconds; never negative. */
export function formatSeconds(total: number): string {
  if (!Number.isFinite(total)) return DASH;
  let seconds = Math.max(0, Math.round(total));
  const hours = Math.floor(seconds / 3600);
  seconds -= hours * 3600;
  const minutes = Math.floor(seconds / 60);
  seconds -= minutes * 60;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${seconds}s`;
  return `${seconds}s`;
}

/**
 * How long a run spent on one account, for the per-account rows: the server's
 * seconds when it sent them (its clock stamped both ends), else the two stamps
 * seen live; null while the account is still waiting or running, and for one
 * the run passed over (skipped: no session, no card) — nothing was spent on
 * it, so "0s" would only be noise.
 */
export function formatSpent(item: {
  status?: string;
  seconds?: number | null;
  started_at?: string | null;
  finished_at?: string | null;
}): string | null {
  if (item.status === "pending" || item.status === "running" || item.status === "skipped") return null;
  if (typeof item.seconds === "number") return formatSeconds(item.seconds);
  if (item.started_at && item.finished_at) return formatDuration(item.started_at, item.finished_at);
  return null;
}

export function pluralize(count: number, singular: string, plural?: string): string {
  return count === 1 ? singular : (plural ?? `${singular}s`);
}

/** "3 accounts" */
export function countLabel(count: number, singular: string, plural?: string): string {
  return `${formatCount(count)} ${pluralize(count, singular, plural)}`;
}

export function truncate(text: string, max = 60): string {
  if (text.length <= max) return text;
  return `${text.slice(0, Math.max(0, max - 1)).trimEnd()}…`;
}

/** A product link shown as host + a trimmed path, so tables stay readable. */
export function formatLink(link: string | null | undefined, max = 48): string {
  if (!link) return DASH;
  try {
    const url = new URL(link);
    const tail = `${url.pathname}${url.search}`;
    return truncate(`${url.host}${tail === "/" ? "" : tail}`, max);
  } catch {
    return truncate(link, max);
  }
}

/** Human file size, for the card/account upload controls. */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return DASH;
  if (bytes < 1024) return `${bytes} B`;
  const kb = bytes / 1024;
  if (kb < 1024) return `${kb.toFixed(kb < 10 ? 1 : 0)} KB`;
  const mb = kb / 1024;
  if (mb < 1024) return `${mb.toFixed(mb < 10 ? 2 : 1)} MB`;
  return `${(mb / 1024).toFixed(2)} GB`;
}

/** Clamp a 0..100 progress value into something safe for a progress bar. */
export function clampProgress(progress: number | null | undefined): number {
  if (progress === null || progress === undefined || Number.isNaN(progress)) return 0;
  return Math.min(100, Math.max(0, progress));
}

/** How late a delivery came against Flipkart's promise: "On time" (early
 *  counts too), "1 day late", "3 days late"; null while either date is unknown. */
export function formatLate(daysLate: number | null | undefined): string | null {
  if (daysLate === null || daysLate === undefined) return null;
  if (daysLate <= 0) return "On time";
  return `${daysLate} ${daysLate === 1 ? "day" : "days"} late`;
}

/**
 * The day Flipkart first promised a unit for, and the day it later moved that
 * promise to — `movedTo` is null when the promise never moved to another day.
 * The lateness is counted from the first; the latest is when the unit is now
 * expected. A row read back before the first promise was kept has only the
 * latest, which then stands for both.
 */
export function promiseDays(order: { promised_date?: string | null; first_promised_date?: string | null }): {
  first: string | null;
  movedTo: string | null;
} {
  const latest = order.promised_date ?? null;
  const first = order.first_promised_date ?? latest;
  const moved = first !== null && latest !== null && formatDate(latest) !== formatDate(first);
  return { first, movedTo: moved ? latest : null };
}
