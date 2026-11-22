/**
 * Dates typed by hand stay the operator's own range (review of 27 Oct 2026):
 * only a preset picked from the Range select is shown and kept as one. Typed
 * dates that happen to equal a preset today used to be read as that preset,
 * so Quick Save kept "Last 7 days" and the view reopened on another week.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { DATE_PRESETS, presetRange, restoredFilters, shownPreset, viewFilters } from "./orderView";

/** Saturday 24 Oct 2026, and ten days later, in the next month. */
const SAVED_ON = new Date(2026, 9, 24, 15, 30);
const LATER_DAY = new Date(2026, 10, 3, 9, 0);

afterEach(() => {
  vi.useRealTimers();
});

describe("dates typed by hand", () => {
  it.each(DATE_PRESETS)("equal to %s today: Quick Save keeps the dates, and a later day restores them", (preset) => {
    vi.setSystemTime(SAVED_ON);
    const typed = presetRange(preset); // the dates alone: nothing was picked

    const kept = viewFilters(typed);
    expect(kept).toEqual(typed);
    expect(kept).not.toHaveProperty("date_preset");

    vi.setSystemTime(LATER_DAY);
    expect(restoredFilters(kept)).toEqual(typed);
  });

  it.each(DATE_PRESETS)("equal to %s today: the Range select says Custom range", (preset) => {
    vi.setSystemTime(SAVED_ON);
    expect(shownPreset(presetRange(preset))).toBe("custom");
  });

  it("typed over a pick, back to the pick's own dates, stay the dates", () => {
    vi.setSystemTime(SAVED_ON);
    // Typing clears the pick (OrderUnitsFilters sets date_preset to null).
    const retyped = { ...presetRange("7d"), date_preset: null };

    expect(shownPreset(retyped)).toBe("custom");
    expect(viewFilters(retyped)).toEqual(presetRange("7d"));
  });

  it("a view saved with dates only reopens as a custom range on a day they equal a preset", () => {
    vi.setSystemTime(SAVED_ON);
    const restored = restoredFilters({ date_from: "2026-10-01", date_to: "2026-10-24" });

    expect(restored).toEqual({ date_from: "2026-10-01", date_to: "2026-10-24" });
    expect(shownPreset(restored)).toBe("custom");
  });
});

describe("what the Range select still shows", () => {
  it("Any time without dates, and a picked preset while its dates are today's", () => {
    vi.setSystemTime(SAVED_ON);

    expect(shownPreset({ date_from: null, date_to: null })).toBe("any");
    expect(shownPreset({ date_from: null, date_to: null, date_preset: "7d" })).toBe("any");
    for (const preset of DATE_PRESETS) {
      expect(shownPreset({ ...presetRange(preset), date_preset: preset })).toBe(preset);
    }
    expect(shownPreset({ date_from: "2026-10-18", date_to: null })).toBe("custom");
  });
});
