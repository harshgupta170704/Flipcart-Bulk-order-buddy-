// @vitest-environment jsdom
/**
 * How many accounts a run works at once is each computer's own .env since
 * 24 Nov 2026 (ORDERING_MAX_CONCURRENCY). Until then a "Max concurrency" box
 * here was saved for every computer and beat the .env, so a laptop set to 6
 * still opened 4 windows. The page now shows the server's number read-only,
 * with where it is set, and a save never sends it.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AppSettings } from "../lib/types";

const mocks = vi.hoisted(() => ({
  getSettings: vi.fn(),
  updateSettings: vi.fn(),
  testOtpInbox: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  ...mocks,
  ApiError: class ApiError extends Error {},
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

import Settings from "./Settings";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function settings(over: Partial<AppSettings> = {}): AppSettings {
  return {
    max_concurrency: 6,
    browser_headless: false,
    proxy_server: "",
    sync_every_hours: 0,
    otp_imap_host: "imap.zoho.in",
    otp_imap_port: 993,
    otp_imap_user: "main@zoho.in",
    otp_imap_mailbox: "INBOX",
    otp_imap_use_ssl: true,
    otp_poll_interval: 3,
    otp_fetch_timeout: 60,
    otp_imap_password_set: true,
    otp_extra_inboxes: [],
    ...over,
  };
}

function performanceCard(): HTMLElement {
  const card = screen.getByRole("heading", { name: "Performance" }).closest("section");
  if (!card) throw new Error("no Performance card");
  return card;
}

function shownNumber(): string {
  const dt = within(performanceCard()).getByText("Max concurrency", { selector: "dt" });
  return dt.nextElementSibling?.textContent ?? "";
}

describe("Settings: Max concurrency", () => {
  it("shows the server's number read-only, and where it is set", async () => {
    mocks.getSettings.mockResolvedValue(settings({ max_concurrency: 6 }));
    render(<Settings />);

    await screen.findByRole("heading", { name: "Performance" });
    const card = within(performanceCard());
    expect(shownNumber()).toBe("6");
    // Text, not a box: nothing to type a number into.
    expect(screen.queryByLabelText("Max concurrency")).toBeNull();
    expect(card.getByText(/server’s \.env file as ORDERING_MAX_CONCURRENCY, from 1 to 50/)).toBeTruthy();
    expect(card.getByText(/A Normal Speed run still works 2 at a time/)).toBeTruthy();
    expect(card.queryByText(/accounts at once is a lot/)).toBeNull();
  });

  it("warns past eight", async () => {
    mocks.getSettings.mockResolvedValue(settings({ max_concurrency: 12 }));
    render(<Settings />);

    await screen.findByRole("heading", { name: "Performance" });
    expect(shownNumber()).toBe("12");
    expect(within(performanceCard()).getByText("12 accounts at once is a lot")).toBeTruthy();
  });

  it("never sends it when the Performance card is saved", async () => {
    mocks.getSettings.mockResolvedValue(settings({ max_concurrency: 6 }));
    mocks.updateSettings.mockImplementation(async (patch: Partial<AppSettings>) => settings({ ...patch, max_concurrency: 6 }));
    render(<Settings />);

    fireEvent.change(await screen.findByLabelText("Read orders back every (hours)"), { target: { value: "5" } });
    fireEvent.click(within(performanceCard()).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(mocks.updateSettings).toHaveBeenCalledTimes(1));
    const patch = mocks.updateSettings.mock.calls[0][0];
    expect(patch).toEqual({ sync_every_hours: 5 });
    expect(await within(performanceCard()).findByText("Saved.")).toBeTruthy();
    expect(shownNumber()).toBe("6");
  });
});
