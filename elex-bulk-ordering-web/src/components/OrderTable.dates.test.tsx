// @vitest-environment jsdom
/**
 * The ledger's Promised date and Order total columns (26 Oct 2026): the
 * promise shown is the first one, which the Late column counts from, with the
 * day Flipkart moved it to beneath; and the order page's total is named for
 * what it is — paid, to be paid on delivery, or never collected. And the
 * Delivery date column (27 Oct 2026), which says "Delivered" only of a unit
 * that was: a card order Flipkart cancelled and refunded reads as returned,
 * but it never arrived.
 */
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Order } from "../lib/types";
import { DASH, formatCurrency, formatDate } from "../lib/format";

vi.mock("../lib/api", () => ({ downloadInvoice: vi.fn() }));

import { ORDER_COLUMN_OPTIONS, OrderTable } from "./OrderTable";

afterEach(cleanup);

function order(over: Partial<Order> = {}): Order {
  return {
    id: 7,
    order_row_id: 7,
    unit_position: 1,
    units_in_order: 1,
    source: "run",
    account_email: "units@test.com",
    product_link: null,
    qty: 1,
    model: "vivo T5 Lite 44W 5G",
    color: null,
    pin: "226013",
    amount: 18207,
    target: null,
    order_id: "OD100000000000000007",
    gst_name: null,
    status: "placed",
    payment_mode: "cod",
    card_number: null,
    delivery_address: null,
    delivery_pincode: null,
    billed_gstin: null,
    billed_business_name: null,
    placed_at: "2026-10-26T10:00:00",
    created_at: "2026-10-26T10:00:00",
    brand: null,
    category: null,
    job_id: 1,
    delivery_state: "in_transit",
    item_id: null,
    unit_id: null,
    seller: null,
    tracking_id: null,
    platform_status: "Delivery expected by Nov 05",
    delivery_date: "2026-11-05T00:00:00",
    delivery_otp: null,
    invoice_url: null,
    invoice_on_file: false,
    synced_at: "2026-10-27T08:00:00",
    open_request_kinds: [],
    ...over,
  };
}

function table(orders: Order[], columns: string[]) {
  render(
    <OrderTable
      orders={orders}
      loading={false}
      error={null}
      onRetry={() => undefined}
      filtered={false}
      onClearFilter={() => undefined}
      visibleColumns={["order_id", ...columns]}
    />,
  );
  return screen.getByRole("table");
}

/** The text of each data row's cell under the column headed `header`. */
function cells(grid: HTMLElement, header: string): string[] {
  const at = within(grid)
    .getAllByRole("columnheader")
    .findIndex((th) => th.textContent === header);
  expect(at).toBeGreaterThanOrEqual(0);
  return within(grid)
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("cell")[at].textContent ?? "");
}

describe("OrderTable: Promised date", () => {
  it("shows the first promise, and the day Flipkart moved it to beneath", () => {
    const grid = table(
      [
        order({ first_promised_date: "2026-11-01T00:00:00", promised_date: "2026-11-05T00:00:00" }),
        order({ id: 8, order_id: "OD100000000000000008", first_promised_date: "2026-11-01T00:00:00", promised_date: "2026-11-01T00:00:00" }),
        // Read back before the first promise was kept: the one promise known.
        order({ id: 9, order_id: "OD100000000000000009", promised_date: "2026-11-03T00:00:00" }),
        order({ id: 10, order_id: "OD100000000000000010", promised_date: null }),
      ],
      ["promised_date"],
    );

    expect(cells(grid, "Promised date")).toEqual([
      `${formatDate("2026-11-01T00:00:00")}moved to ${formatDate("2026-11-05T00:00:00")}`,
      formatDate("2026-11-01T00:00:00"),
      formatDate("2026-11-03T00:00:00"),
      DASH,
    ]);
  });
});

describe("OrderTable: Delivery date", () => {
  it("says Delivered of a returned unit only when it was delivered first", () => {
    const grid = table(
      [
        // A card order Flipkart cancelled, then refunded: "Refund Completed"
        // reads as returned, and the date kept is the cancellation's.
        order({
          status: "failed",
          payment_mode: "card",
          platform_status: "Refund Completed",
          delivery_state: "returned",
          delivery_date: "2026-10-24T00:00:00",
          promised_date: null,
          delivered_date: null,
          cancelled_by: "flipkart",
          cancel_reason: "Sorry, your order was cancelled as per seller policy restrictions applicable to this brand.",
        }),
        // Delivered, then sent back: its delivery day, not the later day of the return.
        order({
          id: 8,
          order_id: "OD100000000000000008",
          payment_mode: "card",
          platform_status: "Refund Completed",
          delivery_state: "returned",
          delivery_date: "2026-11-06T00:00:00",
          promised_date: "2026-11-01T00:00:00",
          delivered_date: "2026-11-03T00:00:00",
        }),
      ],
      ["delivery_date"],
    );

    const shown = cells(grid, "Delivery date");
    expect(shown[0]).not.toContain("Delivered");
    expect(shown).toEqual([formatDate("2026-10-24T00:00:00"), `${formatDate("2026-11-03T00:00:00")}Delivered`]);
  });

  it("keeps what the date means in every other state", () => {
    const grid = table(
      [
        order({
          delivery_state: "delivered",
          platform_status: "Delivered",
          delivery_date: "2026-11-03T00:00:00",
          delivered_date: "2026-11-03T00:00:00",
        }),
        // Read back before the two dates were kept apart: the one date Flipkart showed.
        order({ id: 8, order_id: "OD100000000000000008", delivery_state: "delivered", platform_status: "Delivered", delivery_date: "2026-11-02T00:00:00" }),
        order({ id: 9, order_id: "OD100000000000000009" }),
        order({ id: 10, order_id: "OD100000000000000010", delivery_state: "unknown", platform_status: "Shipped" }),
        order({ id: 11, order_id: "OD100000000000000011", delivery_state: "cancelled", platform_status: "Cancelled", delivery_date: "2026-10-24T00:00:00" }),
        order({ id: 12, order_id: "OD100000000000000012", delivery_state: "returned", platform_status: "Refund Completed", delivery_date: null, delivered_date: null }),
      ],
      ["delivery_date"],
    );

    expect(cells(grid, "Delivery date")).toEqual([
      `${formatDate("2026-11-03T00:00:00")}Delivered`,
      `${formatDate("2026-11-02T00:00:00")}Delivered`,
      `${formatDate("2026-11-05T00:00:00")}Promised`,
      `${formatDate("2026-11-05T00:00:00")}Promised`,
      formatDate("2026-10-24T00:00:00"),
      "No date shown",
    ]);
  });
});

describe("OrderTable: Order total", () => {
  it("names the order page's total for what it is, under the id saved views know", () => {
    expect(ORDER_COLUMN_OPTIONS.find((column) => column.id === "amount_paid")).toEqual({
      id: "amount_paid",
      label: "Order total",
      defaultOn: false,
    });

    const grid = table([order({ payment_method: "Cash On Delivery", amount_paid: 235 })], ["amount_paid"]);
    expect(within(grid).getByRole("columnheader", { name: "Order total" })).toBeTruthy();
    expect(within(grid).queryByRole("columnheader", { name: "Amount paid" })).toBeNull();
    expect(cells(grid, "Order total")).toEqual([formatCurrency(235)]);
  });
});
