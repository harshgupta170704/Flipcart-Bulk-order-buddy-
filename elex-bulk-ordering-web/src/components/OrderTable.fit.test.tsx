// @vitest-environment jsdom
/**
 * The ledger on a laptop screen. Its default fourteen columns made a table of
 * 2,443 px in a space of 1,124: Flipkart's status, the order id and the amount
 * were off the screen, and nothing said the table scrolled sideways (found in
 * a real browser, 11 Nov 2026). The default view now holds what fits, with
 * the order id under the product and the delivery day under the status; every
 * other column is still one tick away.
 */
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Order } from "../lib/types";

vi.mock("../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/api")>()),
  downloadInvoice: vi.fn(),
}));

import { DEFAULT_ORDER_COLUMNS, ORDER_COLUMN_OPTIONS, OrderTable } from "./OrderTable";

afterEach(cleanup);

function order(patch: Partial<Order>): Order {
  return {
    id: 1,
    order_row_id: 1,
    account_id: 2,
    account_email: "buyer@example.com",
    platform: "flipkart",
    source: "run",
    unit_position: 1,
    units_in_order: 1,
    product_link: null,
    qty: 1,
    model: "Pigeon Special 3 L",
    color: null,
    pin: null,
    amount: 638,
    target: 1000,
    payment_mode: "card",
    card_number: null,
    order_id: "OD338996689296590100",
    gst_name: null,
    status: "placed",
    placed_at: "2026-10-27T14:33:00",
    created_at: "2026-10-27T14:30:00",
    brand: null,
    category: null,
    job_id: 64,
    item_id: null,
    unit_id: null,
    seller: null,
    tracking_id: null,
    platform_status: "Shipped",
    delivery_state: "in_transit",
    delivery_date: "2026-11-01T00:00:00",
    delivery_otp: null,
    invoice_url: null,
    invoice_on_file: false,
    synced_at: "2026-10-28T07:57:00",
    ...patch,
  } as Order;
}

function show(rows: Order[], visibleColumns?: string[]) {
  render(
    <OrderTable
      orders={rows}
      loading={false}
      error={null}
      onRetry={() => undefined}
      filtered={false}
      onClearFilter={() => undefined}
      visibleColumns={visibleColumns}
    />,
  );
  return screen.getByRole("table");
}

describe("the ledger's default view", () => {
  it("holds the columns that fit a laptop screen, and offers the rest", () => {
    expect(DEFAULT_ORDER_COLUMNS).toEqual([
      "bob_order",
      "account_email",
      "placed_at",
      "model",
      "status",
      "platform_status",
      "delivery_otp",
      "qty",
      "amount",
      "invoice",
      "actions",
    ]);
    const offered = ORDER_COLUMN_OPTIONS.map((option) => option.id);
    for (const id of ["order_id", "unit_id", "payment_mode", "card_number", "tracking_id", "delivery_date"]) {
      expect(offered).toContain(id);
    }
  });

  it("needs no more width than its columns ask for", () => {
    const table = show([order({})]);
    // 1,124 until 26 Nov 2026, when the delivery OTP (80 px) joined the
    // default view at the operator's request: on a 1,124 px laptop the table
    // now scrolls those 80 px; on a desktop screen it fits.
    expect(Number.parseInt(table.style.minWidth, 10)).toBeLessThanOrEqual(1204);
  });

  it("keeps the order id and the delivery day in sight without their columns", () => {
    const table = show([order({})]);
    expect(within(table).getByText("OD338996689296590100")).toBeTruthy();
    expect(within(table).getByText("Promised Nov 01, 2026")).toBeTruthy();
  });

  it("does not repeat them once their own columns are switched on", () => {
    const table = show([order({})], [...DEFAULT_ORDER_COLUMNS, "order_id", "delivery_date"]);
    expect(within(table).getAllByText("OD338996689296590100")).toHaveLength(1);
    expect(within(table).queryByText("Promised Nov 01, 2026")).toBeNull();
  });

  it("puts no delivery day under an order that was never placed", () => {
    const table = show([
      order({ status: "failed", platform_status: "Order Not Placed", delivery_state: "cancelled" }),
    ]);
    expect(within(table).getByText("Not placed — payment failed")).toBeTruthy();
    expect(within(table).queryByText(/Nov 01, 2026/)).toBeNull();
  });

  it("says why a failed attempt failed, when it was tried and what for", () => {
    const table = show([
      order({
        status: "failed",
        order_id: null,
        model: null,
        placed_at: null,
        platform_status: null,
        delivery_state: "unknown",
        delivery_date: null,
        synced_at: null,
        amount: null,
        product_link: "https://www.flipkart.com/pigeon-special-3-l-inner-lid-pressure-cooker/p/itm5b89360911985?pid=PRC",
        run_message: "Cash on Delivery is not available for this product — nothing was charged",
      }),
    ]);
    expect(within(table).getByText("Cash on Delivery is not available for this product — nothing was charged")).toBeTruthy();
    expect(within(table).getByText("pigeon special 3 l inner lid pressure cooker")).toBeTruthy();
    expect(within(table).getByText(/tried/)).toBeTruthy();
    // Nothing was ever going to be read back for it.
    expect(within(table).queryByText("Not read back")).toBeNull();
  });
});
