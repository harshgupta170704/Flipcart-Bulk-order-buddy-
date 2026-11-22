// @vitest-environment jsdom
/** The CorpAssist (HDFC) page: user ids, Fetch, Create cards, and the child cards. */
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
  getHdfcPortal: vi.fn(),
  testHdfcPortalLogin: vi.fn(),
  syncHdfcPortalUsers: vi.fn(),
  pointHdfcUserEmail: vi.fn(),
  listHdfcBatches: vi.fn(),
  createHdfcBatch: vi.fn(),
  downloadHdfcBatchSheet: vi.fn(),
  retryHdfcBatch: vi.fn(),
  cancelHdfcBatch: vi.fn(),
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
  aan: "12345678901",
  portal_user_id: "4574",
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

const NO_PORTAL = {
  configured: false,
  url: "",
  user_hint: null,
  otp_via: null,
  otp_ready: false,
  otp_detail: null,
  auto_fetch_minutes: 0,
  busy: false,
  last_error: null,
};
const PORTAL = { ...NO_PORTAL, configured: true, url: "https://hdfc.test", user_hint: "ad***in", otp_via: "sms", otp_ready: true };

const BATCH = {
  id: 3,
  status: "failed",
  send: "portal",
  rows: [{ user_id: 1, username: "ops1", amount: 5000, count: 2, valid_days: 7 }],
  card_count: 2,
  cards_received: 0,
  error: "HDFC said the session expired.",
  portal_report: null,
  started_by: "admin",
  created_at: "2026-11-20T10:00:00",
  submitted_at: null,
  finished_at: "2026-11-20T10:01:00",
};

