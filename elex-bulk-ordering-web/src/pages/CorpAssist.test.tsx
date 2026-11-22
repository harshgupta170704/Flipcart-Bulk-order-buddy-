// @vitest-environment jsdom
/** The CorpAssist (HDFC) page: user ids, Fetch, and the child cards. */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  listHdfcUserIds: vi.fn(),
  addHdfcUserId: vi.fn(),
  updateHdfcUserId: vi.fn(),
  deleteHdfcUserId: vi.fn(),
  fetchHdfcCards: vi.fn(),
  listHdfcChildCards: vi.fn(),
  downloadHdfcChildCards: vi.fn(),
  actOnHdfcChildCards: vi.fn(),
}));
vi.mock("../lib/api", () => ({
  ...mocks,
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

import CorpAssist from "./CorpAssist";

const USER = {
  id: 1,
  username: "ops1",
  email: "ops1@corp.test",
  mobile: "9876543210",
  parent_card_last4: "9876",
  active: true,
  last_fetched_at: null,
  last_error: null,
  card_count: 0,
  created_at: "2026-11-19T10:00:00Z",
};

const CARD = {
  id: 7,
  user_id: 1,
  username: "ops1",
  email: "ops1@corp.test",
  card_number: "4111111111111111",
  parent_card_last4: "9876",
  opening_amount: 25000,
  used: 5000,
  remaining: 20000,
  status: "active",
  error: null,
  issued_at: null,
  created_at: "2026-11-19T10:00:00Z",
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("CorpAssist", () => {
  it("lists user ids and fetches the selected one", async () => {
    mocks.listHdfcUserIds.mockResolvedValue([USER]);
    mocks.fetchHdfcCards.mockResolvedValue([{ user_id: 1, username: "ops1", emails: 2, added: 1, error: null }]);
    render(<CorpAssist />);

    expect(await screen.findByText("ops1@corp.test")).toBeTruthy();
    expect(screen.getByText("•••• 9876")).toBeTruthy();
    const fetchSelected = screen.getByRole("button", { name: "Fetch selected" }) as HTMLButtonElement;
    expect(fetchSelected.disabled).toBe(true);

    fireEvent.click(screen.getByLabelText("Select ops1"));
    fireEvent.click(fetchSelected);
    await waitFor(() => expect(mocks.fetchHdfcCards).toHaveBeenCalledWith([1]));
    expect(await screen.findByText("1 new child card from 1 user id.")).toBeTruthy();
  });

  it("Fetch all sends no ids, so the API takes every active user id", async () => {
    mocks.listHdfcUserIds.mockResolvedValue([USER]);
    mocks.fetchHdfcCards.mockResolvedValue([]);
    render(<CorpAssist />);
    await screen.findByText("ops1@corp.test");
    fireEvent.click(screen.getByRole("button", { name: "Fetch all" }));
    await waitFor(() => expect(mocks.fetchHdfcCards).toHaveBeenCalledWith(null));
  });

  it("shows why a fetch failed, such as no inbox being set up", async () => {
    mocks.listHdfcUserIds.mockResolvedValue([USER]);
    mocks.fetchHdfcCards.mockRejectedValue(new Error("No inbox is set up to read HDFC emails from."));
    render(<CorpAssist />);
    await screen.findByText("ops1@corp.test");
    fireEvent.click(screen.getByRole("button", { name: "Fetch all" }));
    expect(await screen.findByText("No inbox is set up to read HDFC emails from.")).toBeTruthy();
  });

  it("shows child cards with their balance and filters by minimum remaining", async () => {
    mocks.listHdfcUserIds.mockResolvedValue([]);
    mocks.listHdfcChildCards.mockResolvedValue([CARD]);
    render(<CorpAssist />);
    fireEvent.click(screen.getByRole("tab", { name: "Child cards" }));

    expect(await screen.findByText("4111111111111111")).toBeTruthy();
    expect(screen.getByText("Child cards (1)")).toBeTruthy();

    fireEvent.change(screen.getByLabelText(/Minimum Remaining Amount/), { target: { value: "5000" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() =>
      expect(mocks.listHdfcChildCards).toHaveBeenLastCalledWith(expect.objectContaining({ min_remaining: "5000" })),
    );
  });

  it("cancels the cards shown after asking", async () => {
    mocks.listHdfcUserIds.mockResolvedValue([]);
    mocks.listHdfcChildCards.mockResolvedValue([CARD]);
    mocks.actOnHdfcChildCards.mockResolvedValue({ changed: 1 });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<CorpAssist />);
    fireEvent.click(screen.getByRole("tab", { name: "Child cards" }));
    await screen.findByText("4111111111111111");

    fireEvent.click(screen.getByRole("button", { name: "Cancel all" }));
    await waitFor(() => expect(mocks.actOnHdfcChildCards).toHaveBeenCalledWith([7], "cancel"));
    expect(confirm).toHaveBeenCalled();
    confirm.mockRestore();
  });
});
