// @vitest-environment jsdom
/**
 * The dashboard's "Out for delivery" card rendered for real: each unit with
 * its delivery code, the empty state, and "Read back now" starting a read-back
 * of exactly the accounts holding those orders, then reloading the list.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

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
  vi.clearAllMocks();
});

const unit = {
  unit_pk: 31,
  order_row_id: 14,
  order_id: "OD338980309549917100",
  unit_id: "338980309549917100000",
  position: 1,
  account_id: 2,
  account_email: "two@test.com",
  model: "Sa Rasa Women Floral Print Straight Kurta",
  color: "Blue",
  platform_status: "Out For Delivery",
  delivery_otp: "482713",
  delivery_date: null,
  delivery_address: null,
  delivery_pincode: "226013",
  tracking_id: null,
  synced_at: "2026-10-26T04:30:00",
};

describe("OutForDelivery", () => {
  it("lists each unit out for delivery with its delivery code", async () => {
    mocks.getOutForDelivery.mockResolvedValue({
      total: 2,
      units: [unit, { ...unit, unit_pk: 32, delivery_otp: null, order_id: "OD338980148530712100" }],
      account_ids: [2],
      order_ids: ["OD338980148530712100", "OD338980309549917100"],
      last_read_back: "2026-10-26T04:30:00",
    });
    render(<OutForDelivery refreshKey={0} />);

    expect(await screen.findByText("482713")).toBeTruthy();
    expect(screen.getByText("Out for delivery — 2 units")).toBeTruthy();
    expect(screen.getByText("Not shown by Flipkart yet")).toBeTruthy();
    expect(screen.getAllByText("two@test.com")).toHaveLength(2);
  });

  it("says so when nothing is out for delivery", async () => {
    mocks.getOutForDelivery.mockResolvedValue({ total: 0, units: [], account_ids: [], order_ids: [], last_read_back: null });
    render(<OutForDelivery refreshKey={0} />);

    expect(await screen.findByText("Nothing is out for delivery")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Read back now" })).toBeNull();
  });

  it("reads exactly those accounts back, then reloads the list", async () => {
    mocks.getOutForDelivery.mockResolvedValue({
      total: 1,
      units: [unit],
      account_ids: [2],
      order_ids: [unit.order_id],
      last_read_back: null,
    });
    mocks.startSyncJob.mockResolvedValue({ id: 23 });
    mocks.waitForJob.mockResolvedValue({ id: 23, status: "completed", items: [] });
    render(<OutForDelivery refreshKey={0} />);

    fireEvent.click(await screen.findByRole("button", { name: "Read back now" }));

    await waitFor(() => expect(mocks.getOutForDelivery).toHaveBeenCalledTimes(2));
    expect(mocks.startSyncJob).toHaveBeenCalledWith({ account_ids: [2] });
    expect(mocks.waitForJob).toHaveBeenCalledWith(23, expect.objectContaining({ signal: expect.anything() }));
    expect(await screen.findByText("Read back from Flipkart just now.")).toBeTruthy();
  });

  it("names the accounts the run passed over", async () => {
    mocks.getOutForDelivery.mockResolvedValue({
      total: 1,
      units: [unit],
      account_ids: [2],
      order_ids: [unit.order_id],
      last_read_back: null,
    });
    mocks.startSyncJob.mockResolvedValue({ id: 24 });
    mocks.waitForJob.mockResolvedValue({
      id: 24,
      status: "completed",
      items: [{ account_id: 2, status: "skipped", message: "order run #25 is using this account — read back after that run" }],
    });
    render(<OutForDelivery refreshKey={0} />);

    fireEvent.click(await screen.findByRole("button", { name: "Read back now" }));

    expect(await screen.findByText(/Read back from Flipkart, except 1 account: order run #25 is using this account/)).toBeTruthy();
  });
});
