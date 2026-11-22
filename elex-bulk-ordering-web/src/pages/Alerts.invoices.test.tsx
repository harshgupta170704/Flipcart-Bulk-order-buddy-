// @vitest-environment jsdom
/**
 * The two invoice checks in the queue: each kind reads as plain words — in the
 * Kind column and the Kind filter alike — and the facts the rule recorded
 * show under the message, the way the other rules' do.
 */
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { kindLabel } from "../components/AlertSeverity";
import type { Alert } from "../lib/types";

const mocks = vi.hoisted(() => ({
  listAlerts: vi.fn(),
  alertsSummary: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  listAlerts: mocks.listAlerts,
  alertsSummary: mocks.alertsSummary,
  evaluateAlerts: vi.fn(),
  reopenAlert: vi.fn(),
  resolveAlert: vi.fn(),
  errorMessage: (err: unknown) => String(err),
}));

import Alerts from "./Alerts";

afterEach(cleanup);

function row(over: Partial<Alert> & Pick<Alert, "id" | "kind" | "message">): Alert {
  return {
    key: `${over.kind}:order:${over.id}`,
    severity: "warning",
    account_id: 1,
    account_email: "invoices@test.com",
    order_id: over.id,
    details: null,
    resolved: false,
    created_at: "2026-10-26T06:00:00",
    updated_at: "2026-10-26T06:00:00",
    resolved_at: null,
    resolution_note: null,
    resolved_by: null,
    last_seen_at: "2026-10-26T06:00:00",
    ...over,
  };
}

describe("kindLabel", () => {
  it("names the invoice checks in plain words, leaving the other names as they were", () => {
    expect(kindLabel("invoice_amount_mismatch")).toBe("Invoice amount differs");
    expect(kindLabel("product_invoice_missing")).toBe("Product invoice missing");
    expect(kindLabel("gst_mismatch")).toBe("Order unit GST mismatch");
    expect(kindLabel("delivery_overdue")).toBe("Delivery overdue");
  });
});

describe("the Alerts page", () => {
  it("shows an invoice check by its plain name, with the facts the rule recorded", async () => {
    mocks.listAlerts.mockResolvedValue([
      row({
        id: 7,
        kind: "invoice_amount_mismatch",
        message: "Order OD1's product invoice comes to ₹17,999.00, not the ₹18,207.00 the order was recorded at (invoices@test.com)",
        details: { amount: 18207, product_invoice_total: 17999, with_fee_invoices: null, invoice_numbers: "FAXYZ1" },
      }),
      row({
        id: 8,
        kind: "product_invoice_missing",
        message: "Order OD2 was delivered 5 days ago and has no product invoice on file (invoices@test.com)",
        details: { days: 5, delivered_on: "2026-10-21", other_invoices_on_file: 1, unreadable_invoices: 0, grace_days: 3 },
      }),
    ]);
    mocks.alertsSummary.mockResolvedValue({
      open: 2,
      resolved: 0,
      by_severity: { critical: 0, warning: 2, info: 0 },
      by_kind: { invoice_amount_mismatch: 1, product_invoice_missing: 1 },
    });

    render(
      <MemoryRouter initialEntries={["/alerts"]}>
        <Alerts />
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(screen.getByText(/product invoice comes to ₹17,999\.00, not the ₹18,207\.00 the order/)).toBeTruthy(),
    );
    const table = screen.getByRole("table");
    expect(within(table).getByText("Invoice amount differs")).toBeTruthy();
    expect(within(table).getByText("Product invoice missing")).toBeTruthy();
    // A null fact is left out rather than printed as "null".
    expect(
      within(table).getByText("Amount: ₹18,207.00 · Product invoice total: ₹17,999.00 · Invoice numbers: FAXYZ1"),
    ).toBeTruthy();
    expect(within(table).getByText("Days: 5 · Delivered on: 2026-10-21 · Other invoices on file: 1")).toBeTruthy();

    const kinds = screen.getByLabelText("Kind") as HTMLSelectElement;
    const offered = Array.from(kinds.options).map((option) => option.textContent);
    expect(offered).toContain("Invoice amount differs");
    expect(offered).toContain("Product invoice missing");
  });
});
