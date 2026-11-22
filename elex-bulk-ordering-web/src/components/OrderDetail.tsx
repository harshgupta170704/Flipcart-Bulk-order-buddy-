/**
 * Every stored field of one order, and the one action an operator may take on
 * it: settling an UNCONFIRMED order after checking the account on Flipkart.
 * The action lives here rather than in the table row, so the table stays a
 * table and the explanation is read once, where the decision is made.
 */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { downloadOrderInvoice, errorMessage, listOrderInvoices, setOrderReferences, settleOrder, startSyncJob, waitForJob } from "../lib/api";
import { cancelledAfterPlacing, neverPlaced, orderOutcome } from "../lib/orderOutcome";
import type { Order, OrderInvoice, OrderInvoiceLine } from "../lib/types";
import { DASH, formatCurrency, formatDate, formatDateTime, formatLate, promiseDays } from "../lib/format";
import { imeiNote } from "../lib/imei";
import { Alert } from "./Alert";
import { Button } from "./Button";
import { Dialog } from "./Dialog";
import { TextInput } from "./Field";
import { StatusBadge } from "./StatusBadge";
import { UnitRequests } from "./UnitRequests";

export interface OrderDetailProps {
  order: Order | null;
  onClose: () => void;
  /** Called after an unconfirmed order has been settled, so the list reloads. */
  onSettled?: () => void;
  /** Called after a read-back started from the dialog has ended, so the list — and this dialog — reload. */
  onRefreshed?: () => void;
}

/**
 * On the courier's van: Flipkart's wording says so, or the page showed a
 * delivery code on a unit still on its way (the code appears only from then).
 */
export function isOutForDelivery(order: Pick<Order, "platform_status" | "delivery_otp" | "delivery_state">): boolean {
  if (/out\s*for\s*delivery/i.test(order.platform_status ?? "")) return true;
  return Boolean(order.delivery_otp) && (order.delivery_state === "in_transit" || order.delivery_state === "unknown");
}

/** The order page's timeline as real steps only, whatever an older server stored. */
function timelineSteps(value: unknown): { status: string; date: string | null }[] {
  if (!Array.isArray(value)) return [];
  return value.filter(
    (step): step is { status: string; date: string | null } =>
      Boolean(step) && typeof step === "object" && typeof (step as { status?: unknown }).status === "string",
  );
}

/** The delivery code, first thing in the dialog, while the unit is out for delivery. */
function OutForDeliveryNotice({ order }: { order: Order }) {
  return (
    <Alert tone="warning" title="Out for delivery">
      {order.delivery_otp ? (
        <p className="text-[13px]">
          Delivery code{" "}
          <span className="font-mono text-[20px] font-semibold tracking-[0.2em] text-ink tabular-nums">{order.delivery_otp}</span>
          <span className="mt-1 block text-[12px] text-ink-2">
            Give it to the courier only after checking the package. Read from Flipkart{" "}
            {order.synced_at ? formatDateTime(order.synced_at) : "at the last read-back"}.
          </span>
        </p>
      ) : (
        <p className="text-[13px]">
          Flipkart has not shown a delivery code on this order's page yet. While the order is out for
          delivery it is read back from Flipkart every half hour, and the code appears here as soon as
          Flipkart shows it.
        </p>
      )}
    </Alert>
  );
}

/**
 * Read this order's account back from Flipkart now, rather than at the next
 * automatic read-back: its recent orders are read, one Flipkart page each
 * (about a minute), and the dialog then shows what came back.
 */
