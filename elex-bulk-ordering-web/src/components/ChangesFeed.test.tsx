// @vitest-environment jsdom
/**
 * The dashboard's "Changes since you last looked" rendered for real: each
 * change with its kind and a link to its order's rows, the empty state,
 * "Mark all as seen" sending back the moment the list was read and clearing
 * the list and the nav badge together, "Show more", and a failed load.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ChangesPage, UnitChange } from "../lib/types";

const mocks = vi.hoisted(() => ({
  listChanges: vi.fn(),
  markChangesSeen: vi.fn(),
  getToken: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  ...mocks,
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

/** The list and the badge share one module-level store: a fresh copy per test. */
let feed: typeof import("./ChangesFeed");
/** And the same copy of the sign-in signal the store listens to. */
let session: typeof import("../lib/session");

beforeEach(async () => {
  vi.resetModules();
  feed = await import("./ChangesFeed");
  session = await import("../lib/session");
  // No token unless a test says so: a change of who is signed in is then a
  // sign-out, after which nothing may be fetched.
  mocks.getToken.mockReturnValue(null);
});

afterEach(() => {
  cleanup();
  // Reset, not only cleared: an answer queued for a fetch that never came (a
  // failing test's) must not be handed to the next test's first fetch.
  vi.resetAllMocks();
});

const delivered: UnitChange = {
  id: 7,
  at: "2026-10-26T08:30:00",
  kind: "delivered",
  from_state: "out_for_delivery",
  to_state: "delivered",
  unit_pk: 31,
  unit_position: 1,
  unit_id: "338980309549917100000",
  order_row_id: 14,
  order_id: "OD338980309549917100",
  model: "vivo T5x 5G",
  color: "Space Black",
  account_id: 2,
  account_email: "two@test.com",
};

const cancelled: UnitChange = {
  ...delivered,
  id: 6,
  at: "2026-10-26T07:00:00",
  kind: "cancelled",
  from_state: null,
  to_state: "cancelled",
  unit_pk: 32,
  order_row_id: 15,
  order_id: "OD338980148530712100",
  model: "Galaxy M36",
};

function page(items: UnitChange[], over: Partial<ChangesPage> = {}): ChangesPage {
  const by_kind: Record<string, number> = { delivered: 0, out_for_delivery: 0, cancelled: 0, returned: 0, failed: 0 };
  for (const item of items) by_kind[item.kind] += 1;
  return {
    items,
    total: items.length,
    page: 1,
    size: 20,
    pages: 1,
    by_kind,
    since: "2026-10-19T09:00:00",
    seen_at: null,
    capped: false,
    as_of: "2026-10-26T09:00:00",
    ...over,
  };
}

function mount() {
  return render(
    <MemoryRouter>
      <nav>
        Dashboard <feed.UnseenChangesBadge />
      </nav>
      <feed.ChangesPanel />
    </MemoryRouter>,
  );
}

