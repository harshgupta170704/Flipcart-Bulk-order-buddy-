// @vitest-environment jsdom
/** The Agents page (step H). */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ getAgents: vi.fn() }));
vi.mock("../lib/api", () => ({
  ...mocks,
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

import Agents from "./Agents";

const API = { online: true, max_concurrency: 4, contexts_per_browser: 8, jobs_running: 1, jobs_queued: 0 };

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Agents", () => {
  it("shows the API online and a queue that is not set up", async () => {
    mocks.getAgents.mockResolvedValue({ api: API, queue: { configured: false, reachable: null, health: null, error: null } });
    render(<Agents />);
    expect(await screen.findByText("Online")).toBeTruthy();
    expect(screen.getByText("Not set up")).toBeTruthy();
  });

  it("shows a queue that does not answer", async () => {
    mocks.getAgents.mockResolvedValue({ api: API, queue: { configured: true, reachable: false, health: null, error: "ConnectionError" } });
    render(<Agents />);
    expect(await screen.findByText("Queue unreachable")).toBeTruthy();
  });

  it("says when the API cannot be reached", async () => {
    mocks.getAgents.mockRejectedValue(new Error("502 Bad Gateway"));
    render(<Agents />);
    expect(await screen.findByText("Could not reach the API")).toBeTruthy();
  });
});
