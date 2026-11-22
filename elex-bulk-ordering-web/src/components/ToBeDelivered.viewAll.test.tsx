// @vitest-environment jsdom
/**
 * The "To be delivered" card worded to spec (FR-040..042; review of 27 Oct
 * 2026): its line reads "<n> order units across <m> models", "View all <m>
 * models" opens the full list whenever there is a model to list — not only
 * once the bars run out — and the row for orders that name no model says so.
 * The 15+ days view says what it counts: orders placed more than fifteen days
 * before today (FR-041), as the server counts them.
 *
 * Each test waits for the figures by a product's name, not by the card's
 * line, so each one checks only the wording it is named for.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ToBeDelivered as ToBeDeliveredData } from "../lib/types";

const mocks = vi.hoisted(() => ({
  getToBeDelivered: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  ...mocks,
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

import { ToBeDelivered } from "./ToBeDelivered";

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

const THREE: ToBeDeliveredData = {
  total_units: 6,
  total_orders: 5,
  older_than_days: null,
  models: [
    { brand: "vivo", model: "T5x 5G", units: 3, orders: 2, oldest_ordered_at: "2026-10-01T10:00:00" },
    // Orders handed in on the order-id sheet, no model read for them yet.
    { brand: null, model: null, units: 2, orders: 2, oldest_ordered_at: "2026-10-05T10:00:00" },
    { brand: "Samsung", model: "Galaxy M36", units: 1, orders: 1, oldest_ordered_at: "2026-10-20T10:00:00" },
  ],
};

const ONE: ToBeDeliveredData = {
  total_units: 1,
  total_orders: 1,
  older_than_days: null,
  models: [{ brand: "HP", model: "Backpack", units: 1, orders: 1, oldest_ordered_at: "2026-10-05T10:00:00" }],
};

function mount() {
  return render(
    <MemoryRouter>
      <ToBeDelivered refreshKey={0} />
    </MemoryRouter>,
  );
}

/** The products the "View all" dialog lists, top to bottom. */
function listed(dialog: HTMLElement): (string | null)[] {
  return within(dialog)
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("cell")[0].textContent);
}

describe("To be delivered, worded to spec", () => {
  it("counts order units across models", async () => {
    mocks.getToBeDelivered.mockResolvedValue(THREE);
    mount();
    await screen.findByText("vivo T5x 5G");

    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe(
      "To be delivered — 6 order units across 3 models",
    );
  });

  it("offers View all for three models, though all three fit on the card", async () => {
    mocks.getToBeDelivered.mockResolvedValue(THREE);
    mount();
    await screen.findByText("vivo T5x 5G");

    fireEvent.click(screen.getByRole("button", { name: "View all 3 models" }));

    expect(listed(screen.getByRole("dialog"))).toEqual(["vivo T5x 5G", "Model not known yet", "Samsung Galaxy M36"]);
  });

  it("offers View all for a single model too", async () => {
    mocks.getToBeDelivered.mockResolvedValue(ONE);
    mount();
    await screen.findByText("HP Backpack");

    fireEvent.click(screen.getByRole("button", { name: "View all 1 model" }));

    expect(listed(screen.getByRole("dialog"))).toEqual(["HP Backpack"]);
  });

  it("names the row for orders with no model yet in plain words", async () => {
    mocks.getToBeDelivered.mockResolvedValue(THREE);
    mount();
    await screen.findByText("vivo T5x 5G");

    expect(screen.getByText("Model not known yet")).toBeTruthy();
    expect(screen.queryByText(/Unnamed product/)).toBeNull();
  });

  it("says the older view counts orders placed more than 15 days before today", async () => {
    // Six models, so the list is offered with or without the fix above and
    // this test checks the older view's wording alone.
    const six: ToBeDeliveredData = {
      total_units: 6,
      total_orders: 6,
      older_than_days: 15,
      models: ["A1", "B2", "C3", "D4", "E5", "F6"].map((model) => ({
        brand: "vivo",
        model,
        units: 1,
        orders: 1,
        oldest_ordered_at: "2026-10-01T10:00:00",
      })),
    };
    mocks.getToBeDelivered.mockResolvedValue(six);
    mount();
    await screen.findByText("vivo A1");

    fireEvent.click(screen.getByLabelText("Ordered 15+ days ago"));

    await waitFor(() => expect(mocks.getToBeDelivered).toHaveBeenLastCalledWith(15));
    expect(screen.getByText(/^Only units ordered more than 15 days before today/)).toBeTruthy();
    expect(screen.queryByText(/15 or more days/)).toBeNull();

    fireEvent.click(await screen.findByRole("button", { name: "View all 6 models" }));
    expect(within(screen.getByRole("dialog")).getByText("Ordered more than 15 days before today.")).toBeTruthy();
  });
});
