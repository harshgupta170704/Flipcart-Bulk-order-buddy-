import type { Order } from "./types";

/** What the ledger's Outcome can say: the stored status, one of the two cancellations, or an order never placed. */
export type OrderOutcome = Order["status"] | "cancelled_by_flipkart" | "cancelled_by_user" | "not_placed";

/**
 * Flipkart's wording for an order it never took as a real one: the payment did
 * not go through, yet the attempt has an order id ("Order Not Placed", read
 * back live on 28 Oct 2026; "Payment Failed"). The server reads such a unit as
 * cancelled (app.core.order_status.never_placed, the same pattern), so the
 * order stops standing; this is what lets the page say "Not placed" and
 * "nothing charged" where it would say "Cancelled by Flipkart" and "refund due".
 */
const NEVER_PLACED =
  /\bnot\s+placed\b|\b(?:could\s*n[o']?t|can\s*not|cannot|unable\s+to)\s+(?:be\s+)?place|\bpayment\s+(?:has\s+|was\s+|is\s+)?(?:failed|declined|unsuccessful|rejected)\b|\b(?:failed|declined|unsuccessful)\s+payment\b|\btransaction\s+(?:has\s+|was\s+)?(?:failed|declined)\b|\border\s+failed\b/i;

export function neverPlaced(status: string | null | undefined): boolean {
  return NEVER_PLACED.test(status ?? "");
}

type OutcomeFields = Pick<Order, "status" | "order_id" | "cancelled_by"> & Partial<Pick<Order, "platform_status">>;

/**
 * The Outcome the ledger shows for an order: its stored status, except for an
 * order Flipkart took and later cancelled. The read-back marks that one
 * "failed" too, but it carries Flipkart's order id — a run records one only
 * once Flipkart has confirmed the order — so it reads "Cancelled by Flipkart",
 * or "Cancelled by user" when Flipkart's words say the account asked for it,
 * never the red "Failed" of a run that stopped before anything was sent. An
 * order whose wording says it was never placed reads "Not placed". The
 * server asks the same question (app.core.cancellations) for the dashboard's
 * counts and the alerts, and says who cancelled in `cancelled_by`.
 */
export function orderOutcome(order: OutcomeFields): OrderOutcome {
  if (order.status === "failed" && order.order_id) {
    if (order.cancelled_by === "user") return "cancelled_by_user";
    return neverPlaced(order.platform_status) ? "not_placed" : "cancelled_by_flipkart";
  }
  return order.status;
}

/** True when the order was taken by Flipkart and cancelled there afterwards (or never placed there). */
export function cancelledAfterPlacing(order: OutcomeFields): boolean {
  const outcome = orderOutcome(order);
  return outcome === "cancelled_by_flipkart" || outcome === "cancelled_by_user" || outcome === "not_placed";
}