function ReadBackNow({ order, onRefreshed }: { order: Order; onRefreshed?: () => void }) {
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const live = useRef(true);
  const stop = useRef<AbortController | null>(null);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      stop.current?.abort();
    };
  }, []);
  const accountId = order.account_id;
  if (accountId === undefined || accountId === null) return null;

  const start = async () => {
    const controller = new AbortController();
    stop.current = controller;
    setBusy(true);
    setNote(null);
    try {
      const job = await startSyncJob({ account_ids: [accountId] });
      if (live.current) setNote(`Reading this account's orders back from Flipkart (run #${job.id}) — this takes about a minute.`);
      const done = await waitForJob(job.id, { signal: controller.signal });
      if (!live.current) return;
      const item = done.items.find((each) => each.account_id === accountId);
      const said = item?.message ?? null;
      setNote(
        // Passed over (an order run was using the account, say): nothing new
        // was read, whatever the run's own status.
        item?.status === "skipped"
          ? `Not read back${said ? `: ${said}` : "."}`
          : done.status === "completed"
            ? `Read back from Flipkart${said ? `: ${said}` : "."}`
            : `The read-back did not finish${said ? `: ${said}` : " — see Recent jobs on the dashboard"}.`,
      );
      onRefreshed?.();
    } catch (err) {
      if (live.current && !controller.signal.aborted) setNote(errorMessage(err));
    } finally {
      if (live.current) setBusy(false);
    }
  };

  return (
    <span className="mt-1 flex flex-col items-start gap-1">
      <Button size="sm" loading={busy} disabled={busy} onClick={() => void start()}>
        Read back now
      </Button>
      {note ? (
        <span role="status" className="text-[12px] text-ink-2">
          {note}
        </span>
      ) : null}
    </span>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[160px_1fr] gap-3 py-1.5 text-[13px]">
      <dt className="text-ink-2">{label}</dt>
      <dd className="min-w-0 break-words text-ink">{children}</dd>
    </div>
  );
}

function text(value: string | number | null | undefined): ReactNode {
  return value === null || value === undefined || value === "" ? <span className="text-muted">{DASH}</span> : String(value);
}

/**
 * Why the order was cancelled, in Flipkart's own words as the order page put
 * it — or that no reason was recorded (a cancellation read back before the
 * reason was, or a page that gave none).
 */
function CancelReason({ order }: { order: Order }) {
  if (neverPlaced(order.platform_status)) {
    return (
      <span className="mt-1 block text-[12px] leading-snug text-ink-2">
        Flipkart says: <span className="text-ink">{order.platform_status}</span>. The payment did not go through, so
        nothing is on its way. If the card shows a charge, the bank reverses it.
      </span>
    );
  }
  return order.cancel_reason ? (
    <span className="mt-1 block text-[12px] leading-snug text-ink-2">
      Flipkart says: <span className="text-ink">{order.cancel_reason}</span>
    </span>
  ) : (
    <span className="mt-1 block text-[12px] text-muted">No reason recorded.</span>
  );
}

/** The dialog's heading: Flipkart's order id when the order has one; else
 *  what the row is — never the ledger's own row number, which means nothing
 *  to a person ("Order #18" until 11 Nov 2026). */
function dialogTitle(order: Order): string {
  if (order.order_id) return `Order ${order.order_id}`;
  if (order.status === "unconfirmed") return "Order sent, not confirmed";
  if (order.status === "failed_over_target") return "Order not placed — over the price cap";
  if (order.status === "failed") return "Order attempt — not placed";
  return "Order";
}

/** A run's attempt that never became an order on Flipkart: there is nothing
 *  to read back, now or later, so its delivery side says so instead of "not yet". */
function neverReachedFlipkart(order: Order): boolean {
  return !order.order_id && (order.status === "failed" || order.status === "failed_over_target");
}

function deliveryKey(state: Order["delivery_state"]): string {
  if (state === "delivered") return "done";
  if (state === "cancelled" || state === "returned" || state === "failed") return "failed";
  return "pending";
}

