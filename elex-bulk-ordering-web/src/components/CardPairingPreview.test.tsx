// @vitest-environment jsdom
/**
 * The pairing a card run will use, shown before Start: each account with its
 * card's last four digits and whether a bank password is there, a warning for
 * the accounts without a card, the rows set aside — and the server's refusal,
 * said before Start rather than after.
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AccountSelection, CardPreview } from "../lib/types";

const mocks = vi.hoisted(() => ({ previewOrderCards: vi.fn() }));

vi.mock("../lib/api", () => ({
  previewOrderCards: mocks.previewOrderCards,
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

import { CardPairingPreview, PAIRING_ROWS_SHOWN } from "./CardPairingPreview";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const FILE = new File(["sheet"], "cards.csv", { type: "text/csv" });
const IDS: AccountSelection = { mode: "ids", ids: [1, 2, 3] };

const PREVIEW: CardPreview = {
  accounts: [
    { account_id: 1, email: "a1@test.com", card_last4: "4242", has_password: true },
    { account_id: 2, email: "a2@test.com", card_last4: "7777", has_password: false },
    { account_id: 3, email: "a3@test.com", card_last4: null, has_password: null },
  ],
  paired: 2,
  without_card: 1,
  paired_by: "email",
  rows_set_aside: [{ row: 4, reason: "card number: not 16 digits" }],
};

describe("CardPairingPreview", () => {
  it("lists each account's card ending and password, warns about the one without, and names the rows set aside", async () => {
    mocks.previewOrderCards.mockResolvedValue(PREVIEW);
    render(<CardPairingPreview file={FILE} selection={IDS} />);

    expect(await screen.findByText("ending 4242")).toBeTruthy();
    expect(screen.getByText("ending 7777")).toBeTruthy();
    expect(screen.getByText("No card — will be skipped")).toBeTruthy();
    expect(screen.getByText("1 account without a card")).toBeTruthy();
    expect(screen.getByText("Row 4: card number: not 16 digits")).toBeTruthy();
    expect(mocks.previewOrderCards).toHaveBeenCalledWith({ cards_file: FILE, account_ids: [1, 2, 3], account_filter: undefined });
  });

  it("sends a filter selection as the filter", async () => {
    mocks.previewOrderCards.mockResolvedValue({ ...PREVIEW, without_card: 0, rows_set_aside: [] });
    const filter: AccountSelection = { mode: "filter", filter: { statuses: ["session_valid"] }, count: 3 };
    render(<CardPairingPreview file={FILE} selection={filter} />);

    await screen.findByText("ending 4242");
    expect(mocks.previewOrderCards).toHaveBeenCalledWith({
      cards_file: FILE,
      account_ids: null,
      account_filter: { statuses: ["session_valid"] },
    });
    expect(screen.queryByText(/without a card/)).toBeNull();
  });

  it("says the run would refuse the sheet, in the server's words", async () => {
    mocks.previewOrderCards.mockRejectedValue(new Error("The card file has no email column, and 3 accounts are selected"));
    render(<CardPairingPreview file={FILE} selection={IDS} />);

    expect(await screen.findByText("The run would refuse this sheet")).toBeTruthy();
    expect(screen.getByText(/no email column/)).toBeTruthy();
  });

  it("lists the first rows of a long run and counts the rest", async () => {
    const many = Array.from({ length: PAIRING_ROWS_SHOWN + 5 }, (_, index) => ({
      account_id: index + 1,
      email: `a${index + 1}@test.com`,
      card_last4: "1111",
      has_password: false,
    }));
    mocks.previewOrderCards.mockResolvedValue({ ...PREVIEW, accounts: many, paired: many.length, without_card: 0, rows_set_aside: [] });
    render(<CardPairingPreview file={FILE} selection={{ mode: "ids", ids: many.map((row) => row.account_id) }} />);

    expect(await screen.findByText("And 5 more accounts, paired the same way.")).toBeTruthy();
    expect(screen.getAllByText("ending 1111")).toHaveLength(PAIRING_ROWS_SHOWN);
  });

  it("drops an earlier sheet's pairing the moment another sheet is attached", async () => {
    mocks.previewOrderCards.mockResolvedValueOnce(PREVIEW).mockReturnValueOnce(new Promise(() => undefined));
    const { rerender } = render(<CardPairingPreview file={FILE} selection={IDS} />);
    expect(await screen.findByText("ending 4242")).toBeTruthy();

    const other = new File(["another sheet"], "cards-b.csv", { type: "text/csv" });
    rerender(<CardPairingPreview file={other} selection={IDS} />);

    // At once, before the new sheet is even sent...
    expect(screen.queryByText("ending 4242")).toBeNull();
    expect(screen.getByText("Reading the sheet…")).toBeTruthy();
    // ...and for as long as its answer is awaited.
    await waitFor(() => expect(mocks.previewOrderCards).toHaveBeenCalledTimes(2));
    expect(mocks.previewOrderCards.mock.calls[1][0].cards_file).toBe(other);
    expect(screen.queryByText("ending 4242")).toBeNull();
    expect(screen.queryByText(/have a card in this sheet/)).toBeNull();
  });

  it("keeps the table while a new selection is checked, marked out of date and without its counts", async () => {
    let answer: (preview: CardPreview) => void = () => undefined;
    mocks.previewOrderCards.mockResolvedValueOnce(PREVIEW).mockReturnValueOnce(
      new Promise<CardPreview>((resolve) => {
        answer = resolve;
      }),
    );
    const { rerender } = render(<CardPairingPreview file={FILE} selection={IDS} />);
    expect(await screen.findByText("ending 4242")).toBeTruthy();
    const panel = screen.getByRole("region", { name: "Which card pays for which account" });
    expect(panel.getAttribute("aria-busy")).toBe("false");

    rerender(<CardPairingPreview file={FILE} selection={{ mode: "ids", ids: [1, 2] }} />);

    // The table stays, so the account picker below it does not jump...
    expect(screen.getByText("ending 4242")).toBeTruthy();
    // ...but nothing presents it as this selection's pairing.
    expect(panel.getAttribute("aria-busy")).toBe("true");
    expect(screen.getByText("Checking the new selection…")).toBeTruthy();
    expect(screen.queryByText(/have a card in this sheet/)).toBeNull();
    expect(screen.queryByText("1 account without a card")).toBeNull();
    await waitFor(() => expect(mocks.previewOrderCards).toHaveBeenCalledTimes(2));
    expect(screen.queryByText(/have a card in this sheet/)).toBeNull();

    answer({ ...PREVIEW, accounts: PREVIEW.accounts.slice(0, 2), without_card: 0 });

    expect(await screen.findByText(/2 of 2 selected accounts have a card in this sheet/)).toBeTruthy();
    expect(screen.queryByText("Checking the new selection…")).toBeNull();
    expect(panel.getAttribute("aria-busy")).toBe("false");
  });
});
