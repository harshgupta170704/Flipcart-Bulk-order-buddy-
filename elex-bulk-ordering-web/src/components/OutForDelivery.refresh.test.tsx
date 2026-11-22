// @vitest-environment jsdom
/**
 * "Out for delivery" through the dashboard's Refresh (spec FR-049; audit of
 * 27 Oct 2026): from the very frame the page hands the card a new Refresh, the
 * card shows placeholders in place of its list until its own reply lands,
 * then the new list; a Refresh that fails brings the last list back, saying
 * so. The card's own two-minute reload is not the user's Refresh: the list,
 * and the delivery codes someone may be reading out at the door, stay on
 * screen through it.
 */
import { Profiler } from "react";
import { act, cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { OutForDelivery as OutForDeliveryData, OutForDeliveryUnit } from "../lib/types";

const mocks = vi.hoisted(() => ({
  getOutForDelivery: vi.fn(),
  startSyncJob: vi.fn(),
  waitForJob: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  ...mocks,
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

import { OutForDelivery } from "./OutForDelivery";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.resetAllMocks();
});

function list(code: string, model: string): OutForDeliveryData {
  const unit: OutForDeliveryUnit = {
    unit_pk: 31,
    order_row_id: 14,
    order_id: "OD338980309549917100",
    unit_id: "338980309549917100000",
    position: 1,
    account_id: 2,
    account_email: "two@test.com",
    model,
    color: "Blue",
    platform_status: "Out For Delivery",
    delivery_otp: code,
    delivery_date: null,
    delivery_address: null,
    delivery_pincode: "226013",
    tracking_id: null,
    synced_at: "2026-10-27T04:30:00",
  };
  return { total: 1, units: [unit], account_ids: [2], order_ids: [unit.order_id ?? ""], last_read_back: unit.synced_at };
}

const BEFORE = list("482713", "Sa Rasa Women Floral Print Straight Kurta");
const AFTER = list("915204", "vivo T5x 5G");

/** A reply the test answers when it chooses. */
function later<T>() {
  let resolve: (value: T) => void = () => undefined;
  let reject: (reason: Error) => void = () => undefined;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

function card(): HTMLElement {
  return screen.getByRole("heading", { level: 2 }).closest("section") as HTMLElement;
}

interface Showing {
  placeholders: boolean;
  before: boolean;
  after: boolean;
}

/** What the card shows right now. */
function showing(): Showing {
  return {
    placeholders: within(card()).queryByRole("status", { name: "Loading" }) !== null,
    before: screen.queryByText("482713") !== null,
    after: screen.queryByText("915204") !== null,
  };
}

/** Every frame the card has drawn since the test last emptied it, as each showed. */
let frames: Showing[] = [];

beforeEach(() => {
  frames = [];
});

function page(refreshKey: number) {
  return (
    <Profiler id="out-for-delivery" onRender={() => frames.push(showing())}>
      <OutForDelivery refreshKey={refreshKey} />
    </Profiler>
  );
}

const WAITING: Showing = { placeholders: true, before: false, after: false };
const LAST_LIST: Showing = { placeholders: false, before: true, after: false };

describe("Out for delivery during the page's Refresh", () => {
  it("shows placeholders from the Refresh's first frame until its own reply lands, then the new list", async () => {
    const reply = later<OutForDeliveryData>();
    mocks.getOutForDelivery.mockResolvedValueOnce(BEFORE).mockReturnValueOnce(reply.promise);
    const view = render(page(0));
    await screen.findByText("482713");
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("Out for delivery — 1 unit");
    frames = [];

    view.rerender(page(1));

    expect(mocks.getOutForDelivery).toHaveBeenCalledTimes(2);
    expect(frames.length).toBeGreaterThan(0);
    expect(frames).toEqual(frames.map(() => WAITING));
    expect(showing()).toEqual(WAITING);
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("Out for delivery");
    // "Read back now" is a way to act, not a figure: still offered for the units last seen.
    expect(screen.getByRole("button", { name: "Read back now" })).toBeTruthy();

    await act(async () => reply.resolve(AFTER));

    expect(showing()).toEqual({ placeholders: false, before: false, after: true });
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("Out for delivery — 1 unit");
  });

  it("brings the last list back, saying so, when the Refresh fails", async () => {
    const reply = later<OutForDeliveryData>();
    mocks.getOutForDelivery.mockResolvedValueOnce(BEFORE).mockReturnValueOnce(reply.promise);
    const view = render(page(0));
    await screen.findByText("482713");

    view.rerender(page(1));
    expect(showing()).toEqual(WAITING);

    await act(async () => reply.reject(new Error("Server unreachable")));

    expect(showing()).toEqual(LAST_LIST);
    expect(screen.getByText("Showing the last list that loaded — the refresh failed: Server unreachable")).toBeTruthy();
  });

  it("keeps the list on screen through its own two-minute reload", async () => {
    // Only the intervals are faked: the card's reload timer.
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const reload = later<OutForDeliveryData>();
    mocks.getOutForDelivery.mockResolvedValueOnce(BEFORE).mockReturnValueOnce(reload.promise);
    render(page(0));
    await screen.findByText("482713");
    frames = [];

    act(() => {
      vi.advanceTimersByTime(120_000);
    });

    expect(mocks.getOutForDelivery).toHaveBeenCalledTimes(2);
    expect(frames.length).toBeGreaterThan(0);
    expect(frames).toEqual(frames.map(() => LAST_LIST));
    expect(showing()).toEqual(LAST_LIST);

    await act(async () => reload.resolve(AFTER));
    expect(showing()).toEqual({ placeholders: false, before: false, after: true });
  });
});
