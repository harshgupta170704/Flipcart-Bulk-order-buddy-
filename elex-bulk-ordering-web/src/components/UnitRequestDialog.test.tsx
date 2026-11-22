// @vitest-environment jsdom
/**
 * The Cancel / Reschedule Order Units dialog: a cancel cannot be recorded
 * until a reason is typed, a reschedule until a date is chosen, and the
 * submit posts exactly the selected unit ids; the outcome is read back into
 * the dialog with each skipped unit named.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  createUnitRequests: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  createUnitRequests: mocks.createUnitRequests,
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

import { UnitRequestDialog, outcomeLine } from "./UnitRequestDialog";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const units = [
  { id: 3, order_id: "OD100000000000000001" },
  { id: 5, order_id: "OD100000000000000002", unit_position: 2, units_in_order: 3 },
];

describe("UnitRequestDialog", () => {
  it("keeps a cancel disabled until a reason is typed, then posts the selected ids", async () => {
    mocks.createUnitRequests.mockResolvedValue({
      created: [{ id: 1, unit_pk: 3 }],
      skipped: [{ unit_id: 5, why: "already delivered" }],
    });
    const onRecorded = vi.fn();
    render(<UnitRequestDialog draft={{ kind: "cancel", units }} onClose={vi.fn()} onRecorded={onRecorded} />);

    expect(screen.getByText(/2 units selected/)).toBeTruthy();
    const submit = screen.getByRole("button", { name: "Record cancel request" }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);

    fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: "  Ordered twice  " } });
    expect(submit.disabled).toBe(false);
    fireEvent.click(submit);

    await waitFor(() =>
      expect(mocks.createUnitRequests).toHaveBeenCalledWith({ unit_ids: [3, 5], kind: "cancel", reason: "Ordered twice" }),
    );
    await screen.findByText("1 request recorded; 1 skipped (already delivered).");
    expect(screen.getByText("OD100000000000000002 (unit 2 of 3)")).toBeTruthy();
    expect(onRecorded).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "Record cancel request" })).toBeNull();
  });

  it("keeps a reschedule disabled until a date is chosen and sends it", async () => {
    mocks.createUnitRequests.mockResolvedValue({ created: [{ id: 1, unit_pk: 3 }, { id: 2, unit_pk: 5 }], skipped: [] });
    render(<UnitRequestDialog draft={{ kind: "reschedule", units }} onClose={vi.fn()} />);

    const submit = screen.getByRole("button", { name: "Record reschedule request" }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: "Office shut" } });
    expect(submit.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText(/New delivery date/), { target: { value: "2099-11-02" } });
    expect(submit.disabled).toBe(false);
    fireEvent.click(submit);

    await waitFor(() =>
      expect(mocks.createUnitRequests).toHaveBeenCalledWith({
        unit_ids: [3, 5],
        kind: "reschedule",
        reason: "Office shut",
        requested_for: "2099-11-02",
      }),
    );
    await screen.findByText("2 requests recorded.");
  });

  it("keeps the dialog and the reason when the server refuses", async () => {
    mocks.createUnitRequests.mockRejectedValue(new Error("a cancel request needs a reason"));
    render(<UnitRequestDialog draft={{ kind: "cancel", units }} onClose={vi.fn()} />);
    fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Record cancel request" }));

    await screen.findByText("a cancel request needs a reason");
    expect((screen.getByLabelText(/Reason/) as HTMLTextAreaElement).value).toBe("x");
  });

  it("words the outcome by count and groups the reasons", () => {
    expect(outcomeLine({ created: [], skipped: [] })).toBe("0 requests recorded.");
    expect(
      outcomeLine({
        created: [],
        skipped: [
          { unit_id: 1, why: "already delivered" },
          { unit_id: 2, why: "already delivered" },
          { unit_id: 3, why: "no such unit" },
        ],
      }),
    ).toBe("0 requests recorded; 3 skipped (already delivered ×2; no such unit).");
  });
});
