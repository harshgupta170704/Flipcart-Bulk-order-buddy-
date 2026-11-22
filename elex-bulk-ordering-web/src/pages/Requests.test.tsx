// @vitest-environment jsdom
/**
 * The Requests page rendered for real: the open queue from the API, and
 * "Mark done" sending the row's id, the new status and the note to the server.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { UnitRequest } from "../lib/types";

const mocks = vi.hoisted(() => ({
  listUnitRequests: vi.fn(),
  updateUnitRequest: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  listUnitRequests: mocks.listUnitRequests,
  updateUnitRequest: mocks.updateUnitRequest,
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

import Requests from "./Requests";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function row(over: Partial<UnitRequest> = {}): UnitRequest {
  return {
    id: 5,
    unit_pk: 12,
    kind: "cancel",
    reason: "Ordered twice by mistake",
    requested_for: null,
    status: "open",
    requested_by: "ops@test.com",
    requested_at: "2026-10-25T09:00:00",
    closed_by: null,
    closed_at: null,
    note: null,
    order_row_id: 3,
    order_id: "OD100000000000000002",
    unit_id: "U-SHIPPED",
    product: "vivo T5x 5G (Glossy Black, 128 GB)",
    platform_status: "Shipped",
    delivery_state: "in_transit",
    account_email: "units@test.com",
    ...over,
  };
}

describe("Requests page", () => {
  it("shows the open requests and marks one done through the API", async () => {
    mocks.listUnitRequests.mockResolvedValue({
      items: [row(), row({ id: 6, kind: "reschedule", reason: null, requested_for: "2026-11-02", order_id: "OD100000000000000003" })],
      total: 2,
    });
    mocks.updateUnitRequest.mockResolvedValue(row({ status: "done", closed_by: "tester", closed_at: "2026-10-25T10:00:00" }));

    render(
      <MemoryRouter>
        <Requests />
      </MemoryRouter>,
    );

    await screen.findByText("OD100000000000000002");
    expect(mocks.listUnitRequests.mock.calls[0][0]).toMatchObject({ status: "open", page: 1 });
    expect(screen.getByText("Ordered twice by mistake")).toBeTruthy();
    expect(screen.getAllByText("ops@test.com")).toHaveLength(2);
    expect(screen.getByText(/New date:/)).toBeTruthy();
    expect(screen.getAllByText("Open").length).toBeGreaterThanOrEqual(2);

    fireEvent.click(screen.getAllByRole("button", { name: "Mark done" })[0]);
    fireEvent.change(screen.getByLabelText(/Note/), { target: { value: "Cancelled on the app" } });
    fireEvent.click(screen.getByRole("button", { name: "Mark it done" }));

    await waitFor(() => expect(mocks.updateUnitRequest).toHaveBeenCalledWith(5, "done", "Cancelled on the app"));
    await screen.findByText(/Marked done: cancel OD100000000000000002/);
    // The list is read again so the row leaves the open queue.
    expect(mocks.listUnitRequests.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it("asks the server for the chosen status and offers Reopen on a closed row", async () => {
    mocks.listUnitRequests.mockResolvedValue({ items: [], total: 0 });

    render(
      <MemoryRouter>
        <Requests />
      </MemoryRouter>,
    );
    await screen.findByText("No open requests");
    expect(screen.getByText("Select units in Order Units and choose Cancel or Reschedule.")).toBeTruthy();

    mocks.listUnitRequests.mockResolvedValue({
      items: [row({ status: "done", closed_by: "tester", closed_at: "2026-10-25T10:00:00", note: "Cancelled on the app" })],
      total: 1,
    });
    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "done" } });

    await waitFor(() =>
      expect(mocks.listUnitRequests).toHaveBeenLastCalledWith(expect.objectContaining({ status: "done", page: 1 })),
    );
    await screen.findByRole("button", { name: "Reopen" });
    expect(screen.queryByRole("button", { name: "Mark done" })).toBeNull();

    mocks.updateUnitRequest.mockResolvedValue(row());
    fireEvent.click(screen.getByRole("button", { name: "Reopen" }));
    await waitFor(() => expect(mocks.updateUnitRequest).toHaveBeenCalledWith(5, "open"));
  });
});
