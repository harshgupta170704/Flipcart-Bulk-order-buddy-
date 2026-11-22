// @vitest-environment jsdom
/**
 * The filter box rendered for real: the contract tests beside it cannot see a
 * controlled input eating the separator as it is typed, or a wording landing
 * in the wrong group.
 */
import { useEffect, useState } from "react";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { OrderFilterOptions, OrderFilters } from "../lib/types";
import { OrderUnitsFilters } from "./OrderUnitsFilters";

// Without globals, Testing Library cannot register its own cleanup: a render
// left behind doubles every label ("Date Date") for the next test.
afterEach(cleanup);

const OPTIONS: OrderFilterOptions = {
  statuses: [],
  platform_statuses: [
    { value: "Out for delivery", count: 2, state: "in_transit" },
    { value: "Cancellation requested", count: 1, state: "unknown" },
    { value: "Cancelled", count: 3, state: "cancelled" },
  ],
  delivery_states: [],
  total: 6,
};

function Harness({ onDraft }: { onDraft: (draft: OrderFilters) => void }) {
  const [draft, setDraft] = useState<OrderFilters>({ date_field: "placed_at" });
  useEffect(() => {
    onDraft(draft);
  }, [draft, onDraft]);
  return (
    <OrderUnitsFilters
      draft={draft}
      onChange={(patch) => setDraft((d) => ({ ...d, ...patch }))}
      onApply={() => undefined}
      onReset={() => undefined}
      appliedNarrowed={false}
      options={OPTIONS}
      applying={false}
      idPrefix="t"
    />
  );
}

describe("OrderUnitsFilters", () => {
  it("offers Flipkart's cancellations as an outcome of their own, apart from Failed", () => {
    const drafts: OrderFilters[] = [];
    render(<Harness onDraft={(d) => drafts.push(d)} />);
    const outcome = screen.getByLabelText("Outcome") as HTMLSelectElement;
    const labels = Array.from(outcome.options).map((option) => option.textContent);

    expect(labels).toContain("Failed");
    expect(labels).toContain("Cancelled on Flipkart");
    fireEvent.change(outcome, { target: { value: "cancelled" } });
    expect(drafts.at(-1)?.status).toBe("cancelled");
  });

  it("lets several order ids be typed one after another", () => {
    const drafts: OrderFilters[] = [];
    render(<Harness onDraft={(d) => drafts.push(d)} />);
    const box = screen.getByLabelText("Order ID") as HTMLInputElement;

    fireEvent.change(box, { target: { value: "OD1 " } });
    expect(box.value).toBe("OD1 "); // the separator survives the keystroke
    fireEvent.change(box, { target: { value: "OD1 OD2" } });

    expect(box.value).toBe("OD1 OD2");
    expect(drafts.at(-1)?.order_ids).toEqual(["OD1", "OD2"]);
  });

  it("groups the wordings the way the spec's picker does", () => {
    render(<Harness onDraft={() => undefined} />);
    // The button is named by its field label, as a labelled control should be.
    fireEvent.click(screen.getByRole("button", { name: "Order status" }));

    const cancelled = screen.getByRole("group", { name: "Cancelled" });
    within(cancelled).getByRole("button", { name: /Cancellation requested/ });
    within(cancelled).getByRole("button", { name: /^Cancelled/ });
    const confirmed = screen.getByRole("group", { name: "Confirmed" });
    within(confirmed).getByRole("button", { name: /Out for delivery/ });
  });

  it("ticks every wording of a group from the group's checkbox", () => {
    const drafts: OrderFilters[] = [];
    render(<Harness onDraft={(d) => drafts.push(d)} />);
    fireEvent.click(screen.getByRole("button", { name: "Order status" }));

    const cancelled = screen.getByRole("group", { name: "Cancelled" });
    fireEvent.click(within(cancelled).getByRole("checkbox"));

    expect(drafts.at(-1)?.platform_statuses).toEqual(["Cancellation requested", "Cancelled"]);
  });
});
