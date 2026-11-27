// @vitest-environment jsdom
/** The CorpAssist (HDFC) page: user ids, Fetch, and the child cards with their NetSafe status. */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  listHdfcUserIds: vi.fn(),
  addHdfcUserId: vi.fn(),
  updateHdfcUserId: vi.fn(),
  deleteHdfcUserId: vi.fn(),
  fetchHdfcCards: vi.fn(),
  listHdfcChildCards: vi.fn(),
  downloadHdfcChildCards: vi.fn(),
  actOnHdfcChildCards: vi.fn(),
  retryHdfcChildCards: vi.fn(),
  getHdfcNetSafe: vi.fn(),
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
  card_last4: "1111",
  parent_card_last4: "9876",
  opening_amount: 25000,
  used: 5000,
  remaining: 20000,
  status: "active",
  fetch_status: "completed",
  fetch_attempts: 1,
  fetched_at: "2026-11-19T10:01:00Z",
  valid_until: "2026-12-04",
  has_details: true,
  error: null,
  issued_at: null,
  created_at: "2026-11-19T10:00:00Z",
};

const PENDING = { ...CARD, id: 8, card_number: null, card_last4: "4321", fetch_status: "pending", has_details: false, used: 0, remaining: 1 };
const FAILED = { ...PENDING, id: 9, card_last4: "5555", fetch_status: "failed", error: "OTP timeout: no OTP reached the Employee Phone" };

