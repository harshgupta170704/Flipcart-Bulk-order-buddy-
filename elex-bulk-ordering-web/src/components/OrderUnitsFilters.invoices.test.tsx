// @vitest-environment jsdom
/**
 * The four filters for finding orders by GST and by invoice, typed into the
 * real ledger page against a stand-in server: Apply must put them on the
 * list's request under the names backend/app/api/orders.py parse_filter()
 * reads, and Extract Invoices Data must ask for the same filter — it acts on
 * the ledger's applied filter, like Compile Invoices and both exports. The
 * saved view and the Reset rule are held to the same four fields.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { orderParams } from "../lib/api";
import { restoredFilters, viewFilters } from "../lib/orderView";
import OrderUnits from "../pages/OrderUnits";
import { isNarrowed } from "./OrderUnitsFilters";

const fetchMock = vi.fn();

/** What the stand-in server answers, by path: just enough for the page to settle. */
function answer(url: string): unknown {
  const path = new URL(url, "http://test").pathname;
  switch (path) {
    case "/api/orders":
      return { items: [], total: 3, page: 1, size: 100, pages: 1 };
    case "/api/orders/filter-options":
      return { statuses: [], platform_statuses: [], delivery_states: [], total: 3 };
    case "/api/invoices/types":
      return { types: [], units: 3, units_without_invoice: 3 };
    case "/api/invoices/compile/status":
      return { active: null, last: null, archive_retention_days: 0 };
    case "/api/invoices/archives":
    case "/api/jobs":
      return [];
    default:
      return null; // the saved view: none
  }
}

/** Every request made to one path, in order, as URLs. */
function requests(path: string): URL[] {
  return fetchMock.mock.calls
    .map(([url]) => new URL(String(url), "http://test"))
    .filter((url) => url.pathname === path);
}

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockImplementation(
    async (url: string) =>
      new Response(JSON.stringify(answer(String(url))), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
  );
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("the GST and invoice filters on the ledger page", () => {
  it("send what was typed under the server's names on Apply, and the invoice tools follow", async () => {
    render(
      <MemoryRouter>
        <OrderUnits />
      </MemoryRouter>,
    );
    await waitFor(() => expect(requests("/api/orders")).toHaveLength(1));
    const first = requests("/api/orders")[0].searchParams;
    for (const name of ["billed_gst", "invoice_number", "invoice_gstin", "invoice_month"]) {
      expect(first.has(name)).toBe(false); // nothing narrows until something is typed
    }

    fireEvent.change(screen.getByLabelText("Billed GST"), { target: { value: " 09aaaaa1111a1z1 " } });
    fireEvent.change(screen.getByLabelText("Invoice number"), { target: { value: "FA_26" } });
    fireEvent.change(screen.getByLabelText("Invoice GSTIN"), { target: { value: "29DDDDD" } });
    fireEvent.change(screen.getByLabelText("Invoice month"), { target: { value: "2026-10" } });
    expect(requests("/api/orders")).toHaveLength(1); // a draft: nothing is sent before Apply

    fireEvent.click(screen.getByRole("button", { name: "Apply Filters" }));

    await waitFor(() => expect(requests("/api/orders")).toHaveLength(2));
    const sent = requests("/api/orders")[1].searchParams;
    // Trimmed, otherwise as typed: the server upper-cases GSTINs and checks the month.
    expect(sent.get("billed_gst")).toBe("09aaaaa1111a1z1");
    expect(sent.get("invoice_number")).toBe("FA_26");
    expect(sent.get("invoice_gstin")).toBe("29DDDDD");
    expect(sent.get("invoice_month")).toBe("2026-10");

    fireEvent.click(screen.getByRole("button", { name: "Extract Invoices Data" }));
    await waitFor(() => expect(requests("/api/invoices/types")).toHaveLength(1));
    const asked = requests("/api/invoices/types")[0].searchParams;
    expect(asked.get("billed_gst")).toBe("09aaaaa1111a1z1");
    expect(asked.get("invoice_number")).toBe("FA_26");
    expect(asked.get("invoice_gstin")).toBe("29DDDDD");
    expect(asked.get("invoice_month")).toBe("2026-10");
  });

  it("caps the GSTIN and invoice-number boxes at the server's limits", () => {
    render(
      <MemoryRouter>
        <OrderUnits />
      </MemoryRouter>,
    );
    expect((screen.getByLabelText("Billed GST") as HTMLInputElement).maxLength).toBe(32);
    expect((screen.getByLabelText("Invoice GSTIN") as HTMLInputElement).maxLength).toBe(32);
    expect((screen.getByLabelText("Invoice number") as HTMLInputElement).maxLength).toBe(64);
    expect((screen.getByLabelText("Invoice month") as HTMLInputElement).type).toBe("month");
  });
});

describe("orderParams", () => {
  it("names the four filters as parse_filter reads them, trimmed, and leaves blanks out", () => {
    const params = orderParams({
      billed_gst: " 09AAAAA ",
      invoice_number: " FA1 ",
      invoice_gstin: "29DDDDD4444D1Z4",
      invoice_month: "2026-10",
    });
    expect(params.get("billed_gst")).toBe("09AAAAA");
    expect(params.get("invoice_number")).toBe("FA1");
    expect(params.get("invoice_gstin")).toBe("29DDDDD4444D1Z4");
    expect(params.get("invoice_month")).toBe("2026-10");
    expect(orderParams({ billed_gst: "  ", invoice_number: "", invoice_gstin: " ", invoice_month: null }).toString()).toBe("");
  });
});

describe("isNarrowed", () => {
  it("counts any of the four once it holds more than spaces", () => {
    expect(isNarrowed({ date_field: "placed_at", billed_gst: "09" })).toBe(true);
    expect(isNarrowed({ date_field: "placed_at", invoice_number: "FA" })).toBe(true);
    expect(isNarrowed({ date_field: "placed_at", invoice_gstin: "29" })).toBe(true);
    expect(isNarrowed({ date_field: "placed_at", invoice_month: "2026-10" })).toBe(true);
    expect(
      isNarrowed({ date_field: "placed_at", billed_gst: " ", invoice_number: "", invoice_gstin: "  ", invoice_month: null }),
    ).toBe(false);
  });
});

describe("the saved view", () => {
  it("keeps the four filters, and puts back only what every load would accept", () => {
    const kept = viewFilters({
      billed_gst: " 09AAAAA ",
      invoice_number: " FA1 ",
      invoice_gstin: "29DDDDD",
      invoice_month: "2026-10",
      job_id: 4,
    });
    expect(kept).toEqual({ billed_gst: "09AAAAA", invoice_number: "FA1", invoice_gstin: "29DDDDD", invoice_month: "2026-10" });
    expect(restoredFilters(kept)).toEqual(kept); // the round trip

    expect(restoredFilters({ invoice_month: "2026-13" })).toEqual({});
    expect(restoredFilters({ invoice_month: "Oct 2026" })).toEqual({});
    expect(restoredFilters({ invoice_month: 202610 })).toEqual({});
    expect(restoredFilters({ billed_gst: "9".repeat(33) })).toEqual({});
    expect(restoredFilters({ billed_gst: "9".repeat(32) })).toEqual({ billed_gst: "9".repeat(32) });
    expect(restoredFilters({ invoice_gstin: ["29DDDDD"] })).toEqual({});
    expect(restoredFilters({ invoice_number: "F".repeat(65) })).toEqual({});
    expect(restoredFilters({ invoice_number: "  " })).toEqual({});
  });
});
