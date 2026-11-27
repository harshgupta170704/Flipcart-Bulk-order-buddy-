// @vitest-environment jsdom
/** Captchas: the ones waiting for a person, answered, skipped, solved on the live view; the alert on a new one. */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  listCaptchas: vi.fn(),
  captchaHistory: vi.fn(),
  getCaptchaImage: vi.fn(),
  answerCaptcha: vi.fn(),
  clickCaptcha: vi.fn(),
  sendCaptchaKeys: vi.fn(),
  captchaDone: vi.fn(),
  skipCaptcha: vi.fn(),
}));
vi.mock("../lib/api", () => ({
  ...mocks,
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}));

import Captchas from "./Captchas";
import { CaptchaWatchProvider } from "../components/CaptchaWatch";

const TEXT = {
  id: 7,
  site: "flipkart",
  kind: "text",
  step: "checkout",
  job_id: 42,
  account_id: 3,
  account_email: "buyer@example.com",
  created_at: "2026-11-23T09:00:00",
  seconds_left: 95,
};

const CLICK = { ...TEXT, id: 8, site: "hdfc", kind: "click", step: "login", job_id: null, account_email: null };

function list(open: unknown[]) {
  return {
    open,
    last_hour: { pending: open.length, answered: 4, skipped: 1, expired: 2, blocked: 0 },
    ask: true,
    wait_seconds: 120,
    stop_count: 5,
    stop_minutes: 10,
  };
}

function mount() {
  return render(
    <CaptchaWatchProvider>
      <Captchas />
    </CaptchaWatchProvider>,
  );
}

let urls = 0;
beforeEach(() => {
  urls = 0;
  URL.createObjectURL = vi.fn(() => `blob:picture-${++urls}`);
  URL.revokeObjectURL = vi.fn();
  mocks.captchaHistory.mockResolvedValue([]);
  mocks.getCaptchaImage.mockResolvedValue(new Blob(["png"], { type: "image/png" }));
});

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
  vi.unstubAllGlobals();
  document.title = "";
});

