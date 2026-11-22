// @vitest-environment jsdom
/**
 * "To be delivered" while a new load is on its way (spec FR-049; audit of 27
 * Oct 2026). From the very frame the page hands the card a new Refresh, the
 * card shows placeholders in place of its figures until its own reply lands,
 * then the new figures; a Refresh that fails brings the last figures back,
 * saying so. And from the very frame "Ordered 15+ days ago" flips, the card
 * shows placeholders — not one frame of "Could not load" before the request
 * has even been sent.
 */
import { Profiler } from "react";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ToBeDelivered as ToBeDeliveredData } from "../lib/types";

const mocks = vi.hoisted(() => ({
  getToBeDelivered: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  ...mocks,
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

import { ToBeDelivered } from "./ToBeDelivered";

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

const BEFORE: ToBeDeliveredData = {
  total_units: 5,
  total_orders: 4,
  older_than_days: null,
  models: [{ brand: "vivo", model: "T5x 5G", units: 5, orders: 4, oldest_ordered_at: "2026-10-01T10:00:00" }],
};
const AFTER: ToBeDeliveredData = {
  total_units: 7,
  total_orders: 6,
  older_than_days: null,
  models: [{ brand: "Samsung", model: "Galaxy M36", units: 7, orders: 6, oldest_ordered_at: "2026-10-20T10:00:00" }],
};

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

/** The card itself: the section its heading titles. */
function card(): HTMLElement {
  return screen.getByRole("heading", { level: 2 }).closest("section") as HTMLElement;
}

interface Showing {
  placeholders: boolean;
  before: boolean;
  after: boolean;
  failed: boolean;
}

/** What the card shows right now. */
function showing(): Showing {
  return {
    placeholders: within(card()).queryByRole("status", { name: "Loading" }) !== null,
    before: screen.queryByText("vivo T5x 5G") !== null || screen.queryByText(/5 order units/) !== null,
    after: screen.queryByText("Samsung Galaxy M36") !== null,
    failed: screen.queryByText("Could not load what is still to be delivered") !== null,
  };
}

/** Every frame the card has drawn since the test last emptied it, as each showed. */
let frames: Showing[] = [];

beforeEach(() => {
  frames = [];
});

function page(refreshKey: number) {
  return (
    <MemoryRouter>
      <Profiler id="to-be-delivered" onRender={() => frames.push(showing())}>
        <ToBeDelivered refreshKey={refreshKey} />
      </Profiler>
    </MemoryRouter>
  );
}

const WAITING: Showing = { placeholders: true, before: false, after: false, failed: false };

describe("To be delivered while a new load is on its way", () => {
  it("shows placeholders from the Refresh's first frame until its own reply lands, then the new figures", async () => {
    const reply = later<ToBeDeliveredData>();
    mocks.getToBeDelivered.mockResolvedValueOnce(BEFORE).mockReturnValueOnce(reply.promise);
    const view = render(page(0));
    await screen.findByText("vivo T5x 5G");
    frames = [];

    view.rerender(page(1));

    expect(mocks.getToBeDelivered).toHaveBeenCalledTimes(2);
    expect(frames.length).toBeGreaterThan(0);
    expect(frames).toEqual(frames.map(() => WAITING));
    expect(showing()).toEqual(WAITING);
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("To be delivered");

    await act(async () => reply.resolve(AFTER));

    expect(showing()).toEqual({ placeholders: false, before: false, after: true, failed: false });
    expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("To be delivered — 7 order units across 1 model");
  });

  it("brings the last figures back, saying so, when the Refresh fails", async () => {
    const reply = later<ToBeDeliveredData>();
    mocks.getToBeDelivered.mockResolvedValueOnce(BEFORE).mockReturnValueOnce(reply.promise);
    const view = render(page(0));
    await screen.findByText("vivo T5x 5G");

    view.rerender(page(1));
    expect(showing()).toEqual(WAITING);

    await act(async () => reply.reject(new Error("Server unreachable")));

    expect(showing()).toEqual({ placeholders: false, before: true, after: false, failed: false });
    expect(screen.getByText("Showing the last figures that loaded — the refresh failed: Server unreachable")).toBeTruthy();
  });

  it("shows placeholders from the first frame after the 15+ days toggle, never a moment of Could not load", async () => {
    const reply = later<ToBeDeliveredData>();
    mocks.getToBeDelivered.mockResolvedValueOnce(BEFORE).mockReturnValueOnce(reply.promise);
    render(page(0));
    await screen.findByText("vivo T5x 5G");
    frames = [];

    fireEvent.click(screen.getByLabelText("Ordered 15+ days ago"));

    expect(mocks.getToBeDelivered).toHaveBeenLastCalledWith(15);
    expect(frames.length).toBeGreaterThan(0);
    expect(frames).toEqual(frames.map(() => WAITING));

    await act(async () => reply.resolve({ ...AFTER, older_than_days: 15 }));
    expect(showing()).toEqual({ placeholders: false, before: false, after: true, failed: false });
  });
});
