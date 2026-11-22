/**
 * The "order_failed" rule also reports the orders Flipkart took and cancelled
 * later (reworded in place, under the same kind), so the queue's name for the
 * kind says both rather than "Order failed" alone.
 */
import { describe, expect, it } from "vitest";
import { kindLabel } from "./AlertSeverity";

describe("kindLabel", () => {
  it("names the order-outcome kind for both things it reports", () => {
    expect(kindLabel("order_failed")).toBe("Order failed or cancelled");
  });

  it("still humanises every other kind as before", () => {
    expect(kindLabel("order_unconfirmed")).toBe("Order unconfirmed");
    expect(kindLabel("cancel_reason")).toBe("Cancel reason");
    expect(kindLabel("gst_mismatch")).toBe("Order unit GST mismatch");
  });
});
