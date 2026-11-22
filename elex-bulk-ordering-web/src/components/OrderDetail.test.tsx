// @vitest-environment jsdom
/**
 * The row dialog rendered for real: the card the order was paid with, in full,
 * and the address and pincode the read-back saw — the subject of an address
 * alert, which no default column of the table shows.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Order, OrderInvoice } from "../lib/types";
import { DASH } from "../lib/format";

const mocks = vi.hoisted(() => ({
  listOrderInvoices: vi.fn(),
  downloadOrderInvoice: vi.fn(),
  startSyncJob: vi.fn(),
  waitForJob: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  downloadInvoice: vi.fn(),
  listOrderInvoices: mocks.listOrderInvoices,
  listOrderRequests: async () => [],
  downloadOrderInvoice: mocks.downloadOrderInvoice,
  startSyncJob: mocks.startSyncJob,
  waitForJob: mocks.waitForJob,
  settleOrder: vi.fn(),
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

import { OrderDetail } from "./OrderDetail";

beforeEach(() => {
  mocks.listOrderInvoices.mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function invoice(over: Partial<OrderInvoice>): OrderInvoice {
  return {
    id: 11,
    kind: "product",
    invoice_type: "Product",
    invoice_number: "LIBFX4S270000245",
    invoice_date: "2026-10-19T00:00:00",
    order_date: "2026-10-18T00:00:00",
    seller_name: "hatman retails private limited",
    seller_gstin: "29AAHCH2846P2ZU",
    buyer_name: "Sk Enterprises",
    buyer_gstin: "09BUAPA6939F1ZP",
    grand_total: 15162,
    original_invoice: null,
    reason: null,
    lines: [],
    source: "upload",
    fetched_at: "2026-10-26T10:00:00",
    size: 1501440,
    parse_error: null,
    download_path: "/orders/7/invoices/11.pdf",
    ...over,
  };
}

describe("OrderDetail invoices", () => {
  it("lists every invoice on file with its number, amount and lines, each downloadable", async () => {
    mocks.listOrderInvoices.mockResolvedValue([
      invoice({
        lines: [
          {
            title: "vivo T5 Lite 44W 5G (Twilight Shadow, 64 GB)",
            fsn: "MOBHQSFF6MZK4YNP",
            hsn_sac: "85171300",
            imei: "864414083556330",
            qty: 1,
            gross: 16912,
            discount: -1750,
            taxable: 12849.15,
            cgst: null,
            sgst: null,
            igst: 2312.85,
            total: 15162,
          },
        ],
      }),
      invoice({
        id: 12,
        kind: "fee",
        invoice_type: "GT Charges",
        invoice_number: "NBABJ27012027019",
        invoice_date: "2026-10-23T00:00:00",
        order_date: null,
        seller_name: "Flipkart India Private Limited",
        seller_gstin: "29AABCF8078M1ZX",
        grand_total: 587,
        size: 147388,
        download_path: "/orders/7/invoices/12.pdf",
      }),
    ]);

    render(<OrderDetail order={order({ invoice_on_file: true, amount: 15162 })} onClose={() => undefined} />);

    await waitFor(() => expect(screen.getByText("LIBFX4S270000245")).toBeTruthy());
    expect(mocks.listOrderInvoices).toHaveBeenCalledWith(7);
    expect(screen.getByText("GT Charges")).toBeTruthy();
    expect(screen.getByText(/IMEI 864414083556330/)).toBeTruthy();
    expect(screen.getByText(/2 invoices on file/)).toBeTruthy();
    expect(screen.getByText(/matches the order amount/)).toBeTruthy();
    const buttons = screen.getAllByRole("button", { name: /Download invoice/ });
    expect(buttons).toHaveLength(2);
    fireEvent.click(buttons[1]);
    expect(mocks.downloadOrderInvoice).toHaveBeenCalledWith(7, 12, "OD100000000000000007-NBABJ27012027019.pdf");
  });

  it("shows the order page's timeline, dated steps first-class and the rest greyed as not yet", () => {
    render(
      <OrderDetail
        order={order({
          synced_at: "2026-10-26T10:00:00",
          timeline: [
            { status: "Order Confirmed", date: "2026-10-18T00:00:00" },
            { status: "Shipped", date: "2026-10-20T00:00:00" },
            { status: "Delivered", date: null },
          ],
        })}
        onClose={() => undefined}
      />,
    );
    expect(screen.getByText(/Order Confirmed · /)).toBeTruthy();
    expect(screen.getByText(/Shipped · /)).toBeTruthy();
    expect(screen.getByText("Delivered · not yet")).toBeTruthy();
  });

  it("shows a kept reading's note as a remark, and an unread PDF's as an error", async () => {
    mocks.listOrderInvoices.mockResolvedValue([
      invoice({ parse_error: "took too long to read; the earlier reading is kept" }),
      invoice({ id: 12, invoice_type: null, invoice_number: null, parse_error: "the reader found no invoice on it" }),
    ]);
    render(<OrderDetail order={order({ invoice_on_file: true })} onClose={() => undefined} />);

    const kept = await screen.findByText("took too long to read; the earlier reading is kept");
    expect(kept.className).not.toMatch(/text-critical/);
    expect(screen.getAllByText("Product").length).toBeGreaterThan(0);
    expect(screen.getByText("the reader found no invoice on it").className).toMatch(/text-critical/);
    expect(screen.getByText("Could not read this PDF")).toBeTruthy();
  });

  it("says why an invoice could not be downloaded", async () => {
    mocks.listOrderInvoices.mockResolvedValue([invoice({})]);
    mocks.downloadOrderInvoice.mockRejectedValue(new Error("404 the PDF is not in the store"));
    render(<OrderDetail order={order({ invoice_on_file: true })} onClose={() => undefined} />);

    fireEvent.click(await screen.findByRole("button", { name: /Download invoice/ }));

    expect(await screen.findByText("Could not download the invoice")).toBeTruthy();
    expect(screen.getByText("404 the PDF is not in the store")).toBeTruthy();
  });

  it("shows the IMEI, the name billed to and the last tracking update", () => {
    render(
      <OrderDetail
        order={order({
          imei: "358107262645113",
          imei_scope: "position",
          billing_name: "Sk Enterprises",
          last_tracking_update: "2026-10-27T00:00:00",
          last_tracking_step: "Shipped",
        })}
        onClose={() => undefined}
      />,
    );
    expect(screen.getByText("358107262645113")).toBeTruthy();
    expect(screen.getByText(/Matched by position/)).toBeTruthy();
    expect(screen.getByText("Sk Enterprises")).toBeTruthy();
    expect(screen.getByText(/· Shipped/)).toBeTruthy();
  });

  it("says when no invoice on file gives an IMEI", () => {
    render(<OrderDetail order={order({})} onClose={() => undefined} />);
    expect(screen.getByText("Not on an invoice on file")).toBeTruthy();
  });

  it("says when nothing is on file, without asking the server", () => {
    render(<OrderDetail order={order({ invoice_on_file: false })} onClose={() => undefined} />);
    expect(screen.getByText("None on file")).toBeTruthy();
    expect(mocks.listOrderInvoices).not.toHaveBeenCalled();
  });
});

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
    color: "Glossy Black",
    pin: "110001",
    amount: 15000,
    target: null,
    order_id: "OD100000000000000007",
    gst_name: null,
    status: "placed",
    payment_mode: "card",
    card_number: "4111 1111 1111 1111",
    delivery_address: "Home · 12 Park Street, Kolkata · Ayush · 98xxxx1234",
    delivery_pincode: "700016",
    billed_gstin: null,
    billed_business_name: null,
    placed_at: "2026-10-24T10:00:00",
    created_at: "2026-10-24T10:00:00",
    brand: null,
    category: null,
    job_id: 1,
    delivery_state: "in_transit",
    item_id: null,
    unit_id: null,
    seller: null,
    tracking_id: null,
    platform_status: "Shipped",
    delivery_date: null,
    delivery_otp: null,
    invoice_url: null,
    invoice_on_file: false,
    synced_at: "2026-10-25T08:00:00",
    open_request_kinds: [],
    ...over,
  };
}

/** The dialog's label/value pair under `label`. */
function row(label: string): HTMLElement {
  return screen.getByText(label).closest("div") as HTMLElement;
}