describe("ChangesPanel", () => {
  it("lists each change with its kind, its order and a link to its rows", async () => {
    mocks.listChanges.mockResolvedValue(page([delivered, cancelled]));
    mount();

    const link = await screen.findByRole("link", { name: "vivo T5x 5G" });
    expect(link.getAttribute("href")).toBe("/order-units?order_id=OD338980309549917100");
    expect(screen.getByRole("link", { name: "Galaxy M36" }).getAttribute("href")).toBe(
      "/order-units?order_id=OD338980148530712100",
    );
    expect(screen.getByText("Changes since you last looked — 2")).toBeTruthy();
    expect(screen.getByText("Delivered")).toBeTruthy();
    expect(screen.getByText("Cancelled")).toBeTruthy();
    expect(screen.getByText(/OD338980309549917100 · two@test.com · was out for delivery/)).toBeTruthy();
    // An order this tool placed, cancelled before it was ever read back.
    expect(screen.getByText(/OD338980148530712100 · two@test.com · first read back/)).toBeTruthy();
    expect(screen.getByText("1 delivered · 1 cancelled")).toBeTruthy();
    // One request for the list and the badge together.
    expect(mocks.listChanges).toHaveBeenCalledTimes(1);
    expect(mocks.listChanges).toHaveBeenCalledWith({ page: 1, size: feed.CHANGES_PANEL_SIZE });
  });

  it("says so when nothing changed, with nothing to mark", async () => {
    mocks.listChanges.mockResolvedValue(page([]));
    mount();

    expect(await screen.findByText("Nothing new since you last looked")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Mark all as seen" })).toBeNull();
  });

  it("marks all as seen up to the moment the list was read, and the badge clears with it", async () => {
    mocks.listChanges.mockResolvedValueOnce(page([delivered, cancelled])).mockResolvedValueOnce(page([]));
    mocks.markChangesSeen.mockResolvedValue({ seen_at: "2026-10-26T09:00:00" });
    mount();
    expect(await screen.findByText("2 changes since you last looked")).toBeTruthy(); // the badge

    fireEvent.click(screen.getByRole("button", { name: "Mark all as seen" }));

    expect(await screen.findByText("Nothing new since you last looked")).toBeTruthy();
    expect(mocks.markChangesSeen).toHaveBeenCalledWith("2026-10-26T09:00:00");
    expect(mocks.listChanges).toHaveBeenCalledTimes(2);
    expect(screen.queryByText("2 changes since you last looked")).toBeNull(); // the badge is gone
  });

  it("says when marking failed and keeps the list", async () => {
    mocks.listChanges.mockResolvedValue(page([delivered]));
    mocks.markChangesSeen.mockRejectedValue(new Error("This sign-in has no user of its own."));
    mount();

    fireEvent.click(await screen.findByRole("button", { name: "Mark all as seen" }));

    expect(await screen.findByText(/Could not mark them as seen: This sign-in has no user of its own/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "vivo T5x 5G" })).toBeTruthy();
  });

  it("fetches the rest of the window with Show more", async () => {
    const third = { ...cancelled, id: 5, unit_pk: 33, order_id: "OD100000000005", model: "Redmi 15C" };
    mocks.listChanges
      .mockResolvedValueOnce(page([delivered, cancelled], { total: 3, size: 2, pages: 2 }))
      .mockResolvedValueOnce(page([third], { total: 3, size: 2, page: 2, pages: 2 }));
    mount();
    expect(await screen.findByText("Showing 2 of 3")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Show more" }));

    expect(await screen.findByRole("link", { name: "Redmi 15C" })).toBeTruthy();
    expect(mocks.listChanges).toHaveBeenLastCalledWith({ page: 2, size: 2 });
    expect(screen.queryByRole("button", { name: "Show more" })).toBeNull();
  });

  it("keeps what Show more added when a reload brings nothing new", async () => {
    const third = { ...cancelled, id: 5, unit_pk: 33, order_id: "OD100000000005", model: "Redmi 15C" };
    const first = page([delivered, cancelled], { total: 3, size: 2, pages: 2 });
    mocks.listChanges
      .mockResolvedValueOnce(first)
      .mockResolvedValueOnce(page([third], { total: 3, size: 2, page: 2, pages: 2 }))
      .mockResolvedValueOnce({ ...first, as_of: "2026-10-26T09:02:00" });
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "Show more" }));
    expect(await screen.findByRole("link", { name: "Redmi 15C" })).toBeTruthy();

    await act(async () => {
      await feed.refreshChanges(); // what the two-minute reload does
    });

    expect(mocks.listChanges).toHaveBeenCalledTimes(3);
    expect(screen.getByRole("link", { name: "Redmi 15C" })).toBeTruthy();
  });

  it("names the stretch of time: since the mark, or 30 days back at most", async () => {
    mocks.listChanges.mockResolvedValue(page([delivered], { capped: true, seen_at: "2026-08-01T00:00:00", since: "2026-09-26T09:00:00" }));
    mount();

    expect(await screen.findByText(/You last looked more than 30 days ago/)).toBeTruthy();
  });

  it("offers to try again when the first load fails", async () => {
    mocks.listChanges.mockRejectedValueOnce(new Error("Could not reach the server.")).mockResolvedValueOnce(page([delivered]));
    mount();

    expect(await screen.findByText("Could not load what changed")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByRole("link", { name: "vivo T5x 5G" })).toBeTruthy();
  });
});

describe("UnseenChangesBadge", () => {
  it("shows nothing when nothing changed", async () => {
    mocks.listChanges.mockResolvedValue(page([]));
    const { container } = render(
      <MemoryRouter>
        <feed.UnseenChangesBadge />
      </MemoryRouter>,
    );

    await waitFor(() => expect(mocks.listChanges).toHaveBeenCalled());
    expect(container.textContent).toBe("");
  });

  it("counts the changes and spells out their kinds", async () => {
    mocks.listChanges.mockResolvedValue(page([delivered, cancelled]));
    render(
      <MemoryRouter>
        <feed.UnseenChangesBadge />
      </MemoryRouter>,
    );

    const said = await screen.findByText("2 changes since you last looked");
    expect(said.parentElement?.getAttribute("title")).toBe("2 changes since you last looked — 1 delivered, 1 cancelled");
  });
});

