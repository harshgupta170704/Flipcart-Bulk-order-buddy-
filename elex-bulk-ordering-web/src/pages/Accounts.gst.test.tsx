// @vitest-environment jsdom
/**
 * An account's own GST and billing phone on the Accounts page: shown on every
 * row, edited per row (and put back when the server refuses), and the GST
 * given to a whole selection at once — ticked rows as ids, "all matching" as
 * the filter, as bulk tagging sends them. Both are setup (spec C-7): an
 * operator sees them but gets no control for them, and still tags accounts.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Account, AccountPage, Gst } from "../lib/types";

const mocks = vi.hoisted(() => ({
  listAccounts: vi.fn(),
  getFilterOptions: vi.fn(),
  listTags: vi.fn(),
  listGsts: vi.fn(),
  patchAccount: vi.fn(),
  assignAccountsGst: vi.fn(),
  tagAccounts: vi.fn(),
  createAccount: vi.fn(),
  downloadAccounts: vi.fn(),
  importAccounts: vi.fn(),
  createTag: vi.fn(),
  deleteTag: vi.fn(),
  renameTag: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  ...mocks,
  ApiError: class ApiError extends Error {
    status = 0;
  },
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));
/** Who is signed in: an admin unless a test says otherwise. */
const auth = vi.hoisted(() => ({ isAdmin: true }));
vi.mock("../lib/auth", () => ({ useAuth: () => auth }));

import Accounts from "./Accounts";

const DIGIDRIVE: Gst = {
  id: 7,
  label: "DIGIDRIVE",
  gstin: "27AADCD5678E1Z3",
  business_name: "Digidrive LLP",
  is_default: false,
  address_count: 0,
  account_count: 1,
  created_at: "2026-10-26T09:00:00",
};
const SONIC: Gst = { ...DIGIDRIVE, id: 8, label: "SONIC", gstin: "33AAECS4321F1ZK", business_name: "Sonic Kommerce", account_count: 0 };

function account(over: Partial<Account> = {}): Account {
  return {
    id: 1,
    email: "a1@test.com",
    phone: "9000000001",
    status: "session_valid",
    subscription_type: null,
    tag: null,
    gst: null,
    billing_phone: null,
    created_at: "2026-10-26T09:00:00",
    updated_at: "2026-10-26T09:00:00",
    ...over,
  };
}

const withGst = account({
  id: 1,
  email: "a1@test.com",
  gst: { id: 7, label: "DIGIDRIVE", gstin: "27AADCD5678E1Z3", business_name: "Digidrive LLP" },
  billing_phone: "9811111111",
});
const plain = account({ id: 2, email: "a2@test.com", phone: "9000000002" });

function page(items: Account[], total = items.length): AccountPage {
  return { items, total, page: 1, size: 50, pages: Math.max(1, Math.ceil(total / 50)) };
}

