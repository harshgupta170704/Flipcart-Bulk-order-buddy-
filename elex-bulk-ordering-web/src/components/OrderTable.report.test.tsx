// @vitest-environment jsdom
/**
 * What an order report filed shows in the ledger table (review of 27 Oct
 * 2026, surfaces). A report fills a unit's status, dates, tracking id, seller
 * and address without a read-back ever seeing the unit, so its `synced_at`
 * stays empty on purpose — and the table printed "Not read back" in every one
 * of those cells, while the row's dialog, the CSV and the Order Details API
 * showed the values (filtering by Delivered listed rows whose Status said
 * "Not read back"). A cell now shows whatever is stored, and says "Not read
 * back" only of a field nothing has filled on a unit never read back.
 */
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Order } from "../lib/types";
import { DASH, formatDate } from "../lib/format";

vi.mock("../lib/api", () => ({ downloadInvoice: vi.fn() }));

import { OrderTable } from "./OrderTable";

afterEach(cleanup);

const NOT_READ_BACK = "Not read back";

/** The columns a read-back fills, each of which used to wait for `synced_at`. */
const READ_BACK_COLUMNS = [
  "platform_status",
  "delivery_otp",
  "delivery_date",
  "promised_date",
  "delivered_date",
  "payment_method",
  "tracking_id",
  "delivery_address",
  "billed_gst",
  "seller",
  "invoice",
];

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

describe("OrderTable: units an order report filed", () => {
  it("shows the report's status, tracking id, seller, address and dates though Flipkart was never read", () => {
    const grid = table(
      [
        // On its way: the report gives a promise, never the read-back's one date.
        filed({
          platform_status: "Shipped",
          delivery_state: "in_transit",
          tracking_id: "FMPP1234567890",
          seller: "Example Retail Pvt Ltd",
          delivery_address: "Home, Tower 5, Garden Bay, Lucknow, Uttar Pradesh, 226013",
          promised_date: "2026-11-05T00:00:00",
          first_promised_date: "2026-11-05T00:00:00",
        }),
        filed({
          id: 22,
          order_row_id: 22,
          order_id: "OD338967210239377022",
          unit_id: "439123456789012377022",
          platform_status: "Delivered",
          delivery_state: "delivered",
          tracking_id: "FMPP1234567891",
          seller: "Example Retail Pvt Ltd",
          delivery_address: "Office, Plot 9, Gomti Nagar, Lucknow, Uttar Pradesh, 226010",
          promised_date: "2026-11-01T00:00:00",
          first_promised_date: "2026-11-01T00:00:00",
          delivered_date: "2026-11-03T00:00:00",
        }),
      ],
      ["platform_status", "tracking_id", "seller", "delivery_address", "delivery_date", "promised_date", "delivered_date"],
    );

    expect(cells(grid, "Status")).toEqual(["Shipped", "Delivered"].map((label) => expect.stringContaining(label)));
    expect(cells(grid, "Tracking ID")).toEqual(["FMPP1234567890", "FMPP1234567891"]);
    expect(cells(grid, "Seller")).toEqual(["Example Retail Pvt Ltd", "Example Retail Pvt Ltd"]);
    expect(cells(grid, "Delivery address")).toEqual([
      "Home, Tower 5, Garden Bay, Lucknow, Uttar Pradesh, 226013",
      "Office, Plot 9, Gomti Nagar, Lucknow, Uttar Pradesh, 226010",
    ]);
    expect(cells(grid, "Delivery date")).toEqual([
      `${formatDate("2026-11-05T00:00:00")}Promised`,
      `${formatDate("2026-11-03T00:00:00")}Delivered`,
    ]);
    expect(cells(grid, "Promised date")).toEqual([formatDate("2026-11-05T00:00:00"), formatDate("2026-11-01T00:00:00")]);
    // A unit on its way has no delivery day; it was never read back, so nothing says there is none.
    expect(cells(grid, "Delivered date")).toEqual([NOT_READ_BACK, formatDate("2026-11-03T00:00:00")]);
    expect(within(grid).getAllByText(NOT_READ_BACK)).toHaveLength(1);
  });

  it("shows any stored value in every column a read-back fills, whoever stored it", () => {
    const grid = table(
      [
        filed({
          platform_status: "Out For Delivery",
          delivery_state: "in_transit",
          delivery_otp: "0123",
          delivery_date: "2026-11-05T00:00:00",
          promised_date: "2026-11-05T00:00:00",
          delivered_date: null,
          payment_method: "Cash On Delivery",
          tracking_id: "FMPP1234567890",
          delivery_address: "Home, Tower 5, Lucknow, 226013",
          billed_gstin: "09ABCDE1234F1Z5",
          billed_business_name: "Example Trading Pvt Ltd",
          seller: "Example Retail Pvt Ltd",
          invoice_url: "https://www.flipkart.com/invoice/OD338967210239377021",
        }),
      ],
      READ_BACK_COLUMNS,
    );

    const [row] = within(grid).getAllByRole("row").slice(1);
    // The one empty field is the delivery day of a unit still on its way.
    expect(cells(grid, "Delivered date")).toEqual([NOT_READ_BACK]);
    expect(within(row).getAllByText(NOT_READ_BACK)).toHaveLength(1);
    expect(cells(grid, "Delivery code")).toEqual(["0123"]);
    expect(cells(grid, "Paid by")).toEqual(["Cash On Delivery"]);
    expect(cells(grid, "Billed GST")).toEqual(["09ABCDE1234F1Z5 · Example Trading Pvt Ltd"]);
    expect(cells(grid, "Invoice")).toEqual(["Not fetched yet"]);
  });

  it("still says Not read back of a field nothing has filled, and what an empty field means after a read-back", () => {
    const grid = table(
      [
        filed(),
        filed({
          id: 23,
          order_row_id: 23,
          order_id: "OD338967210239377023",
          unit_id: "439123456789012377023",
          source: "sync",
          synced_at: "2026-10-27T08:00:00",
        }),
      ],
      READ_BACK_COLUMNS,
    );

    const [never, read] = within(grid).getAllByRole("row").slice(1);
    // Never read back and nothing filed: every one of those columns says so.
    expect(within(never).getAllByText(NOT_READ_BACK)).toHaveLength(READ_BACK_COLUMNS.length);
    // Read back, and Flipkart's page showed none of them.
    expect(within(read).queryByText(NOT_READ_BACK)).toBeNull();
    expect(cells(grid, "Status")[1]).toBe("Not shown by Flipkart");
    expect(cells(grid, "Delivery date")[1]).toBe("No date shown");
    expect(cells(grid, "Invoice")[1]).toBe("None yet");
    expect(cells(grid, "Tracking ID")[1]).toBe(DASH);
  });
});
