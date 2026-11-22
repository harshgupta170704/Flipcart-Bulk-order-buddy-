/**
 * "Changes since you last looked" (the owner's ask of 26 Oct 2026): what the
 * read-backs found since you last marked the list as seen — a unit delivered,
 * out for delivery, cancelled, returned or not delivered — each with the way
 * to its rows in the ledger. The server reads back every account with an
 * order on its way on its own timer; without this list, a delivery it found
 * was news only to someone scanning the ledger for it.
 *
 * The first page lives in one small store shared by the dashboard panel and
 * the nav badge, the way the alert counts are (AlertsSummary): whoever mounts
 * first fetches, the rest read the same answer, and "Mark all as seen" or the
 * dashboard's Refresh updates both at once. The panel also reloads it every
 * two minutes while it is on screen, as the out-for-delivery card does.
 *
 * The page is one person's — what is new to them, against their own seen
 * mark — so the store drops it whenever who is signed in changes (lib/session),
 * and a page fetched before that is never marked as seen. If a reader is still
 * on screen (the nav badge always is) and someone is signed in, theirs is
 * fetched at once: the badge would otherwise stay blank until a reader mounts.
 */
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { Link } from "react-router-dom";
import { errorMessage, getToken, listChanges, markChangesSeen } from "../lib/api";
import type { ChangeKind, ChangeState, ChangesPage, UnitChange } from "../lib/types";
import { countLabel, formatCount, formatDateTime, formatRelative } from "../lib/format";
import { onSessionChange } from "../lib/session";
import { Alert } from "./Alert";
import { Button } from "./Button";
import { Card } from "./Card";
import { EmptyState } from "./EmptyState";
import { SkeletonRows } from "./Skeleton";
import { StatusBadge } from "./StatusBadge";

/** Rows per fetch: the panel's first screen, and each "Show more". */
export const CHANGES_PANEL_SIZE = 20;
/** An answer older than this is fetched again when something mounts. */
const STALE_MS = 60_000;
/** The panel reloads the list this often while it is on screen. */
const RELOAD_MS = 120_000;

/* --- the shared first page -------------------------------------------------- */

export interface ChangesState {
  /** null until a load has succeeded — "not loaded" is not "nothing new". */
  page: ChangesPage | null;
  loading: boolean;
  /** Already human-readable: it came through errorMessage(). */
  error: string | null;
}

let state: ChangesState = { page: null, loading: false, error: null };
let fetchedAt = 0;
/** Only the newest request may land: a stale reply would bring back what was just marked seen. */
let ticket = 0;
/** The pages fetched since the last change of who is signed in: only these may be marked as seen. */
let fetchedThisSession = new WeakSet<ChangesPage>();
/**
 * Moves only when who is signed in changes. "Show more" holds on to it, not to
 * `ticket`: the two-minute reload moves `ticket` too, and a page of rows asked
 * for just before one is still good, while one asked for by the person signed
 * in before is not.
 */
let generation = 0;
const listeners = new Set<() => void>();

