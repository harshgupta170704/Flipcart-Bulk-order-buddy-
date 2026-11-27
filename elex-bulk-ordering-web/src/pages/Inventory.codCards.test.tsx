// @vitest-environment jsdom
/**
 * Inventory → "Update COD Card Details" (review of 24 Nov 2026): the server
 * now writes only Cash-on-Delivery orders and refuses any other row with its
 * reason. The result shows each refused row and the order ids not found, not
 * just their counts, so a refused row is seen.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { COD_ROWS_SHOWN, CodCardsResult } from "./Inventory";

afterEach(cleanup);

describe("CodCardsResult", () => {
  it("lists each refused row with why, and the order ids not found", () => {
    render(
      <CodCardsResult
        result={{
          updated: 1,
          not_found: ["OD4000000000000000300"],
          skipped: [
            "Row 3: OD4000000000000000200 was not a Cash-on-Delivery order (paid by card); its card stays as the run recorded it",
          ],
        }}
      />,
    );

    expect(screen.getByText("Updated: 1 · Not found: 1 · Skipped: 1")).toBeTruthy();
    expect(screen.getByText(/OD4000000000000000200 was not a Cash-on-Delivery order \(paid by card\)/)).toBeTruthy();
    expect(screen.getByText(/Not found: OD4000000000000000300/)).toBeTruthy();
  });

  it("a clean upload shows only the counts", () => {
    render(<CodCardsResult result={{ updated: 2, not_found: [], skipped: [] }} />);

    expect(screen.getByText("Updated: 2 · Not found: 0 · Skipped: 0")).toBeTruthy();
    expect(screen.queryByRole("listitem")).toBeNull();
    expect(screen.queryByText(/Not found: OD/)).toBeNull();
  });

  it("counts the rows past the first ones shown", () => {
    const skipped = Array.from({ length: COD_ROWS_SHOWN + 4 }, (_, i) => `Row ${i + 2}: invalid order_id or card digits`);
    render(<CodCardsResult result={{ updated: 0, not_found: [], skipped }} />);

    expect(screen.getByText("And 4 more.")).toBeTruthy();
    expect(screen.getAllByRole("listitem")).toHaveLength(COD_ROWS_SHOWN + 1);
  });
});
