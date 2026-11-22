// @vitest-environment jsdom
/**
 * The ledger opened from the dashboard's "To be delivered" card ("View in
 * ledger", SC-004): the page reads the outcome and the delivery states off the
 * address, as it reads a run's or an order's link, asks for exactly the units
 * the card counted, shows the delivery states as a chip that can be taken off,
 * and is not replaced by a saved view. Without that, the card's total could be
 * checked against the ledger only through the API.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  listOrders: vi.fn(),
  orderFilterOptions: vi.fn(),
  getOrderView: vi.fn(),
  listJobs: vi.fn(),
}));

// The real module with the page's own calls replaced: some of the panels the
// page imports read the module's constants as they load.
vi.mock("../lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/api")>()),
  ...mocks,
  saveOrderView: vi.fn(),
  clearOrderView: vi.fn(),
  downloadOrders: vi.fn(),
  downloadOrdersCsv: vi.fn(),
  downloadInvoice: vi.fn(),
}));
// Toolbar panels with fetches of their own, beside the point here.
vi.mock("../components/CompileInvoices", () => ({ CompileInvoicesControls: () => null }));
vi.mock("../components/ExtractInvoicesData", () => ({ ExtractInvoicesDataControls: () => null }));

import { orderParams } from "../lib/api";
import { LEDGER_LINK } from "../components/ToBeDelivered";
import OrderUnits, { deliveryFromParams, outcomeFromParams } from "./OrderUnits";

beforeEach(() => {
  mocks.listOrders.mockResolvedValue({ items: [], total: 0, page: 1, size: 100, pages: 1 });
  mocks.orderFilterOptions.mockResolvedValue({ statuses: [], platform_statuses: [], delivery_states: [], total: 0 });
  mocks.getOrderView.mockResolvedValue(null);
  mocks.listJobs.mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function Address() {
  return <output data-testid="address">{useLocation().search}</output>;
}

function mount(entry: string) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route
          path="/order-units"
          element={
            <>
              <OrderUnits />
              <Address />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

/** The filter of the newest list request. */
function lastAsked() {
  return mocks.listOrders.mock.calls[mocks.listOrders.mock.calls.length - 1][0];
}

describe("Order Units opened from the To be delivered card", () => {
  it("asks at once for the units the card counted, under the same filter", async () => {
    mount(LEDGER_LINK);

    await waitFor(() => expect(mocks.listOrders).toHaveBeenCalled());
    const first = mocks.listOrders.mock.calls[0][0];
    expect(first).toMatchObject({ status: "placed", delivery_states: ["in_transit", "unknown"], page: 1 });
    // Sent on as the card's own link names them.
    const sent = orderParams(first);
    expect(sent.getAll("status")).toEqual(["placed"]);
    expect(sent.getAll("delivery_state")).toEqual(["in_transit", "unknown"]);
  });

  it("shows the outcome in its select and the delivery states as a chip", async () => {
    mount(LEDGER_LINK);
    await waitFor(() => expect(mocks.listOrders).toHaveBeenCalled());

    expect((screen.getByLabelText("Outcome") as HTMLSelectElement).value).toBe("placed");
    expect(screen.getByText(/^Delivery: on its way or not known yet/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Remove the delivery filter" })).toBeTruthy();
  });

  it("is not replaced by a saved view", async () => {
    mocks.getOrderView.mockResolvedValue({ columns: [], filters: { q: "kurta" }, saved_at: "2026-10-25T10:00:00" });
    mount(LEDGER_LINK);

    await waitFor(() => expect(mocks.getOrderView).toHaveBeenCalled());
    await waitFor(() => expect(mocks.listOrders).toHaveBeenCalled());
    for (const [filters] of mocks.listOrders.mock.calls) {
      expect(filters.delivery_states).toEqual(["in_transit", "unknown"]);
      expect(filters.q).toBeUndefined();
    }
  });

  it("takes the delivery states off with the chip and off the address on Apply, keeping the outcome", async () => {
    mount(LEDGER_LINK);
    await waitFor(() => expect(mocks.listOrders).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: "Remove the delivery filter" }));
    expect(screen.queryByText(/^Delivery:/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Apply Filters" }));

    await waitFor(() => expect(screen.getByTestId("address").textContent).toBe("?status=placed"));
    expect(lastAsked().delivery_states).toBeUndefined();
    expect(lastAsked().status).toBe("placed");
  });

  it("takes the whole link off the address when the filters are reset", async () => {
    mount(LEDGER_LINK);
    await waitFor(() => expect(mocks.listOrders).toHaveBeenCalled());

    // The filter bar's (the empty table offers the same reset).
    fireEvent.click(screen.getAllByRole("button", { name: "Reset Filters" })[0]);

    await waitFor(() => expect(screen.getByTestId("address").textContent).toBe(""));
    expect(lastAsked().delivery_states).toBeUndefined();
    expect(lastAsked().status).toBeUndefined();
    expect(screen.queryByText(/^Delivery:/)).toBeNull();
  });
});

describe("reading the link", () => {
  it("takes an outcome the server knows, and nothing else", () => {
    expect(outcomeFromParams(new URLSearchParams("status=placed"))).toBe("placed");
    expect(outcomeFromParams(new URLSearchParams("status=%20cancelled%20"))).toBe("cancelled");
    expect(outcomeFromParams(new URLSearchParams("status=refunded"))).toBeNull();
    expect(outcomeFromParams(new URLSearchParams(""))).toBeNull();
  });

  it("takes each known delivery state once, in the server's order", () => {
    expect(deliveryFromParams(new URLSearchParams("delivery_state=unknown&delivery_state=in_transit"))).toEqual([
      "in_transit",
      "unknown",
    ]);
    expect(deliveryFromParams(new URLSearchParams("delivery_state=unknown&delivery_state=unknown"))).toEqual(["unknown"]);
    expect(deliveryFromParams(new URLSearchParams("delivery_state=lost&delivery_state=delivered"))).toEqual(["delivered"]);
    expect(deliveryFromParams(new URLSearchParams("delivery_state=lost"))).toBeNull();
    expect(deliveryFromParams(new URLSearchParams(""))).toBeNull();
  });
});
