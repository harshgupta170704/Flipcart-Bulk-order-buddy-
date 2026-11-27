import { useEffect, useMemo, useRef, type ReactNode } from "react";
import { downloadInvoice } from "../lib/api";
import { imeiNote } from "../lib/imei";
import { cancelledAfterPlacing, orderOutcome } from "../lib/orderOutcome";
import type { Order, UnitRequestKind } from "../lib/types";
import {
  DASH,
  formatCount,
  formatCurrency,
  formatDate,
  formatDateTime,
  formatLate,
  formatRelative,
  promiseDays,
} from "../lib/format";
import { Alert } from "./Alert";
import { Button } from "./Button";
import { EmptyState } from "./EmptyState";
import { Spinner } from "./Spinner";
import { StatusBadge } from "./StatusBadge";
import { Table, type Column } from "./Table";

/** One choosable column of the ledger. */
export interface OrderColumnOption {
  id: string;
  label: string;
  /** Shown until the operator chooses otherwise. */
  defaultOn: boolean;
}

export interface OrderTableProps {
  orders: Order[];
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  /** True when a filter is narrowing the list — changes the empty copy. */
  filtered: boolean;
  onClearFilter: () => void;
  /** Open the row's detail (every field, and the settle action if unconfirmed). */
  onView?: (order: Order) => void;
  /** Column ids to show, in registry order. Omit for the defaults. */
  visibleColumns?: readonly string[];
  /**
   * Row selection for the cancel / reschedule requests (spec FR-020). With
   * `selectedIds` given, a checkbox column leads the table and the header box
   * selects or clears the whole page. Omit it and no checkboxes show.
   */
  selectedIds?: ReadonlySet<number>;
  onToggleRow?: (id: number, checked: boolean) => void;
  onTogglePage?: (ids: number[], checked: boolean) => void;
  /** The row's Cancel action — offered only on a unit still on its way (in transit, or not yet read). */
  onCancel?: (order: Order) => void;
  /** Told why an invoice download failed, to say so (a refused or missing file). */
  onInvoiceDownloadError?: (message: string) => void;
  className?: string;
}

/** What a row's action buttons can do; the registry's columns are built from it. */
interface RowActions {
  onView?: (order: Order) => void;
  onCancel?: (order: Order) => void;
  onInvoiceDownloadError?: (message: string) => void;
  /** True when the Order ID has no column of its own: the product cell then
   *  carries it on a second line, so it is never out of sight. */
  orderIdUnderProduct?: boolean;
  /** True when the Delivery date has no column of its own: the status cell
   *  then carries the day under Flipkart's wording. */
  deliveryUnderStatus?: boolean;
  /** True when the Delivery OTP has no column of its own: the status cell
   *  then carries the code while the unit is on its way. */
  otpUnderStatus?: boolean;
}

/** "OD123… , unit 2 of 3" — the row, named for an accessible label. */
function rowName(o: Order): string {
  const order = o.order_id ?? `#${o.order_row_id ?? o.id}`;
  return o.units_in_order > 1 ? `${order}, unit ${o.unit_position} of ${o.units_in_order}` : order;
}

/**
 * A cancel can still be asked for only while the unit is on its way — in
 * transit, or not yet read — and not already asked. This mirrors the server
 * (app.core.unit_requests), which refuses a cancel on every settled unit:
 * delivered, cancelled, returned or failed. A row with no reading at all (an
 * older server) keeps the button; the server has the last word either way.
 */
function canCancel(o: Order): boolean {
  // An attempt Flipkart never took (failed, or stopped over its target, with
  // no order id) has no order to cancel; the server refuses such a request too.
  if ((o.status === "failed" || o.status === "failed_over_target") && !o.order_id) return false;
  const state: Order["delivery_state"] | null | undefined = o.delivery_state;
  if (state && state !== "in_transit" && state !== "unknown") return false;
  return !(o.open_request_kinds ?? []).includes("cancel");
}

const REQUEST_TAG: Record<UnitRequestKind, string> = {
  cancel: "Cancel requested",
  reschedule: "Reschedule requested",
};

/** The open cancel / reschedule requests on a unit, as small tags under its status. */
function RequestTags({ kinds }: { kinds: readonly UnitRequestKind[] | undefined }) {
  if (!kinds || kinds.length === 0) return null;
  return (
    <span className="mt-1 flex flex-wrap gap-1">
      {kinds.map((kind) => (
        <span
          key={kind}
          className="inline-flex items-center whitespace-nowrap rounded-control border border-line bg-page px-1.5 text-[11px] font-medium leading-5 text-ink-2"
          title="Open under Requests"
        >
          {REQUEST_TAG[kind] ?? kind}
        </span>
      ))}
    </span>
  );
}