beforeEach(() => {
  auth.isAdmin = true;
  mocks.listAccounts.mockResolvedValue(page([withGst, plain]));
  mocks.getFilterOptions.mockResolvedValue({ statuses: [], subscriptions: [], tags: [], total: 2, untagged: 2 });
  mocks.listTags.mockResolvedValue([]);
  mocks.listGsts.mockResolvedValue([DIGIDRIVE, SONIC]);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

async function rendered() {
  render(<Accounts />);
  await screen.findByText("a2@test.com");
  // The GST list has arrived once a row's GST control is usable.
  await waitFor(() => expect((screen.getByLabelText("GST for a2@test.com") as HTMLSelectElement).disabled).toBe(false));
}

function gstSelect(email: string): HTMLSelectElement {
  return screen.getByLabelText(`GST for ${email}`) as HTMLSelectElement;
}

describe("Accounts: an account's own GST and billing phone", () => {
  it("shows each row's own GST and billing phone, and the run's GST where it has none", async () => {
    await rendered();

    expect(gstSelect("a1@test.com").value).toBe("7");
    expect(gstSelect("a2@test.com").value).toBe("");
    expect(screen.getAllByRole("option", { name: "Run's GST" }).length).toBeGreaterThan(0);
    expect(screen.getByText("9811111111")).toBeTruthy();
  });

  it("gives a row a GST, and takes one off", async () => {
    mocks.patchAccount.mockImplementation(async (id: number, patch: { gst_id: number | null }) =>
      id === 2
        ? { ...plain, gst: { id: 8, label: "SONIC", gstin: "33AAECS4321F1ZK", business_name: "Sonic Kommerce" } }
        : { ...withGst, gst: patch.gst_id === null ? null : withGst.gst },
    );
    await rendered();

    fireEvent.change(gstSelect("a2@test.com"), { target: { value: "8" } });
    await waitFor(() => expect(mocks.patchAccount).toHaveBeenCalledWith(2, { gst_id: 8 }));
    await waitFor(() => expect(gstSelect("a2@test.com").value).toBe("8"));

    fireEvent.change(gstSelect("a1@test.com"), { target: { value: "" } });
    await waitFor(() => expect(mocks.patchAccount).toHaveBeenCalledWith(1, { gst_id: null }));
    await waitFor(() => expect(gstSelect("a1@test.com").value).toBe(""));
  });

  it("edits a billing phone in the row, trimmed, and a blank one is the account's own phone", async () => {
    mocks.patchAccount.mockImplementation(async (id: number, patch: { billing_phone: string | null }) => ({
      ...(id === 1 ? withGst : plain),
      billing_phone: patch.billing_phone,
    }));
    await rendered();

    fireEvent.click(screen.getByRole("button", { name: "Edit the billing phone for a2@test.com" }));
    const input = screen.getByLabelText("Billing phone for a2@test.com") as HTMLInputElement;
    expect(input.value).toBe("");
    expect(input.maxLength).toBe(20);
    fireEvent.change(input, { target: { value: "  98765 43210 " } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mocks.patchAccount).toHaveBeenCalledWith(2, { billing_phone: "98765 43210" }));
    expect(await screen.findByText("98765 43210")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Edit the billing phone for a1@test.com" }));
    const own = screen.getByLabelText("Billing phone for a1@test.com") as HTMLInputElement;
    expect(own.value).toBe("9811111111");
    fireEvent.change(own, { target: { value: "   " } });
    fireEvent.submit(own.form as HTMLFormElement);
    await waitFor(() => expect(mocks.patchAccount).toHaveBeenCalledWith(1, { billing_phone: null }));
    await waitFor(() => expect(screen.queryByText("9811111111")).toBeNull());
  });

  it("Escape leaves the billing phone as it was, and saves nothing", async () => {
    await rendered();

    fireEvent.click(screen.getByRole("button", { name: "Edit the billing phone for a1@test.com" }));
    const input = screen.getByLabelText("Billing phone for a1@test.com");
    fireEvent.change(input, { target: { value: "7000000000" } });
    fireEvent.keyDown(input, { key: "Escape" });

    expect(screen.queryByLabelText("Billing phone for a1@test.com")).toBeNull();
    expect(screen.getByText("9811111111")).toBeTruthy();
    expect(mocks.patchAccount).not.toHaveBeenCalled();
  });

  it("puts the row back and says why when the server refuses an edit", async () => {
    mocks.patchAccount.mockRejectedValue(new Error("billing_phone: String should have at most 20 characters"));
    await rendered();

    fireEvent.click(screen.getByRole("button", { name: "Edit the billing phone for a1@test.com" }));
    fireEvent.change(screen.getByLabelText("Billing phone for a1@test.com"), { target: { value: "9822222222" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText(/Could not change the billing phone on a1@test.com/)).toBeTruthy();
    expect(screen.getByText("9811111111")).toBeTruthy();
    expect(screen.queryByText("9822222222")).toBeNull();
  });

  it("gives the ticked rows a GST in one request, as ids", async () => {
    mocks.assignAccountsGst.mockResolvedValue({ updated: 2 });
    await rendered();

    fireEvent.click(screen.getByLabelText("Select a1@test.com"));
    fireEvent.click(screen.getByLabelText("Select a2@test.com"));
    fireEvent.change(screen.getByLabelText(/Give 2 accounts a GST of their own/), { target: { value: "8" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply GST" }));

    await waitFor(() => expect(mocks.assignAccountsGst).toHaveBeenCalledWith({ gst_id: 8, account_ids: [1, 2] }));
    expect(await screen.findByText(/2 accounts now bill under “SONIC”/)).toBeTruthy();
    expect(mocks.listAccounts.mock.calls.length).toBeGreaterThan(1); // the page reloads to show it
  });

  it("gives every matching account a GST as the filter, after a second look", async () => {
    mocks.listAccounts.mockResolvedValue(page([withGst, plain], 120));
    mocks.assignAccountsGst.mockResolvedValue({ updated: 120 });
    await rendered();

    fireEvent.click(screen.getByRole("button", { name: "Select all 120 matching" }));
    fireEvent.change(screen.getByLabelText(/Give 120 accounts a GST of their own/), { target: { value: "7" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply GST" }));
    expect(mocks.assignAccountsGst).not.toHaveBeenCalled(); // past the rows on screen: asked first

    fireEvent.click(await screen.findByRole("button", { name: "Yes, apply to 120" }));
    await waitFor(() => expect(mocks.assignAccountsGst).toHaveBeenCalledWith({ gst_id: 7, filter: {} }));
  });

  it("takes the selection's own GST off with the run's-GST choice", async () => {
    mocks.assignAccountsGst.mockResolvedValue({ updated: 1 });
    await rendered();

    fireEvent.click(screen.getByLabelText("Select a1@test.com"));
    fireEvent.change(screen.getByLabelText(/Give 1 account a GST of its own/), { target: { value: "clear" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply GST" }));

    await waitFor(() => expect(mocks.assignAccountsGst).toHaveBeenCalledWith({ gst_id: null, account_ids: [1] }));
    expect(await screen.findByText(/1 account back on the run's GST/)).toBeTruthy();
  });
});

describe("Accounts: an operator's view of the GST and billing phone", () => {
  async function renderedAsOperator() {
    auth.isAdmin = false;
    render(<Accounts />);
    await screen.findByText("a2@test.com");
    await waitFor(() => expect(mocks.listGsts).toHaveBeenCalled());
  }

  it("shows each row's own GST and billing phone with no control to change them", async () => {
    await renderedAsOperator();

    expect(screen.queryByLabelText("GST for a1@test.com")).toBeNull();
    expect(screen.queryByLabelText("GST for a2@test.com")).toBeNull();
    expect(screen.getByText("DIGIDRIVE").getAttribute("title")).toBe("27AADCD5678E1Z3 · Digidrive LLP");
    expect(screen.getByText("Run's GST")).toBeTruthy(); // a2 has none of its own
    expect(screen.getByText("9811111111")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Edit the billing phone/ })).toBeNull();
    expect(screen.getByText("Only an admin can change an account's GST and billing phone.")).toBeTruthy();
    // The tag stays the operator's to change.
    await waitFor(() => expect((screen.getByLabelText("Tag for a1@test.com") as HTMLSelectElement).disabled).toBe(false));
  });

  it("offers a selection the tag but not a GST", async () => {
    await renderedAsOperator();

    fireEvent.click(screen.getByLabelText("Select a1@test.com"));

    expect(screen.getByLabelText(/Apply tag to 1 account/)).toBeTruthy();
    expect(screen.queryByLabelText(/a GST of its own/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Apply GST" })).toBeNull();
    expect(mocks.patchAccount).not.toHaveBeenCalled();
    expect(mocks.assignAccountsGst).not.toHaveBeenCalled();
  });

  it("leaves an admin the controls and no such note", async () => {
    await rendered();

    expect(screen.queryByText(/Only an admin can change/)).toBeNull();
    expect(screen.getByRole("button", { name: "Edit the billing phone for a1@test.com" })).toBeTruthy();
  });
});