describe("OrderDetail", () => {
  it("shows the full card number and the address and pincode the read-back saw", () => {
    render(<OrderDetail order={order()} onClose={vi.fn()} />);

    // In full, as the owner decided: no masking of the first twelve digits.
    expect(row("Card number").textContent).toBe("Card number4111 1111 1111 1111");
    expect(within(row("Delivery address")).getByText("Home · 12 Park Street, Kolkata · Ayush · 98xxxx1234")).toBeTruthy();
    expect(within(row("Delivery pincode")).getByText("700016")).toBeTruthy();
  });

  it("says COD for a cash-on-delivery order and dashes the address until a read-back records one", () => {
    render(
      <OrderDetail
        order={order({ payment_mode: "cod", card_number: null, delivery_address: null, delivery_pincode: null, synced_at: null })}
        onClose={vi.fn()}
      />,
    );

    within(row("Card number")).getByText("COD");
    within(row("Delivery address")).getByText(DASH);
    within(row("Delivery pincode")).getByText(DASH);
  });

  it("dashes the card number when a card order has none recorded", () => {
    // A row read back or uploaded was never paid for by this tool, so no card is known.
    render(<OrderDetail order={order({ source: "sync", card_number: null })} onClose={vi.fn()} />);

    within(row("Card number")).getByText(DASH);
  });

  it("shows the GST the order page says the order was billed under, beside the one the run chose", () => {
    // The subject of a GST-mismatch alert (the owner's addition of 26 Oct 2026).
    render(
      <OrderDetail
        order={order({ gst_name: "Acme", billed_gstin: "09AAUFT2204P1ZI", billed_business_name: "TRANSPARENT DEALS" })}
        onClose={vi.fn()}
      />,
    );
    expect(row("Billed GST").textContent).toBe("Billed GST09AAUFT2204P1ZI · TRANSPARENT DEALS");
    expect(within(row("Business (GST)")).getByText("Acme")).toBeTruthy();

    cleanup();
    render(<OrderDetail order={order()} onClose={vi.fn()} />);
    within(row("Billed GST")).getByText(DASH);
  });
});

