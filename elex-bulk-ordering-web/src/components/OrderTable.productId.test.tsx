// @vitest-environment jsdom
/**
 * The ledger's Product ID column (Smart ID, part 2): the exact variant's
 * Flipkart id each order recorded from its product link, offered in the
 * column picker (and so in a saved view) and off until chosen.
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
    product_link: "https://www.flipkart.com/vivo-t5x-5g/p/itm1?pid=MOBHNDTD57UJUK9Q",
    qty: 1,
    model: "vivo T5x 5G",
    color: null,
    pin: "226013",
    amount: 15000,
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
    placed_at: "2026-11-24T10:00:00",
    created_at: "2026-11-24T10:00:00",
    brand: "vivo",
    category: "Mobiles",
    product_id: "MOBHNDTD57UJUK9Q",
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

describe("OrderTable: Product ID", () => {
  it("is a column the picker offers after the category, off until chosen", () => {
    const ids = ORDER_COLUMN_OPTIONS.map((column) => column.id);
    expect(ORDER_COLUMN_OPTIONS.find((column) => column.id === "product_id")).toEqual({
      id: "product_id",
      label: "Product ID",
      defaultOn: false,
    });
    expect(ids.indexOf("product_id")).toBe(ids.indexOf("category") + 1);
    expect(DEFAULT_ORDER_COLUMNS).not.toContain("product_id");
  });

  it("shows each order's product id, and a dash for one whose link named none", () => {
    render(
      <OrderTable
        orders={[order(), order({ id: 8, order_id: "OD100000000000000008", product_id: null })]}
        loading={false}
        error={null}
        onRetry={() => undefined}
        filtered={false}
        onClearFilter={() => undefined}
        visibleColumns={["order_id", "product_id"]}
      />,
    );
    const grid = screen.getByRole("table");

    expect(within(grid).getByRole("columnheader", { name: "Product ID" })).toBeTruthy();
    const rows = within(grid).getAllByRole("row").slice(1);
    expect(within(rows[0]).getByText("MOBHNDTD57UJUK9Q")).toBeTruthy();
    expect(within(rows[1]).getByText(DASH)).toBeTruthy();
  });
});
