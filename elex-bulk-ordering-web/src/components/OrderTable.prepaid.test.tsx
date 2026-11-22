// @vitest-environment jsdom
/**
 * The ledger's COD column for an order report's "Cash on Delivery: No",
 * kept as "prepaid" (27 Oct 2026): it reads No, as the CSV and the row dialog
 * read it, unless the unit's own page says it is paid at the door.
 */
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Order } from "../lib/types";
import { DASH } from "../lib/format";

vi.mock("../lib/api", () => ({ downloadInvoice: vi.fn() }));

import { OrderTable } from "./OrderTable";

afterEach(cleanup);


/** A unit an order report filed: never read back, its values the report's. Made-up values. */
function filed(over: Partial<Order> = {}): Order {
  return {
    id: 21,
    order_row_id: 21,
    unit_position: 1,
    units_in_order: 1,
    source: "upload",
    account_email: "buyer1@example.com",
    product_link: null,
    qty: 1,
    model: "vivo T5 Lite 44W 5G",
    color: null,
    pin: "226013",
    amount: 18207,
    target: null,
    order_id: "OD338967210239377021",
    gst_name: null,
    status: "placed",
    payment_mode: "unknown",
    card_number: null,
    delivery_address: null,
    delivery_pincode: null,
    billed_gstin: null,
    billed_business_name: null,
    placed_at: "2026-10-24T04:40:15",
    created_at: "2026-10-27T05:00:00",
    brand: null,
    category: null,
    job_id: null,
    delivery_state: "unknown",
    item_id: null,
    unit_id: "439123456789012377021",
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

describe("OrderTable: the COD column", () => {
  it("reads a report's prepaid order as No, and Yes only when its page says it is paid at the door", () => {
    const grid = table(
      [
        filed({ payment_mode: "prepaid" }),
        filed({ id: 22, order_row_id: 22, order_id: "OD338967210239377022", unit_id: "439123456789012377022", payment_mode: "prepaid", payment_method: "Cash On Delivery" }),
        filed({ id: 23, order_row_id: 23, order_id: "OD338967210239377023", unit_id: "439123456789012377023", payment_mode: "cod" }),
        filed({ id: 24, order_row_id: 24, order_id: "OD338967210239377024", unit_id: "439123456789012377024", payment_mode: "card" }),
        filed({ id: 25, order_row_id: 25, order_id: "OD338967210239377025", unit_id: "439123456789012377025", payment_mode: "unknown" }),
      ],
      ["payment_mode"],
    );
    expect(cells(grid, "COD")).toEqual(["No", "Yes", "Yes", "No", DASH]);
  });
});
