// @vitest-environment jsdom
/**
 * The reset page reads its code from after the link's '#' (a browser never
 * sends that part, so no access log can keep it), or from after the '?' of a
 * link mailed before that move, and clears it from the address bar once read.
 */
import { StrictMode } from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  resetPassword: vi.fn(),
  setToken: vi.fn(),
  me: vi.fn(),
  applySession: vi.fn(),
}));

vi.mock("../lib/api", () => ({
  resetPassword: mocks.resetPassword,
  setToken: mocks.setToken,
  me: mocks.me,
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

vi.mock("../lib/auth", () => ({
  useAuth: () => ({ applySession: mocks.applySession }),
}));

import ResetPassword, { resetTokenFrom } from "./ResetPassword";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.history.replaceState(null, "", "/");
});

/** The page as the mail's link opens it: jsdom's address bar, the app's router on it. */
function openLink(address: string) {
  window.history.replaceState(null, "", address);
  return render(
    <StrictMode>
      <BrowserRouter>
        <Routes>
          <Route path="/reset-password" element={<ResetPassword />} />
          <Route path="/" element={<p>signed in, at home</p>} />
        </Routes>
      </BrowserRouter>
    </StrictMode>,
  );
}

async function chooseAndSave(password: string) {
  fireEvent.change(screen.getByLabelText(/^New password\*?$/), { target: { value: password } });
  fireEvent.change(screen.getByLabelText(/^New password, again/), { target: { value: password } });
  fireEvent.click(screen.getByRole("button", { name: "Save and sign in" }));
  expect(await screen.findByText("signed in, at home")).toBeTruthy();
}

describe("resetTokenFrom", () => {
  it("takes the code after the '#', else after the '?'", () => {
    expect(resetTokenFrom({ hash: "#token=a.b.c", search: "" })).toBe("a.b.c");
    expect(resetTokenFrom({ hash: "", search: "?token=a.b.c" })).toBe("a.b.c");
    expect(resetTokenFrom({ hash: "#token=new", search: "?token=old" })).toBe("new");
    expect(resetTokenFrom({ hash: "#", search: "" })).toBe("");
    expect(resetTokenFrom({ hash: "", search: "" })).toBe("");
  });
});

describe("ResetPassword", () => {
  it("uses the code after the '#' and clears it from the address bar", async () => {
    mocks.resetPassword.mockResolvedValue({ access_token: "session-token", must_change_password: false });
    mocks.me.mockResolvedValue({ email: "ops@test.com", role: "operator" });
    openLink("/reset-password#token=from-the-hash");

    expect(screen.queryByText("This link is missing its code")).toBeNull();
    expect(window.location.pathname).toBe("/reset-password");
    expect(window.location.hash).toBe("");
    expect(window.location.search).toBe("");
    // The router's own entry state is handed back, not wiped.
    expect(window.history.state).not.toBeNull();

    await chooseAndSave("a-new-password-1");
    expect(mocks.resetPassword).toHaveBeenCalledWith("from-the-hash", "a-new-password-1");
    expect(mocks.setToken).toHaveBeenCalledWith("session-token");
    expect(mocks.applySession).toHaveBeenCalledWith("session-token", { email: "ops@test.com", role: "operator" });
  });

  it("still takes the code from a link mailed before it moved behind the '#'", async () => {
    mocks.resetPassword.mockResolvedValue({ access_token: "session-token", must_change_password: false });
    mocks.me.mockResolvedValue({ email: "ops@test.com", role: "operator" });
    openLink("/reset-password?token=from-the-query");

    expect(window.location.search).toBe("");
    await chooseAndSave("a-new-password-1");
    expect(mocks.resetPassword).toHaveBeenCalledWith("from-the-query", "a-new-password-1");
  });

  it("takes the newer code when a second link opens in the same tab", async () => {
    // Only the part after '#' changes, so the browser keeps the same page:
    // the router hears a popstate and the page is not opened afresh.
    mocks.resetPassword.mockResolvedValue({ access_token: "session-token", must_change_password: false });
    mocks.me.mockResolvedValue({ email: "ops@test.com", role: "operator" });
    openLink("/reset-password#token=the-first-link");

    await act(async () => {
      window.history.pushState(null, "", "/reset-password#token=the-second-link");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });

    expect(window.location.hash).toBe("");
    await chooseAndSave("a-new-password-1");
    expect(mocks.resetPassword).toHaveBeenCalledWith("the-second-link", "a-new-password-1");
  });

  it("says so when the link has no code", () => {
    openLink("/reset-password");
    expect(screen.getByText("This link is missing its code")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Save and sign in" })).toBeNull();
  });
});
