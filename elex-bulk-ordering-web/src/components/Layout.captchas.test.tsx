// @vitest-environment jsdom
/** The "Captchas" nav entry carries the number of captchas waiting, from the one shared poll. */
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getToken: vi.fn(() => "token"),
  listCaptchas: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  ...mocks,
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));
vi.mock("../lib/auth", () => ({
  useAuth: () => ({
    username: "op",
    name: "Operator",
    role: "operator",
    isAdmin: false,
    logout: vi.fn(),
    retry: vi.fn(),
    expiresAt: null,
    token: "token",
  }),
}));
vi.mock("./AlertsSummary", () => ({ OpenAlertsBadge: () => null }));
vi.mock("./ChangesFeed", () => ({ UnseenChangesBadge: () => null }));

import { Layout } from "./Layout";

const OPEN = (id: number) => ({
  id,
  site: "flipkart",
  kind: "text",
  step: "checkout",
  job_id: 1,
  account_id: 2,
  account_email: null,
  created_at: "2026-11-23T09:00:00",
  seconds_left: 100,
});

function list(open: unknown[]) {
  return {
    open,
    last_hour: { pending: open.length, answered: 0, skipped: 0, expired: 0, blocked: 0 },
    ask: true,
    wait_seconds: 120,
    stop_count: 5,
    stop_minutes: 10,
  };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Layout captcha badge", () => {
  it("shows how many captchas are waiting beside Captchas", async () => {
    mocks.listCaptchas.mockResolvedValue(list([OPEN(1), OPEN(2)]));
    render(
      <MemoryRouter>
        <Layout />
      </MemoryRouter>,
    );

    const nav = screen.getByRole("navigation", { name: "Sections" });
    const link = within(nav).getByRole("link", { name: /Captchas/ });
    expect(link.getAttribute("href")).toBe("/captchas");
    await waitFor(() => expect(within(link).getByText("2")).toBeTruthy());
    expect(within(link).getByText("2 captchas waiting")).toBeTruthy();
    // Also in the top bar itself, for the folded (narrow) navigation.
    expect(screen.getByRole("link", { name: "Captchas: 2 waiting" })).toBeTruthy();
    // One poll for the whole shell.
    expect(mocks.listCaptchas).toHaveBeenCalledTimes(1);
  });

  it("shows no badge when nothing is waiting", async () => {
    mocks.listCaptchas.mockResolvedValue(list([]));
    render(
      <MemoryRouter>
        <Layout />
      </MemoryRouter>,
    );
    await waitFor(() => expect(mocks.listCaptchas).toHaveBeenCalled());
    const link = within(screen.getByRole("navigation", { name: "Sections" })).getByRole("link", { name: /Captchas/ });
    expect(link.textContent).toBe("Captchas");
    expect(screen.queryByRole("link", { name: /waiting/ })).toBeNull();
  });
});
