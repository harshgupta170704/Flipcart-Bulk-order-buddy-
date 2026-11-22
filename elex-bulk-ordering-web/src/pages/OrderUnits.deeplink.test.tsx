// @vitest-environment jsdom
/**
 * The ledger opened on one order, as the dashboard's "Changes since you last
 * looked" links each change: the page asks for that order's rows at once, a
 * saved view does not replace them, and resetting the filters takes the link
 * off the address. A link that landed on the whole ledger would promise a row
 * it does not show.
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

import OrderUnits, { orderFromParams, rowFromParams } from "./OrderUnits";

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

describe("Order Units opened on one order", () => {
  it("asks for that order's rows at once", async () => {
    mount("/order-units?order_id=od338980309549917100");

    await waitFor(() => expect(mocks.listOrders).toHaveBeenCalled());
    expect(mocks.listOrders.mock.calls[0][0]).toMatchObject({ order_ids: ["OD338980309549917100"], page: 1 });
  });

  it("is not replaced by a saved view", async () => {
    mocks.getOrderView.mockResolvedValue({ columns: [], filters: { q: "kurta" }, saved_at: "2026-10-25T10:00:00" });
    mount("/order-units?order_id=OD338980309549917100");

    await waitFor(() => expect(mocks.getOrderView).toHaveBeenCalled());
    await waitFor(() => expect(mocks.listOrders).toHaveBeenCalled());
    for (const [filters] of mocks.listOrders.mock.calls) {
      expect(filters.order_ids).toEqual(["OD338980309549917100"]);
      expect(filters.q).toBeUndefined();
    }
  });

  it("takes the link off the address when the filters are reset", async () => {
    mount("/order-units?order_id=OD338980309549917100");
    await waitFor(() => expect(mocks.listOrders).toHaveBeenCalled());

    // The filter bar's (the empty table offers the same reset).
    fireEvent.click(screen.getAllByRole("button", { name: "Reset Filters" })[0]);

    await waitFor(() => expect(screen.getByTestId("address").textContent).toBe(""));
    const last = mocks.listOrders.mock.calls[mocks.listOrders.mock.calls.length - 1][0];
    expect(last.order_ids).toBeUndefined();
  });

  it("keeps the link while the filter still holds just that order", async () => {
    mount("/order-units?order_id=OD338980309549917100");
    await waitFor(() => expect(mocks.listOrders).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: "Apply Filters" }));

    await waitFor(() => expect(mocks.listOrders.mock.calls.length).toBeGreaterThan(1));
    expect(screen.getByTestId("address").textContent).toBe("?order_id=OD338980309549917100");
  });
});

describe("orderFromParams", () => {
  it("reads an order id, trimmed and upper-cased as the ledger stores it", () => {
    expect(orderFromParams(new URLSearchParams("order_id=%20od1-2%20"))).toBe("OD1-2");
  });

  it("ignores anything that cannot be an order id", () => {
    expect(orderFromParams(new URLSearchParams("order_id=OD1;DROP"))).toBeNull();
    expect(orderFromParams(new URLSearchParams("order_id="))).toBeNull();
    expect(orderFromParams(new URLSearchParams(""))).toBeNull();
    expect(orderFromParams(new URLSearchParams(`order_id=${"9".repeat(65)}`))).toBeNull();
  });
});

describe("Order Units opened on one order by its row (an alert's link)", () => {
  it("asks for that one order and says so, with the way back", async () => {
    mount("/order-units?row=26");

    await waitFor(() => expect(mocks.listOrders).toHaveBeenCalled());
    expect(mocks.listOrders.mock.calls[0][0]).toMatchObject({ order_row: 26, page: 1 });
    expect(screen.getByText(/Showing one order only/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "show all orders" }));
    fireEvent.click(screen.getByRole("button", { name: "Apply Filters" }));

    await waitFor(() => expect(screen.getByTestId("address").textContent).toBe(""));
    const last = mocks.listOrders.mock.calls[mocks.listOrders.mock.calls.length - 1][0];
    expect(last.order_row ?? null).toBeNull();
  });

  it("ignores a row that is not a number", () => {
    expect(rowFromParams(new URLSearchParams("row=abc"))).toBeNull();
    expect(rowFromParams(new URLSearchParams("row=0"))).toBeNull();
    expect(rowFromParams(new URLSearchParams("row=26"))).toBe(26);
  });
});
