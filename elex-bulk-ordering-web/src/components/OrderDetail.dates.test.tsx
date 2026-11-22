// @vitest-environment jsdom
/**
 * The row dialog's delivery dates and payment, as the read-back now keeps them
 * apart (26 Oct 2026): the day Flipkart promised beside the day it delivered,
 * how late that was — from the first promise when Flipkart moved it — the
 * latest promise beside the delivery step still to come, and the order page's
 * total, called paid only when it was.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { formatCurrency, formatDate, formatLate } from "../lib/format";
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
    status: "placed",
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
    delivery_state: "in_transit",
    item_id: null,
    unit_id: null,
    seller: "HatmanRetails",
    tracking_id: null,
    platform_status: "Delivery expected by Nov 01",
    delivery_date: null,
    delivery_otp: null,
    invoice_url: null,
    invoice_on_file: false,
    synced_at: "2026-10-25T08:00:00",
    open_request_kinds: [],
    cancel_reason: null,
    cancelled_by: null,
    ...over,
  };
}

/** The dialog's label/value pair under `label`. */
function row(label: string): HTMLElement {
  return screen.getByText(label).closest("div") as HTMLElement;
}

describe("OrderDetail's delivery dates and payment", () => {
  it("shows the promise beside the delivery, and how late it came", () => {
    render(
      <OrderDetail
        order={order({
          delivery_state: "delivered",
          platform_status: "Delivered",
          delivery_date: "2026-11-03T00:00:00",
          promised_date: "2026-11-01T00:00:00",
          delivered_date: "2026-11-03T00:00:00",
          days_late: 2,
        })}
        onClose={vi.fn()}
      />,
    );

    const dates = row("Delivery date").textContent ?? "";
    expect(dates).toContain("Promised");
    expect(dates).toContain("Delivered");
    expect(dates).toContain("(2 days late)");
  });

  it("says on time when the delivery kept the promise", () => {
    render(
      <OrderDetail
        order={order({
          delivery_state: "delivered",
          promised_date: "2026-11-01T00:00:00",
          delivered_date: "2026-11-01T00:00:00",
          days_late: 0,
        })}
        onClose={vi.fn()}
      />,
    );
    expect(row("Delivery date").textContent).toContain("(on time)");
  });

  it("puts the promise beside the delivery step still to come", () => {
    render(
      <OrderDetail
        order={order({
          promised_date: "2026-11-01T00:00:00",
          timeline: [
            { status: "Order Confirmed", date: "2026-10-28T00:00:00" },
            { status: "Shipped", date: "2026-10-29T00:00:00" },
            { status: "Delivery", date: null },
          ],
        })}
        onClose={vi.fn()}
      />,
    );
    const step = screen.getByText(/^Delivery/, { selector: "li" });
    expect(step.textContent).toContain("not yet");
    expect(step.textContent).toContain("promised");
  });

  it("shows the first promise when Flipkart moved it, the move beside it, and the lateness from the first", () => {
    render(
      <OrderDetail
        order={order({
          delivery_state: "delivered",
          platform_status: "Delivered",
          delivery_date: "2026-11-05T00:00:00",
          first_promised_date: "2026-11-01T00:00:00",
          promised_date: "2026-11-05T00:00:00",
          delivered_date: "2026-11-05T00:00:00",
          days_late: 4,
        })}
        onClose={vi.fn()}
      />,
    );
    expect(row("Delivery date").textContent).toBe(
      `Delivery datePromised ${formatDate("2026-11-01T00:00:00")} (moved to ${formatDate("2026-11-05T00:00:00")}) · ` +
        `Delivered ${formatDate("2026-11-05T00:00:00")} (4 days late)`,
    );
  });

  it("says nothing of a move when the promise stayed on its day", () => {
    render(
      <OrderDetail
        order={order({ first_promised_date: "2026-11-01T00:00:00", promised_date: "2026-11-01T00:00:00" })}
        onClose={vi.fn()}
      />,
    );
    expect(row("Delivery date").textContent).toBe(`Delivery datePromised ${formatDate("2026-11-01T00:00:00")}`);
  });

  it("puts the latest promise, not the first, beside the delivery step still to come", () => {
    render(
      <OrderDetail
        order={order({
          first_promised_date: "2026-11-01T00:00:00",
          promised_date: "2026-11-05T00:00:00",
          timeline: [
            { status: "Order Confirmed", date: "2026-10-28T00:00:00" },
            { status: "Delivery", date: null },
          ],
        })}
        onClose={vi.fn()}
      />,
    );
    const step = screen.getByText(/^Delivery/, { selector: "li" });
    expect(step.textContent).toContain(`promised ${formatDate("2026-11-05T00:00:00")}`);
    expect(step.textContent).not.toContain(formatDate("2026-11-01T00:00:00"));
  });

  it("keeps the one date of a row read back before the dates were apart", () => {
    render(<OrderDetail order={order({ delivery_date: "2026-11-01T00:00:00" })} onClose={vi.fn()} />);
    const dates = row("Delivery date").textContent ?? "";
    expect(dates).not.toContain("Promised");
    expect(dates).not.toBe("Delivery date");
  });
});

