// @vitest-environment jsdom
/**
 * "Smart ID Selection starts on" (Smart ID, part 1): where the order form's
 * Smart ID Selection switch starts for every user. It sits in an Ordering
 * card of its own, reads the saved value (on when a server sends none), and
 * saves like the page's other yes-or-no settings, on its own.
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
    smart_id_default_on: true,
    ...over,
  };
}

function orderingCard(): HTMLElement {
  const card = screen.getByRole("heading", { name: "Ordering" }).closest("section");
  if (!card) throw new Error("no Ordering card");
  return card;
}

function toggle(): HTMLInputElement {
  return within(orderingCard()).getByLabelText("Smart ID Selection starts on") as HTMLInputElement;
}

describe("Settings: Smart ID Selection starts on", () => {
  it("shows the saved value in the Ordering card", async () => {
    mocks.getSettings.mockResolvedValue(settings({ smart_id_default_on: false }));
    render(<Settings />);

    await screen.findByRole("heading", { name: "Performance" });
    expect(toggle().checked).toBe(false);
  });

  it("is on for a server that sends no value", async () => {
    const saved = settings();
    delete saved.smart_id_default_on;
    mocks.getSettings.mockResolvedValue(saved);
    render(<Settings />);

    await screen.findByRole("heading", { name: "Performance" });
    expect(toggle().checked).toBe(true);
  });

  it("saves a change on its own, and shows what the server answered", async () => {
    mocks.getSettings.mockResolvedValue(settings({ smart_id_default_on: true }));
    mocks.updateSettings.mockImplementation(async (patch: Partial<AppSettings>) => settings(patch));
    render(<Settings />);

    await screen.findByRole("heading", { name: "Performance" });
    fireEvent.click(toggle());
    expect(within(orderingCard()).getByText("Unsaved changes")).toBeTruthy();
    fireEvent.click(within(orderingCard()).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(mocks.updateSettings).toHaveBeenCalledTimes(1));
    expect(mocks.updateSettings.mock.calls[0][0]).toEqual({ smart_id_default_on: false });
    expect(await within(orderingCard()).findByText("Saved.")).toBeTruthy();
    expect(toggle().checked).toBe(false);
  });

  it("Reset puts back the saved value without saving", async () => {
    mocks.getSettings.mockResolvedValue(settings({ smart_id_default_on: true }));
    render(<Settings />);

    await screen.findByRole("heading", { name: "Performance" });
    fireEvent.click(toggle());
    fireEvent.click(within(orderingCard()).getByRole("button", { name: "Reset" }));

    expect(toggle().checked).toBe(true);
    expect(mocks.updateSettings).not.toHaveBeenCalled();
  });
});
