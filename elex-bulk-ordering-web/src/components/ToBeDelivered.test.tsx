// @vitest-environment jsdom
/**
 * The dashboard's "To be delivered" card rendered for real: flipping "Ordered
 * 15+ days ago" shows placeholders until that view's own figures land, never
 * the other view's figures under the new caption (spec FR-049), and "View in
 * ledger" opens Order Units History on the filter the card counts by (SC-004).
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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

import { LEDGER_LINK, ToBeDelivered } from "./ToBeDelivered";

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

const ALL: ToBeDeliveredData = {
  total_units: 5,
  total_orders: 4,
  older_than_days: null,
  models: [
    { brand: "vivo", model: "T5x 5G", units: 3, orders: 2, oldest_ordered_at: "2026-10-01T10:00:00" },
    { brand: "Samsung", model: "Galaxy M36", units: 2, orders: 2, oldest_ordered_at: "2026-10-20T10:00:00" },
  ],
};
const OLDER: ToBeDeliveredData = { total_units: 3, total_orders: 2, older_than_days: 15, models: [ALL.models[0]] };

function mount() {
  return render(
    <MemoryRouter>
      <ToBeDelivered refreshKey={0} />
    </MemoryRouter>,
  );
}

/** The card itself: the section its heading titles. */
function card(): HTMLElement {
  return screen.getByRole("heading", { level: 2 }).closest("section") as HTMLElement;
}

describe("To be delivered", () => {
  it("shows placeholders after the 15+ days toggle until that view's figures land", async () => {
    let answer: (value: ToBeDeliveredData) => void = () => undefined;
    mocks.getToBeDelivered.mockResolvedValueOnce(ALL).mockReturnValueOnce(
      new Promise<ToBeDeliveredData>((resolve) => {
        answer = resolve;
      }),
    );
    mount();
    expect(await screen.findByText("To be delivered — 5 order units across 2 models")).toBeTruthy();
    expect(screen.getByText("Samsung Galaxy M36")).toBeTruthy();

    fireEvent.click(screen.getByLabelText("Ordered 15+ days ago"));

    await waitFor(() => expect(mocks.getToBeDelivered).toHaveBeenLastCalledWith(15));
    // The caption already speaks of the older view; nothing of the unfiltered view stays under it.
    expect(within(card()).getByText(/^Only units ordered more than 15 days before today/)).toBeTruthy();
    expect(within(card()).getByRole("status", { name: "Loading" })).toBeTruthy();
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("To be delivered");
    expect(screen.queryByText(/5 order units/)).toBeNull();
    expect(screen.queryByText("Samsung Galaxy M36")).toBeNull();

    await act(async () => answer(OLDER));

    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("To be delivered — 3 order units across 1 model");
    expect(within(card()).queryByRole("status", { name: "Loading" })).toBeNull();
    expect(screen.queryByText("Samsung Galaxy M36")).toBeNull();
    expect(screen.getByText("vivo T5x 5G")).toBeTruthy();
  });

  it("says it could not load the toggled view rather than show the other view's figures", async () => {
    mocks.getToBeDelivered.mockResolvedValueOnce(ALL).mockRejectedValueOnce(new Error("Server unreachable"));
    mount();
    expect(await screen.findByText("To be delivered — 5 order units across 2 models")).toBeTruthy();

    fireEvent.click(screen.getByLabelText("Ordered 15+ days ago"));

    expect(await screen.findByText("Could not load what is still to be delivered")).toBeTruthy();
    expect(screen.queryByText(/5 order units/)).toBeNull();
    expect(screen.queryByText("Samsung Galaxy M36")).toBeNull();
  });

  it("links to the ledger under the filter it counts by, and says when the toggle cannot follow", async () => {
    mocks.getToBeDelivered.mockResolvedValue(ALL);
    mount();
    const link = await screen.findByRole("link", { name: "View in ledger" });
    expect(LEDGER_LINK).toBe("/order-units?status=placed&delivery_state=in_transit&delivery_state=unknown");
    expect(link.getAttribute("href")).toBe(LEDGER_LINK);
    expect(link.getAttribute("title")).not.toMatch(/15\+ days/);

    fireEvent.click(screen.getByLabelText("Ordered 15+ days ago"));

    // The ledger has no "ordered so many days ago" filter: the same link, and its title says so.
    expect(screen.getByRole("link", { name: "View in ledger" }).getAttribute("href")).toBe(LEDGER_LINK);
    expect(screen.getByRole("link", { name: "View in ledger" }).getAttribute("title")).toMatch(
      /15\+ days ago, so that choice is not carried over/,
    );
  });
});
