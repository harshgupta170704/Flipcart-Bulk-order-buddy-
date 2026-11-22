/** The Range presets are computed when chosen; both ends are inclusive days. */
import { describe, expect, it } from "vitest";
import { presetOf, presetRange } from "./OrderUnitsFilters";

// 24 Oct 2026, 15:30 local time — the time of day must not matter.
const now = new Date(2026, 9, 24, 15, 30);

describe("presetRange", () => {
  it("counts the last 7 days inclusive of today", () => {
    expect(presetRange("7d", now)).toEqual({ date_from: "2026-10-18", date_to: "2026-10-24" });
  });

  it("counts the last 30 days inclusive of today", () => {
    expect(presetRange("30d", now)).toEqual({ date_from: "2026-09-25", date_to: "2026-10-24" });
  });

  it("starts this month on the 1st", () => {
    expect(presetRange("month", now)).toEqual({ date_from: "2026-10-01", date_to: "2026-10-24" });
  });

  it("clears the range for any time, and leaves custom to the date inputs", () => {
    expect(presetRange("any", now)).toEqual({ date_from: null, date_to: null });
    expect(presetRange("custom", now)).toEqual({ date_from: null, date_to: null });
  });
});

describe("presetOf", () => {
  it("is derived from the dates, so a reset can never leave the select stale", () => {
    expect(presetOf({ date_from: null, date_to: null }, now)).toBe("any");
    expect(presetOf(presetRange("7d", now), now)).toBe("7d");
    expect(presetOf(presetRange("month", now), now)).toBe("month");
    expect(presetOf({ date_from: "2026-10-02", date_to: "2026-10-24" }, now)).toBe("custom");
    expect(presetOf({ date_from: "2026-10-02", date_to: null }, now)).toBe("custom");
  });
});
