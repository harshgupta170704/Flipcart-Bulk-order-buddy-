/**
 * "To be delivered" on the dashboard (spec FR-040..042): "<n> order units
 * across <m> models", one row per model, with a toggle for orders placed more
 * than fifteen days before today, a "View all <m> models" that lists every
 * one, and the same units in the ledger, so the two totals can be checked
 * against each other on screen (SC-004).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { errorMessage, getToBeDelivered } from "../lib/api";
import type { ToBeDelivered as ToBeDeliveredData, ToBeDeliveredModel } from "../lib/types";
import { countLabel, formatCount, formatDate } from "../lib/format";
import { Alert } from "./Alert";
import { Button } from "./Button";
import { Card } from "./Card";
import { Dialog } from "./Dialog";
import { Checkbox } from "./Field";
import { EmptyState } from "./EmptyState";
import { SkeletonRows } from "./Skeleton";
import { Table, type Column } from "./Table";

const SHOWN = 5;
const OLDER_THAN_DAYS = 15;

/**
 * The ledger under the filter this card counts by: orders placed, units on
 * their way or never read back (GET /stats/to-be-delivered). The ledger has no
 * filter for "ordered so many days ago" — its date range is the order date's,
 * while this card counts an order without one from the day it was recorded —
 * so the 15+ days toggle does not carry over, and the link's title says so.
 */
export const LEDGER_LINK = "/order-units?status=placed&delivery_state=in_transit&delivery_state=unknown";

const LINK_CLASS =
  "inline-flex h-8 items-center rounded-control border border-line px-3 text-[13px] font-medium text-ink transition-colors hover:bg-page";

/**
 * The one row for orders that name no model: those handed in on the order-id
 * sheet carry none until a read-back reads it off the order page. The server
 * sends it with neither model nor brand (GET /stats/to-be-delivered).
 */
const UNKNOWN_MODEL = "Model not known yet";

function productName(row: ToBeDeliveredModel): string {
  const model = row.model?.trim();
  if (!model) return UNKNOWN_MODEL;
  return row.brand && !model.toLowerCase().startsWith(row.brand.toLowerCase()) ? `${row.brand} ${model}` : model;
}

function Bars({ rows, max }: { rows: ToBeDeliveredModel[]; max: number }) {
  return (
    <ul className="flex flex-col gap-2">
      {rows.map((row) => (
        <li key={`${row.brand ?? ""}|${row.model ?? ""}`} className="flex items-center gap-3 text-[13px]">
          <span className="w-[42%] min-w-0 truncate text-ink" title={productName(row)}>
            {productName(row)}
          </span>
          <span aria-hidden="true" className="h-2 flex-1 overflow-hidden rounded-full bg-page">
            <span
              className="block h-full rounded-full"
              style={{ width: `${max > 0 ? Math.max(4, (row.units / max) * 100) : 0}%`, backgroundColor: "var(--accent)" }}
            />
          </span>
          <span className="w-[120px] shrink-0 text-right tabular-nums text-ink-2">
            {formatCount(row.units)} unit{row.units === 1 ? "" : "s"} · {formatCount(row.orders)} order{row.orders === 1 ? "" : "s"}
          </span>
        </li>
      ))}
    </ul>
  );
}

const COLUMNS: Column<ToBeDeliveredModel>[] = [
  { key: "product", header: "Product", render: (row) => productName(row) },
  { key: "units", header: "Units", numeric: true, width: "90px", render: (row) => formatCount(row.units) },
  { key: "orders", header: "Orders", numeric: true, width: "90px", render: (row) => formatCount(row.orders) },
  {
    key: "oldest",
    header: "Oldest order",
    width: "140px",
    render: (row) => <span className="text-ink-2">{formatDate(row.oldest_ordered_at)}</span>,
  },
];