beforeEach(() => {
  mocks.getHdfcNetSafe.mockResolvedValue({ key_set: true, busy: false, pending: 0, failed: 0 });
});

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

  it("adds a user id with username, email, mobile and parent card only", async () => {
    mocks.listHdfcUserIds.mockResolvedValue([USER]);
    mocks.addHdfcUserId.mockResolvedValue(USER);
    render(<CorpAssist />);
    await screen.findByText("ops1@corp.test");
    expect(screen.queryByLabelText(/^Aan/)).toBeNull();
    expect(screen.queryByLabelText(/^HDFC Virtual Card User ID/)).toBeNull();

    fireEvent.change(screen.getByLabelText(/^Username/), { target: { value: "mm" } });
    fireEvent.change(screen.getByLabelText(/^Email/), { target: { value: "mm@corp.test" } });
    fireEvent.change(screen.getByLabelText(/^Mobile/), { target: { value: "9876501234" } });
    fireEvent.change(screen.getByLabelText(/^Parent card/), { target: { value: "4421490000166989" } });
    fireEvent.click(screen.getByRole("button", { name: "Add user id" }));
    await waitFor(() =>
      expect(mocks.addHdfcUserId).toHaveBeenCalledWith({
        username: "mm",
        email: "mm@corp.test",
        mobile: "9876501234",
        parent_card: "4421490000166989",
      }),
    );
  });

  it("edits a user id and sends the parent card only when typed", async () => {
    mocks.listHdfcUserIds.mockResolvedValue([USER]);
    mocks.updateHdfcUserId.mockResolvedValue(USER);
    render(<CorpAssist />);
    await screen.findByText("ops1@corp.test");

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const dialog = await screen.findByRole("dialog");
    const mobile = within(dialog).getByLabelText(/^Mobile/) as HTMLInputElement;
    expect(mobile.value).toBe("9876543210");
    fireEvent.change(mobile, { target: { value: "9000000001" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mocks.updateHdfcUserId).toHaveBeenCalledWith(1, expect.objectContaining({ mobile: "9000000001" })));
    expect(mocks.updateHdfcUserId.mock.calls[0][1]).not.toHaveProperty("parent_card");
    expect(mocks.updateHdfcUserId.mock.calls[0][1]).not.toHaveProperty("aan");
  });

  it("has no Create cards tab and no HDFC portal buttons", async () => {
    mocks.listHdfcUserIds.mockResolvedValue([USER]);
    render(<CorpAssist />);
    await screen.findByText("ops1@corp.test");
    expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual(["User IDs", "Child cards"]);
    expect(screen.queryByRole("button", { name: "Sync from HDFC" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Set email on HDFC" })).toBeNull();
  });
it("shows a pending card by its last four digits and a failed one with its reason and Retry", async () => {
    mocks.listHdfcUserIds.mockResolvedValue([]);
    mocks.listHdfcChildCards.mockResolvedValue([PENDING, FAILED, CARD]);
    mocks.retryHdfcChildCards.mockResolvedValue({ changed: 1 });
    render(<CorpAssist />);
    fireEvent.click(screen.getByRole("tab", { name: "Child cards" }));

    expect(await screen.findByText("•••• 4321")).toBeTruthy();
    const table = within(screen.getByRole("table"));
    expect(table.getByText("4111111111111111")).toBeTruthy();
    expect(table.getByText("Pending")).toBeTruthy();
    expect(table.getByText("Completed")).toBeTruthy();
    expect(table.getByText("OTP timeout: no OTP reached the Employee Phone")).toBeTruthy();

    const retryRow = screen.getAllByRole("button", { name: "Retry" });
    expect(retryRow).toHaveLength(1);
    fireEvent.click(retryRow[0]);
    await waitFor(() => expect(mocks.retryHdfcChildCards).toHaveBeenCalledWith([9]));

    fireEvent.click(screen.getByRole("button", { name: "Retry all (1)" }));
    await waitFor(() => expect(mocks.retryHdfcChildCards).toHaveBeenCalledTimes(2));
  });

  it("filters by NetSafe status", async () => {
    mocks.listHdfcUserIds.mockResolvedValue([]);
    mocks.listHdfcChildCards.mockResolvedValue([]);
    render(<CorpAssist />);
    fireEvent.click(screen.getByRole("tab", { name: "Child cards" }));
    await waitFor(() => expect(mocks.listHdfcChildCards).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText(/^NetSafe/), { target: { value: "failed" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() =>
      expect(mocks.listHdfcChildCards).toHaveBeenLastCalledWith(expect.objectContaining({ fetch_status: "failed" })),
    );
  });

  it("stops a card being read with Stop, and offers Retry on a cancelled one", async () => {
    const CANCELLED = { ...PENDING, id: 10, card_last4: "7777", fetch_status: "cancelled", error: "cancelled: press Retry to read it" };
    mocks.listHdfcUserIds.mockResolvedValue([]);
    mocks.listHdfcChildCards.mockResolvedValue([PENDING, CANCELLED]);
    mocks.actOnHdfcChildCards.mockResolvedValue({ changed: 1 });
    mocks.retryHdfcChildCards.mockResolvedValue({ changed: 1 });
    render(<CorpAssist />);
    fireEvent.click(screen.getByRole("tab", { name: "Child cards" }));

    expect(await screen.findByText("•••• 7777")).toBeTruthy();
    const table = within(screen.getByRole("table"));
    expect(table.getByText("Cancelled")).toBeTruthy();
    fireEvent.click(table.getByRole("button", { name: "Stop" }));
    await waitFor(() => expect(mocks.actOnHdfcChildCards).toHaveBeenCalledWith([8], "cancel"));
    fireEvent.click(table.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(mocks.retryHdfcChildCards).toHaveBeenCalledWith([10]));
    expect(screen.getByRole("button", { name: "Retry all (1)" })).toBeTruthy();
  });

  it("warns that Cancel all stops cards being read", async () => {
    mocks.listHdfcUserIds.mockResolvedValue([]);
    mocks.listHdfcChildCards.mockResolvedValue([PENDING]);
    mocks.actOnHdfcChildCards.mockResolvedValue({ changed: 1 });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<CorpAssist />);
    fireEvent.click(screen.getByRole("tab", { name: "Child cards" }));
    await screen.findByText("•••• 4321");
    fireEvent.click(screen.getByRole("button", { name: "Cancel all" }));
    await waitFor(() => expect(mocks.actOnHdfcChildCards).toHaveBeenCalledWith([8], "cancel"));
    expect(confirm.mock.calls[0][0]).toMatch(/stop at once/);
    confirm.mockRestore();
  });

  it("warns when the server has no card key, so nothing can be read from NetSafe", async () => {
    mocks.listHdfcUserIds.mockResolvedValue([]);
    mocks.listHdfcChildCards.mockResolvedValue([PENDING]);
    mocks.getHdfcNetSafe.mockResolvedValue({ key_set: false, busy: false, pending: 1, failed: 0 });
    render(<CorpAssist />);
    fireEvent.click(screen.getByRole("tab", { name: "Child cards" }));
    expect(await screen.findByText(/no ORDERING_CARD_SECRET_KEY/)).toBeTruthy();
  });
});
