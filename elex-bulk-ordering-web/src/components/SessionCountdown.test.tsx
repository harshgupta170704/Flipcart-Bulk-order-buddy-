// @vitest-environment jsdom
/**
 * The session banner (spec C-13): quiet until five minutes before the end,
 * then a countdown; at the end the app signs out — unless another tab has
 * signed in since, whose newer token this tab must adopt, not clear.
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getToken: vi.fn(),
  logout: vi.fn(),
  retry: vi.fn(),
  expiresAt: null as string | null,
}));

vi.mock("../lib/api", () => ({ getToken: mocks.getToken }));
vi.mock("../lib/auth", () => ({
  useAuth: () => ({ expiresAt: mocks.expiresAt, token: "old-token", logout: mocks.logout, retry: mocks.retry }),
}));
vi.mock("./AlertsSummary", () => ({ OpenAlertsBadge: () => null }));

import { SessionCountdown } from "./Layout";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function mount() {
  return render(
    <MemoryRouter initialEntries={["/alerts"]}>
      <SessionCountdown />
    </MemoryRouter>,
  );
}

describe("SessionCountdown", () => {
  it("says nothing while more than five minutes remain", () => {
    mocks.expiresAt = new Date(Date.now() + 30 * 60_000).toISOString();
    mocks.getToken.mockReturnValue("old-token");
    mount();
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("counts down the last minutes", () => {
    mocks.expiresAt = new Date(Date.now() + 4 * 60_000).toISOString();
    mocks.getToken.mockReturnValue("old-token");
    mount();
    expect(screen.getByRole("status").textContent).toContain("Your session ends in 4 minutes");
  });

  it("signs out at the end when the stored token is its own", async () => {
    mocks.expiresAt = new Date(Date.now() - 1_000).toISOString();
    mocks.getToken.mockReturnValue("old-token");
    mount();
    await waitFor(() => expect(mocks.logout).toHaveBeenCalled());
    expect(mocks.retry).not.toHaveBeenCalled();
  });

  it("adopts a newer token from another tab instead of clearing it", async () => {
    mocks.expiresAt = new Date(Date.now() - 1_000).toISOString();
    mocks.getToken.mockReturnValue("newer-token-from-another-tab");
    mount();
    await waitFor(() => expect(mocks.retry).toHaveBeenCalled());
    expect(mocks.logout).not.toHaveBeenCalled();
  });
});
