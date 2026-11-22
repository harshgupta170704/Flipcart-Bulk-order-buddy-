/**
 * Dates and times read as India Standard Time, written one way, in every
 * browser (spec, Assumptions: "Times are India Standard Time. Screens show
 * dates like "Oct 21, 2026" and 24-hour times like "13:55""; US3 scenario 2:
 * "last modified Oct 21, 2026 15:20"). The page used to follow the browser:
 * 12-hour times with AM/PM on an American English machine, the day and hour
 * of whatever zone the machine was set to, and the order and spelling of
 * whatever language it was set to ("21 Oct 2026, 03:20 pm" in Indian English).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { formatDate, formatDateTime, formatTime } from "./format";

afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

/**
 * Zones a viewer's machine might be set to, each with its offset on 20 Oct
 * 2026 as getTimezoneOffset counts it (minutes behind UTC), so a test cannot
 * pass only because the zone never changed: India, UTC, a zone behind UTC
 * and one ahead of India.
 */
const ZONES: [string, number][] = [
  ["Asia/Kolkata", -330],
  ["UTC", 0],
  ["America/New_York", 240],
  ["Pacific/Kiritimati", -840],
];

describe.each(ZONES)("in a browser set to %s", (zone, offset) => {
  beforeEach(() => {
    // Node takes a new TZ at once, for Date and Intl alike.
    vi.stubEnv("TZ", zone);
    expect(new Date("2026-10-20T20:00:00Z").getTimezoneOffset()).toBe(offset);
  });

  it("writes a moment as the spec's screens do, on a 24-hour clock", () => {
    // The API's offset-less UTC: 09:50 UTC is 15:20 in India.
    expect(formatDateTime("2026-10-21T09:50:00")).toBe("Oct 21, 2026 15:20");
    expect(formatDateTime("2026-10-21T06:30:00")).toBe("Oct 21, 2026 12:00");
    // Midnight is 00:00, not 12:00 AM, and not 24:00.
    expect(formatDateTime("2026-10-20T18:30:00")).toBe("Oct 21, 2026 00:00");
  });

  it("puts a moment on India's day, not the browser's", () => {
    // 20:00 UTC on the 20th is 01:30 on the 21st in India.
    expect(formatDateTime("2026-10-20T20:00:00Z")).toBe("Oct 21, 2026 01:30");
    expect(formatDate("2026-10-20T20:00:00Z")).toBe("Oct 21, 2026");
  });

  it("keeps a day on its own day", () => {
    // A requested delivery day comes as a bare date and a promised one as
    // midnight UTC; west of UTC the browser's own zone made both the day before.
    expect(formatDate("2026-11-02")).toBe("Nov 02, 2026");
    expect(formatDate("2026-11-02T00:00:00")).toBe("Nov 02, 2026");
  });

  it("writes a live log's time on the same 24-hour Indian clock", () => {
    expect(formatTime("2026-10-21T09:50:02Z")).toBe("15:20:02");
    expect(formatTime("2026-10-20T18:30:05")).toBe("00:00:05");
  });
});

/**
 * A browser set to another language: whatever formats a date without naming a
 * language of its own gets that one, as it does in such a browser.
 */
function setBrowserLanguage(language: string): void {
  const RealDateTimeFormat = Intl.DateTimeFormat;
  vi.spyOn(Intl, "DateTimeFormat").mockImplementation(function (
    locales?: Intl.LocalesArgument,
    options?: Intl.DateTimeFormatOptions,
  ) {
    return new RealDateTimeFormat(locales ?? language, options);
  });
  for (const method of ["toLocaleString", "toLocaleDateString", "toLocaleTimeString"] as const) {
    const real = Date.prototype[method] as (locales?: Intl.LocalesArgument, options?: Intl.DateTimeFormatOptions) => string;
    vi.spyOn(Date.prototype, method).mockImplementation(function (
      this: Date,
      locales?: Intl.LocalesArgument,
      options?: Intl.DateTimeFormatOptions,
    ) {
      return real.call(this, locales ?? language, options);
    });
  }
}

// Indian English (day first, "pm"), British English (day first, a 24-hour
// clock already) and Hindi (the month in Devanagari): languages a machine in
// India is often set to.
describe.each(["en-IN", "en-GB", "hi-IN"])("in a browser whose language is %s", (language) => {
  beforeEach(() => {
    setBrowserLanguage(language);
    // The stand-in took: with no language named, a date now comes out in this
    // one, which writes a day and month in another order than American English.
    const jan1 = new Date("1970-01-01T00:00:00Z");
    const dayAndMonth = { day: "2-digit", month: "short", timeZone: "UTC" } as const;
    expect(jan1.toLocaleDateString(undefined, dayAndMonth)).toBe(jan1.toLocaleDateString(language, dayAndMonth));
    expect(new Intl.DateTimeFormat(undefined, dayAndMonth).format(jan1)).not.toBe("Jan 01");
  });

  it("still writes the spec's English month, day and 24-hour clock", async () => {
    // Loaded afresh, so whatever the module builds when it loads is built in
    // this browser too.
    vi.resetModules();
    const fresh = await import("./format");
    expect(fresh.formatDateTime("2026-10-21T09:50:00")).toBe("Oct 21, 2026 15:20");
    expect(fresh.formatDate("2026-11-02")).toBe("Nov 02, 2026");
    expect(fresh.formatTime("2026-10-21T09:50:02Z")).toBe("15:20:02");
  });
});
