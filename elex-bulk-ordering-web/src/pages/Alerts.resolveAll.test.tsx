// @vitest-environment jsdom
/**
 * A queue of old alerts used to be cleared one dialog at a time, and an alert
 * named its order by the ledger's own row number ("Order #26"), which no
 * screen shows. The page now links each alert to its order and closes every
 * open row on screen under one note.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Alert as AlertRow } from "../lib/types";

const mocks = vi.hoisted(() => ({
  listAlerts: vi.fn(),
  alertsSummary: vi.fn(),
  resolveAlerts: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  listAlerts: mocks.listAlerts,
  alertsSummary: mocks.alertsSummary,
  resolveAlerts: mocks.resolveAlerts,
  evaluateAlerts: vi.fn(),
  reopenAlert: vi.fn(),
  resolveAlert: vi.fn(),
  errorMessage: (err: unknown) => String(err),
}));

import Alerts from "./Alerts";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function alert(id: number, orderRow: number | null): AlertRow {
  return {
    id,
    key: `order_failed:order:${orderRow ?? id}`,
    kind: "order_failed",
    severity: "warning",
    message: "The order for buyer@example.com was not placed: Cash on Delivery is not available",
    account_id: 2,
    account_email: "buyer@example.com",
    order_id: orderRow,
    details: null,
    resolved: false,
    created_at: "2026-11-10T06:00:00",
    updated_at: "2026-11-10T06:00:00",
  } as AlertRow;
}

function open() {
  mocks.listAlerts.mockResolvedValue([alert(11, 26), alert(12, 27), alert(13, null)]);
  mocks.alertsSummary.mockResolvedValue({
    open: 3,
    resolved: 0,
    by_severity: { critical: 0, warning: 3, info: 0 },
    by_kind: { order_failed: 3 },
  });
  render(
    <MemoryRouter initialEntries={["/alerts"]}>
      <Alerts />
    </MemoryRouter>,
  );
}

describe("Alerts page", () => {
  it("links an alert to its order in the ledger instead of naming a row number", async () => {
    open();
    const links = await screen.findAllByRole("link", { name: "View the order" });
    expect(links.map((link) => link.getAttribute("href")).sort()).toEqual(["/order-units?row=26", "/order-units?row=27"]);
    expect(screen.queryByText(/Order #26/)).toBeNull();
  });

  it("closes every open alert on screen under one note", async () => {
    open();
    mocks.resolveAlerts.mockResolvedValue({ resolved: 3, open: 0 });

    fireEvent.click(await screen.findByRole("button", { name: "Resolve all 3 shown" }));
    fireEvent.change(screen.getByLabelText("Note (optional)"), { target: { value: "test runs" } });
    fireEvent.click(screen.getByRole("button", { name: "Resolve 3" }));

    await waitFor(() => expect(mocks.resolveAlerts).toHaveBeenCalledTimes(1));
    const [ids, note] = mocks.resolveAlerts.mock.calls[0];
    expect([...ids].sort()).toEqual([11, 12, 13]);
    expect(note).toBe("test runs");
    expect(await screen.findByText(/Resolved 3 alerts with your note/)).toBeTruthy();
  });
});
