// @vitest-environment jsdom
/**
 * The account import's answer on the Accounts page: the rows it refused are
 * listed with their reasons and counted, instead of vanishing from every
 * count (audit F-26; review of 27 Oct 2026). Setup is the GST test's.
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

describe("Accounts: the import names the rows it refused", () => {
  async function importWith(result: unknown) {
    mocks.importAccounts.mockResolvedValue(result);
    await rendered();
    const input = screen.getByLabelText(/Workbook \(\.xlsx\)/) as HTMLInputElement;
    const workbook = new File(["x"], "accounts.xlsx", {
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    });
    fireEvent.change(input, { target: { files: [workbook] } });
    fireEvent.click(screen.getByRole("button", { name: "Import accounts" }));
    await waitFor(() => expect(mocks.importAccounts).toHaveBeenCalledWith(workbook));
  }

  it("lists each refused row with its reason and counts them", async () => {
    await importWith({
      created: 2,
      updated: 0,
      skipped: 0,
      rejected: [{ row: 3, reason: "not an email address" }],
    });
    expect(await screen.findByText(/1 refused/)).toBeTruthy();
    expect(screen.getByText("Row 3: not an email address")).toBeTruthy();
  });

  it("shows the first 20 of many, and all of them on request", async () => {
    const rejected = Array.from({ length: 25 }, (_, i) => ({ row: i + 2, reason: "not an email address" }));
    await importWith({ created: 0, updated: 0, skipped: 0, rejected });
    expect(await screen.findByText("… and 5 more")).toBeTruthy();
    expect(screen.queryByText("Row 26: not an email address")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show all 25" }));
    expect(screen.getByText("Row 26: not an email address")).toBeTruthy();
  });

  it("says nothing of refusals when there were none, or an older server sent no list", async () => {
    await importWith({ created: 1, updated: 0, skipped: 0 });
    expect(await screen.findByText(/1 created/)).toBeTruthy();
    expect(screen.queryByText(/refused/)).toBeNull();
    expect(screen.queryByRole("list", { name: "Refused rows" })).toBeNull();
  });
});
