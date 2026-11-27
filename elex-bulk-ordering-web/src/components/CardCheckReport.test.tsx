// @vitest-environment jsdom
/**
 * "Validate card file" also says how many of the sheet's cards are still
 * unused (the owner, 24 Nov 2026: "how many cards are still pending"): how
 * many have paid for no order in this tool yet, and which already have. Rows
 * and last four digits only. All values here are made up.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { formatCurrency, formatDate } from "../lib/format";
import type { CardCheck } from "../lib/types";
import { CardCheckReport, CHECK_ROWS_SHOWN, usedNote } from "./CardCheckReport";

afterEach(cleanup);

const BASE: CardCheck = { cards: 3, with_password: 3, fixed: [], set_aside: [], warnings: [] };

const WITH_USE: CardCheck = {
  ...BASE,
  used: [{ row: 2, card_last4: "2611", orders: 2, amount: 32423, last_used: "2026-11-24T09:49:00" }],
  unused: [
    { row: 3, card_last4: "3729" },
    { row: 4, card_last4: "4835" },
  ],
};

describe("CardCheckReport: cards used and not used", () => {
  it("gives both counts in one line", () => {
    render(<CardCheckReport check={WITH_USE} fileName="cards.csv" />);

    const line = screen.getByText(/Not used in any order yet:/);
    expect(line.textContent).toBe("Not used in any order yet: 2 · Already paid for an order: 1");
  });

  it("lists the unused cards by row and last four digits", () => {
    render(<CardCheckReport check={WITH_USE} fileName="cards.csv" />);

    expect(screen.getByText("2 cards not used in any order yet")).toBeTruthy();
    expect(screen.getByText("Row 3 · card ending 3729")).toBeTruthy();
    expect(screen.getByText("Row 4 · card ending 4835")).toBeTruthy();
  });

  it("says how many orders each used card paid for, the rupees and the latest date", () => {
    render(<CardCheckReport check={WITH_USE} fileName="cards.csv" />);

    expect(screen.getByText("1 card already paid for an order")).toBeTruthy();
    const item = screen.getByText("Row 2 · card ending 2611").closest("li");
    expect(item?.textContent).toBe(`Row 2 · card ending 2611: 2 orders, ${formatCurrency(32423)}, the last on ${formatDate("2026-11-24T09:49:00")}`);
  });

  it("keeps the row lists folded under a toggle", () => {
    render(<CardCheckReport check={WITH_USE} fileName="cards.csv" />);

    const toggles = screen.getAllByText("Show the rows");
    expect(toggles).toHaveLength(2);
    for (const toggle of toggles) expect(toggle.closest("details")?.open).toBe(false);
  });

  it("with every card unused, shows no used list", () => {
    render(
      <CardCheckReport
        check={{ ...BASE, used: [], unused: [{ row: 2, card_last4: "2611" }, { row: 3, card_last4: "3729" }, { row: 4, card_last4: "4835" }] }}
        fileName="cards.csv"
      />,
    );

    expect(screen.getByText(/Not used in any order yet:/).textContent).toBe("Not used in any order yet: 3 · Already paid for an order: 0");
    expect(screen.queryByText(/already paid for an order$/)).toBeNull();
    expect(screen.getByText("3 cards not used in any order yet")).toBeTruthy();
  });

  it("counts the rows past the first ones shown", () => {
    const unused = Array.from({ length: CHECK_ROWS_SHOWN + 5 }, (_, index) => ({ row: index + 2, card_last4: String(1000 + index) }));
    render(<CardCheckReport check={{ ...BASE, cards: unused.length, used: [], unused }} fileName="cards.csv" />);

    expect(screen.getByText("And 5 more.")).toBeTruthy();
    expect(screen.getAllByRole("listitem", { hidden: true })).toHaveLength(CHECK_ROWS_SHOWN);
  });

  it("an answer without the counts (an older server) shows the rest as before", () => {
    render(<CardCheckReport check={BASE} fileName="cards.csv" />);

    expect(screen.queryByText(/Not used in any order yet/)).toBeNull();
    expect(screen.getByText(/No problems found/)).toBeTruthy();
  });

  it("a sheet with no usable card gives no counts line", () => {
    render(<CardCheckReport check={{ ...BASE, cards: 0, with_password: 0, used: [], unused: [] }} fileName="cards.csv" />);

    expect(screen.queryByText(/Not used in any order yet/)).toBeNull();
  });

  it("says the run leaves used cards out when Skip cards already used is ticked", () => {
    render(<CardCheckReport check={WITH_USE} fileName="cards.csv" skipUsed />);
    expect(screen.getByText(/is ticked, so the run will not pay with these/)).toBeTruthy();
  });

  it("says the run may use them again when it is not ticked", () => {
    render(<CardCheckReport check={WITH_USE} fileName="cards.csv" skipUsed={false} />);
    expect(screen.getByText(/is not ticked, so the run may use these again/)).toBeTruthy();
  });

  it("a used card with no date leaves the date out", () => {
    expect(usedNote({ row: 2, card_last4: "2611", orders: 1, amount: 15999, last_used: null })).toBe(`1 order, ${formatCurrency(15999)}`);
  });
});
