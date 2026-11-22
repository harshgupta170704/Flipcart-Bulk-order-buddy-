/** Timestamps from the API are UTC without an offset; the page must say so. */
import { describe, expect, it } from "vitest";
import { DASH, formatCurrency, formatDate, formatDateTime, formatSeconds, formatSpent } from "./format";

describe("money (NFR-010)", () => {
  it("is rupees with the ₹ sign, Indian grouping and two decimals", () => {
    expect(formatCurrency(89447)).toBe("₹89,447.00");
    expect(formatCurrency(129900)).toBe("₹1,29,900.00");
    expect(formatCurrency(0)).toBe("₹0.00");
    expect(formatCurrency(0.5)).toBe("₹0.50");
    expect(formatCurrency(18207)).toBe("₹18,207.00");
    expect(formatCurrency(12345678.9)).toBe("₹1,23,45,678.90");
    expect(formatCurrency(-129900)).toBe("-₹1,29,900.00");
  });

  it("rounds a half paisa away from zero, as the server's own messages do", () => {
    // backend/app/core/money.py rupees() gives the same for these.
    expect(formatCurrency(1.005)).toBe("₹1.01");
    expect(formatCurrency(0.125)).toBe("₹0.13");
    expect(formatCurrency(18206.995)).toBe("₹18,207.00");
  });

  it("shows a dash, not ₹0.00, when there is no amount", () => {
    expect(formatCurrency(null)).toBe(DASH);
    expect(formatCurrency(undefined)).toBe(DASH);
    expect(formatCurrency(Number.NaN)).toBe(DASH);
  });
});

describe("time a run spent on an account", () => {
  it("renders seconds the way the run history reads them", () => {
    expect(formatSeconds(14.3)).toBe("14s");
    expect(formatSeconds(102)).toBe("1m 42s");
    expect(formatSeconds(3_725)).toBe("1h 2m");
    expect(formatSeconds(-5)).toBe("0s");
  });

  it("prefers the server's seconds, falls back to the two stamps, and says nothing while running", () => {
    expect(formatSpent({ status: "done", seconds: 14.3 })).toBe("14s");
    expect(
      formatSpent({
        status: "failed",
        seconds: null,
        started_at: "2026-10-26T04:30:00Z",
        finished_at: "2026-10-26T04:31:42Z",
      }),
    ).toBe("1m 42s");
    expect(formatSpent({ status: "skipped", seconds: null, started_at: null, finished_at: "2026-10-26T04:30:00Z" })).toBeNull();
    // A skip the worker had marked started before its gates: still no time.
    expect(
      formatSpent({ status: "skipped", seconds: 0, started_at: "2026-10-26T04:30:00Z", finished_at: "2026-10-26T04:30:00Z" }),
    ).toBeNull();
    expect(formatSpent({ status: "running", seconds: 3, started_at: "2026-10-26T04:30:00Z" })).toBeNull();
    expect(formatSpent({ status: "pending" })).toBeNull();
  });
});

describe("timestamps from the API", () => {
  it("reads an offset-less timestamp as UTC, whatever the browser's zone", () => {
    expect(formatDateTime("2026-10-24T04:40:15")).toBe(formatDateTime("2026-10-24T04:40:15Z"));
    expect(formatDateTime("2026-10-24T04:40:15.123456")).toBe(formatDateTime("2026-10-24T04:40:15Z"));
    expect(formatDateTime("2026-10-24T04:40:15+05:30")).not.toBe(formatDateTime("2026-10-24T04:40:15Z"));
  });

  it("renders the IST wall clock, whatever the browser's zone", () => {
    // The page writes India Standard Time in every browser now (see
    // format.indiaTime.test.ts), so this no longer waits for an IST machine.
    expect(formatDateTime("2026-10-24T04:40:15")).toBe("Oct 24, 2026 10:10");
    // 20:30 UTC on the 23rd is 02:00 IST on the 24th — the CSV's date.
    expect(formatDate("2026-10-23T20:30:00")).toBe("Oct 24, 2026");
  });
});
