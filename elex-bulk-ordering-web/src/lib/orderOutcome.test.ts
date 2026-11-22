/**
 * The ledger's Outcome: a failed order that carries Flipkart's order id is one
 * Flipkart took and cancelled later, never the run's own "Failed".
 */
import { describe, expect, it } from "vitest";
import { statusMeta } from "../components/StatusBadge";
import { cancelledAfterPlacing, neverPlaced, orderOutcome } from "./orderOutcome";

describe("orderOutcome", () => {
  it("keeps Failed for a run that never reached Flipkart", () => {
    expect(orderOutcome({ status: "failed", order_id: null })).toBe("failed");
    expect(orderOutcome({ status: "failed", order_id: "" })).toBe("failed");
    expect(cancelledAfterPlacing({ status: "failed", order_id: null })).toBe(false);
  });

  it("reads a failed order with Flipkart's order id as cancelled by Flipkart", () => {
    const order = { status: "failed" as const, order_id: "OD338967210239333100" };
    expect(orderOutcome(order)).toBe("cancelled_by_flipkart");
    expect(orderOutcome({ ...order, cancelled_by: "flipkart" })).toBe("cancelled_by_flipkart");
    expect(cancelledAfterPlacing(order)).toBe(true);
    expect(statusMeta(orderOutcome(order)).label).toBe("Cancelled by Flipkart");
  });

  it("says so when the account itself asked for the cancellation", () => {
    const outcome = orderOutcome({ status: "failed", order_id: "OD338967210239333100", cancelled_by: "user" });
    expect(outcome).toBe("cancelled_by_user");
    expect(statusMeta(outcome).label).toBe("Cancelled by user");
  });

  it("leaves every other outcome as recorded", () => {
    for (const status of ["placed", "failed_over_target", "unconfirmed", "pending"] as const) {
      expect(orderOutcome({ status, order_id: "OD338967210239333100" })).toBe(status);
    }
  });

  it("never draws a Flipkart cancellation in the run's failure colour", () => {
    expect(statusMeta("cancelled_by_flipkart").colorVar).not.toBe(statusMeta("failed").colorVar);
    expect(statusMeta("cancelled_by_flipkart").glyph).not.toBe(statusMeta("failed").glyph);
  });
});

describe("an order Flipkart never placed", () => {
  const order = { status: "failed" as const, order_id: "OD338996689296590100", platform_status: "Order Not Placed" };

  it("reads Not placed, never Cancelled by Flipkart", () => {
    expect(neverPlaced("Order Not Placed")).toBe(true);
    expect(neverPlaced("Payment Failed")).toBe(true);
    expect(neverPlaced("Your payment has failed")).toBe(true);
    expect(orderOutcome(order)).toBe("not_placed");
    expect(cancelledAfterPlacing(order)).toBe(true);
    expect(statusMeta(orderOutcome(order)).label).toBe("Not placed — payment failed");
  });

  it("does not catch an order that was placed", () => {
    for (const status of ["Order Placed", "Your order has been placed", "Cancelled", "Shipped", "", null]) {
      expect(neverPlaced(status)).toBe(false);
    }
    expect(orderOutcome({ ...order, platform_status: "Cancelled" })).toBe("cancelled_by_flipkart");
  });

  it("leaves the account's own cancellation as the account's", () => {
    expect(orderOutcome({ ...order, cancelled_by: "user" })).toBe("cancelled_by_user");
  });
});
