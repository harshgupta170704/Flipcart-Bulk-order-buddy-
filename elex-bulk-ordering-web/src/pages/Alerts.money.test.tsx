// @vitest-environment jsdom
/**
 * The facts under an alert's message show money as money (NFR-010; review of
 * 27 Oct 2026): rupees with the ₹ sign, Indian grouping and two decimals, as
 * the message above them already reads — never "Amount: 129900" under
 * "₹1,29,900.00". A count among the facts stays a plain number.
 */
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
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
    account_email: "money@test.com",
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

describe("an alert's facts", () => {
  it("show amounts in rupees, as the message does, and counts as plain numbers", async () => {
    mocks.listAlerts.mockResolvedValue([
      row({
        id: 3,
        kind: "order_over_target",
        severity: "info",
        message: "Order 3 was not placed: ₹1,29,900.00 is over the ₹1,25,000.00 target (money@test.com)",
        details: { amount: 129900, target: 125000, model: "vivo X200" },
      }),
      row({
        id: 4,
        kind: "invoice_amount_mismatch",
        message:
          "Order OD4's product invoice comes to ₹17,999.00 (₹18,206.50 with its 1 fee invoice), not the ₹18,207.00 the order was recorded at (money@test.com)",
        details: { amount: 18207, product_invoice_total: 17999, with_fee_invoices: 18206.5, invoice_numbers: "FAXYZ1" },
      }),
      row({
        id: 5,
        kind: "delivery_overdue",
        message: "Order 5 is 12 days old and still reads 'Shipped' (money@test.com)",
        details: { days: 12, overdue_days: 10, platform_status: "Shipped" },
      }),
    ]);
    mocks.alertsSummary.mockResolvedValue({
      open: 3,
      resolved: 0,
      by_severity: { critical: 0, warning: 2, info: 1 },
      by_kind: { order_over_target: 1, invoice_amount_mismatch: 1, delivery_overdue: 1 },
    });

    render(
      <MemoryRouter initialEntries={["/alerts"]}>
        <Alerts />
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByRole("table")).toBeTruthy());
    const table = screen.getByRole("table");
    expect(within(table).getByText("Amount: ₹1,29,900.00 · Target: ₹1,25,000.00 · Model: vivo X200")).toBeTruthy();
    expect(
      within(table).getByText("Amount: ₹18,207.00 · Product invoice total: ₹17,999.00 · With fee invoices: ₹18,206.50"),
    ).toBeTruthy();
    expect(within(table).getByText("Days: 12 · Overdue days: 10 · Platform status: Shipped")).toBeTruthy();
  });
});