describe("the list belongs to whoever fetched it", () => {
  it("is dropped the moment who is signed in changes", async () => {
    mocks.listChanges.mockResolvedValue(page([delivered]));
    mount();
    expect(await screen.findByText("1 change since you last looked")).toBeTruthy(); // the badge

    act(() => session.sessionChanged());

    expect(screen.queryByText("1 change since you last looked")).toBeNull();
    expect(screen.queryByRole("link", { name: "vivo T5x 5G" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Mark all as seen" })).toBeNull();
  });

  it("is never marked as seen once someone else has signed in", async () => {
    const theirs = page([delivered]);
    mocks.listChanges.mockResolvedValueOnce(theirs).mockResolvedValue(page([]));
    mount();
    expect(await screen.findByText("1 change since you last looked")).toBeTruthy();

    act(() => session.sessionChanged());

    await expect(feed.markAllSeen(theirs)).rejects.toThrow(/earlier sign-in/);
    expect(mocks.markChangesSeen).not.toHaveBeenCalled();
  });

  it("drops a reply still on its way when the sign-in changes", async () => {
    let answer: (value: ChangesPage) => void = () => undefined;
    mocks.listChanges.mockReturnValueOnce(new Promise<ChangesPage>((resolve) => (answer = resolve)));
    mount();
    await waitFor(() => expect(mocks.listChanges).toHaveBeenCalledTimes(1));

    act(() => session.sessionChanged());
    await act(async () => answer(page([delivered])));

    expect(screen.queryByText("1 change since you last looked")).toBeNull();
  });
});

describe("after a change of who is signed in", () => {
  function badgeOnly() {
    return render(
      <MemoryRouter>
        <feed.UnseenChangesBadge />
      </MemoryRouter>,
    );
  }

  it("fetches the new person's list at once while the badge is on screen", async () => {
    // Another tab signed in, or a password change handed this tab a new
    // token: the badge stays mounted, so nothing else would fetch again.
    mocks.listChanges.mockResolvedValueOnce(page([delivered])).mockResolvedValueOnce(page([delivered, cancelled]));
    badgeOnly();
    expect(await screen.findByText("1 change since you last looked")).toBeTruthy();

    mocks.getToken.mockReturnValue("token-of-whoever-signed-in");
    act(() => session.sessionChanged());

    expect(await screen.findByText("2 changes since you last looked")).toBeTruthy();
    expect(mocks.listChanges).toHaveBeenCalledTimes(2);
  });

  it("fetches nothing after a sign-out, nor with no reader on screen", async () => {
    mocks.listChanges.mockResolvedValue(page([delivered]));
    const { unmount } = badgeOnly();
    expect(await screen.findByText("1 change since you last looked")).toBeTruthy();

    // Signed out: no token, so a fetch would only come back 401 and sign out again.
    act(() => session.sessionChanged());
    await act(async () => undefined);
    expect(mocks.listChanges).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("1 change since you last looked")).toBeNull();

    // Signed in again on the sign-in page: nobody shows the list yet.
    unmount();
    mocks.getToken.mockReturnValue("token-of-whoever-signed-in");
    act(() => session.sessionChanged());
    await act(async () => undefined);
    expect(mocks.listChanges).toHaveBeenCalledTimes(1);
  });

  const theirs: UnitChange = { ...cancelled, id: 5, unit_pk: 33, order_id: "OD100000000005", model: "Redmi 15C" };
  const next: UnitChange = { ...delivered, id: 9, unit_pk: 40, order_id: "OD100000000009", model: "Moto G96" };

  /**
   * The first person presses Show more; before the answer comes, someone else
   * signs in and their list loads. Returns the way to settle that answer.
   */
  async function showMoreThenSomeoneElse() {
    let settle: { resolve: (value: ChangesPage) => void; reject: (reason: unknown) => void } = {
      resolve: () => undefined,
      reject: () => undefined,
    };
    mocks.listChanges
      .mockResolvedValueOnce(page([delivered, cancelled], { total: 3, size: 2, pages: 2 }))
      .mockReturnValueOnce(new Promise<ChangesPage>((resolve, reject) => (settle = { resolve, reject })))
      .mockResolvedValueOnce(page([next]));
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "Show more" }));
    await waitFor(() => expect(mocks.listChanges).toHaveBeenCalledTimes(2));

    mocks.getToken.mockReturnValue("token-of-whoever-signed-in");
    act(() => session.sessionChanged());
    expect(await screen.findByRole("link", { name: "Moto G96" })).toBeTruthy();
    return settle;
  }

  it("drops the rows Show more asked for under the sign-in before", async () => {
    const settle = await showMoreThenSomeoneElse();

    await act(async () => settle.resolve(page([theirs], { total: 3, size: 2, page: 2, pages: 2 })));

    expect(screen.queryByRole("link", { name: "Redmi 15C" })).toBeNull();
    expect(screen.getByRole("link", { name: "Moto G96" })).toBeTruthy();
  });

  it("drops that request's failure too", async () => {
    const settle = await showMoreThenSomeoneElse();

    await act(async () => settle.reject(new Error("Could not reach the server.")));

    expect(screen.queryByText(/Could not load more/)).toBeNull();
    expect(screen.getByRole("link", { name: "Moto G96" })).toBeTruthy();
  });
});
