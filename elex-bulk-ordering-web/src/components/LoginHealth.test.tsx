// @vitest-environment jsdom
/** The dashboard's Account logins card and the runs' time taken (step G). */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ getLoginHealth: vi.fn() }));
vi.mock("../lib/api", () => ({
  ...mocks,
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

import { LoginHealth } from "./LoginHealth";
import { timeTaken } from "../pages/Dashboard";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Account logins", () => {
  it("shows the share signed in and every status", async () => {
    mocks.getLoginHealth.mockResolvedValue({
      platform: "flipkart",
      total: 4,
      logged_in: 2,
      statuses: [
        { status: "session_valid", count: 2 },
        { status: "logged_out", count: 1 },
        { status: "blocked", count: 1 },
      ],
    });
    render(<LoginHealth />);
    expect(await screen.findByText(/2 of 4 accounts/)).toBeTruthy();
    expect(screen.getByRole("table", { name: "Accounts by login status" }).querySelectorAll("tr").length).toBe(3);
  });

  it("says when the counts could not load", async () => {
    mocks.getLoginHealth.mockRejectedValue(new Error("offline"));
    render(<LoginHealth />);
    expect(await screen.findByText(/Could not load the login counts: offline/)).toBeTruthy();
  });
});

describe("time taken", () => {
  it.each([
    ["2026-11-13T10:00:00Z", "2026-11-13T10:00:58Z", "58s"],
    ["2026-11-13T10:00:00Z", "2026-11-13T10:04:12Z", "4m 12s"],
    ["2026-11-13T10:00:00Z", "2026-11-13T11:03:00Z", "1h 3m"],
  ])("%s to %s is %s", (start, end, expected) => {
    expect(timeTaken({ started_at: start, finished_at: end, status: "completed" })).toBe(expected);
  });

  it("counts a running job so far, and a job never started as a dash", () => {
    const now = Date.parse("2026-11-13T10:00:30Z");
    expect(timeTaken({ started_at: "2026-11-13T10:00:00Z", finished_at: null, status: "running" }, now)).toBe("30s");
    expect(timeTaken({ started_at: "", finished_at: null, status: "queued" })).toBe("—");
  });
});
