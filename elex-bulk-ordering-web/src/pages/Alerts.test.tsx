// @vitest-environment jsdom
/**
 * The dashboard links each alert kind to /alerts?kind=<kind>. The page must
 * read that and ask the server for that slice — a link that lands on the full
 * queue promises a filter it does not apply.
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  listAlerts: vi.fn(),
  alertsSummary: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  listAlerts: mocks.listAlerts,
  alertsSummary: mocks.alertsSummary,
  evaluateAlerts: vi.fn(),
  reopenAlert: vi.fn(),
  resolveAlert: vi.fn(),
  errorMessage: (err: unknown) => String(err),
}));

import Alerts from "./Alerts";

afterEach(cleanup);

describe("Alerts page", () => {
  it("reads ?kind= from the URL and asks the server for that slice", async () => {
    mocks.listAlerts.mockResolvedValue([]);
    mocks.alertsSummary.mockResolvedValue({
      open: 0,
      resolved: 0,
      by_severity: { critical: 0, warning: 0, info: 0 },
      by_kind: {},
    });

    render(
      <MemoryRouter initialEntries={["/alerts?kind=delivery_overdue"]}>
        <Alerts />
      </MemoryRouter>,
    );

    await waitFor(() => expect(mocks.listAlerts).toHaveBeenCalled());
    expect(mocks.listAlerts.mock.calls[0][0]).toMatchObject({ kind: "delivery_overdue", resolved: false });
    const select = screen.getByLabelText("Kind") as HTMLSelectElement;
    expect(select.value).toBe("delivery_overdue");
  });
});
