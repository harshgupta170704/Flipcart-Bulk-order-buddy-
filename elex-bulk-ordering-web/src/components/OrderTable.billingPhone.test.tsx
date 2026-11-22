// @vitest-environment jsdom
/**
 * The ledger's Billing Phone column: the phone each order was placed with,
 * offered in the column picker (and so in a saved view) and off until chosen,
 * like the Business GST beside it.
 */
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Order } from "../lib/types";
import { DASH } from "../lib/format";

vi.mock("../lib/api", () => ({ downloadInvoice: vi.fn() }));

import { DEFAULT_ORDER_COLUMNS, ORDER_COLUMN_OPTIONS, OrderTable } from "./OrderTable";

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
    model: "vivo T5x 5G",
    color: null,
    pin: "110001",
    amount: 15000,
    target: null,
    order_id: "OD100000000000000007",
    gst_name: "Digidrive LLP",
    billing_phone: "9811111111",
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
    delivery_state: "unknown",
    item_id: null,
    unit_id: null,
    seller: null,
    tracking_id: null,
    platform_status: null,
    delivery_date: null,
    delivery_otp: null,
    invoice_url: null,
    invoice_on_file: false,
    synced_at: null,
    open_request_kinds: [],
    ...over,
  };
}

function table(orders: Order[]) {
  render(
    <OrderTable
      orders={orders}
      loading={false}
      error={null}
      onRetry={() => undefined}
      filtered={false}
      onClearFilter={() => undefined}
      visibleColumns={["order_id", "billing_phone"]}
    />,
  );
  return screen.getByRole("table");
}

describe("OrderTable: Billing Phone", () => {
  it("is a column the picker offers, off until chosen", () => {
    expect(ORDER_COLUMN_OPTIONS.find((column) => column.id === "billing_phone")).toEqual({
      id: "billing_phone",
      label: "Billing Phone",
      defaultOn: false,
    });
    expect(DEFAULT_ORDER_COLUMNS).not.toContain("billing_phone");
  });

  it("shows each order's phone, and a dash for a row that holds none", () => {
    const grid = table([order(), order({ id: 8, order_id: "OD100000000000000008", billing_phone: null })]);

    expect(within(grid).getByRole("columnheader", { name: "Billing Phone" })).toBeTruthy();
    const rows = within(grid).getAllByRole("row").slice(1);
    expect(within(rows[0]).getByText("9811111111")).toBeTruthy();
    expect(within(rows[1]).getByText(DASH)).toBeTruthy();
  });
});