beforeEach(() => {
  mocks.getHdfcPortal.mockResolvedValue(NO_PORTAL);
  mocks.listHdfcBatches.mockResolvedValue([]);
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

  it("shows the Aan and saves the two new fields when adding a user id", async () => {
    mocks.listHdfcUserIds.mockResolvedValue([USER]);
    mocks.addHdfcUserId.mockResolvedValue(USER);
    render(<CorpAssist />);
    expect(await screen.findByText("12345678901")).toBeTruthy();

    fireEvent.change(screen.getByLabelText(/^Username/), { target: { value: "ops2" } });
    fireEvent.change(screen.getByLabelText(/^Email/), { target: { value: "ops2@corp.test" } });
    fireEvent.change(screen.getByLabelText(/^Aan \(parent card's Alternate A\/c No\.\)/), { target: { value: "9988 7766 5544" } });
    fireEvent.change(screen.getByLabelText(/^HDFC Virtual Card User ID/), { target: { value: "4575" } });
    fireEvent.click(screen.getByRole("button", { name: "Add user id" }));
    await waitFor(() =>
      expect(mocks.addHdfcUserId).toHaveBeenCalledWith(
        expect.objectContaining({ username: "ops2", aan: "9988 7766 5544", portal_user_id: "4575" }),
      ),
    );
  });

  it("edits a user id's Aan and HDFC Virtual Card User ID", async () => {
    mocks.listHdfcUserIds.mockResolvedValue([USER]);
    mocks.updateHdfcUserId.mockResolvedValue(USER);
    render(<CorpAssist />);
    await screen.findByText("ops1@corp.test");

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const dialog = await screen.findByRole("dialog");
    const aan = within(dialog).getByLabelText(/^Aan/) as HTMLInputElement;
    expect(aan.value).toBe("12345678901");
    fireEvent.change(aan, { target: { value: "11112222333" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(mocks.updateHdfcUserId).toHaveBeenCalledWith(1, expect.objectContaining({ aan: "11112222333", portal_user_id: "4574" })),
    );
    expect(mocks.updateHdfcUserId.mock.calls[0][1]).not.toHaveProperty("parent_card");
  });

  it("Sync from HDFC shows only with the portal set up, and reports what changed", async () => {
    mocks.listHdfcUserIds.mockResolvedValue([USER]);
    render(<CorpAssist />);
    await screen.findByText("ops1@corp.test");
    await waitFor(() => expect(mocks.getHdfcPortal).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "Sync from HDFC" })).toBeNull();
    cleanup();

    mocks.listHdfcUserIds.mockClear();
    mocks.getHdfcPortal.mockResolvedValue(PORTAL);
    mocks.syncHdfcPortalUsers.mockResolvedValue({ seen: 5, added: 2, updated: 3 });
    render(<CorpAssist />);
    fireEvent.click(await screen.findByRole("button", { name: "Sync from HDFC" }));
    expect(await screen.findByText("2 added, 3 updated.")).toBeTruthy();
    expect(mocks.syncHdfcPortalUsers).toHaveBeenCalledTimes(1);
    expect(mocks.listHdfcUserIds).toHaveBeenCalledTimes(2);
  });

  it("Set email on HDFC shows only with the portal set up and asks first", async () => {
    mocks.listHdfcUserIds.mockResolvedValue([USER]);
    render(<CorpAssist />);
    await screen.findByText("ops1@corp.test");
    await waitFor(() => expect(mocks.getHdfcPortal).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "Set email on HDFC" })).toBeNull();
    cleanup();

    mocks.getHdfcPortal.mockResolvedValue(PORTAL);
    mocks.pointHdfcUserEmail.mockResolvedValue(USER);
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    render(<CorpAssist />);
    const button = await screen.findByRole("button", { name: "Set email on HDFC" });
    fireEvent.click(button);
    expect(mocks.pointHdfcUserEmail).not.toHaveBeenCalled();
    fireEvent.click(button);
    expect(await screen.findByText(`${USER.username}'s email on HDFC is now ops1@corp.test.`)).toBeTruthy();
    expect(mocks.pointHdfcUserEmail).toHaveBeenCalledWith(USER.id);
    confirm.mockRestore();
  });

  it("Create cards posts one row per selected user id and sends to the portal", async () => {
    mocks.getHdfcPortal.mockResolvedValue(PORTAL);
    mocks.listHdfcUserIds.mockResolvedValue([USER, { ...USER, id: 2, username: "ops2", email: "ops2@corp.test" }]);
    mocks.createHdfcBatch.mockResolvedValue({ ...BATCH, status: "sending", error: null, card_count: 6 });
    render(<CorpAssist />);
    fireEvent.click(screen.getByRole("tab", { name: "Create cards" }));

    expect(await screen.findByText(/HDFC portal: signed in as ad\*\*\*in, OTP via sms/)).toBeTruthy();
    fireEvent.click(await screen.findByLabelText("Select all"));
    fireEvent.change(screen.getByLabelText(/^Amount/), { target: { value: "5000" } });
    fireEvent.change(screen.getByLabelText(/^Cards per user id/), { target: { value: "3" } });
    expect(screen.getByText(/Total: 6 cards, ₹30,000/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Create on HDFC" }));
    await waitFor(() =>
      expect(mocks.createHdfcBatch).toHaveBeenCalledWith(
        [
          { user_id: 1, amount: 5000, count: 3, valid_days: 7 },
          { user_id: 2, amount: 5000, count: 3, valid_days: 7 },
        ],
        "portal",
      ),
    );
    expect(mocks.downloadHdfcBatchSheet).not.toHaveBeenCalled();
  });

  it("without the portal, Create on HDFC is off and the sheet is downloaded instead", async () => {
    mocks.listHdfcUserIds.mockResolvedValue([USER]);
    mocks.createHdfcBatch.mockResolvedValue({ ...BATCH, id: 9, send: "download", status: "waiting", error: null });
    mocks.downloadHdfcBatchSheet.mockResolvedValue(undefined);
    render(<CorpAssist />);
    fireEvent.click(screen.getByRole("tab", { name: "Create cards" }));

    expect(await screen.findByText(/HDFC portal not set up/)).toBeTruthy();
    fireEvent.click(await screen.findByLabelText("Create for ops1"));
    expect((screen.getByRole("button", { name: "Create on HDFC" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Download HDFC sheet" }));
    await waitFor(() => expect(mocks.createHdfcBatch).toHaveBeenCalledWith([{ user_id: 1, amount: 1, count: 1, valid_days: 7 }], "download"));
    await waitFor(() => expect(mocks.downloadHdfcBatchSheet).toHaveBeenCalledWith(9));
  });

  it("warns when a selected user id has no Aan, and still shows the API's refusal", async () => {
    mocks.getHdfcPortal.mockResolvedValue(PORTAL);
    mocks.listHdfcUserIds.mockResolvedValue([{ ...USER, aan: null }]);
    mocks.createHdfcBatch.mockRejectedValue(new Error("ops1 has no Aan."));
    render(<CorpAssist />);
    fireEvent.click(screen.getByRole("tab", { name: "Create cards" }));

    fireEvent.click(await screen.findByLabelText("Create for ops1"));
    expect(screen.getByText(/ops1 has no Aan \(parent card's Alternate/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Create on HDFC" }));
    expect(await screen.findByText("ops1 has no Aan.")).toBeTruthy();
  });

  it("lists batches with their progress, and retries a failed portal batch", async () => {
    mocks.getHdfcPortal.mockResolvedValue(PORTAL);
    mocks.listHdfcUserIds.mockResolvedValue([USER]);
    mocks.listHdfcBatches.mockResolvedValue([
      BATCH,
      { ...BATCH, id: 4, status: "waiting", send: "download", error: null, cards_received: 1, card_count: 2 },
    ]);
    mocks.retryHdfcBatch.mockResolvedValue({ ...BATCH, status: "sending" });
    render(<CorpAssist />);
    fireEvent.click(screen.getByRole("tab", { name: "Create cards" }));

    expect(await screen.findByText("Batches (2)")).toBeTruthy();
    expect(screen.getByText("0/2")).toBeTruthy();
    expect(screen.getByText("1/2")).toBeTruthy();
    expect(screen.getByText("HDFC said the session expired.")).toBeTruthy();
    expect(screen.getByText("Failed")).toBeTruthy();
    expect(screen.getByText("Waiting for cards")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Retry" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Cancel" })).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(mocks.retryHdfcBatch).toHaveBeenCalledWith(3));
  });
});
