// @vitest-environment jsdom
/**
 * Deleting a GST that accounts have as their own: the dialog says so before
 * the delete (the accounts are kept and go back to their runs' GST), and the
 * note after it says how many — as it always has for the linked addresses.
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
    account_count: 3,
    created_at: "2026-10-26T09:00:00",
    ...over,
  };
}

beforeEach(() => {
  mocks.listAddresses.mockResolvedValue([]);
  // What the server answers it unlinked: the note after the delete reads this.
  mocks.deleteGst.mockResolvedValue({ unlinked_addresses: 0, unlinked_accounts: 3 });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

async function openDelete() {
  render(
    <MemoryRouter>
      <AddressBook />
    </MemoryRouter>,
  );
  fireEvent.click(await screen.findByRole("tab", { name: /GSTs \(1\)/ }));
  fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
  return screen.getByRole("dialog");
}

describe("Address Book: deleting a GST accounts have as their own", () => {
  it("says the accounts are kept and go back to the run's GST, before and after", async () => {
    mocks.listGsts.mockResolvedValue([gst()]);
    const dialog = await openDelete();

    expect(within(dialog).getByText(/3 accounts have it as their own GST/)).toBeTruthy();
    expect(within(dialog).getByText(/bill under the GST each run picks/)).toBeTruthy();

    fireEvent.click(within(dialog).getByRole("button", { name: "Delete GST" }));
    await waitFor(() => expect(mocks.deleteGst).toHaveBeenCalledWith(8));
    expect(
      await screen.findByText(/3 accounts that had it as their own GST now bill under the GST each run picks/),
    ).toBeTruthy();
  });

  it("says nothing about accounts when none has it", async () => {
    mocks.listGsts.mockResolvedValue([gst({ account_count: 0 })]);
    const dialog = await openDelete();

    expect(within(dialog).queryByText(/as their own GST|as its own GST/)).toBeNull();
  });
});
