/** The saved view both ways: what Quick Save keeps, what a restore may trust. */
import { afterEach, describe, expect, it, vi } from "vitest";
import { presetRange, restoredFilters, shownPreset, viewFilters } from "./orderView";

describe("viewFilters", () => {
  it("keeps what narrows the list and drops the run deep link and empties", () => {
    expect(
      viewFilters({
        q: " vivo ",
        status: "placed",
        job_id: 42,
        order_ids: [],
        date_field: "placed_at",
        date_from: null,
      }),
    ).toEqual({ q: "vivo", status: "placed" });
  });

  it("keeps a non-default date field and a range", () => {
    expect(viewFilters({ date_field: "synced_at", date_from: "2026-10-01", date_to: "2026-10-24" })).toEqual({
      date_field: "synced_at",
      date_from: "2026-10-01",
      date_to: "2026-10-24",
    });
  });

  it("keeps the card filter as typed, trimmed, and drops a blank one", () => {
    expect(viewFilters({ card: "  4111 1111 " })).toEqual({ card: "4111 1111" });
    expect(viewFilters({ card: "   " })).toEqual({});
  });
});

describe("restoredFilters", () => {
  it("accepts only what the page can apply", () => {
    expect(
      restoredFilters({
        q: "x",
        status: ["placed"],
        platform_statuses: ["Out for delivery"],
        processing_stages: ["Card OTP Pending"],
        error_codes: ["card_otp_incorrect_or_unfetched"],
        delivery_states: ["unknown", "bogus"],
        date_field: "synced_at",
        date_from: "2026-10-01",
        date_to: "garbage",
        job_id: 42,
        mystery: 1,
      }),
    ).toEqual({
      q: "x",
      status: "placed",
      platform_statuses: ["Out for delivery"],
      processing_stages: ["Card OTP Pending"],
      error_codes: ["card_otp_incorrect_or_unfetched"],
      delivery_states: ["unknown"],
      date_field: "synced_at",
      date_from: "2026-10-01",
    });
  });

  it("turns several outcomes into the list form the API repeats", () => {
    expect(restoredFilters({ status: "placed", statuses: ["failed", "placed"] })).toEqual({
      statuses: ["placed", "failed"],
    });
  });

  it("puts back the cancelled-on-Flipkart outcome, apart from Failed", () => {
    expect(restoredFilters({ status: "cancelled" })).toEqual({ status: "cancelled" });
    expect(restoredFilters({ statuses: ["failed", "cancelled", "refunded"] })).toEqual({
      statuses: ["failed", "cancelled"],
    });
  });

  it("puts a saved card filter back as text and drops anything else under that name", () => {
    // Round trip: what Quick Save kept is what a restore applies.
    expect(restoredFilters(viewFilters({ card: " 4111 1111 " }))).toEqual({ card: "4111 1111" });
    expect(restoredFilters({ card: "  " })).toEqual({});
    expect(restoredFilters({ card: 4111 })).toEqual({});
    expect(restoredFilters({ card: ["4111"] })).toEqual({});
    // Longer than the server accepts (32): a view saved before that limit
    // existed must not break every load until Default View.
    expect(restoredFilters({ card: "4".repeat(33) })).toEqual({});
    expect(restoredFilters({ card: "4".repeat(32) })).toEqual({ card: "4".repeat(32) });
  });

  it("is empty for anything that is not an object", () => {
    expect(restoredFilters(null)).toEqual({});
    expect(restoredFilters("nope")).toEqual({});
    expect(restoredFilters([1, 2])).toEqual({});
  });
});

describe("a relative date range in the saved view (spec FR-011)", () => {
  // The clock is faked: saved on Saturday 24 Oct 2026, reopened ten days
  // later, in the next month. Only Date is faked; timers stay real.
  const savedOn = new Date(2026, 9, 24, 15, 30);
  const laterDay = new Date(2026, 10, 3, 9, 0);

  afterEach(() => {
    vi.useRealTimers();
  });

  it("keeps Last 7 days as such, and reopens on the 7 days before the later day", () => {
    vi.setSystemTime(savedOn);
    const kept = viewFilters({ date_field: "placed_at", ...presetRange("7d"), date_preset: "7d" });
    expect(kept).toEqual({ date_from: "2026-10-18", date_to: "2026-10-24", date_preset: "7d" });

    vi.setSystemTime(laterDay);
    expect(restoredFilters(kept)).toEqual({ date_from: "2026-10-28", date_to: "2026-11-03", date_preset: "7d" });
  });

  it("works Last 30 days and This month out again too", () => {
    vi.setSystemTime(savedOn);
    const thirty = viewFilters({ ...presetRange("30d"), date_preset: "30d" });
    const month = viewFilters({ ...presetRange("month"), date_preset: "month" });
    expect(thirty).toEqual({ date_from: "2026-09-25", date_to: "2026-10-24", date_preset: "30d" });
    expect(month).toEqual({ date_from: "2026-10-01", date_to: "2026-10-24", date_preset: "month" });

    vi.setSystemTime(laterDay);
    expect(restoredFilters(thirty)).toEqual({ date_from: "2026-10-05", date_to: "2026-11-03", date_preset: "30d" });
    expect(restoredFilters(month)).toEqual({ date_from: "2026-11-01", date_to: "2026-11-03", date_preset: "month" });
  });

  it("keeps a range typed by hand as its fixed dates", () => {
    vi.setSystemTime(savedOn);
    const kept = viewFilters({ date_from: "2026-10-02", date_to: "2026-10-24" });
    expect(kept).toEqual({ date_from: "2026-10-02", date_to: "2026-10-24" });

    vi.setSystemTime(laterDay);
    expect(restoredFilters(kept)).toEqual({ date_from: "2026-10-02", date_to: "2026-10-24" });
  });

  it("keeps the preset picked when two share their dates: This month on the 7th", () => {
    vi.setSystemTime(new Date(2026, 9, 7, 12, 0));
    expect(presetRange("7d")).toEqual(presetRange("month")); // the same seven days
    const month = { ...presetRange("month"), date_preset: "month" as const };
    expect(shownPreset(month)).toBe("month");
    const kept = viewFilters(month);
    expect(kept.date_preset).toBe("month");

    vi.setSystemTime(laterDay);
    expect(restoredFilters(kept)).toMatchObject({ date_from: "2026-11-01", date_to: "2026-11-03" });
  });

  it("saves fixed dates once the picked preset no longer means them, as the Range select then says", () => {
    vi.setSystemTime(savedOn);
    const picked = { ...presetRange("7d"), date_preset: "7d" as const };
    // The page was left open overnight: yesterday's seven days are not today's.
    vi.setSystemTime(new Date(2026, 9, 25, 9, 0));
    expect(shownPreset(picked)).toBe("custom");
    expect(viewFilters(picked)).toEqual({ date_from: "2026-10-18", date_to: "2026-10-24" });
  });

  it("drops a preset it cannot read and keeps the dates saved beside it", () => {
    vi.setSystemTime(laterDay);
    expect(restoredFilters({ date_preset: "yesterday", date_from: "2026-10-18", date_to: "2026-10-24" })).toEqual({
      date_from: "2026-10-18",
      date_to: "2026-10-24",
    });
    expect(restoredFilters({ date_preset: 7 })).toEqual({});
    expect(restoredFilters({ date_preset: ["7d"] })).toEqual({});
  });
});