export function ToBeDelivered({ refreshKey }: { refreshKey: number }) {
  const [olderOnly, setOlderOnly] = useState(false);
  const [data, setData] = useState<ToBeDeliveredData | null>(null);
  /** Which view `data` answers: the toggle flips before its reply lands. */
  const [dataOlderOnly, setDataOlderOnly] = useState(false);
  /** What the last reply to land answered, figures or an error: the view and
   *  the page's Refresh it was asked under. null until the first lands. */
  const [answered, setAnswered] = useState<{ olderOnly: boolean; refreshKey: number } | null>(null);
  /** The page's Refresh a load started now answers: the newest one handed in. */
  const asking = useRef(refreshKey);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [allOpen, setAllOpen] = useState(false);
  /* The toggle can change while a refresh is in flight; only the newest
     request may land, or a slow unfiltered reply would overwrite the 15-day
     view it no longer belongs to. */
  const ticket = useRef(0);

  const load = useCallback(async () => {
    const mine = ++ticket.current;
    const refreshAsked = asking.current;
    setLoading(true);
    try {
      const next = await getToBeDelivered(olderOnly ? OLDER_THAN_DAYS : null);
      if (mine !== ticket.current) return;
      setData(next);
      setDataOlderOnly(olderOnly);
      setError(null);
    } catch (err) {
      if (mine !== ticket.current) return;
      setError(errorMessage(err));
    } finally {
      if (mine === ticket.current) {
        setLoading(false);
        setAnswered({ olderOnly, refreshKey: refreshAsked });
      }
    }
  }, [olderOnly]);

  useEffect(() => {
    asking.current = refreshKey;
    void load();
  }, [load, refreshKey]);

  /* The figures shown are only ever the answer to the toggle as it stands and
     to the page's latest Refresh (spec FR-049). Just after a flip, `data`
     still answers the other view; just after a Refresh, the time before it.
     From that very frame until the new reply lands the card shows
     placeholders: never those figures under the new caption or as the
     Refresh's answer, nor "Could not load" before the request has had its
     chance. A Refresh that fails brings the last figures back, saying so; a
     flip that fails says it could not load that view. */
  const waiting = answered === null || answered.olderOnly !== olderOnly || answered.refreshKey !== refreshKey;
  const shown = !waiting && data !== null && dataOlderOnly === olderOnly ? data : null;
  const rows = shown?.models ?? [];
  const max = rows.reduce((top, row) => Math.max(top, row.units), 0);
  const title =
    shown === null
      ? "To be delivered"
      : `To be delivered — ${countLabel(shown.total_units, "order unit")} across ${countLabel(rows.length, "model")}`;

  return (
    <Card
      title={title}
      description={
        olderOnly
          ? `Only units ordered more than ${OLDER_THAN_DAYS} days before today that Flipkart has not yet called delivered.`
          : "Units placed and not yet delivered, cancelled or returned — including those not read back yet."
      }
      actions={
        <>
          <Link
            to={LEDGER_LINK}
            className={LINK_CLASS}
            title={
              olderOnly
                ? `Opens every unit still to be delivered: Order Units History cannot pick out orders placed ${OLDER_THAN_DAYS}+ days ago, so that choice is not carried over`
                : "The same units in Order Units History: orders placed, units on their way or not read back yet"
            }
          >
            View in ledger
          </Link>
          <Checkbox
            label={`Ordered ${OLDER_THAN_DAYS}+ days ago`}
            checked={olderOnly}
            onChange={(event) => setOlderOnly(event.target.checked)}
          />
        </>
      }
    >
      {shown === null && (waiting || loading) ? (
        <SkeletonRows rows={4} />
      ) : shown === null ? (
        <Alert tone="error" title="Could not load what is still to be delivered">
          {error}
          <Button size="sm" className="mt-2" onClick={() => void load()}>
            Try again
          </Button>
        </Alert>
      ) : rows.length === 0 ? (
        <EmptyState
          title="Nothing waiting to be delivered"
          description={olderOnly ? "No order this old is still on its way." : "Every placed unit has been delivered, cancelled or returned."}
        />
      ) : (
        <div className="flex flex-col gap-3">
          {error ? (
            <p role="status" className="text-[12px] text-ink-2">
              Showing the last figures that loaded — the refresh failed: {error}
            </p>
          ) : null}
          <Bars rows={rows.slice(0, SHOWN)} max={max} />
          {/* Offered whenever there is a model to list (FR-042), not only
              once the bars run out: the list is where each model's orders and
              its oldest order date are. */}
          <div>
            <Button size="sm" onClick={() => setAllOpen(true)}>
              View all {countLabel(rows.length, "model")}
            </Button>
          </div>
        </div>
      )}

      <Dialog
        open={allOpen}
        onClose={() => setAllOpen(false)}
        title={`To be delivered — ${countLabel(shown?.total_units ?? 0, "order unit")}`}
        description={olderOnly ? `Ordered more than ${OLDER_THAN_DAYS} days before today.` : undefined}
        width="720px"
        footer={
          <Button size="sm" onClick={() => setAllOpen(false)}>
            Close
          </Button>
        }
      >
        <Table columns={COLUMNS} rows={rows} rowKey={(row) => `${row.brand ?? ""}|${row.model ?? ""}`} minWidth="520px" />
      </Dialog>
    </Card>
  );
}

export default ToBeDelivered;
