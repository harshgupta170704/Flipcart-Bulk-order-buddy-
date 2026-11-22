// @vitest-environment jsdom
/**
 * The Settings page names an inbox the server's settings file sets but the
 * tool cannot use, and why, beside the inboxes it reads (review of 27 Oct 2026).
 */
import { cleanup, render, screen } from "@testing-library/react";
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

describe("Settings: numbered OTP inboxes", () => {
  it("says which inbox in the server's settings file is not used, and why", async () => {
    mocks.getSettings.mockResolvedValue(
      settings({
        otp_unused_inboxes: [
          { number: 3, user: "codes@zoho.in", problem: "it names no IMAP host (ORDERING_OTP_IMAP_HOST_3)" },
        ],
      }),
    );
    render(<Settings />);

    expect(await screen.findByText("An inbox in the server's settings file is not used")).toBeTruthy();
    expect(screen.getByText("Inbox 3 (codes@zoho.in)")).toBeTruthy();
    expect(screen.getByText(/ORDERING_OTP_IMAP_HOST_3/)).toBeTruthy();
  });

  it("says nothing of the kind when every inbox is used", async () => {
    mocks.getSettings.mockResolvedValue(settings({ otp_unused_inboxes: [] }));
    render(<Settings />);

    await screen.findByDisplayValue("imap.zoho.in");
    expect(screen.queryByText(/settings file (is|are) not used/)).toBeNull();
  });
});