function publish(next: ChangesState): void {
  state = next;
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function snapshot(): ChangesState {
  return state;
}

/**
 * Drop the page — someone signed in or out: it was what was new to someone
 * else — and fetch the new person's at once when a reader is on screen. The
 * readers fetch only when they mount, and the nav badge stays mounted through
 * a sign-in from another tab or a password change, so without this it would
 * stay blank. Never on a sign-out: with no token the fetch would come back
 * 401, and the sign-out that follows would land here again, round and round.
 */
export function resetChanges(): void {
  ticket += 1; // a reply still on its way lands nowhere
  generation += 1;
  fetchedAt = 0;
  fetchedThisSession = new WeakSet();
  publish({ page: null, loading: false, error: null });
  if (listeners.size > 0 && getToken()) void refreshChanges();
}

onSessionChange(resetChanges);

/** Fetch the first page now and tell every reader. */
export function refreshChanges(): Promise<void> {
  ticket += 1;
  const mine = ticket;
  publish({ ...state, loading: true });
  return listChanges({ page: 1, size: CHANGES_PANEL_SIZE }).then(
    (page) => {
      if (ticket !== mine) return;
      fetchedAt = Date.now();
      fetchedThisSession.add(page);
      publish({ page, loading: false, error: null });
    },
    (err: unknown) => {
      if (ticket !== mine) return;
      fetchedAt = Date.now();
      // Keep the last good list: a stale count with a visible error beats a
      // badge that silently drops to nothing.
      publish({ ...state, loading: false, error: errorMessage(err) });
    },
  );
}

/**
 * Mark everything on `page` as seen — up to the moment it was read — and
 * fetch the list again. Refused for a page fetched before who is signed in
 * last changed: it would move the new person's mark by someone else's list.
 */
export async function markAllSeen(page: ChangesPage): Promise<void> {
  if (!fetchedThisSession.has(page)) {
    void refreshChanges();
    throw new Error("this list was loaded for an earlier sign-in; it is being loaded again");
  }
  await markChangesSeen(page.as_of);
  await refreshChanges();
}

/** Read the shared first page, fetching it when nobody has in the last minute. */
export function useChanges(): ChangesState & { reload: () => void } {
  const shared = useSyncExternalStore(subscribe, snapshot, snapshot);

  useEffect(() => {
    if (state.loading) return;
    if (state.page !== null && Date.now() - fetchedAt < STALE_MS) return;
    void refreshChanges();
  }, []);

  return { ...shared, reload: () => void refreshChanges() };
}

/* --- how a change reads ------------------------------------------------------- */

/** The kinds in the order the list's summary names them. */
const KIND_ORDER: ChangeKind[] = ["delivered", "out_for_delivery", "cancelled", "returned", "failed"];

const KIND_LABEL: Record<ChangeKind, string> = {
  delivered: "Delivered",
  out_for_delivery: "Out for delivery",
  cancelled: "Cancelled",
  returned: "Returned",
  failed: "Not delivered",
};

const STATE_LABEL: Record<ChangeState, string> = {
  delivered: "delivered",
  out_for_delivery: "out for delivery",
  cancelled: "cancelled",
  returned: "returned",
  failed: "not delivered",
  in_transit: "on its way",
  unknown: "unknown",
};

export function kindLabel(kind: string): string {
  return KIND_LABEL[kind as ChangeKind] ?? kind;
}

/** The colour the ledger gives the same state (OrderTable): delivered is
 *  good, a unit that will not arrive is critical, one on its way waits. */
function kindStatus(kind: string): string {
  switch (kind) {
    case "delivered":
      return "done";
    case "cancelled":
    case "returned":
    case "failed":
      return "failed";
    default:
      return "pending";
  }
}

/** Where a change's rows are: the ledger, narrowed to its order. */
export function ledgerLink(change: Pick<UnitChange, "order_id">): string {
  return change.order_id ? `/order-units?order_id=${encodeURIComponent(change.order_id)}` : "/order-units";
}

/** "3 delivered, 1 cancelled" — the kinds in the window that have any. */
function kindsText(page: ChangesPage | null, separator = ", "): string {
  if (page === null) return "";
  return KIND_ORDER.filter((kind) => (page.by_kind[kind] ?? 0) > 0)
    .map((kind) => `${formatCount(page.by_kind[kind])} ${kindLabel(kind).toLowerCase()}`)
    .join(separator);
}

/* --- the nav badge ---------------------------------------------------------- */

/**
 * How many changes came since you last looked, beside "Dashboard" in the
 * navigation. Nothing at zero — an empty list should not wear a badge — and
 * the kinds ride along for screen readers and as a tooltip.
 */
export function UnseenChangesBadge() {
  const { page } = useChanges();
  const total = page?.total ?? 0;
  if (total <= 0) return null;

  const said = `${countLabel(total, "change")} since you last looked`;
  const kinds = kindsText(page);
  return (
    <span
      className="ml-auto inline-flex min-w-[22px] items-center justify-center rounded-full border px-1.5 py-px text-[11px] font-semibold tabular-nums text-ink"
      style={{
        borderColor: "color-mix(in oklab, var(--accent) 45%, transparent)",
        backgroundColor: "color-mix(in oklab, var(--accent) 18%, var(--surface-1))",
      }}
      title={kinds === "" ? said : `${said} — ${kinds}`}
    >
      <span aria-hidden="true">{formatCount(total)}</span>
      <span className="sr-only">{said}</span>
    </span>
  );
}

/* --- the dashboard panel -------------------------------------------------------- */

function ChangeRow({ change }: { change: UnitChange }) {
  const order = change.order_id ?? `Order #${change.order_row_id}`;
  const unit = change.unit_position !== null && change.unit_position > 1 ? ` · unit ${change.unit_position}` : "";
  const account = change.account_email ?? (change.account_id !== null ? `Account #${change.account_id}` : "No account");
  const before =
    change.from_state === null ? "first read back" : `was ${STATE_LABEL[change.from_state] ?? change.from_state}`;
  return (
    <li className="flex flex-wrap items-start gap-x-3 gap-y-1 py-2.5">
      <StatusBadge status={kindStatus(change.kind)} label={kindLabel(change.kind)} />
      <span className="min-w-0 flex-1">
        <Link
          to={ledgerLink(change)}
          className="block truncate text-[13px] font-medium text-ink underline decoration-transparent underline-offset-2 transition-colors hover:decoration-current"
          title={`Open ${order} in Order Units`}
        >
          {change.model ?? "Unnamed product"}
        </Link>
        <span className="block truncate text-[12px] text-muted">
          {order}
          {unit} · {account} · {before}
        </span>
      </span>
      <span className="whitespace-nowrap text-[12px] text-ink-2" title={formatDateTime(change.at)}>
        {formatRelative(change.at)}
      </span>
    </li>
  );
}

/** Which stretch of time the list covers, in the card's own words. */
function windowText(page: ChangesPage | null): string {
  if (page === null) return "";
  if (page.capped) return ` You last looked more than 30 days ago, so this starts on ${formatDateTime(page.since)}.`;
  if (page.seen_at !== null) return ` Since you last marked them as seen, ${formatRelative(page.seen_at)}.`;
  return " The last 7 days — once you mark them as seen, only what is new shows here.";
}

/**
 * The list on the dashboard: each change newest first, linked to its rows in
 * Order Units, "Show more" for the rest of the window, and "Mark all as seen",
 * which clears it for you alone — up to the moment the list was read, so a
 * change that arrives while you look at it stays.
 */
export function ChangesPanel({ className = "" }: { className?: string }) {
  const { page, loading, error, reload } = useChanges();
  /** Rows "Show more" fetched beyond the first page, and how many pages are shown. */
  const [more, setMore] = useState<UnitChange[]>([]);
  const [pagesShown, setPagesShown] = useState(1);
  const [moreBusy, setMoreBusy] = useState(false);
  const [moreError, setMoreError] = useState<string | null>(null);
  const [marking, setMarking] = useState(false);
  const [markError, setMarkError] = useState<string | null>(null);
  const live = useRef(true);

  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);

  // A first page that says something new starts the list over: what "Show
  // more" added was read against the window as it was then. A reload that
  // brings nothing new (the two-minute one, mostly) keeps it.
  const pageKey = page === null ? "" : `${page.seen_at}|${page.total}|${page.items.map((row) => row.id).join(",")}`;
  useEffect(() => {
    setMore([]);
    setPagesShown(1);
    setMoreError(null);
  }, [pageKey]);

  // While on screen, reload every two minutes: the server's timer finds
  // deliveries without anyone pressing anything.
  useEffect(() => {
    const timer = window.setInterval(() => void refreshChanges(), RELOAD_MS);
    return () => window.clearInterval(timer);
  }, []);

  const rows = useMemo(() => {
    if (page === null) return [];
    const shown = new Set(page.items.map((row) => row.id));
    return [...page.items, ...more.filter((row) => !shown.has(row.id))];
  }, [page, more]);
  const total = page?.total ?? 0;

  const showMore = useCallback(async () => {
    if (page === null) return;
    // A reply asked for by whoever was signed in before lands nowhere: added
    // under the next person's list, it would show them someone else's rows.
    const asked = generation;
    setMoreBusy(true);
    setMoreError(null);
    try {
      const next = await listChanges({ page: pagesShown + 1, size: page.size });
      if (!live.current || asked !== generation) return;
      setMore((current) => {
        const known = new Set(current.map((row) => row.id));
        return [...current, ...next.items.filter((row) => !known.has(row.id))];
      });
      setPagesShown((count) => count + 1);
    } catch (err) {
      if (live.current && asked === generation) setMoreError(errorMessage(err));
    } finally {
      if (live.current) setMoreBusy(false);
    }
  }, [page, pagesShown]);

  const markAll = useCallback(async () => {
    if (page === null) return;
    setMarking(true);
    setMarkError(null);
    try {
      await markAllSeen(page);
    } catch (err) {
      if (live.current) setMarkError(errorMessage(err));
    } finally {
      if (live.current) setMarking(false);
    }
  }, [page]);

  const body = (() => {
    if (page === null && error === null) return <SkeletonRows rows={3} />;
    if (page === null) {
      return (
        <div className="flex flex-col items-start gap-3">
          <Alert tone="error" title="Could not load what changed" className="w-full">
            {error}
          </Alert>
          <Button size="sm" onClick={reload} loading={loading}>
            Try again
          </Button>
        </div>
      );
    }
    if (rows.length === 0) {
      return (
        <EmptyState
          title="Nothing new since you last looked"
          description="When a read-back finds an order delivered, out for delivery, cancelled or returned, it shows up here. The server reads back every account with an order on its way every hour, and every half hour once it is out for delivery."
        />
      );
    }
    return (
      <div className="flex flex-col gap-2">
        <p className="text-[13px] text-ink-2">{kindsText(page, " · ")}</p>
        <ul aria-label="Changes, newest first" className="flex flex-col divide-y divide-line">
          {rows.map((row) => (
            <ChangeRow key={row.id} change={row} />
          ))}
        </ul>
        {rows.length < total ? (
          <div className="flex flex-wrap items-center gap-3">
            <Button size="sm" variant="secondary" onClick={() => void showMore()} loading={moreBusy} disabled={moreBusy}>
              Show more
            </Button>
            <span className="text-[12px] text-muted">
              Showing {formatCount(rows.length)} of {formatCount(total)}
            </span>
          </div>
        ) : null}
        {moreError !== null ? (
          <p role="status" className="text-[12px] text-ink-2">
            Could not load more: {moreError}
          </p>
        ) : null}
      </div>
    );
  })();

  return (
    <Card
      title={total > 0 ? `Changes since you last looked — ${formatCount(total)}` : "Changes since you last looked"}
      description={`Orders the read-backs found delivered, out for delivery, cancelled, returned or not delivered, newest first.${windowText(page)}`}
      className={className}
      actions={
        page !== null && total > 0 ? (
          <Button size="sm" onClick={() => void markAll()} loading={marking} disabled={marking}>
            Mark all as seen
          </Button>
        ) : null
      }
    >
      <div className="flex flex-col gap-3">
        {error !== null && page !== null ? (
          <p role="status" className="text-[12px] text-ink-2">
            Showing the list that last loaded — the refresh failed: {error}
          </p>
        ) : null}
        {markError !== null ? (
          <p role="status" className="text-[12px] text-ink-2">
            Could not mark them as seen: {markError}
          </p>
        ) : null}
        {body}
      </div>
    </Card>
  );
}

export default ChangesPanel;