function Settle({ order, onSettled }: { order: Order; onSettled?: () => void }) {
  const [orderId, setOrderId] = useState("");
  const [busy, setBusy] = useState<"placed" | "failed" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const id = orderId.trim();
  const valid = /^OD\d{10,}$/.test(id);

  const settle = async (body: { status: "placed"; order_id: string } | { status: "failed" }) => {
    setBusy(body.status);
    setError(null);
    try {
      // Settling is an order-level action: address the order row, not the unit.
      await settleOrder(order.order_row_id ?? order.id, body);
      onSettled?.();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  };

  return (
    <Alert tone="error" title="Submitted to Flipkart, never confirmed — it may exist">
      <p className="text-[13px]">
        Until someone looks, this order counts against the unit limits and stays in the alerts.
        Open the account on Flipkart: if the order is there, enter its id; if it is not, mark it failed.
      </p>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <TextInput
          aria-label="Order id found on Flipkart"
          placeholder="OD…"
          value={orderId}
          onChange={(event) => setOrderId(event.target.value)}
          className="!h-8 w-[220px] text-[13px]"
        />
        <Button
          size="sm"
          disabled={!valid || busy !== null}
          loading={busy === "placed"}
          onClick={() => void settle({ status: "placed", order_id: id })}
        >
          Found it — mark placed
        </Button>
        <Button
          size="sm"
          variant="secondary"
          disabled={busy !== null}
          loading={busy === "failed"}
          onClick={() => void settle({ status: "failed" })}
        >
          Not there — mark failed
        </Button>
      </div>
      {error ? (
        <p role="alert" className="mt-2 text-[12px] text-critical">
          {error}
        </p>
      ) : null}
    </Alert>
  );
}

const SOURCE_LABEL: Record<Order["source"], string> = {
  run: "Placed by this tool",
  sync: "First seen on Flipkart",
  upload: "Handed in by id to track",
};

/** The platform's name as people write it; the server keeps it lower-case. */
const PLATFORM_LABEL: Record<string, string> = { flipkart: "Flipkart" };

/** Flipkart's words for paying at the door, as its order page's "Paid By" puts them. */
const PAID_ON_DELIVERY = /cash on delivery|pay on delivery/i;

/**
 * How the order is paid, as the ledger holds it: the run's card or Cash on
 * Delivery, or — "prepaid" — an order report's "Cash on Delivery: No", which
 * says it was paid before it came but not how, so it never reads as a card.
 * The order page's own "Cash On Delivery" stands over a report's "No", as the
 * CSV and the Order Details API read it.
 */
function paymentLabel(order: Order): ReactNode {
  const mode = order.payment_mode;
  if (mode === "cod") return "Cash on Delivery";
  if (mode === "card") return "Card";
  if (mode === "gv_card") return "Gift voucher + card";
  if (mode === "gv") return "Gift voucher";
  if (mode === "cod_card") return "Cash on Delivery + card advance";
  if (mode === "rtgs") return "RTGS";
  if (mode === "imps") return "IMPS";
  if (mode === "prepaid") {
    return PAID_ON_DELIVERY.test(order.payment_method ?? "") ? "Cash on Delivery" : "Paid in advance, not Cash on Delivery";
  }
  return <span className="text-muted">Not recorded</span>;
}

/**
 * The refund, when one is known: its status, its amount and how the money
 * went back ("Refund Completed · ₹18,207.00 · Original payment method") —
 * Flipkart's own words for a cancelled unit's refund, else what an order
 * report said (spec FR-022). Null when nothing says.
 */
function refundLine(order: Order): string | null {
  const amount = order.refund_amount ?? null;
  const parts = [order.refund_status || null, amount === null ? null : formatCurrency(amount), order.refund_method || null];
  const said = parts.filter((part): part is string => part !== null);
  return said.length > 0 ? said.join(" · ") : null;
}

/**
 * The order page's figure for this unit and how it is paid, worded for where
 * the unit stands. The figure is the page's "Total amount" — paid, or to be
 * paid on delivery — so it is called paid only when it was: Cash on Delivery
 * is paid at the door, so not before delivery and never for a unit that did
 * not arrive; a card is paid when the order is placed, and is owed back when
 * the unit is cancelled, never delivered or sent back. Cash on Delivery is
 * told from the page's own words, not the run's choice: an order first seen
 * on Flipkart, or handed in by id, carries no choice the run made.
 * Null until a read-back saw the page's price details.
 */
/**
 * The payment line of a unit the portal reads as returned, from Flipkart's own
 * words, because "returned" covers money moving both ways and not at all: a
 * return or replacement withdrawn ("Return Cancelled", "Replacement
 * Cancelled") leaves the unit delivered, and a replacement swaps the item,
 * so nothing is owed; "Refund Completed" means the money is back; a return or
 * refund still under way means it is owed. A unit the courier took back
 * undelivered ("Return to Origin") collected nothing at the door, and owes a
 * card payment back.
 */
function returnedPayment(status: string, paid: string, atTheDoor: boolean, total: string | null, method: string): string {
  if (/cancel/i.test(status) || /replac/i.test(status)) return paid;
  if (/return\s*to\s*origin/i.test(status)) {
    return atTheDoor ? `${total === null ? "" : `${total}, `}${method} — not delivered, nothing collected` : `${paid} — refund due`;
  }
  if (/refund/i.test(status) && /complet|credit|success/i.test(status)) return `${paid} — refunded`;
  if (/return|refund/i.test(status)) return `${paid} — refund due`;
  return paid;
}

function paidLine(order: Order): string | null {
  const amount = order.amount_paid ?? null;
  const method = order.payment_method ?? null;
  if (amount === null && method === null) return null;
  if (method === null) return `Order total ${formatCurrency(amount)}`;
  const total = amount === null ? null : formatCurrency(amount);
  const paid = total === null ? `Paid by ${method}` : `Paid ${total} by ${method}`;
  const state = order.delivery_state;
  const atTheDoor = PAID_ON_DELIVERY.test(method);
  // An order never placed was never paid for: no "refund due" for it.
  if (neverPlaced(order.platform_status)) {
    return `${total === null ? "" : `${total}, `}${method} — payment not completed, nothing charged`;
  }
  if (state === "returned") return returnedPayment(order.platform_status ?? "", paid, atTheDoor, total, method);
  if (state === "cancelled" || state === "failed") {
    if (!atTheDoor) return `${paid} — refund due`;
    const outcome = state === "cancelled" ? "cancelled" : "not delivered";
    return `${total === null ? "" : `${total}, `}${method} — ${outcome}, nothing collected`;
  }
  if (atTheDoor && state !== "delivered") {
    return total === null ? `To pay on delivery (${method})` : `${total} to pay on delivery (${method})`;
  }
  return paid;
}

/** The promise and the delivery, apart: "Promised 01 Nov 2026 · Delivered 03
 *  Nov 2026 (2 days late)". When Flipkart moved its promise the first one is
 *  shown, since the lateness is counted from it, and the move beside it:
 *  "Promised 01 Nov 2026 (moved to 05 Nov 2026) · Delivered 05 Nov 2026 (4
 *  days late)". A row read back before they were kept apart has only the one
 *  date Flipkart showed. */
function deliveryDates(order: Order): ReactNode {
  const { first, movedTo } = promiseDays(order);
  const delivered = order.delivered_date ?? null;
  if (first === null && delivered === null) return formatDate(order.delivery_date);
  const late = formatLate(order.days_late);
  const parts = [
    first === null ? null : `Promised ${formatDate(first)}${movedTo === null ? "" : ` (moved to ${formatDate(movedTo)})`}`,
    delivered === null ? null : `Delivered ${formatDate(delivered)}${late === null ? "" : ` (${late.toLowerCase()})`}`,
  ].filter((part): part is string => part !== null);
  return parts.join(" · ");
}

/** A timeline step that is the delivery itself ("Delivered", "Delivery expected"). */
function isDeliveryStep(status: string): boolean {
  return /^deliver/i.test(status.trim());
}

/** The card the order was paid with, in full (the owner's decision of 25 Oct 2026); COD has none. */
function cardNumber(order: Order): ReactNode {
  if (order.card_number) return <span className="font-mono tracking-wider tabular-nums">{order.card_number}</span>;
  if (order.payment_mode === "cod") return "COD";
  return text(null);
}

/**
 * Flipkart's own link to the invoice of an order with no PDF on file yet. It
 * opens only in a browser signed in to that account on Flipkart; the next
 * read-back fetches the PDF with the account's session and files it here.
 * Only a web address is made a link: the value was read off Flipkart's page.
 */
function FlipkartInvoiceLink({ url }: { url: string }) {
  return (
    <span className="flex flex-col gap-0.5">
      <span>Seen on Flipkart — fetched at the next read-back</span>
      {/^https?:\/\//i.test(url) ? (
        <a
          href={url}
          target="_blank"
          rel="noopener noreferrer"
          title="Opens on Flipkart, where you must be signed in to this account"
          className="break-all text-[12px] underline decoration-from-font underline-offset-2"
        >
          {url}
        </a>
      ) : (
        <span className="break-all text-[12px] text-ink-2">{url}</span>
      )}
    </span>
  );
}

