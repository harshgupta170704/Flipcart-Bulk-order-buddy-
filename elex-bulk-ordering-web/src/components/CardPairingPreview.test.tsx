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

import { cardLeaveOut } from "../lib/cardLeaveOut";
import { CardPairingPreview, PAIRING_ROWS_SHOWN, PASSED_OVER_ROWS_SHOWN } from "./CardPairingPreview";

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

  it("sends the run's minimum remaining and says when HDFC child cards were paired by balance", async () => {
    mocks.previewOrderCards.mockResolvedValue({ ...PREVIEW, paired_by: "balance" });
    render(<CardPairingPreview file={FILE} selection={IDS} minRemaining={500} />);
    expect(await screen.findByText(/matched by what each HDFC child card has left in CorpAssist/)).toBeTruthy();
    expect(mocks.previewOrderCards).toHaveBeenCalledWith({
      cards_file: FILE,
      account_ids: [1, 2, 3],
      account_filter: undefined,
      card_min_remaining: 500,
    });
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

describe("CardPairingPreview and the IDs the run passes over", () => {
  const LEAVE_OUT = {
    qty: 1,
    qty_mode: "upto" as const,
    total_quantity: 190,
    exclude_ordered_days: 7,
    skip_bought_product: true,
    product_link: "https://www.flipkart.com/x/p/itm5420c578ec2df?pid=MOBHQSFFCMZN6GBC",
    smart_id: true,
    ai_run: false,
  };
  /** Run #960 in small: the two oldest IDs ordered already, the pool's cards go to the fresh ones. */
  const POOL: CardPreview = {
    accounts: [
      { account_id: 3, email: "fresh1@catbar.net", card_last4: "4242", has_password: true, passed_over: null },
      { account_id: 4, email: "fresh2@catbar.net", card_last4: "7777", has_password: true, passed_over: null },
      {
        account_id: 1,
        email: "bought@catbar.net",
        card_last4: null,
        has_password: null,
        passed_over: "already bought this product (OD1 on 23 Nov 2026)",
      },
      {
        account_id: 2,
        email: "recent@catbar.net",
        card_last4: null,
        has_password: null,
        passed_over: "ordered in the last 7 day(s)",
      },
    ],
    paired: 2,
    without_card: 0,
    paired_by: "balance",
    rows_set_aside: [],
    passed_over: 2,
  };
  const FOUR: AccountSelection = { mode: "ids", ids: [1, 2, 3, 4] };

  it("sends who the run passes over, and lists them last with why and no card", async () => {
    mocks.previewOrderCards.mockResolvedValue(POOL);
    render(<CardPairingPreview file={FILE} selection={FOUR} leaveOut={LEAVE_OUT} />);

    expect(await screen.findByText("ending 4242")).toBeTruthy();
    expect(mocks.previewOrderCards).toHaveBeenCalledWith({
      cards_file: FILE,
      account_ids: [1, 2, 3, 4],
      account_filter: undefined,
      leave_out: LEAVE_OUT,
    });
    expect(screen.getByText("Not tried — already bought this product (OD1 on 23 Nov 2026)")).toBeTruthy();
    expect(screen.getByText("Not tried — ordered in the last 7 day(s)")).toBeTruthy();
    expect(screen.getByText(/2 of the 2 accounts the run will try have a card/)).toBeTruthy();
    expect(screen.getByText(/2 selected accounts will not be tried/)).toBeTruthy();
    expect(screen.getByText(/They get no card, so the cards go to the accounts the run tries/)).toBeTruthy();
    expect(screen.queryByText(/without a card/)).toBeNull();
    expect(screen.queryByText("No card — will be skipped")).toBeNull();
  });

  it("says an email sheet's cards of those IDs are not used", async () => {
    mocks.previewOrderCards.mockResolvedValue({
      ...POOL,
      paired_by: "email",
      accounts: POOL.accounts.map((row) => (row.account_id === 1 ? { ...row, card_last4: "1111", has_password: true } : row)),
    });
    render(<CardPairingPreview file={FILE} selection={FOUR} leaveOut={LEAVE_OUT} />);

    expect(await screen.findByText(/Their cards in this sheet are not used/)).toBeTruthy();
    // Its own card, never shown as paying.
    expect(screen.queryByText("ending 1111")).toBeNull();
  });

  it("asks again when what decides who is passed over changes", async () => {
    mocks.previewOrderCards.mockResolvedValue(POOL);
    const view = render(<CardPairingPreview file={FILE} selection={FOUR} leaveOut={LEAVE_OUT} />);
    await screen.findByText("ending 4242");
    expect(mocks.previewOrderCards).toHaveBeenCalledTimes(1);

    // An equal object from a new render: not a new question.
    view.rerender(<CardPairingPreview file={FILE} selection={FOUR} leaveOut={{ ...LEAVE_OUT }} />);
    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(mocks.previewOrderCards).toHaveBeenCalledTimes(1);

    view.rerender(<CardPairingPreview file={FILE} selection={FOUR} leaveOut={{ ...LEAVE_OUT, exclude_ordered_days: 3 }} />);
    await waitFor(() => expect(mocks.previewOrderCards).toHaveBeenCalledTimes(2));
    expect(mocks.previewOrderCards.mock.calls[1][0].leave_out.exclude_ordered_days).toBe(3);
  });

  it("says plainly when the run will try none of the selected accounts", async () => {
    mocks.previewOrderCards.mockResolvedValue({
      ...POOL,
      accounts: POOL.accounts.filter((row) => row.passed_over),
      paired: 0,
      passed_over: 2,
    });
    render(<CardPairingPreview file={FILE} selection={{ mode: "ids", ids: [1, 2] }} leaveOut={LEAVE_OUT} />);

    expect(await screen.findByText(/The run will try none of the 2 selected accounts/)).toBeTruthy();
    expect(screen.queryByText(/0 of the 0 accounts/)).toBeNull();
    expect(screen.queryByText(/They get no card/)).toBeNull();
  });

  it("always shows some of the passed-over rows, however many accounts the run tries", async () => {
    const tried = Array.from({ length: PAIRING_ROWS_SHOWN + 10 }, (_, i) => ({
      account_id: 100 + i,
      email: `t${i}@catbar.net`,
      card_last4: "4242",
      has_password: true,
      passed_over: null,
    }));
    const passed = Array.from({ length: PASSED_OVER_ROWS_SHOWN + 5 }, (_, i) => ({
      account_id: 500 + i,
      email: `p${i}@catbar.net`,
      card_last4: null,
      has_password: null,
      passed_over: "ordered in the last 7 day(s)",
    }));
    mocks.previewOrderCards.mockResolvedValue({
      ...POOL,
      accounts: [...tried, ...passed],
      paired: tried.length,
      passed_over: passed.length,
    });
    render(<CardPairingPreview file={FILE} selection={{ mode: "ids", ids: [1] }} leaveOut={LEAVE_OUT} />);

    expect(await screen.findByText("p0@catbar.net")).toBeTruthy();
    expect(screen.getAllByText("Not tried — ordered in the last 7 day(s)")).toHaveLength(PASSED_OVER_ROWS_SHOWN);
    expect(screen.getAllByText("ending 4242")).toHaveLength(PAIRING_ROWS_SHOWN);
    expect(screen.getByText("And 10 more accounts the run will try are paired the same way; 5 more will not be tried.")).toBeTruthy();
  });

  it("reads as before when nobody is passed over or an older server does not say", async () => {
    mocks.previewOrderCards.mockResolvedValue(PREVIEW);
    render(<CardPairingPreview file={FILE} selection={IDS} leaveOut={LEAVE_OUT} />);

    expect(await screen.findByText(/2 of 3 selected accounts have a card/)).toBeTruthy();
    expect(screen.queryByText(/will not be tried/)).toBeNull();
  });
});

describe("cardLeaveOut", () => {
  const check = {
    brand: "vivo",
    category: "mobile",
    variant: "MOBHQSFFCMZN6GBC",
    qty: 2,
    qty_mode: "minimum" as const,
    total_quantity: 190,
    exclude_ordered_days: 7,
    skip_bought_product: true,
    product_link: "https://www.flipkart.com/x/p/itm5420c578ec2df?pid=MOBHQSFFCMZN6GBC",
    limit_overrides: [{ limit_id: 1, max_units: 1, window_days: 30, enabled: true }],
  };

  it("sends the whole check with Smart ID on, since the run sets IDs aside for the limits", () => {
    expect(cardLeaveOut(check, true, false)).toEqual({ ...check, smart_id: true, ai_run: false });
  });

  it("sends only the bought and Exclude IDs rules otherwise: nothing else can change the answer", () => {
    const off = {
      qty: 1,
      qty_mode: "upto",
      total_quantity: null,
      exclude_ordered_days: 7,
      skip_bought_product: true,
      product_link: check.product_link,
    };
    expect(cardLeaveOut(check, false, false)).toEqual({ ...off, smart_id: false, ai_run: false });
    expect(cardLeaveOut(check, true, true)).toEqual({ ...off, smart_id: true, ai_run: true });
    expect(cardLeaveOut({ ...check, skip_bought_product: undefined, product_link: undefined }, false, false)).toEqual({
      qty: 1,
      qty_mode: "upto",
      total_quantity: null,
      exclude_ordered_days: 7,
      smart_id: false,
      ai_run: false,
    });
    expect(cardLeaveOut(null, true, false)).toBeNull();
  });

  it("adds the run switches the server also goes by, only when they are on", () => {
    const on = { mock: true, spreadOrders: true, totalAttempts: 50, useSupercoins: true, minSupercoins: 100 };
    expect(cardLeaveOut(check, true, false, on)).toEqual({
      ...check,
      smart_id: true,
      ai_run: false,
      mock: true,
      smart_selection: true,
      total_attempts: 50,
      use_supercoins: true,
      min_supercoins: 100,
    });
    expect(cardLeaveOut(check, false, false, { mock: false, spreadOrders: false, totalAttempts: null, useSupercoins: false })).toEqual(
      cardLeaveOut(check, false, false),
    );
    expect(cardLeaveOut(check, false, false, { useSupercoins: true, minSupercoins: null })).toMatchObject({ use_supercoins: true });
    expect(cardLeaveOut(check, false, false, { useSupercoins: true, minSupercoins: null })).not.toHaveProperty("min_supercoins");
  });
});
