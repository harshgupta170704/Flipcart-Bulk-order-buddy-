// @vitest-environment jsdom
/**
 * The note after deleting a GST counts what the delete unlinked, as the
 * server answers it (review of 27 Oct 2026). It used to be worded from the
 * list loaded earlier, so an address or account linked or unlinked since — by
 * another admin, in another tab — made it wrong. The dialog before the delete
 * still reads the list: it is the best guess there is until the delete runs.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Gst } from "../lib/types";

const mocks = vi.hoisted(() => ({
  listAddresses: vi.fn(),
  listGsts: vi.fn(),
  deleteGst: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  ...mocks,
  ApiError: class ApiError extends Error {
    status = 0;
  },
  createAddress: vi.fn(),
  createGst: vi.fn(),
  deleteAddress: vi.fn(),
  updateAddress: vi.fn(),
  updateGst: vi.fn(),
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));
vi.mock("../lib/auth", () => ({ useAuth: () => ({ isAdmin: true }) }));

import AddressBook from "./AddressBook";

function gst(over: Partial<Gst> = {}): Gst {
  return {
    id: 8,
    label: "SONIC",
    gstin: "33AAECS4321F1ZK",
    business_name: "Sonic Kommerce",
    is_default: false,
    address_count: 0,
    account_count: 0,
    created_at: "2026-10-26T09:00:00",
    ...over,
  };
}

beforeEach(() => {
  mocks.listAddresses.mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

async function deleteIt(button: string) {
  render(
    <MemoryRouter>
      <AddressBook />
    </MemoryRouter>,
  );
  fireEvent.click(await screen.findByRole("tab", { name: /GSTs \(1\)/ }));
  fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
  const dialog = screen.getByRole("dialog");
  fireEvent.click(within(dialog).getByRole("button", { name: button }));
  await waitFor(() => expect(mocks.deleteGst).toHaveBeenCalledWith(8));
}

describe("Address Book: the note after deleting a GST", () => {
  it("counts what the server unlinked, not what the list said", async () => {
    // Loaded with one address; two more addresses and two accounts were linked since.
    mocks.listGsts.mockResolvedValue([gst({ address_count: 1, account_count: 0 })]);
    mocks.deleteGst.mockResolvedValue({ unlinked_addresses: 3, unlinked_accounts: 2 });

    await deleteIt("Delete and unlink 1 address");

    expect(await screen.findByText(/its 3 addresses are no longer linked to a GST/)).toBeTruthy();
    expect(screen.getByText(/2 accounts that had it as their own GST now bill under the GST each run picks/)).toBeTruthy();
    expect(screen.queryByText(/its 1 address is/)).toBeNull();
  });

  it("says only that it is gone when nothing was linked by then", async () => {
    // The list said two addresses and three accounts; all were unlinked meanwhile.
    mocks.listGsts.mockResolvedValue([gst({ address_count: 2, account_count: 3 })]);
    mocks.deleteGst.mockResolvedValue({ unlinked_addresses: 0, unlinked_accounts: 0 });

    await deleteIt("Delete and unlink 2 addresses");

    expect(await screen.findByText("“SONIC” is gone.")).toBeTruthy();
    expect(screen.queryByText(/no longer linked to a GST/)).toBeNull();
    expect(screen.queryByText(/as their own GST now bill/)).toBeNull();
  });
});