function gstOf(line: OrderInvoiceLine): number {
  return (line.cgst ?? 0) + (line.sgst ?? 0) + (line.igst ?? 0);
}

/**
 * Every invoice on file for the order — the product's, the fees', the
 * transport's — with what was read off each PDF and a download per invoice.
 * Loaded when the dialog opens; an order with nothing on file shows why.
 */
function Invoices({ order }: { order: Order }) {
  const rowId = order.order_row_id ?? order.id;
  const [rows, setRows] = useState<OrderInvoice[] | null>(order.invoice_on_file ? null : []);
  const [error, setError] = useState<string | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setError(null);
    if (!order.invoice_on_file) {
      setRows([]);
      return () => {
        live = false;
      };
    }
    setRows(null);
    listOrderInvoices(rowId)
      .then((list) => {
        if (live) setRows(list);
      })
      .catch((err) => {
        if (live) {
          setRows([]);
          setError(errorMessage(err));
        }
      });
    return () => {
      live = false;
    };
  }, [rowId, order.invoice_on_file]);

  if (error) return <Alert tone="error">{error}</Alert>;
  if (rows === null) return <span className="text-muted">Loading…</span>;
  if (rows.length === 0) {
    return order.invoice_url ? <FlipkartInvoiceLink url={order.invoice_url} /> : <span className="text-muted">None on file</span>;
  }

  const total = rows.reduce((sum, inv) => sum + (inv.grand_total ?? 0), 0);
  const product = rows.find((inv) => inv.kind === "product");
  const comparison =
    product && product.grand_total !== null && order.amount !== null && order.amount !== undefined
      ? product.grand_total === order.amount
        ? " · the product invoice matches the order amount"
        : ` · the product invoice says ${formatCurrency(product.grand_total)}, the order was recorded at ${formatCurrency(order.amount)}`
      : "";

  return (
    <div className="flex flex-col gap-2">
      {downloadError ? (
        <Alert tone="error" title="Could not download the invoice">
          {downloadError}
        </Alert>
      ) : null}
      <ul className="divide-y divide-line rounded-control border border-line">
        {rows.map((inv) => (
          <li key={inv.id} className="flex flex-col gap-1 px-3 py-2">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium text-ink">
                {inv.invoice_type ?? (inv.parse_error ? "Could not read this PDF" : "Invoice")}
              </span>
              <span className="text-ink-2">{inv.invoice_number ?? DASH}</span>
              <span className="text-ink-2">{formatDate(inv.invoice_date)}</span>
              <span className="ml-auto tabular-nums">{formatCurrency(inv.grand_total)}</span>
              <Button
                size="sm"
                variant="ghost"
                aria-label={`Download invoice ${inv.invoice_number ?? inv.id}`}
                onClick={() => {
                  setDownloadError(null);
                  // A failed download used to vanish; the dialog now says why.
                  void (async () => {
                    try {
                      await downloadOrderInvoice(
                        rowId,
                        inv.id,
                        `${order.order_id ?? rowId}-${inv.invoice_number ?? inv.id}.pdf`,
                      );
                    } catch (err) {
                      setDownloadError(errorMessage(err));
                    }
                  })();
                }}
              >
                PDF
              </Button>
            </div>
            {inv.seller_name || inv.buyer_gstin ? (
              <div className="text-[12px] text-ink-2">
                {inv.seller_name ? `${inv.seller_name}${inv.seller_gstin ? ` · ${inv.seller_gstin}` : ""}` : ""}
                {inv.buyer_gstin ? `${inv.seller_name ? " · " : ""}billed to ${inv.buyer_gstin}` : ""}
              </div>
            ) : null}
            {inv.lines.map((line, index) => (
              <div key={index} className="text-[12px] text-ink-2">
                {line.title ?? "Line"}
                {line.qty ? ` × ${line.qty}` : ""} · taxable {formatCurrency(line.taxable)} · GST {formatCurrency(gstOf(line))} ·
                total {formatCurrency(line.total)}
                {line.imei ? ` · IMEI ${line.imei}` : ""}
              </div>
            ))}
            {inv.parse_error ? (
              // With a type on file the reading stands (a newer reader's
              // failed re-read keeps the earlier one), so its note is a
              // remark, not an error.
              <div className={`text-[12px] ${inv.invoice_type ? "text-ink-2" : "text-critical"}`}>{inv.parse_error}</div>
            ) : null}
          </li>
        ))}
      </ul>
      <p className="text-[12px] text-ink-2">
        {rows.length} invoice{rows.length === 1 ? "" : "s"} on file, {formatCurrency(total)} in all{comparison}
      </p>
    </div>
  );
}