describe("OrderDetail's payment line", () => {
  const COD = "Cash On Delivery";
  const CARD = "Credit Card";
  /** The line under the run's own choice in the Payment row. */
  function paidLine(over: Partial<Order>): string {
    render(<OrderDetail order={order({ amount_paid: 235, ...over })} onClose={vi.fn()} />);
    const line = row("Payment").querySelector("span.block");
    return line?.textContent ?? "";
  }
  const total = formatCurrency(235);

  it("does not call Cash on Delivery paid while the unit is on its way", () => {
    expect(paidLine({ payment_method: COD, delivery_state: "in_transit" })).toBe(`${total} to pay on delivery (${COD})`);
    // The run's own choice stays above it.
    expect(row("Payment").textContent).toContain("Cash on Delivery");
  });

  it("calls it paid once delivered, whichever the method", () => {
    expect(paidLine({ payment_method: COD, delivery_state: "delivered" })).toBe(`Paid ${total} by ${COD}`);
    cleanup();
    expect(paidLine({ payment_method: CARD, payment_mode: "card", delivery_state: "delivered" })).toBe(`Paid ${total} by ${CARD}`);
  });

  it("calls a card paid as soon as the order is placed", () => {
    expect(paidLine({ payment_method: CARD, payment_mode: "card", delivery_state: "in_transit" })).toBe(`Paid ${total} by ${CARD}`);
  });

  it("says nothing was collected for Cash on Delivery that was cancelled", () => {
    expect(paidLine({ payment_method: COD, delivery_state: "cancelled", platform_status: "Cancelled" })).toBe(
      `${total}, ${COD} — cancelled, nothing collected`,
    );
  });

  it("says a refund is due for a card payment cancelled or sent back", () => {
    expect(paidLine({ payment_method: CARD, payment_mode: "card", delivery_state: "cancelled" })).toBe(
      `Paid ${total} by ${CARD} — refund due`,
    );
    cleanup();
    expect(
      paidLine({ payment_method: CARD, payment_mode: "card", delivery_state: "returned", platform_status: "Return Requested" }),
    ).toBe(`Paid ${total} by ${CARD} — refund due`);
  });

  it("reads a returned unit's payment from Flipkart's own words", () => {
    const returned = (status: string, method = CARD) =>
      paidLine({ payment_method: method, payment_mode: "card", delivery_state: "returned", platform_status: status });
    // A return or replacement withdrawn: the unit stays delivered, nothing is owed.
    expect(returned("Return Cancelled")).toBe(`Paid ${total} by ${CARD}`);
    cleanup();
    expect(returned("Replacement Cancelled")).toBe(`Paid ${total} by ${CARD}`);
    cleanup();
    // A replacement swaps the item; no money moves back.
    expect(returned("Replacement Completed")).toBe(`Paid ${total} by ${CARD}`);
    cleanup();
    expect(returned("Replacement Completed", COD)).toBe(`Paid ${total} by ${COD}`);
    cleanup();
    // The money is back.
    expect(returned("Refund Completed")).toBe(`Paid ${total} by ${CARD} — refunded`);
    cleanup();
    // Still owed while the return or the refund is under way.
    expect(returned("Refund Initiated")).toBe(`Paid ${total} by ${CARD} — refund due`);
    cleanup();
    // Sent back by the courier, never delivered.
    expect(returned("Return to Origin", COD)).toBe(`${total}, ${COD} — not delivered, nothing collected`);
    cleanup();
    expect(returned("Return to Origin")).toBe(`Paid ${total} by ${CARD} — refund due`);
  });

  it("tells Cash on Delivery from the page's own words, not the run's choice", () => {
    // A unit first seen on Flipkart keeps the column's "card" default.
    expect(paidLine({ payment_method: "Pay on Delivery", payment_mode: "card", source: "sync", delivery_state: "in_transit" })).toBe(
      `${total} to pay on delivery (Pay on Delivery)`,
    );
  });

  it("calls the figure the order total when the page did not say how it is paid", () => {
    expect(paidLine({ payment_method: null, delivery_state: "in_transit" })).toBe(`Order total ${total}`);
  });

  it("says a unit that was never delivered collected nothing, or owes the card back", () => {
    expect(paidLine({ payment_method: COD, delivery_state: "failed" })).toBe(`${total}, ${COD} — not delivered, nothing collected`);
    cleanup();
    expect(paidLine({ payment_method: CARD, payment_mode: "card", delivery_state: "failed" })).toBe(
      `Paid ${total} by ${CARD} — refund due`,
    );
  });
});

describe("formatLate", () => {
  it("words the lateness", () => {
    expect(formatLate(null)).toBeNull();
    expect(formatLate(undefined)).toBeNull();
    expect(formatLate(0)).toBe("On time");
    expect(formatLate(-1)).toBe("On time");
    expect(formatLate(1)).toBe("1 day late");
    expect(formatLate(3)).toBe("3 days late");
  });
});
