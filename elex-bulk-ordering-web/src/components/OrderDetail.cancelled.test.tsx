// @vitest-environment jsdom
/**
 * The row dialog for a cancelled order: "Cancelled by Flipkart" with the
 * reason Flipkart gave, never the run's "Failed"; and, for one unit of an
 * order that stands, the reason beside that unit's own status.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Order } from "../lib/types";

const mocks = vi.hoisted(() => ({ listOrderInvoices: vi.fn() }));

vi.mock("../lib/api", () => ({
  downloadInvoice: vi.fn(),
  listOrderInvoices: mocks.listOrderInvoices,
  listOrderRequests: async () => [],
  downloadOrderInvoice: vi.fn(),
  startSyncJob: vi.fn(),
  waitForJob: vi.fn(),
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

const SELLER_POLICY = "Sorry, your order was cancelled as per seller policy restrictions applicable to this brand.";

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
    color: "Twilight Shadow",
    pin: "226013",
    amount: 18207,
    target: 18250,
    order_id: "OD338967210239333100",
    gst_name: "EXAMPLE TRADING",
    status: "failed",
    payment_mode: "cod",
    card_number: null,
    delivery_address: null,
    delivery_pincode: null,
    billed_gstin: "09AAAAA0000A1Z5",
    billed_business_name: "EXAMPLE TRADING",
    placed_at: "2026-10-24T10:00:00",
    created_at: "2026-10-24T10:00:00",
    brand: "vivo",
    category: null,
    job_id: 1,
    delivery_state: "cancelled",
    item_id: null,
    unit_id: null,
    seller: "HatmanRetails",
    tracking_id: null,
    platform_status: "Cancelled",
    delivery_date: null,
    delivery_otp: null,
    invoice_url: null,
    invoice_on_file: false,
    synced_at: "2026-10-25T08:00:00",
    open_request_kinds: [],
    cancel_reason: SELLER_POLICY,
    cancelled_by: "flipkart",
    ...over,
  };
}

/** The dialog's label/value pair under `label`. */
function row(label: string): HTMLElement {
  return screen.getByText(label).closest("div") as HTMLElement;
}

describe("OrderDetail for a cancelled order", () => {
  it("says Flipkart cancelled it, and why, under Outcome", () => {
    render(<OrderDetail order={order()} onClose={vi.fn()} />);

    const outcome = row("Outcome");
    expect(outcome.textContent).toContain("Cancelled by Flipkart");
    expect(outcome.textContent).toContain(`Flipkart says: ${SELLER_POLICY}`);
    expect(outcome.textContent).not.toContain("Failed");
    // Said once: not again beside the platform's own "Cancelled".
    expect(row("Platform status").textContent).toBe("Platform status✕Cancelled");
  });

  it("says no reason was recorded when the read-back has none", () => {
    render(<OrderDetail order={order({ cancel_reason: null })} onClose={vi.fn()} />);
    expect(row("Outcome").textContent).toContain("No reason recorded.");
  });

  it("names a cancellation the account asked for", () => {
    render(
      <OrderDetail
        order={order({ cancelled_by: "user", cancel_reason: "You requested a cancellation because you changed your mind about this product." })}
        onClose={vi.fn()}
      />,
    );
    expect(row("Outcome").textContent).toContain("Cancelled by user");
    expect(row("Outcome").textContent).toContain("You requested a cancellation");
  });

  it("keeps Failed, with no reason line, for a run that never reached Flipkart", () => {
    render(
      <OrderDetail
        order={order({ order_id: null, cancelled_by: null, cancel_reason: null, platform_status: null, delivery_state: "unknown", synced_at: null })}
        onClose={vi.fn()}
      />,
    );
    expect(row("Outcome").textContent).toBe("Outcome✕Failed");
  });

  it("puts one cancelled unit's reason beside its own status while the order stands", () => {
    render(
      <OrderDetail
        order={order({ status: "placed", units_in_order: 2, unit_position: 2, cancelled_by: null })}
        onClose={vi.fn()}
      />,
    );
    expect(row("Outcome").textContent).toBe("Outcome✓Placed");
    expect(row("Platform status").textContent).toContain(`Flipkart says: ${SELLER_POLICY}`);
  });
});