/** The header checkbox: ticked when the whole page is selected, half-ticked when part of it is. */
function PageCheckbox({
  ids,
  selected,
  onTogglePage,
}: {
  ids: number[];
  selected: ReadonlySet<number>;
  onTogglePage?: (ids: number[], checked: boolean) => void;
}) {
  const box = useRef<HTMLInputElement>(null);
  const picked = ids.filter((id) => selected.has(id)).length;
  const all = ids.length > 0 && picked === ids.length;
  useEffect(() => {
    if (box.current) box.current.indeterminate = picked > 0 && !all;
  }, [picked, all]);
  return (
    <input
      ref={box}
      type="checkbox"
      className="size-4 accent-accent"
      aria-label="Select every unit on this page"
      checked={all}
      disabled={ids.length === 0}
      onChange={(event) => onTogglePage?.(ids, event.target.checked)}
    />
  );
}

function selectionColumn(
  orders: Order[],
  selected: ReadonlySet<number>,
  onToggleRow?: (id: number, checked: boolean) => void,
  onTogglePage?: (ids: number[], checked: boolean) => void,
): Column<Order> {
  const ids = orders.map((o) => o.id);
  return {
    key: "select",
    header: <PageCheckbox ids={ids} selected={selected} onTogglePage={onTogglePage} />,
    width: "44px",
    render: (o) => (
      <input
        type="checkbox"
        className="size-4 accent-accent"
        aria-label={`Select ${rowName(o)}`}
        checked={selected.has(o.id)}
        onChange={(event) => onToggleRow?.(o.id, event.target.checked)}
      />
    ),
  };
}

/**
 * The money sentence for a row that breached the cap: what was asked, what the
 * ceiling was, by how much it missed, and — the part that matters — that
 * nothing was charged.
 */
/** What hovering the Outcome says: Flipkart's reason for a cancellation, or
 *  what the run said about an attempt that did not end as a placed order. */
function outcomeHint(o: Order): string | undefined {
  if (cancelledAfterPlacing(o)) return o.cancel_reason || undefined;
  if (o.status !== "placed" && o.run_message) return o.run_message;
  return undefined;
}

function overTargetNote(order: Order): string {
  if (order.amount !== null && order.target !== null) {
    const over = order.amount - order.target;
    const by = over > 0 ? ` — ${formatCurrency(over)} over` : "";
    return `${formatCurrency(order.amount)} > ${formatCurrency(order.target)}${by}, not charged`;
  }
  return "The final amount was above the target — not charged.";
}

/**
 * The width the table needs: its columns' own widths added up (a column
 * without one counts as 150 px), never under 720. Until 11 Nov 2026 it was 130
 * px a column whatever they held, and the default fourteen columns made a
 * table of 2,443 px in a space of 1,124: Flipkart's status, the order id and
 * the amount were all off the screen.
 */
function tableWidth(columns: Column<Order>[]): number {
  const total = columns.reduce((sum, column) => {
    const px = /^(\d+)px$/.exec(column.width ?? "");
    return sum + (px ? Number(px[1]) : 150);
  }, 0);
  return Math.max(720, total);
}

/** A moment as the table shows it: the day, and the time under it — the
 *  same words as everywhere else ("Oct 27, 2026 20:03") in two-thirds the width. */
function dayAndTime(iso: string | null, note?: string) {
  const text = formatDateTime(iso);
  const cut = text.lastIndexOf(" ");
  if (text === DASH || cut < 0) return <span className="text-ink-2">{text}</span>;
  return (
    <span className={`block whitespace-nowrap ${note ? "text-muted" : "text-ink-2"}`} title={note ? `${note} ${text}` : text}>
      {text.slice(0, cut)}
      <span className="block text-[12px] text-muted">
        {note ? `${note} ` : ""}
        {text.slice(cut + 1)}
      </span>
    </span>
  );
}

/** What a Flipkart product address names, as words: its first path part,
 *  "pigeon-special-3-l-inner-lid-pressure-cooker" read "pigeon special 3 l
 *  inner lid pressure cooker". The whole address when it has no such part. */
function productFromLink(link: string): string {
  try {
    const slug = new URL(link).pathname.split("/").filter(Boolean)[0] ?? "";
    const words = decodeURIComponent(slug).replace(/[-_]+/g, " ").trim();
    return words && !/^(p|dl|product)$/i.test(words) ? words : link;
  } catch {
    return link;
  }
}

function truncatedCell(value: string | null, width: string) {
  if (!value) return DASH;
  return (
    <span className="block truncate" style={{ maxWidth: width }} title={value}>
      {value}
    </span>
  );
}

