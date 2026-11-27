// @vitest-environment jsdom
/**
 * Smart Bulk Order's ID picker with explicit ticks (owner, 24 Nov 2026):
 * choosing a domain selects every one of its IDs on every page — shown ticked,
 * not "nothing ticked means all" — unticking one keeps the others, and
 * unticking everything leaves none rather than quietly meaning all.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Account, AccountSelection, PagedAccountsParams } from "../lib/types";

/** 55 catbar.net ids (three pages of 25) and 5 gmail.com ids, all signed in. */
const ACCOUNTS = Array.from({ length: 60 }, (_, at) => {
  const id = at + 1;
  return {
    id,
    email: id <= 55 ? `buyer${id}@catbar.net` : `buyer${id}@gmail.com`,
    phone: null,
    tag: null,
    subscription_type: null,
    status: "session_valid",
    gst: null,
  } as unknown as Account;
});

const matching = (params: { domains?: string[] }) =>
  ACCOUNTS.filter(
    (account) =>
      !params.domains?.length || params.domains.some((domain) => account.email.endsWith(`@${domain}`)),
  );

const mocks = vi.hoisted(() => ({
  listAccounts: vi.fn(),
  listAccountIds: vi.fn(),
  getFilterOptions: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  ...mocks,
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

import { AccountPicker } from "./AccountPicker";

beforeEach(() => {
  mocks.listAccounts.mockImplementation((params: PagedAccountsParams) => {
    const all = matching(params);
    const size = params.size ?? 25;
    const page = params.page ?? 1;
    return Promise.resolve({
      items: all.slice((page - 1) * size, page * size),
      total: all.length,
      page,
      size,
      pages: Math.max(1, Math.ceil(all.length / size)),
    });
  });
  mocks.listAccountIds.mockImplementation((filter: { domains?: string[] }) => {
    const ids = matching(filter).map((account) => account.id);
    return Promise.resolve({ ids, total: ids.length, truncated: false });
  });
  mocks.getFilterOptions.mockResolvedValue({
    statuses: [{ value: "session_valid", count: 60 }],
    subscriptions: [],
    tags: [],
    total: 60,
    untagged: 60,
    domains: [
      { value: "catbar.net", count: 55 },
      { value: "gmail.com", count: 5 },
    ],
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

let latest: AccountSelection = { mode: "filter", filter: {}, count: 0 };

function Host({ explicitTicks = true }: { explicitTicks?: boolean }) {
  const [selection, setSelection] = useState<AccountSelection>({
    mode: "filter",
    filter: { statuses: ["session_valid"] },
    count: 0,
  });
  latest = selection;
  return (
    <AccountPicker
      value={selection}
      onChange={setSelection}
      filterStatus={["session_valid"]}
      checklists
      explicitTicks={explicitTicks}
    />
  );
}

const selectAllBox = () => screen.getByRole("checkbox", { name: /^Select all \d+ \(every page\)/ }) as HTMLInputElement;
const rowBox = (email: string) => screen.getByRole("checkbox", { name: new RegExp(`^${email}`) }) as HTMLInputElement;
const rowBoxes = () =>
  within(screen.getByRole("group", { name: /page \d+$/ }))
    .getAllByRole("checkbox")
    .map((box) => (box as HTMLInputElement).checked);

async function pickCatbar() {
  render(<Host />);
  await screen.findByText("buyer1@catbar.net");
  fireEvent.click(await screen.findByRole("checkbox", { name: /^catbar\.net/ }));
  await waitFor(() => expect(latest).toMatchObject({ mode: "filter", count: 55 }));
  await screen.findByRole("checkbox", { name: "Select all 55 (every page)" });
}

describe("Smart Bulk Order: Select all IDs of a domain", () => {
  it("picking a domain ticks all of its IDs, on every page, and sends them as the domain", async () => {
    await pickCatbar();
    expect(latest).toEqual({
      mode: "filter",
      filter: { statuses: ["session_valid"], domains: ["catbar.net"] },
      count: 55,
    });
    expect(selectAllBox().checked).toBe(true);
    expect(rowBoxes()).toHaveLength(25);
    expect(rowBoxes().every(Boolean)).toBe(true);
    expect(screen.getByText(/All 55 accounts selected, on every page/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText("buyer26@catbar.net");
    expect(rowBoxes().every(Boolean)).toBe(true);
  });

  it("unticking one ID keeps the other 54 ticked, on other pages too", async () => {
    await pickCatbar();
    fireEvent.click(rowBox("buyer3@catbar.net"));
    await waitFor(() => expect(latest.mode).toBe("ids"));
    expect(mocks.listAccountIds).toHaveBeenCalledWith(
      { statuses: ["session_valid"], domains: ["catbar.net"] },
      expect.any(Number),
    );
    const ids = latest.mode === "ids" ? latest.ids : [];
    expect(ids).toHaveLength(54);
    expect(ids).not.toContain(3);
    expect(rowBox("buyer3@catbar.net").checked).toBe(false);
    expect(rowBox("buyer4@catbar.net").checked).toBe(true);
    expect(selectAllBox().checked).toBe(false);
    expect(selectAllBox().indeterminate).toBe(true);
    expect(screen.getByText(/54 of 55 accounts ticked — exactly those will run/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText("buyer26@catbar.net");
    expect(rowBoxes().every(Boolean)).toBe(true);

    // Ticking it again is every match ticked: back to "all", said as the domain.
    fireEvent.click(screen.getByRole("button", { name: "Previous" }));
    await screen.findByText("buyer1@catbar.net");
    fireEvent.click(rowBox("buyer3@catbar.net"));
    await waitFor(() => expect(latest).toMatchObject({ mode: "filter", count: 55 }));
    expect(selectAllBox().checked).toBe(true);
  });

  it("unticking Select all leaves nothing selected; ticking it selects all again", async () => {
    await pickCatbar();
    fireEvent.click(selectAllBox());
    await waitFor(() => expect(latest).toEqual({ mode: "ids", ids: [] }));
    expect(rowBoxes().some(Boolean)).toBe(false);
    expect(screen.getByText(/No account ticked — nothing will run/)).toBeTruthy();

    fireEvent.click(rowBox("buyer2@catbar.net"));
    await waitFor(() => expect(latest).toEqual({ mode: "ids", ids: [2] }));

    fireEvent.click(selectAllBox());
    await waitFor(() => expect(latest).toMatchObject({ mode: "filter", count: 55 }));
    expect(rowBoxes().every(Boolean)).toBe(true);
  });

  it("a page ticked by hand, then Select all, covers the other pages too", async () => {
    await pickCatbar();
    fireEvent.click(screen.getByRole("button", { name: "Untick all" }));
    await waitFor(() => expect(latest).toEqual({ mode: "ids", ids: [] }));
    fireEvent.click(screen.getByRole("button", { name: "Tick this page" }));
    await waitFor(() => expect(latest.mode === "ids" && latest.ids.length).toBe(25));
    fireEvent.click(selectAllBox());
    await waitFor(() => expect(latest).toMatchObject({ mode: "filter", count: 55 }));
  });

  it("other pages keep the old picker: no Select all box, nothing ticked means all", async () => {
    render(<Host explicitTicks={false} />);
    await screen.findByText("buyer1@catbar.net");
    expect(screen.queryByRole("checkbox", { name: /every page/ })).toBeNull();
    expect(rowBoxes().some(Boolean)).toBe(false);
    expect(screen.getByRole("button", { name: /All 60 matching selected/ })).toBeTruthy();
  });
});
