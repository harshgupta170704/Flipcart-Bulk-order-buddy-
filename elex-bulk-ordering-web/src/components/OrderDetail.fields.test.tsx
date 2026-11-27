// @vitest-environment jsdom
/**
 * Every stored field of a unit in the row dialog (spec FR-013, 27 Oct 2026):
 * the GSTIN the run billed under beside its business name, the billing phone,
 * the unit's platform, when its delivery address changed, Flipkart's invoice
 * link while no PDF is on file, and the unit's own cancel and reschedule
 * requests — read once when the dialog opens, never with the ledger's rows.
 */
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DASH, formatDate, formatDateTime } from "../lib/format";
import type { Order, UnitRequest } from "../lib/types";

const mocks = vi.hoisted(() => ({ listOrderInvoices: vi.fn(), listOrderRequests: vi.fn() }));

vi.mock("../lib/api", () => ({
  downloadInvoice: vi.fn(),
  listOrderInvoices: mocks.listOrderInvoices,
  listOrderRequests: mocks.listOrderRequests,
  downloadOrderInvoice: vi.fn(),
  startSyncJob: vi.fn(),
  waitForJob: vi.fn(),
  settleOrder: vi.fn(),
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

import { OrderDetail } from "./OrderDetail";

beforeEach(() => {
  mocks.listOrderInvoices.mockResolvedValue([]);
  mocks.listOrderRequests.mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

/** The second unit (row 8) of a two-unit order (row 5). */
function order(over: Partial<Order> = {}): Order {
  return {
    id: 8,
    order_row_id: 5,
    unit_position: 2,
    units_in_order: 2,
    source: "run",
    platform: "flipkart",
    account_email: "units@test.com",
    product_link: null,
    qty: 2,
    model: "vivo T5x 5G",
    color: "Glossy Black",
    pin: "226013",
    amount: 30000,
    target: null,
    order_id: "OD100000000000000011",
    gst_name: "EXAMPLE TRADING",
    gstin: "09AAAAA0000A1Z5",
    billing_phone: "+91 98765 43210",
    status: "placed",
    payment_mode: "cod",
    card_number: null,
    delivery_address: "Home · 12 Park Street, Kolkata · 98xxxx1234",
    delivery_pincode: "700016",
    address_changed_at: null,
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

function request(over: Partial<UnitRequest>): UnitRequest {
  return {
    id: 1,
    unit_pk: 8,
    kind: "cancel",
    reason: null,
    requested_for: null,
    status: "open",
    requested_by: "ops@test.com",
    requested_at: "2026-10-26T10:00:00",
    closed_by: null,
    closed_at: null,
    note: null,
    order_row_id: 5,
    order_id: "OD100000000000000011",
    unit_id: null,
    product: "vivo T5x 5G",
    platform_status: "Shipped",
    delivery_state: "in_transit",
    account_email: "units@test.com",
    ...over,
  };
}

/** The dialog's label/value pair under `label`. */
function row(label: string): HTMLElement {
  return screen.getByText(label).closest("div") as HTMLElement;
}

describe("OrderDetail: the unit's stored fields", () => {
  it("shows the variant's product id, and a dash for an order whose link named none", () => {
    render(<OrderDetail order={order({ product_id: "MOBHNDTD57UJUK9Q" })} onClose={vi.fn()} />);
    expect(row("Product ID").textContent).toBe("Product IDMOBHNDTD57UJUK9Q");
    const labels = screen.getAllByRole("term").map((term) => term.textContent);
    expect(labels.indexOf("Product ID")).toBe(labels.indexOf("Category") + 1);

    cleanup();
    render(<OrderDetail order={order({ product_id: null })} onClose={vi.fn()} />);
    within(row("Product ID")).getByText(DASH);
  });

  it("shows the GSTIN the run billed under next to the business name", () => {
    render(<OrderDetail order={order()} onClose={vi.fn()} />);

    expect(row("Business GST No").textContent).toBe("Business GST No09AAAAA0000A1Z5");
    expect(within(row("Business (GST)")).getByText("EXAMPLE TRADING")).toBeTruthy();
    // Business name, then its GSTIN, one under the other.
    const labels = screen.getAllByRole("term").map((term) => term.textContent);
    expect(labels.indexOf("Business GST No")).toBe(labels.indexOf("Business (GST)") + 1);

    cleanup();
    render(<OrderDetail order={order({ gst_name: null, gstin: null })} onClose={vi.fn()} />);
    within(row("Business GST No")).getByText(DASH);
  });

  it("shows the phone the order was placed with", () => {
    render(<OrderDetail order={order()} onClose={vi.fn()} />);
    expect(row("Billing phone").textContent).toBe("Billing phone+91 98765 43210");

    cleanup();
    // A row read back or handed in by id holds none.
    render(<OrderDetail order={order({ source: "sync", billing_phone: null })} onClose={vi.fn()} />);
    within(row("Billing phone")).getByText(DASH);
  });

  it("names the platform the unit was bought on", () => {
    render(<OrderDetail order={order()} onClose={vi.fn()} />);
    expect(row("Platform").textContent).toBe("PlatformFlipkart");

    cleanup();
    // An older server sends none: a dash, not a guess.
    render(<OrderDetail order={order({ platform: undefined })} onClose={vi.fn()} />);
    within(row("Platform")).getByText(DASH);
  });

  it("says when a read-back saw the delivery address change, under the address", () => {
    render(<OrderDetail order={order({ address_changed_at: "2026-10-26T07:30:00" })} onClose={vi.fn()} />);

    const address = row("Delivery address");
    expect(within(address).getByText("Home · 12 Park Street, Kolkata · 98xxxx1234")).toBeTruthy();
    expect(within(address).getByText(`Delivery address changed ${formatDateTime("2026-10-26T07:30:00")}`)).toBeTruthy();

    cleanup();
    render(<OrderDetail order={order()} onClose={vi.fn()} />);
    expect(screen.queryByText(/Delivery address changed/)).toBeNull();
  });
});

describe("OrderDetail: Flipkart's invoice link", () => {
  it("is a link while no PDF is on file", () => {
    const url = "https://www.flipkart.com/api/invoice/OD100000000000000011?token=made-up";
    render(<OrderDetail order={order({ invoice_url: url })} onClose={vi.fn()} />);

    const invoices = row("Invoices");
    expect(within(invoices).getByText("Seen on Flipkart — fetched at the next read-back")).toBeTruthy();
    const link = within(invoices).getByRole("link", { name: url });
    expect(link.getAttribute("href")).toBe(url);
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
  });

  it("is shown as text, never a link, when it is not a web address", () => {
    const url = "javascript:alert(1)";
    render(<OrderDetail order={order({ invoice_url: url })} onClose={vi.fn()} />);

    const invoices = row("Invoices");
    expect(within(invoices).getByText(url)).toBeTruthy();
    expect(within(invoices).queryByRole("link")).toBeNull();
  });

  it("gives way to the PDFs once they are on file", async () => {
    mocks.listOrderInvoices.mockResolvedValue([
      {
        id: 11,
        kind: "product",
        invoice_type: "Product",
        invoice_number: "LIBFX4S270000245",
        invoice_date: "2026-10-19T00:00:00",
        order_date: null,
        seller_name: null,
        seller_gstin: null,
        buyer_name: null,
        buyer_gstin: null,
        grand_total: 30000,
        original_invoice: null,
        reason: null,
        lines: [],
        source: "sync",
        fetched_at: "2026-10-26T10:00:00",
        size: 1000,
        parse_error: null,
        download_path: "/orders/5/invoices/11.pdf",
      },
    ]);
    render(
      <OrderDetail
        order={order({ invoice_on_file: true, invoice_url: "https://www.flipkart.com/api/invoice/x" })}
        onClose={vi.fn()}
      />,
    );

    expect(await screen.findByText("LIBFX4S270000245")).toBeTruthy();
    expect(within(row("Invoices")).queryByRole("link")).toBeNull();
  });
});

describe("OrderDetail: the unit's requests", () => {
  it("lists this unit's requests, newest first: kind, status, when and by whom", async () => {
    mocks.listOrderRequests.mockResolvedValue([
      request({ id: 3, kind: "cancel", reason: "Ordered twice by mistake", requested_at: "2026-10-26T10:00:00" }),
      // The order's other unit: not this row's.
      request({ id: 2, unit_pk: 9, kind: "cancel", reason: "Wrong colour" }),
      request({
        id: 1,
        kind: "reschedule",
        requested_for: "2026-11-02",
        status: "done",
        requested_by: "tester",
        requested_at: "2026-10-25T10:00:00",
        closed_by: "ops@test.com",
        closed_at: "2026-10-25T18:00:00",
        note: "Moved on the app",
      }),
    ]);
    render(<OrderDetail order={order()} onClose={vi.fn()} />);

    const requests = row("Requests");
    await waitFor(() => expect(within(requests).getAllByRole("listitem")).toHaveLength(2));
    // One call, for the order this unit belongs to.
    expect(mocks.listOrderRequests).toHaveBeenCalledTimes(1);
    expect(mocks.listOrderRequests).toHaveBeenCalledWith(5);

    const [cancel, reschedule] = within(requests).getAllByRole("listitem");
    expect(within(cancel).getByText("Cancel")).toBeTruthy();
    expect(within(cancel).getByText("Open")).toBeTruthy();
    expect(within(cancel).getByText("Ordered twice by mistake")).toBeTruthy();
    expect(within(cancel).getByText(`Requested by ops@test.com on ${formatDateTime("2026-10-26T10:00:00")}`)).toBeTruthy();
    expect(within(cancel).queryByText(/Marked done|Dropped/)).toBeNull();

    expect(within(reschedule).getByText("Reschedule")).toBeTruthy();
    expect(within(reschedule).getByText("Done")).toBeTruthy();
    expect(within(reschedule).getByText(`New date: ${formatDate("2026-11-02")}`)).toBeTruthy();
    expect(within(reschedule).getByText(`Requested by tester on ${formatDateTime("2026-10-25T10:00:00")}`)).toBeTruthy();
    expect(
      within(reschedule).getByText(`Marked done by ops@test.com on ${formatDateTime("2026-10-25T18:00:00")}`),
    ).toBeTruthy();
    expect(within(reschedule).getByText("Note: “Moved on the app”")).toBeTruthy();

    expect(within(requests).queryByText("Wrong colour")).toBeNull();
  });

  it("says so when the unit has none", async () => {
    mocks.listOrderRequests.mockResolvedValue([request({ unit_pk: 9, reason: "Another unit's" })]);
    render(<OrderDetail order={order()} onClose={vi.fn()} />);

    expect(await within(row("Requests")).findByText("No cancel or reschedule request")).toBeTruthy();
  });

  it("says a dropped request was dropped, by whom and when", async () => {
    mocks.listOrderRequests.mockResolvedValue([
      request({ status: "dropped", reason: "Not needed", closed_by: "ops@test.com", closed_at: "2026-10-26T12:00:00" }),
    ]);
    render(<OrderDetail order={order()} onClose={vi.fn()} />);

    const requests = row("Requests");
    expect(await within(requests).findByText(`Dropped by ops@test.com on ${formatDateTime("2026-10-26T12:00:00")}`)).toBeTruthy();
    // The badge says it too.
    expect(within(requests).getByText("Dropped")).toBeTruthy();
  });

  it("keeps every step of a request, the first close included (spec C-4)", async () => {
    mocks.listOrderRequests.mockResolvedValue([
      request({
        status: "dropped",
        closed_by: "bob@test.com",
        closed_at: "2026-10-26T12:00:00",
        history: [
          { status: "open", actor: "alice@test.com", at: "2026-10-26T09:00:00", note: null },
          { status: "done", actor: "bob@test.com", at: "2026-10-26T10:00:00", note: "cancelled on Flipkart" },
          { status: "open", actor: "alice@test.com", at: "2026-10-26T11:00:00", note: null },
          { status: "dropped", actor: "bob@test.com", at: "2026-10-26T12:00:00", note: null },
        ],
      }),
    ]);
    render(<OrderDetail order={order()} onClose={vi.fn()} />);

    const requests = row("Requests");
    expect(await within(requests).findByText("History (4 steps)")).toBeTruthy();
    expect(within(requests).getByText(/Marked done by bob@test.com/)).toBeTruthy();
    expect(within(requests).getByText(/“cancelled on Flipkart”/)).toBeTruthy();
    expect(within(requests).getByText(/Opened again by alice@test.com/)).toBeTruthy();
  });

  it("shows why the list could not be read", async () => {
    mocks.listOrderRequests.mockRejectedValue(new Error("Order not found"));
    render(<OrderDetail order={order()} onClose={vi.fn()} />);

    expect(await within(row("Requests")).findByText("Order not found")).toBeTruthy();
  });
});