function deliveryStateKey(state: Order["delivery_state"]): string {
  // The reading itself is the server's (app.core.order_status), shared with
  // the dashboard and the alerts; this only picks a colour for it.
  switch (state) {
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

const SOURCE_LABEL: Record<Order["source"], string> = {
  run: "Run",
  sync: "Read-back",
  upload: "Upload",
};

/** An empty field of a unit no read-back has seen: nothing has looked yet. */
function notReadBack() {
  return <span className="text-[12px] text-muted">Not read back</span>;
}

/**
 * A cell of a field the read-back fills: the stored value whenever there is
 * one, whoever stored it; when there is none, "Not read back" on a unit no
 * read-back has seen, and `none` (what an empty field means once Flipkart's
 * page was read) otherwise. An order report files a unit's status, dates,
 * tracking id, seller and address without a read-back ever seeing it, and
 * leaves `synced_at` empty on purpose; gating these cells on `synced_at` hid
 * all of that behind "Not read back" while the row's dialog, the CSV and the
 * Order Details API showed it (review of 27 Oct 2026).
 */
function readBackCell<T>(
  o: Order,
  value: T | null | undefined,
  shown: (value: T) => ReactNode,
  none: ReactNode = DASH,
): ReactNode {
  if (value !== null && value !== undefined && value !== "") return shown(value);
  // An attempt that never became an order on Flipkart has nothing to read
  // back, now or ever: a dash, not "Not read back", which promised a reading.
  if (!o.order_id && (o.status === "failed" || o.status === "failed_over_target")) return DASH;
  return o.synced_at ? none : notReadBack();
}

/** Flipkart's words for paying at the door, as its order page's "Paid By" puts them (as in OrderDetail). */
const PAID_AT_THE_DOOR = /cash on delivery|pay on delivery/i;

function lateCell(daysLate: number | null | undefined) {
  const text = formatLate(daysLate);
  if (text === null) return DASH;
  return <span className={daysLate && daysLate > 0 ? "font-medium text-ink" : "text-ink-2"}>{text}</span>;
}

/**
 * The day the Delivery date column shows for a row, and what that day is:
 * "Delivered" only for a unit that was, the promise for one on its way, and
 * nothing for a unit that will not arrive. A returned unit was delivered only
 * when its delivery day is known, and shows that day rather than the later
 * day of its return. Without one it is a card order Flipkart cancelled and
 * refunded: "Refund Completed" reads as returned, and its date is the
 * cancellation's or the last promise, never a delivery (the CSV and the Order
 * Details API leave both of its dates empty). A delivered row read back
 * before the two dates were kept apart has only the one Flipkart showed. A
 * unit an order report filed has only the promise, never that one date.
 */
function deliveryDateShown(o: Order): { day: string | null; note: string | null } {
  switch (o.delivery_state) {
    case "delivered":
      return { day: o.delivered_date ?? o.delivery_date, note: "Delivered" };
    case "returned":
      return o.delivered_date ? { day: o.delivered_date, note: "Delivered" } : { day: o.delivery_date, note: null };
    case "in_transit":
    case "unknown":
      return { day: o.delivery_date ?? o.promised_date ?? null, note: "Promised" };
    default:
      return { day: o.delivery_date, note: null };
  }
}

/** The delivery day in a line, for under the status: "Promised Nov 01,
 *  2026", "Delivered Oct 30, 2026" — nothing when no day is known. */
function deliveryLine(o: Order): string | null {
  const { day, note } = deliveryDateShown(o);
  // Only a day that says what it is: the date beside a cancelled or returned
  // unit is that step's, and under "Order Not Placed" it read as a delivery.
  if (!day || !note) return null;
  return `${note} ${formatDate(day)}`;
}

/** The platform's wording as a badge — or why there is none yet. */
function statusCell(o: Order, deliveryUnderStatus = false, otpUnderStatus = true) {
  const badge = readBackCell(
    o,
    o.platform_status?.trim(),
    (raw) => <StatusBadge status={deliveryStateKey(o.delivery_state)} label={raw} />,
    <span className="text-[12px] text-muted">Not shown by Flipkart</span>,
  );
  // The delivery code sits right under the status while the unit is on its
  // way: Flipkart shows it once the unit is out for delivery, and that is when
  // the person at the door needs it — not in a column they must switch on.
  const onItsWay = o.delivery_state === "in_transit" || o.delivery_state === "unknown";
  return (
    <span className="block">
      {badge}
      {otpUnderStatus && o.delivery_otp && onItsWay ? (
        <span className="mt-1 block text-[12px] text-ink-2">
          Delivery code{" "}
          <span className="font-mono text-[13px] font-semibold tracking-wider text-ink tabular-nums">{o.delivery_otp}</span>
        </span>
      ) : null}
      {/* The day it is promised for, or came: here while it has no column. */}
      {deliveryUnderStatus && deliveryLine(o) ? (
        <span className="mt-1 block whitespace-nowrap text-[12px] text-muted">{deliveryLine(o)}</span>
      ) : null}
    </span>
  );
}

/* --- the registry: every column the ledger can show --------------------------- */
const REGISTRY: (OrderColumnOption & { column: (actions: RowActions) => Column<Order> })[] = [
  {
    id: "platform", label: "Platform", defaultOn: false,
    column: () => ({
      key: "platform", header: "Platform", width: "84px",
      render: (o) => <span className="text-[12px] uppercase text-ink-2">{o.platform ?? "flipkart"}</span>,
    }),
  },
  {
    id: "ref_code", label: "Ref Code", defaultOn: false,
    column: () => ({ key: "ref_code", header: "Ref Code", width: "110px", render: (o) => truncatedCell(o.ref_code ?? null, "100px") }),
  },
  {
    id: "po_number", label: "PO Number", defaultOn: false,
    column: () => ({ key: "po_number", header: "PO Number", width: "110px", render: (o) => truncatedCell(o.po_number ?? null, "100px") }),
  },
  {
    id: "bob_order", label: "BoB Order", defaultOn: true,
    column: () => ({
      key: "bob_order", header: "BoB Order", width: "54px", align: "center",
      // Placed by one of this tool's runs, rather than read back or handed in.
      render: (o) =>
        o.source === "run" ? (
          <span aria-label="Placed by this tool" style={{ color: "var(--status-good)" }}>✓</span>
        ) : (
          <span className="text-muted">—</span>
        ),
    }),
  },
  {
    id: "account_email", label: "Account", defaultOn: true,
    column: () => ({ key: "account_email", header: "Account", width: "148px", render: (o) => truncatedCell(o.account_email, "132px") }),
  },
  {
    id: "placed_at", label: "Order date", defaultOn: true,
    column: () => ({
      key: "placed_at", header: "Order date", width: "104px",
      // An attempt that placed no order has no order date; when it was tried
      // is shown instead, and said to be that — the list is sorted by it too.
      render: (o) =>
        o.placed_at || !o.created_at ? dayAndTime(o.placed_at ?? null) : dayAndTime(o.created_at, "tried"),
    }),
  },
  {
    id: "created_at", label: "Recorded", defaultOn: false,
    column: () => ({
      key: "created_at", header: "Recorded", width: "150px",
      render: (o) => <span className="whitespace-nowrap text-ink-2">{formatDateTime(o.created_at ?? null)}</span>,
    }),
  },
  {
    id: "model", label: "Product", defaultOn: true,
    column: ({ orderIdUnderProduct }) => ({
      key: "model", header: "Product", width: "186px",
      render: (o) => (
        <span className="block">
          {/* With no name read yet, the product page's own address says
              what was being bought: better than a dash on every failed row. */}
          {o.model || !o.product_link ? (
            truncatedCell(o.model, "170px")
          ) : (
            <span className="block truncate text-ink-2" style={{ maxWidth: "170px" }} title={o.product_link}>
              {productFromLink(o.product_link)}
            </span>
          )}
          {/* Flipkart's order id, here while it has no column of its own: the
              one thing every search and every invoice goes by. */}
          {orderIdUnderProduct && o.order_id ? (
            <span className="block truncate font-mono text-[12px] text-ink-2" style={{ maxWidth: "170px" }} title={o.order_id}>
              {o.order_id}
            </span>
          ) : null}
          {o.color ? (
            <span className="block truncate text-[12px] text-muted" style={{ maxWidth: "170px" }}>{o.color}</span>
          ) : null}
        </span>
      ),
    }),
  },
  {
    id: "status", label: "Outcome", defaultOn: true,
    column: () => ({
      key: "status", header: "Outcome", width: "150px",
      // An order Flipkart took and cancelled later reads as that, never as the
      // run's "Failed"; Flipkart's reason is a hover away here, and in full in
      // the row's dialog.
      render: (o) => (
        <span className="block" title={outcomeHint(o)}>
          <StatusBadge status={orderOutcome(o)} />
          {o.status === "failed_over_target" ? (
            <span className="mt-1 block max-w-[172px] text-[12px] leading-snug text-ink-2">{overTargetNote(o)}</span>
          ) : null}
          {/* A failed attempt says why, in the run's own words, cut to two
              lines here and in full in the row's dialog and on hover. */}
          {o.status === "failed" && !cancelledAfterPlacing(o) && o.run_message ? (
            <span className="mt-1 line-clamp-2 max-w-[172px] text-[12px] leading-snug text-ink-2">{o.run_message}</span>
          ) : null}
        </span>
      ),
    }),
  },
  {
    id: "processing_stage", label: "Processing Stage", defaultOn: false,
    column: () => ({
      key: "processing_stage", header: "Processing Stage", width: "148px",
      render: (o) => truncatedCell(o.processing_stage ?? null, "136px"),
    }),
  },
  {
    id: "error_code", label: "Error Code", defaultOn: false,
    column: () => ({
      key: "error_code", header: "Error Code", width: "170px",
      render: (o) => truncatedCell(o.error_code ?? null, "156px"),
    }),
  },
  {
    id: "platform_status", label: "Status", defaultOn: true,
    column: ({ deliveryUnderStatus, otpUnderStatus }) => ({
      key: "platform_status", header: "Status", width: "140px",
      // The open request tags sit under the status whatever the row's state:
      // a unit never read back can still have a cancel asked on it.
      render: (o) => (
        <span className="block">
          {statusCell(o, deliveryUnderStatus, otpUnderStatus)}
          <RequestTags kinds={o.open_request_kinds} />
        </span>
      ),
    }),
  },
  {
    // Beside the status, on by default: the person at the door needs it the
    // day the unit is out for delivery.
    id: "delivery_otp", label: "Delivery OTP", defaultOn: true,
    column: () => ({
      key: "delivery_otp", header: "OTP", width: "80px",
      // Flipkart shows the code only once the unit is out for delivery, so a
      // dash after a read-back means "not out for delivery yet", not "none".
      render: (o) =>
        readBackCell(
          o,
          o.delivery_otp,
          (code) => <span className="font-mono font-semibold tracking-wider tabular-nums">{code}</span>,
          <span className="text-muted">{DASH}</span>,
        ),
    }),
  },
  {
    id: "order_id", label: "Order ID", defaultOn: false,
    column: () => ({ key: "order_id", header: "Order ID", width: "190px", render: (o) => truncatedCell(o.order_id, "190px") }),
  },
  {
    id: "unit_id", label: "Unit ID", defaultOn: false,
    column: () => ({
      key: "unit_id", header: "Unit ID", width: "190px",
      render: (o) => (
        <span className="block">
          {o.unit_id ? truncatedCell(o.unit_id, "190px") : <span className="text-[12px] text-muted">Not yet seen</span>}
          {o.units_in_order > 1 ? (
            <span className="block text-[12px] text-muted">unit {o.unit_position} of {o.units_in_order}</span>
          ) : null}
        </span>
      ),
    }),
  },
  {
    id: "qty", label: "Qty", defaultOn: true,
    column: () => ({ key: "qty", header: "Qty", numeric: true, width: "52px", render: (o) => (o.qty === null ? DASH : formatCount(o.qty)) }),
  },
  {
    id: "amount", label: "Amount", defaultOn: true,
    column: () => ({
      key: "amount", header: "Amount", numeric: true, width: "110px",
      render: (o) => <span className={o.status === "failed_over_target" ? "font-medium" : undefined}>{formatCurrency(o.amount)}</span>,
    }),
  },
  {
    id: "target", label: "Target", defaultOn: false,
    column: () => ({
      key: "target", header: "Target", numeric: true, width: "110px",
      render: (o) => (o.target === null ? <span className="text-muted">No cap</span> : formatCurrency(o.target)),
    }),
  },
  {
    id: "payment_mode", label: "Cash on Delivery", defaultOn: false,
    column: () => ({
      key: "payment_mode", header: "COD", width: "70px",
      // An order report's "Cash on Delivery: No" is kept as "prepaid", which
      // reads No like a card, unless the unit's own page says it is paid at
      // the door — as the CSV and the row dialog read it.
      render: (o) =>
        o.payment_mode === "cod" || (o.payment_mode === "prepaid" && PAID_AT_THE_DOOR.test(o.payment_method ?? "")) ? (
          <span className="font-medium text-ink">Yes</span>
        ) : o.payment_mode === "card" || o.payment_mode === "prepaid" ? (
          <span className="text-muted">No</span>
        ) : (
          DASH
        ),
    }),
  },
  {
    // The card the order was paid with, in full (the owner's decision of 25 Oct 2026, spec FR-004).
    id: "card_number", label: "Card Number", defaultOn: false,
    column: () => ({
      key: "card_number", header: "Card Number", width: "170px",
      render: (o) =>
        o.card_number ? (
          <span className="font-mono text-[12px] text-ink">{o.card_number}</span>
        ) : o.payment_mode === "cod" ? (
          <span className="text-muted">COD</span>
        ) : (
          DASH
        ),
    }),
  },
  {
    id: "delivery_date", label: "Delivery date", defaultOn: false,
    column: () => ({
      key: "delivery_date", header: "Delivery date", width: "120px",
      render: (o) => {
        const { day, note } = deliveryDateShown(o);
        return readBackCell(
          o,
          day,
          (shown) => (
            <span className="block">
              <span className="block whitespace-nowrap text-ink">{formatDate(shown)}</span>
              {note ? <span className="block text-[12px] text-muted">{note}</span> : null}
            </span>
          ),
          <span className="text-[12px] text-muted">No date shown</span>,
        );
      },
    }),
  },
  {
    id: "promised_date", label: "Promised date", defaultOn: false,
    column: () => ({
      key: "promised_date", header: "Promised date", width: "150px",
      // The first promise, which the Late column counts from; when Flipkart
      // moved it, the day it moved to beneath.
      render: (o) => {
        const { first, movedTo } = promiseDays(o);
        return readBackCell(o, first, (day) => (
          <span className="block">
            <span className="block whitespace-nowrap">{formatDate(day)}</span>
            {movedTo === null ? null : (
              <span className="block whitespace-nowrap text-[12px] text-muted">moved to {formatDate(movedTo)}</span>
            )}
          </span>
        ));
      },
    }),
  },
  {
    id: "delivered_date", label: "Delivered date", defaultOn: false,
    column: () => ({
      key: "delivered_date", header: "Delivered date", width: "130px",
      render: (o) => readBackCell(o, o.delivered_date, (day) => <span className="whitespace-nowrap">{formatDate(day)}</span>),
    }),
  },
  {
    id: "days_late", label: "Late", defaultOn: false,
    column: () => ({
      key: "days_late", header: "Late", width: "110px",
      render: (o) => lateCell(o.days_late),
    }),
  },
  {
    id: "payment_method", label: "Paid by", defaultOn: false,
    column: () => ({
      key: "payment_method", header: "Paid by", width: "150px",
      render: (o) => readBackCell(o, o.payment_method, (method) => truncatedCell(method, "150px")),
    }),
  },
  {
    // The order page's "Total amount": paid, or to be paid on delivery, or
    // never collected — so not "Amount paid". The id stays, for saved views.
    id: "amount_paid", label: "Order total", defaultOn: false,
    column: () => ({
      key: "amount_paid", header: "Order total", numeric: true, width: "120px",
      render: (o) => (o.amount_paid === null || o.amount_paid === undefined ? DASH : formatCurrency(o.amount_paid)),
    }),
  },
  {
    id: "tracking_id", label: "Tracking ID", defaultOn: false,
    column: () => ({
      key: "tracking_id", header: "Tracking ID", width: "160px",
      render: (o) => readBackCell(o, o.tracking_id, (id) => truncatedCell(id, "160px")),
    }),
  },
  {
    // What Flipkart's order page says the unit ships to (spec FR-044/045); the
    // address alerts compare it with the Address Book.
    id: "delivery_address", label: "Delivery address", defaultOn: false,
    column: () => ({
      key: "delivery_address", header: "Delivery address", width: "260px",
      render: (o) => readBackCell(o, o.delivery_address, (address) => truncatedCell(address, "260px")),
    }),
  },
  {
    // The GST the order page says the order was billed under; the GST-mismatch
    // alert compares it with the GST the run chose.
    id: "billed_gst", label: "Billed GST", defaultOn: false,
    column: () => ({
      key: "billed_gst", header: "Billed GST", width: "220px",
      render: (o) =>
        readBackCell(
          o,
          o.billed_gstin ? `${o.billed_gstin}${o.billed_business_name ? ` · ${o.billed_business_name}` : ""}` : null,
          (billed) => truncatedCell(billed, "220px"),
        ),
    }),
  },
  {
    id: "seller", label: "Seller", defaultOn: false,
    column: () => ({
      key: "seller", header: "Seller", width: "160px",
      render: (o) => readBackCell(o, o.seller, (seller) => truncatedCell(seller, "160px")),
    }),
  },
  {
    id: "gst_name", label: "Business GST", defaultOn: false,
    column: () => ({ key: "gst_name", header: "Business GST", width: "180px", render: (o) => truncatedCell(o.gst_name, "180px") }),
  },
  {
    // The GSTIN the run billed the order under, beside the business name: one
    // name can hold a registration per state. Blank for rows a read-back or a sheet made.
    id: "gstin", label: "Business GST No", defaultOn: false,
    column: () => ({ key: "gstin", header: "Business GST No", width: "170px", render: (o) => truncatedCell(o.gstin ?? null, "170px") }),
  },
  {
    // The phone the order was placed with: the account's billing phone, else
    // its own, else the address's. Blank for rows a read-back or a sheet made.
    id: "billing_phone", label: "Billing Phone", defaultOn: false,
    column: () => ({
      key: "billing_phone", header: "Billing Phone", width: "140px",
      render: (o) => (o.billing_phone ? <span className="whitespace-nowrap tabular-nums">{o.billing_phone}</span> : DASH),
    }),
  },
  {
    // The name the product invoice was billed to.
    id: "billing_name", label: "Billing Name", defaultOn: false,
    column: () => ({ key: "billing_name", header: "Billing Name", width: "180px", render: (o) => truncatedCell(o.billing_name ?? null, "180px") }),
  },
  {
    // From the order's product invoices; "for the order" when the invoice
    // lists a different number of IMEIs than the order has units.
    id: "imei", label: "IMEI", defaultOn: false,
    column: () => ({
      key: "imei", header: "IMEI", width: "170px",
      render: (o) =>
        o.imei ? (
          <span className="tabular-nums" title={imeiNote(o.imei_scope)}>
            {o.imei}
            {o.imei_scope === "order" ? <span className="block text-[11px] text-ink-2">for the order</span> : null}
          </span>
        ) : (
          DASH
        ),
    }),
  },
  {
    id: "unit_amount", label: "Unit amount", defaultOn: false,
    column: () => ({
      key: "unit_amount", header: "Unit amount", width: "120px",
      render: (o) => (o.unit_amount === null || o.unit_amount === undefined ? DASH : formatCurrency(o.unit_amount)),
    }),
  },
  {
    id: "last_tracking_update", label: "Last Tracking Update", defaultOn: false,
    column: () => ({
      key: "last_tracking_update", header: "Last Tracking Update", width: "170px",
      render: (o) =>
        o.last_tracking_update ? (
          <span>
            {formatDate(o.last_tracking_update)}
            <span className="block text-[11px] text-ink-2">{o.last_tracking_step}</span>
          </span>
        ) : (
          DASH
        ),
    }),
  },
  {
    // Goods in hand, as a person marked them (spec FR-001, FR-017).
    id: "received", label: "Received", defaultOn: false,
    column: () => ({
      key: "received", header: "Received", width: "130px",
      render: (o) =>
        o.received_at ? (
          <span title={o.received_by ? `Marked by ${o.received_by}` : undefined}>Yes · {formatDate(o.received_at)}</span>
        ) : (
          DASH
        ),
    }),
  },
  {
    id: "pin", label: "Pincode", defaultOn: false,
    column: () => ({ key: "pin", header: "Pincode", width: "90px" }),
  },
  {
    id: "brand", label: "Brand", defaultOn: false,
    column: () => ({ key: "brand", header: "Brand", width: "110px" }),
  },
  {
    id: "category", label: "Category", defaultOn: false,
    column: () => ({ key: "category", header: "Category", width: "110px" }),
  },
  {
    // The exact variant's Flipkart id, read from the product link (pid=…).
    id: "product_id", label: "Product ID", defaultOn: false,
    column: () => ({ key: "product_id", header: "Product ID", width: "160px", render: (o) => truncatedCell(o.product_id ?? null, "150px") }),
  },
  {
    id: "job_id", label: "Run", defaultOn: false,
    column: () => ({ key: "job_id", header: "Run", width: "70px", render: (o) => (o.job_id === null ? DASH : `#${o.job_id}`) }),
  },
  {
    id: "source", label: "Source", defaultOn: false,
    column: () => ({
      key: "source", header: "Source", width: "110px",
      render: (o) => SOURCE_LABEL[o.source] ?? o.source,
    }),
  },
  {
    id: "synced_at", label: "Read back", defaultOn: false,
    column: () => ({
      key: "synced_at", header: "Read back", width: "130px",
      render: (o) => (o.synced_at ? <span className="text-[12px] text-ink-2">{formatRelative(o.synced_at)}</span> : notReadBack()),
    }),
  },
  {
    id: "invoice", label: "Invoice", defaultOn: true,
    column: ({ onInvoiceDownloadError }) => ({
      key: "invoice", header: "Invoice", width: "96px",
      render: (o) => {
        if (o.invoice_on_file) {
          const count = o.invoices_on_file ?? 1;
          return (
            <Button
              size="sm"
              variant="ghost"
              onClick={() =>
                // A failed download used to vanish; the page now says why.
                void (async () => {
                  try {
                    await downloadInvoice(o.order_row_id ?? o.id, o.order_id);
                  } catch (err) {
                    onInvoiceDownloadError?.(err instanceof Error ? err.message : String(err));
                  }
                })()
              }
              aria-label={
                count > 1
                  ? `Download the newest of the ${count} invoices for ${o.order_id ?? o.id}; the row's details list them all`
                  : `Download the invoice for ${o.order_id ?? o.id}`
              }
              title={count > 1 ? "The newest downloads here; open the row for every invoice" : undefined}
            >
              {count > 1 ? `${count} PDFs` : "PDF"}
            </Button>
          );
        }
        // The platform's own link only answers inside that account's session,
        // which lives on the server; the next read-back fetches the file.
        return readBackCell(
          o,
          o.invoice_url,
          (url) => <span className="text-[12px] text-ink-2" title={url}>Not fetched yet</span>,
          <span className="text-[12px] text-muted" title="Flipkart's page for this order offered no invoice">None yet</span>,
        );
      },
    }),
  },
  {
    id: "actions", label: "Actions", defaultOn: true,
    column: ({ onView, onCancel }) => ({
      // Narrow, and its buttons one under the other: this column stays in
      // view while the table scrolls, so every pixel of it covers a column.
      key: "actions", header: "", width: "84px", sticky: "right",
      render: (o) => (
        <span className="flex flex-col items-start gap-0.5">
          <Button size="sm" variant="ghost" onClick={() => onView?.(o)} aria-label={`View order ${rowName(o)}`}>
            View
          </Button>
          {/* Red, and only while a cancel can still be asked for (spec FR-020, ui-evidence S4). */}
          {onCancel && canCancel(o) ? (
            <Button
              size="sm"
              variant="ghost"
              className="text-critical hover:text-critical"
              onClick={() => onCancel(o)}
              aria-label={`Cancel ${rowName(o)}`}
            >
              Cancel
            </Button>
          ) : null}
          {o.status === "unconfirmed" ? (
            <span title="Needs settling — open View" className="px-2 text-[11px] font-medium text-critical">
              ● settle
            </span>
          ) : null}
        </span>
      ),
    }),
  },
];

export const ORDER_COLUMN_OPTIONS: OrderColumnOption[] = REGISTRY.map(({ id, label, defaultOn }) => ({ id, label, defaultOn }));
export const DEFAULT_ORDER_COLUMNS: string[] = REGISTRY.filter((c) => c.defaultOn).map((c) => c.id);

/**
 * The ledger table. "Over target" and "Unconfirmed" stay distinct outcomes:
 * one is the cap doing its job, the other is an order that may exist and must
 * be settled before the account is used again. So does "Cancelled by
 * Flipkart": an order Flipkart took and cancelled later, never the run's own
 * "Failed". Which columns show is the operator's choice (the registry above);
 * the actions column is always there.
 */
export function OrderTable({
  orders,
  loading,
  error,
  onRetry,
  filtered,
  onClearFilter,
  onView,
  visibleColumns,
  selectedIds,
  onToggleRow,
  onTogglePage,
  onCancel,
  onInvoiceDownloadError,
  className = "",
}: OrderTableProps) {
  const wanted = useMemo(() => new Set(visibleColumns ?? DEFAULT_ORDER_COLUMNS), [visibleColumns]);
  const orderIdUnderProduct = !wanted.has("order_id");
  const deliveryUnderStatus = !wanted.has("delivery_date");
  const otpUnderStatus = !wanted.has("delivery_otp");
  const rowActions = useMemo<RowActions>(
    () => ({ onView, onCancel, onInvoiceDownloadError, orderIdUnderProduct, deliveryUnderStatus, otpUnderStatus }),
    [onView, onCancel, onInvoiceDownloadError, orderIdUnderProduct, deliveryUnderStatus, otpUnderStatus],
  );
  const columns = useMemo(() => {
    const chosen = REGISTRY.filter((c) => c.id === "actions" || wanted.has(c.id)).map((c) => c.column(rowActions));
    if (!selectedIds) return chosen;
    return [selectionColumn(orders, selectedIds, onToggleRow, onTogglePage), ...chosen];
  }, [wanted, rowActions, selectedIds, orders, onToggleRow, onTogglePage]);

  // Rows are units; the notes count orders, so a 2-unit unconfirmed order is one.
  const distinctOrders = (status: Order["status"]) =>
    new Set(orders.filter((o) => o.status === status).map((o) => o.order_row_id ?? o.id)).size;
  const overTarget = distinctOrders("failed_over_target");
  const unconfirmed = distinctOrders("unconfirmed");
  const firstLoad = loading && orders.length === 0;

  if (firstLoad) {
    return (
      <div
        className={`flex items-center justify-center gap-2 rounded-card border border-line px-6 py-10 text-[13px] text-ink-2 ${className}`}
        aria-live="polite"
      >
        <Spinner size={15} />
        Loading orders…
      </div>
    );
  }

  if (error && orders.length === 0) {
    return (
      <Alert tone="error" title="Could not load orders" className={className}>
        <span className="block">{error}</span>
        <Button size="sm" className="mt-2" onClick={onRetry}>
          Try again
        </Button>
      </Alert>
    );
  }

  const notes: string[] = [];
  if (unconfirmed > 0) {
    notes.push(`${unconfirmed} unconfirmed order${unconfirmed === 1 ? "" : "s"} — may exist; open View to settle`);
  }
  if (overTarget > 0) notes.push(`${overTarget} order${overTarget === 1 ? "" : "s"} over target — nothing charged`);

  return (
    <div className={`flex flex-col gap-3 ${className}`}>
      {error ? (
        <Alert tone="error">
          <span className="block">{error}</span>
          <span className="block text-ink-2">Showing the last results that loaded.</span>
          <Button size="sm" className="mt-2" onClick={onRetry}>
            Try again
          </Button>
        </Alert>
      ) : null}

      {notes.length > 0 ? (
        <p className="text-[12px] text-ink-2" role="status">
          On this page: {notes.join(" · ")}.
        </p>
      ) : null}

      <div aria-live="polite" className="sr-only">
        {loading ? "Refreshing order units" : `${orders.length} order units shown`}
      </div>

      <Table
        columns={columns}
        rows={orders}
        rowKey={(o) => o.id}
        caption="Order units, newest first"
        dense
        minWidth={`${tableWidth(columns)}px`}
        empty={
          filtered ? (
            <EmptyState
              title="Showing 0 of 0 order units"
              description="Nothing matches these filters."
              action={
                <Button size="sm" onClick={onClearFilter}>
                  Reset Filters
                </Button>
              }
            />
          ) : undefined
        }
      />
    </div>
  );
}

export default OrderTable;