describe("OrderDetail out for delivery", () => {
  it("puts the delivery code first when Flipkart says the unit is out for delivery", () => {
    render(<OrderDetail order={order({ platform_status: "Out For Delivery", delivery_otp: "4827" })} onClose={vi.fn()} />);

    expect(screen.getByText("Out for delivery")).toBeTruthy();
    expect(screen.getAllByText("4827").length).toBeGreaterThan(0);
    expect(screen.getByText(/Give it to the courier only after checking the package/)).toBeTruthy();
  });

  it("says the code is not shown yet when Flipkart has not shown one", () => {
    render(<OrderDetail order={order({ platform_status: "Out for delivery", delivery_otp: null })} onClose={vi.fn()} />);

    expect(screen.getByText(/has not shown a delivery code on this order's page yet/)).toBeTruthy();
  });

  it("shows no notice for an order merely on its way", () => {
    render(<OrderDetail order={order({ platform_status: "Shipped" })} onClose={vi.fn()} />);

    expect(screen.queryByText(/delivery code on this order's page/)).toBeNull();
    expect(screen.queryByText(/Give it to the courier/)).toBeNull();
  });

  it("ignores a timeline an older server stored as text instead of a list of steps", () => {
    render(
      <OrderDetail
        order={order({ timeline: "[{'status': 'Order Confirmed', 'date': None}]" as never })}
        onClose={vi.fn()}
      />,
    );

    expect(screen.queryAllByText(/not yet$/)).toHaveLength(0);
    expect(screen.getByText("No steps on the order page")).toBeTruthy();
  });

  it("shows undated steps plainly when the page dates none of them", () => {
    render(
      <OrderDetail
        order={order({
          timeline: [
            { status: "Order Confirmed", date: null },
            { status: "Shipped", date: null },
          ],
        })}
        onClose={vi.fn()}
      />,
    );

    const timeline = row("Timeline");
    expect(within(timeline).getByText("Order Confirmed")).toBeTruthy();
    expect(within(timeline).getByText("Shipped")).toBeTruthy();
    expect(within(timeline).queryAllByText(/not yet$/)).toHaveLength(0);
    // ...and says they are not the order's state: the status line is.
    expect(within(timeline).getByText(/without dates, so they don't show which have happened/)).toBeTruthy();
  });

  it("says nothing of undated steps when the page dates the ones that happened", () => {
    render(
      <OrderDetail
        order={order({
          timeline: [
            { status: "Order Confirmed", date: "2026-10-20T00:00:00" },
            { status: "Delivered", date: null },
          ],
        })}
        onClose={vi.fn()}
      />,
    );

    expect(screen.queryByText(/without dates/)).toBeNull();
  });

  it("says the account was not read back when the run passed it over", async () => {
    mocks.startSyncJob.mockResolvedValue({ id: 32 });
    mocks.waitForJob.mockResolvedValue({
      id: 32,
      status: "completed",
      items: [{ account_id: 2, status: "skipped", message: "order run #24 is using this account — read back after that run" }],
    });
    render(<OrderDetail order={order({ account_id: 2 })} onClose={vi.fn()} onRefreshed={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: "Read back now" }));

    expect(await screen.findByText(/Not read back: order run #24 is using this account/)).toBeTruthy();
    expect(screen.queryByText(/Read back from Flipkart/)).toBeNull();
  });

  it("reads the order's account back from Flipkart when asked, then reloads", async () => {
    mocks.startSyncJob.mockResolvedValue({ id: 31 });
    mocks.waitForJob.mockResolvedValue({
      id: 31,
      status: "completed",
      items: [{ account_id: 2, message: "5 order(s) seen, 2 matched" }],
    });
    const onRefreshed = vi.fn();
    render(<OrderDetail order={order({ account_id: 2 })} onClose={vi.fn()} onRefreshed={onRefreshed} />);

    fireEvent.click(screen.getByRole("button", { name: "Read back now" }));

    await waitFor(() => expect(onRefreshed).toHaveBeenCalled());
    expect(mocks.startSyncJob).toHaveBeenCalledWith({ account_ids: [2] });
    expect(await screen.findByText(/Read back from Flipkart: 5 order\(s\) seen, 2 matched/)).toBeTruthy();
  });
});
