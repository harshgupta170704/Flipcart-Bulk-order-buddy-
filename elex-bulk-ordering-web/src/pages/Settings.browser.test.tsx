// @vitest-environment jsdom
/**
 * Hidden or shown browser is each computer's own .env since 24 Nov 2026
 * (ORDERING_BROWSER_HEADLESS). The Settings page shows what the server uses,
 * read-only, with no switch, and a save never sends it.
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
    max_concurrency: 2,
    browser_headless: true,
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

/** The value written under a read-only term of the Performance card. */
function readOnly(term: string): string {
  const dt = within(performanceCard()).getByText(term, { selector: "dt" });
  return dt.nextElementSibling?.textContent ?? "";
}

describe("Settings: the browser window", () => {
  it("shows the server's choice read-only, and where it is set", async () => {
    mocks.getSettings.mockResolvedValue(settings({ browser_headless: false }));
    render(<Settings />);

    await screen.findByRole("heading", { name: "Performance" });
    const card = within(performanceCard());
    // Plain text read as a pair, not a control: no switch left in the card.
    expect(card.getAllByRole("term").map((term) => term.textContent)).toContain("Browser window");
    expect(readOnly("Browser window")).toBe("Shown");
    expect(card.getByText(/server’s \.env file: ORDERING_BROWSER_HEADLESS=true hides it/)).toBeTruthy();
    expect(card.getByText(/ORDERING_BROWSER_HEADLESS=true hides it.*Restart the API after changing it/s)).toBeTruthy();
    expect(card.queryAllByRole("checkbox")).toHaveLength(0);
    expect(screen.queryByRole("checkbox", { name: /headless/i })).toBeNull();
  });

  it("says hidden when the server hides it", async () => {
    mocks.getSettings.mockResolvedValue(settings({ browser_headless: true }));
    render(<Settings />);

    await screen.findByRole("heading", { name: "Performance" });
    expect(readOnly("Browser window")).toBe("Hidden");
  });

  it("never sends it when the Performance card is saved", async () => {
    mocks.getSettings.mockResolvedValue(settings({ browser_headless: false }));
    mocks.updateSettings.mockImplementation(async (patch: Partial<AppSettings>) => settings({ ...patch, browser_headless: false }));
    render(<Settings />);

    fireEvent.change(await screen.findByLabelText("Read orders back every (hours)"), { target: { value: "3" } });
    fireEvent.click(within(performanceCard()).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(mocks.updateSettings).toHaveBeenCalledTimes(1));
    const patch = mocks.updateSettings.mock.calls[0][0];
    expect(patch.sync_every_hours).toBe(3);
    expect("browser_headless" in patch).toBe(false);
    // After the save the row still reads what the server answered.
    expect(await within(performanceCard()).findByText("Saved.")).toBeTruthy();
    expect(readOnly("Browser window")).toBe("Shown");
  });
});