/** PO Number, RTGS source / beneficiary and e-way bill, filled in by hand. */
function References({ order, onSaved }: { order: Order; onSaved?: () => void }) {
  const [draft, setDraft] = useState({
    po_number: order.po_number ?? "",
    rtgs_source: order.rtgs_source ?? "",
    rtgs_beneficiary: order.rtgs_beneficiary ?? "",
    ewb_number: order.ewb_number ?? "",
  });
  const [note, setNote] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const rowId = order.order_row_id ?? order.id;
  const save = async () => {
    setSaving(true);
    try {
      await setOrderReferences(rowId, draft);
      setNote("Saved — these go to the export's own columns.");
      onSaved?.();
    } catch (err) {
      setNote(errorMessage(err));
    } finally {
      setSaving(false);
    }
  };
  const field = (key: keyof typeof draft, label: string) => (
    <label className="flex flex-col gap-1 text-[12px] text-ink-2">
      {label}
      <input
        className="h-8 rounded-control border border-line bg-surface px-2 text-[13px] text-ink"
        value={draft[key]}
        onChange={(e) => setDraft((d) => ({ ...d, [key]: e.target.value }))}
      />
    </label>
  );
  return (
    <section className="flex flex-col gap-2 rounded-control border border-line p-3">
      <h3 className="text-[13px] font-semibold text-ink">References</h3>
      <div className="grid gap-2 sm:grid-cols-2">
        {field("po_number", "PO Number")}
        {field("ewb_number", "EWB Number")}
        {field("rtgs_source", "RTGS Source")}
        {field("rtgs_beneficiary", "RTGS Beneficiary")}
      </div>
      <div className="flex items-center gap-2">
        <Button size="sm" onClick={() => void save()} loading={saving}>
          Save references
        </Button>
        {note ? <span className="text-[12px] text-ink-2">{note}</span> : null}
      </div>
    </section>
  );
}