describe("Captchas", () => {
  it("says so when nothing is waiting, with the settings and the last hour", async () => {
    mocks.listCaptchas.mockResolvedValue(list([]));
    mocks.captchaHistory.mockResolvedValue([
      {
        id: 1,
        site: "flipkart",
        kind: "text",
        step: "login",
        job_id: 40,
        account_id: 3,
        status: "expired",
        detail: "Nobody answered in 120 s",
        answered_by: null,
        created_at: "2026-11-23T08:00:00",
        resolved_at: "2026-11-23T08:02:00",
      },
    ]);
    mount();

    expect(await screen.findByText("No captchas waiting.")).toBeTruthy();
    expect(screen.getByText("When a run meets one, it shows here with a beep.")).toBeTruthy();
    expect(screen.getByText(/each captcha waits 120 s · a run stops starting accounts after 5 captchas in 10 min/)).toBeTruthy();
    expect(screen.getByText(/answered 4 · skipped 1 · timed out 2 · blocked 0 · waiting 0/)).toBeTruthy();
    // The history table.
    expect(await screen.findByText("Nobody answered in 120 s")).toBeTruthy();
    expect(screen.getByText("Timed out")).toBeTruthy();
    expect(screen.getByText("#40")).toBeTruthy();
  });

  it("answers a picture captcha and drops its card", async () => {
    mocks.listCaptchas.mockResolvedValueOnce(list([TEXT])).mockResolvedValue(list([]));
    mocks.answerCaptcha.mockResolvedValue(undefined);
    mount();

    const card = await screen.findByRole("group", { name: "Captcha 7" });
    expect(screen.getByText("Flipkart")).toBeTruthy();
    expect(screen.getByText(/buyer@example\.com · Run #42/)).toBeTruthy();
    expect(screen.getByText("1:35 left")).toBeTruthy();
    await within(card).findByAltText("Captcha picture from Flipkart");
    expect(mocks.getCaptchaImage).toHaveBeenCalledWith(7, false);

    const input = within(card).getByLabelText("Characters shown");
    expect(document.activeElement).toBe(input);
    fireEvent.change(input, { target: { value: "x7Kp2" } });
    fireEvent.click(within(card).getByRole("button", { name: "Submit" }));

    await waitFor(() => expect(mocks.answerCaptcha).toHaveBeenCalledWith(7, "x7Kp2"));
    await waitFor(() => expect(screen.queryByRole("group", { name: "Captcha 7" })).toBeNull());
    expect(await screen.findByText("No captchas waiting.")).toBeTruthy();
    expect(mocks.listCaptchas.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it("skips the account", async () => {
    mocks.listCaptchas.mockResolvedValueOnce(list([TEXT])).mockResolvedValue(list([]));
    mocks.skipCaptcha.mockResolvedValue(undefined);
    mount();

    const card = await screen.findByRole("group", { name: "Captcha 7" });
    fireEvent.click(within(card).getByRole("button", { name: "Skip this account" }));

    await waitFor(() => expect(mocks.skipCaptcha).toHaveBeenCalledWith(7));
    await waitFor(() => expect(screen.queryByRole("group", { name: "Captcha 7" })).toBeNull());
  });

  it("switches to the live view when no captcha box was found", async () => {
    mocks.listCaptchas.mockResolvedValue(list([TEXT]));
    mocks.answerCaptcha.mockRejectedValue(
      Object.assign(new Error("No captcha box found on the page: use the live view instead."), { status: 422 }),
    );
    mount();

    const card = await screen.findByRole("group", { name: "Captcha 7" });
    fireEvent.change(within(card).getByLabelText("Characters shown"), { target: { value: "abc" } });
    fireEvent.submit(within(card).getByLabelText("Characters shown"));

    expect(await within(card).findByText("No captcha box found on the page: use the live view instead.")).toBeTruthy();
    expect(await within(card).findByAltText("Live view of the Flipkart window")).toBeTruthy();
    expect(mocks.getCaptchaImage).toHaveBeenCalledWith(7, true);
    expect(within(card).getByRole("button", { name: "Solved — continue" })).toBeTruthy();
    // The card stays: the captcha is still waiting.
    expect(screen.getByRole("group", { name: "Captcha 7" })).toBeTruthy();
  });

  it("sends a click on the live view as fractions of the picture, then sends keys and done", async () => {
    mocks.listCaptchas.mockResolvedValueOnce(list([CLICK])).mockResolvedValue(list([CLICK]));
    mocks.clickCaptcha.mockResolvedValue(undefined);
    mocks.sendCaptchaKeys.mockResolvedValue(undefined);
    mocks.captchaDone.mockResolvedValue(undefined);
    mount();

    const card = await screen.findByRole("group", { name: "Captcha 8" });
    expect(screen.getByText("HDFC CorpAssist")).toBeTruthy();
    const img = await within(card).findByAltText("Live view of the HDFC CorpAssist window");
    expect(mocks.getCaptchaImage).toHaveBeenCalledWith(8, true);
    img.getBoundingClientRect = () =>
      ({ left: 100, top: 50, width: 200, height: 100, right: 300, bottom: 150, x: 100, y: 50, toJSON: () => ({}) }) as DOMRect;
    const before = mocks.getCaptchaImage.mock.calls.length;

    fireEvent.click(img, { clientX: 150, clientY: 125 });
    await waitFor(() => expect(mocks.clickCaptcha).toHaveBeenCalledWith(8, 0.25, 0.75));
    // The picture is fetched again straight after the click.
    await waitFor(() => expect(mocks.getCaptchaImage.mock.calls.length).toBeGreaterThan(before));

    fireEvent.change(within(card).getByLabelText("Type in the window"), { target: { value: "hello" } });
    fireEvent.click(within(card).getByRole("button", { name: "Type" }));
    await waitFor(() => expect(mocks.sendCaptchaKeys).toHaveBeenCalledWith(8, { text: "hello" }));
    fireEvent.click(within(card).getByRole("button", { name: "Enter" }));
    await waitFor(() => expect(mocks.sendCaptchaKeys).toHaveBeenCalledWith(8, { key: "Enter" }));

    mocks.listCaptchas.mockResolvedValue(list([]));
    fireEvent.click(within(card).getByRole("button", { name: "Solved — continue" }));
    await waitFor(() => expect(mocks.captchaDone).toHaveBeenCalledWith(8));
    await waitFor(() => expect(screen.queryByRole("group", { name: "Captcha 8" })).toBeNull());
  });

  it("drops a card whose captcha is no longer waiting", async () => {
    mocks.listCaptchas.mockResolvedValueOnce(list([TEXT])).mockResolvedValue(list([]));
    mocks.getCaptchaImage.mockRejectedValue(Object.assign(new Error("This captcha is no longer waiting."), { status: 404 }));
    mount();

    await waitFor(() => expect(mocks.getCaptchaImage).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByRole("group", { name: "Captcha 7" })).toBeNull());
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("beeps, notifies and counts in the title when a new captcha turns up", async () => {
    const shown: string[] = [];
    class FakeNotification {
      static permission = "granted";
      static requestPermission = vi.fn();
      constructor(title: string) {
        shown.push(title);
      }
    }
    const started = vi.fn();
    class FakeAudioContext {
      state = "running";
      currentTime = 0;
      destination = {};
      createOscillator() {
        return { type: "", frequency: { value: 0 }, connect: vi.fn(), start: started, stop: vi.fn() };
      }
      createGain() {
        return { gain: { setValueAtTime: vi.fn(), exponentialRampToValueAtTime: vi.fn() }, connect: vi.fn() };
      }
    }
    vi.stubGlobal("Notification", FakeNotification);
    vi.stubGlobal("AudioContext", FakeAudioContext);
    mocks.listCaptchas.mockResolvedValue(list([TEXT]));
    mount();

    await screen.findByRole("group", { name: "Captcha 7" });
    await waitFor(() => expect(shown).toEqual(["Captcha waiting — Flipkart, checkout"]));
    expect(started).toHaveBeenCalled();
    await waitFor(() => expect(document.title).toBe("(1) Captchas — Ordering Suite"));
    // Already granted: no button to ask.
    expect(screen.queryByRole("button", { name: "Turn on notifications" })).toBeNull();
  });

  it("asks for notification permission and hides the button once granted", async () => {
    class FakeNotification {
      static permission = "default";
      static requestPermission = vi.fn(async () => "granted");
    }
    vi.stubGlobal("Notification", FakeNotification);
    mocks.listCaptchas.mockResolvedValue(list([]));
    mount();

    fireEvent.click(await screen.findByRole("button", { name: "Turn on notifications" }));
    await waitFor(() => expect(FakeNotification.requestPermission).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByRole("button", { name: "Turn on notifications" })).toBeNull());
  });
});
