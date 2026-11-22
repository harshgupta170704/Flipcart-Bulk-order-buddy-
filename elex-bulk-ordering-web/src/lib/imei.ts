import type { Order } from "./types";

/**
 * How a unit's IMEI was told, said in words for the ledger's hover and the
 * row dialog: a product invoice lists IMEIs but never says which unit has
 * which, so a match by position is marked as one, and IMEIs that could not be
 * matched at all are the order's, not the unit's.
 */
export function imeiNote(scope: Order["imei_scope"]): string | undefined {
  if (scope === "position") return "Matched by position: the invoice lists one IMEI per unit but does not say which unit is which";
  if (scope === "order") return "Every IMEI on the order's invoices: they list a different number than the order has units";
  return undefined;
}
