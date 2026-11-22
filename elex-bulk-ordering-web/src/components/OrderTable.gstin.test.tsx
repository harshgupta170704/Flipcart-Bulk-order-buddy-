// @vitest-environment jsdom
/**
 * The ledger's Business GST No column: the GSTIN each order was billed under,
 * offered in the column picker (and so in a saved view) and off until chosen,
 * beside the Business GST name column (spec FR-013, 27 Oct 2026).
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
    pin: "226013",
    amount: 15000,
    target: null,
    order_id: "OD100000000000000007",
    gst_name: "EXAMPLE TRADING",
    gstin: "09AAAAA0000A1Z5",
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

describe("OrderTable: Business GST No", () => {
  it("is a column the picker offers right after the business name, off until chosen", () => {
    const ids = ORDER_COLUMN_OPTIONS.map((column) => column.id);
    expect(ORDER_COLUMN_OPTIONS.find((column) => column.id === "gstin")).toEqual({
      id: "gstin",
      label: "Business GST No",
      defaultOn: false,
    });
    expect(ids.indexOf("gstin")).toBe(ids.indexOf("gst_name") + 1);
    expect(DEFAULT_ORDER_COLUMNS).not.toContain("gstin");
  });

  it("shows each order's GSTIN, and a dash for a row that holds none", () => {
    render(
      <OrderTable
        orders={[order(), order({ id: 8, order_id: "OD100000000000000008", gst_name: null, gstin: null })]}
        loading={false}
        error={null}
        onRetry={() => undefined}
        filtered={false}
        onClearFilter={() => undefined}
        visibleColumns={["order_id", "gst_name", "gstin"]}
      />,
    );
    const grid = screen.getByRole("table");

    expect(within(grid).getByRole("columnheader", { name: "Business GST No" })).toBeTruthy();
    const rows = within(grid).getAllByRole("row").slice(1);
    expect(within(rows[0]).getByText("09AAAAA0000A1Z5")).toBeTruthy();
    expect(within(rows[0]).getByText("EXAMPLE TRADING")).toBeTruthy();
    expect(within(rows[1]).getAllByText(DASH)).toHaveLength(2);
  });
});