export function OrderDetail({ order, onClose, onSettled, onRefreshed }: OrderDetailProps) {
  const unitNote = order && order.units_in_order > 1 ? ` · unit ${order.unit_position} of ${order.units_in_order}` : "";
  return (
    <Dialog
      open={order !== null}
      onClose={onClose}
      title={order ? `${dialogTitle(order)}${unitNote}` : ""}
      description={order?.model ?? undefined}
      width="760px"
      footer={
        <Button size="sm" onClick={onClose}>
          Close
        </Button>
      }
    >
      {order ? (
        <div className="flex flex-col gap-5">
          {order.status === "unconfirmed" ? <Settle order={order} onSettled={onSettled} /> : null}
          <References key={order.id} order={order} onSaved={onRefreshed} />
          {isOutForDelivery(order) ? <OutForDeliveryNotice order={order} /> : null}

          <section>
            <h3 className="mb-1 text-[12px] font-semibold uppercase tracking-wide text-muted">Order</h3>
            <dl className="divide-y divide-line">
              <Row label="Outcome">
                <StatusBadge status={orderOutcome(order)} />
                {cancelledAfterPlacing(order) ? <CancelReason order={order} /> : null}
                {/* Why a run's attempt did not end as a placed order, in the run's
                    own words: "Failed" alone sent the operator hunting for the run. */}
                {order.run_message && order.status !== "placed" && !cancelledAfterPlacing(order) ? (
                  <span className="mt-1 block text-[12px] leading-snug text-ink-2">
                    Why: <span className="text-ink">{order.run_message}</span>
                  </span>
                ) : null}
              </Row>
              {order.processing_stage ? <Row label="Processing stage">{order.processing_stage}</Row> : null}
              {order.error_code ? (
                <Row label="Error code">
                  <span className="font-mono text-[12px]">{order.error_code}</span>
                </Row>
              ) : null}
              <Row label="Platform">{order.platform ? (PLATFORM_LABEL[order.platform] ?? order.platform) : text(null)}</Row>
              <Row label="Account">{text(order.account_email)}</Row>
              <Row label="Product">{text(order.model)}</Row>
              <Row label="Colour">{text(order.color)}</Row>
              <Row label="Quantity">{text(order.qty)}</Row>
              <Row label="Amount">{formatCurrency(order.amount)}</Row>
              <Row label="Target">{order.target === null ? <span className="text-muted">No cap</span> : formatCurrency(order.target)}</Row>
              <Row label="Payment">
                {paymentLabel(order)}
                {/* What the order page itself says about paying: the run's choice
                    above is what it asked for, this is what Flipkart recorded. */}
                {paidLine(order) !== null ? (
                  <span className="mt-0.5 block text-[12px] text-ink-2">{paidLine(order)}</span>
                ) : null}
              </Row>
              {refundLine(order) !== null ? <Row label="Refund">{refundLine(order)}</Row> : null}
              <Row label="Card number">{cardNumber(order)}</Row>
              <Row label="Order ID">{text(order.order_id)}</Row>
              <Row label="Unit">
                {order.unit_position} of {order.units_in_order}
              </Row>
              <Row label="Unit ID">
                {order.unit_id ?? (
                  <span className="text-muted">{neverReachedFlipkart(order) ? DASH : "Not yet seen on Flipkart"}</span>
                )}
              </Row>
              <Row label="Source">
                {neverReachedFlipkart(order) && order.source === "run"
                  ? "Tried by this tool — no order was placed"
                  : (SOURCE_LABEL[order.source] ?? order.source)}
              </Row>
              <Row label="Item ID">{text(order.item_id)}</Row>
              <Row label="Order date">{formatDateTime(order.placed_at)}</Row>
              <Row label="Recorded">{formatDateTime(order.created_at)}</Row>
              <Row label="Run">{order.job_id === null ? <span className="text-muted">{DASH}</span> : `#${order.job_id}`}</Row>
              {/* The cancel and reschedule requests recorded on this unit (spec FR-020), open or closed. */}
              <Row label="Requests">
                <UnitRequests order={order} />
              </Row>
            </dl>
          </section>

          <section>
            <h3 className="mb-1 text-[12px] font-semibold uppercase tracking-wide text-muted">Delivery</h3>
            <dl className="divide-y divide-line">
              <Row label="Platform status">
                {order.platform_status ? (
                  <StatusBadge status={deliveryKey(order.delivery_state)} label={order.platform_status} />
                ) : (
                  <span className="text-muted">{neverReachedFlipkart(order) ? "Never reached Flipkart" : "Not read back yet"}</span>
                )}
                {/* One unit of an order that stands, cancelled: its reason sits with
                    its own status. A whole order cancelled shows it under Outcome. */}
                {order.cancel_reason && order.delivery_state === "cancelled" && !cancelledAfterPlacing(order) ? (
                  <CancelReason order={order} />
                ) : null}
              </Row>
              <Row label="Timeline">
                {timelineSteps(order.timeline).length > 0 ? (
                  <>
                    <ul className="flex flex-col gap-0.5">
                      {timelineSteps(order.timeline).map((step, index, steps) => {
                        // "Not yet" only when the page dates the steps that have
                        // happened: a page that dates none of them (Flipkart's own,
                        // read back 26 Oct 2026) cannot tell done from to come.
                        const dated = steps.some((each) => each.date);
                        return (
                          <li key={index} className={step.date || !dated ? "" : "text-muted"}>
                            {step.status}
                            {step.date ? ` · ${formatDate(step.date)}` : dated ? " · not yet" : ""}
                            {/* The delivery still to come carries the day Flipkart now
                                promises: its latest promise, not the first. */}
                            {!step.date && order.promised_date && isDeliveryStep(step.status)
                              ? ` · promised ${formatDate(order.promised_date)}`
                              : ""}
                          </li>
                        );
                      })}
                    </ul>
                    {/* Flipkart's page lists every step, done or to come, and dates
                        none: an undated "Out For Delivery" there is not where the
                        order is, and must not read as if it were. */}
                    {timelineSteps(order.timeline).some((step) => step.date) ? null : (
                      <span className="mt-1 block text-[12px] text-ink-2">
                        Flipkart lists these steps without dates, so they don't show which have happened. The
                        platform status above says where the order is.
                      </span>
                    )}
                  </>
                ) : (
                  <span className="text-muted">
                    {order.synced_at ? "No steps on the order page" : "Not read back yet"}
                  </span>
                )}
              </Row>
              <Row label="Delivery date">{deliveryDates(order)}</Row>
              <Row label="Tracking ID">{text(order.tracking_id)}</Row>
              {/* The courier, as an order report named it; no read-back reads one. */}
              {order.shipment_partner ? <Row label="Courier">{order.shipment_partner}</Row> : null}
              {/* What Flipkart's order page says the unit ships to (spec FR-044/045): the
                  subject of an address alert, which no default column of the table shows. */}
              <Row label="Delivery address">
                {text(order.delivery_address)}
                {/* When a read-back saw it replaced by another (spec FR-045): what raised the address-change alert. */}
                {order.address_changed_at ? (
                  <span className="mt-1 block text-[12px] text-ink-2">
                    Delivery address changed {formatDateTime(order.address_changed_at)}
                  </span>
                ) : null}
              </Row>
              <Row label="Delivery pincode">{text(order.delivery_pincode)}</Row>
              {/* The GST the order page says the order was billed under: the subject
                  of a GST-mismatch alert, beside the GST the run chose (gst_name). */}
              <Row label="Billed GST">
                {order.billed_gstin ? (
                  <span>
                    <span className="font-mono">{order.billed_gstin}</span>
                    {order.billed_business_name ? ` · ${order.billed_business_name}` : ""}
                  </span>
                ) : (
                  text(null)
                )}
              </Row>
              <Row label="Seller">{text(order.seller)}</Row>
              <Row label="Received">
                {order.received_at ? (
                  <span>
                    Yes, marked {formatDateTime(order.received_at)}
                    {order.received_by ? ` by ${order.received_by}` : ""}
                  </span>
                ) : (
                  <span className="text-muted">Not marked</span>
                )}
              </Row>
              <Row label="Last tracking update">
                {order.last_tracking_update ? (
                  <span>
                    {formatDate(order.last_tracking_update)} · {order.last_tracking_step}
                  </span>
                ) : (
                  // An order report's own line, while Flipkart's page dates no step.
                  text(order.last_tracking_text ?? null)
                )}
              </Row>
              <Row label="Delivery OTP">
                {order.delivery_otp ? (
                  <span className="font-medium tracking-wider tabular-nums">{order.delivery_otp}</span>
                ) : (
                  <span className="text-muted">{DASH}</span>
                )}
              </Row>
              <Row label="Read back">
                <span>
                  {order.synced_at ? (
                    formatDateTime(order.synced_at)
                  ) : (
                    <span className="text-muted">Not yet — orders on their way are read back from Flipkart every hour</span>
                  )}
                </span>
                <ReadBackNow order={order} onRefreshed={onRefreshed} />
              </Row>
            </dl>
          </section>

          <section>
            <h3 className="mb-1 text-[12px] font-semibold uppercase tracking-wide text-muted">Billing</h3>
            <dl className="divide-y divide-line">
              <Row label="Business (GST)">{text(order.gst_name)}</Row>
              {/* The GSTIN the run billed under, beside its business name: one name
                  can hold a registration per state. */}
              <Row label="Business GST No">{order.gstin ? <span className="font-mono">{order.gstin}</span> : text(null)}</Row>
              <Row label="Billing phone">
                {order.billing_phone ? <span className="tabular-nums">{order.billing_phone}</span> : text(null)}
              </Row>
              {/* From the product invoice on file: the name it was billed to. */}
              <Row label="Billing name">{text(order.billing_name ?? null)}</Row>
              <Row label="IMEI">
                {order.imei ? (
                  <span>
                    <span className="font-mono">{order.imei}</span>
                    {imeiNote(order.imei_scope) ? (
                      <span className="mt-1 block text-[12px] text-ink-2">{imeiNote(order.imei_scope)}</span>
                    ) : null}
                  </span>
                ) : (
                  <span className="text-muted">Not on an invoice on file</span>
                )}
              </Row>
              <Row label="Pincode">{text(order.pin)}</Row>
              <Row label="Brand">{text(order.brand)}</Row>
              <Row label="Category">{text(order.category)}</Row>
              <Row label="Invoices">
                <Invoices order={order} />
              </Row>
              <Row label="Product link">
                {order.product_link ? (
                  <a
                    href={order.product_link}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="underline decoration-from-font underline-offset-2"
                  >
                    {order.product_link}
                  </a>
                ) : (
                  <span className="text-muted">{DASH}</span>
                )}
              </Row>
            </dl>
          </section>
        </div>
      ) : null}
    </Dialog>
  );
}

export default OrderDetail;
