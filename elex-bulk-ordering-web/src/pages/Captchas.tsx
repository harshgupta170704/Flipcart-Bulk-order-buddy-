import { useCallback, useEffect, useRef, useState, type FormEvent, type MouseEvent } from "react";
import {
  answerCaptcha,
  captchaDone,
  captchaHistory,
  clickCaptcha,
  errorMessage,
  getCaptchaImage,
  sendCaptchaKeys,
  skipCaptcha,
} from "../lib/api";
import { formatDateTime } from "../lib/format";
import type { CaptchaHistoryRow, CaptchaKey, CaptchaList, OpenCaptcha } from "../lib/types";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { EmptyState } from "../components/EmptyState";
import { Field, TextInput } from "../components/Field";
import { StatusBadge } from "../components/StatusBadge";
import { Table, type Column } from "../components/Table";
import { siteLabel, useCaptchaWatch } from "../components/CaptchaWatch";

/** How often the live view of a browser window is fetched again. */
export const LIVE_REFRESH_MS = 800;

/** The HTTP status of a failed call, or 0. */
function statusOf(err: unknown): number {
  if (err && typeof err === "object" && "status" in err) {
    const status = (err as { status: unknown }).status;
    return typeof status === "number" ? status : 0;
  }
  return 0;
}

function pageVisible(): boolean {
  return typeof document === "undefined" || document.visibilityState !== "hidden";
}

/**
 * The captcha picture (or the live window) as an object URL. With `live`, it is
 * fetched again every LIVE_REFRESH_MS while the tab is visible. Old URLs are
 * revoked. A 404 means the captcha is no longer waiting: `onGone`.
 */
function useCaptchaPicture(id: number, live: boolean, onGone: () => void) {
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const urlRef = useRef<string | null>(null);
  const started = useRef(0);
  const goneRef = useRef(onGone);
  goneRef.current = onGone;

  const load = useCallback(async () => {
    started.current += 1;
    const mine = started.current;
    try {
      const blob = await getCaptchaImage(id, live);
      if (mine !== started.current) return; // a newer picture is on its way
      const next = URL.createObjectURL(blob);
      const old = urlRef.current;
      urlRef.current = next;
      setUrl(next);
      setError(null);
      if (old) URL.revokeObjectURL(old);
    } catch (err) {
      if (mine !== started.current) return;
      if (statusOf(err) === 404) goneRef.current();
      else setError(errorMessage(err));
    }
  }, [id, live]);

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    const tick = async () => {
      if (pageVisible()) await load();
      if (!stopped && live) timer = window.setTimeout(() => void tick(), LIVE_REFRESH_MS);
    };
    void tick();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [load, live]);

  useEffect(
    () => () => {
      started.current += 1; // drop any answer still on its way
      if (urlRef.current) URL.revokeObjectURL(urlRef.current);
      urlRef.current = null;
    },
    [],
  );

  return { url, error, reload: load };
}

/** "1:05" from seconds. */
function clock(seconds: number): string {
  const s = Math.max(0, Math.ceil(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** Counts down locally from the server's `seconds_left`, set again on every poll. */
function Countdown({ secondsLeft }: { secondsLeft: number }) {
  const [deadline, setDeadline] = useState(() => Date.now() + secondsLeft * 1000);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    setDeadline(Date.now() + secondsLeft * 1000);
    setNow(Date.now());
  }, [secondsLeft]);
  useEffect(() => {
    const handle = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(handle);
  }, []);
  const left = Math.max(0, (deadline - now) / 1000);
  const urgent = left < 30;
  return (
    <span
      className={`font-mono text-[13px] font-semibold tabular-nums ${urgent ? "" : "text-ink"}`}
      style={urgent ? { color: "var(--status-critical)" } : undefined}
      aria-label={`${Math.ceil(left)} seconds left`}
    >
      {clock(left)} left
    </span>
  );
}

function CaptchaCard({ captcha, autoFocus, onGone }: { captcha: OpenCaptcha; autoFocus: boolean; onGone: () => void }) {
  const [live, setLive] = useState(captcha.kind === "click");
  const [answer, setAnswer] = useState("");
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const picture = useCaptchaPicture(captcha.id, live, onGone);
  const site = siteLabel(captcha.site);

  /** Run one action; a 404 drops the card, anything else shows inline. */
  const act = async (name: string, run: () => Promise<void>, after?: () => void) => {
    setBusy(name);
    setError(null);
    try {
      await run();
      after?.();
    } catch (err) {
      if (statusOf(err) === 404) {
        onGone();
        return;
      }
      setError(errorMessage(err));
      if (name === "answer" && statusOf(err) === 422) setLive(true);
    } finally {
      setBusy(null);
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const text = answer.trim();
    if (!text) return;
    void act("answer", () => answerCaptcha(captcha.id, text), onGone);
  };

  const clickPicture = (event: MouseEvent<HTMLImageElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    const clamp = (v: number) => Math.min(1, Math.max(0, v));
    const x = clamp((event.clientX - rect.left) / rect.width);
    const y = clamp((event.clientY - rect.top) / rect.height);
    void act("click", () => clickCaptcha(captcha.id, x, y), () => void picture.reload());
  };

  const sendKeys = (input: { text?: string; key?: CaptchaKey }) =>
    act(input.key ?? "type", () => sendCaptchaKeys(captcha.id, input), () => {
      if (input.text) setTyped("");
      void picture.reload();
    });

  const altText = live ? `Live view of the ${site} window` : `Captcha picture from ${site}`;

  return (
    <Card
      title={
        <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span>{site}</span>
          <span className="font-normal text-ink-2">· {captcha.step}</span>
        </span>
      }
      description={
        <>
          {captcha.account_email ?? "—"}
          {captcha.job_id !== null ? ` · Run #${captcha.job_id}` : ""}
        </>
      }
      actions={<Countdown secondsLeft={captcha.seconds_left} />}
    >
      <div className="flex min-w-0 flex-col gap-3" aria-label={`Captcha ${captcha.id}`} role="group">
        {error ? <Alert tone="error">{error}</Alert> : null}
        {picture.error ? <Alert tone="warning">Could not load the picture: {picture.error}</Alert> : null}

        <div className="min-w-0">
          {picture.url ? (
            <img
              src={picture.url}
              alt={altText}
              onClick={live ? clickPicture : undefined}
              className={`block h-auto max-w-full rounded-control border border-line bg-page ${
                live ? "w-full cursor-crosshair" : "w-[360px]"
              }`}
              style={live ? undefined : { imageRendering: "pixelated" }}
              draggable={false}
            />
          ) : (
            <div className="flex h-24 items-center justify-center rounded-control border border-dashed border-line text-[13px] text-ink-2">
              Loading the picture…
            </div>
          )}
          {live ? (
            <p className="mt-1 text-[12px] text-ink-2">Click on the picture to click in the window. It refreshes every second.</p>
          ) : null}
        </div>

        {!live ? (
          <form className="flex flex-wrap items-end gap-2" onSubmit={submit}>
            <Field label="Characters shown" className="min-w-0 flex-1 basis-48">
              <TextInput
                value={answer}
                maxLength={64}
                autoFocus={autoFocus}
                autoComplete="off"
                autoCapitalize="off"
                spellCheck={false}
                onChange={(e) => setAnswer(e.target.value)}
              />
            </Field>
            <Button type="submit" variant="primary" loading={busy === "answer"} disabled={!answer.trim() || busy !== null}>
              Submit
            </Button>
          </form>
        ) : (
          <div className="flex flex-col gap-2">
            <form
              className="flex flex-wrap items-end gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                if (typed) void sendKeys({ text: typed });
              }}
            >
              <Field label="Type in the window" className="min-w-0 flex-1 basis-40">
                <TextInput
                  value={typed}
                  maxLength={64}
                  autoComplete="off"
                  autoCapitalize="off"
                  spellCheck={false}
                  onChange={(e) => setTyped(e.target.value)}
                />
              </Field>
              <Button type="submit" loading={busy === "type"} disabled={!typed || busy !== null}>
                Type
              </Button>
            </form>
            <div className="flex flex-wrap gap-2">
              <Button size="sm" loading={busy === "Enter"} disabled={busy !== null} onClick={() => void sendKeys({ key: "Enter" })}>
                Enter
              </Button>
              <Button
                size="sm"
                loading={busy === "Backspace"}
                disabled={busy !== null}
                onClick={() => void sendKeys({ key: "Backspace" })}
              >
                Backspace
              </Button>
              <Button
                size="sm"
                variant="primary"
                loading={busy === "done"}
                disabled={busy !== null}
                onClick={() => void act("done", () => captchaDone(captcha.id), onGone)}
              >
                Solved — continue
              </Button>
            </div>
          </div>
        )}

        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line pt-3">
          {captcha.kind === "text" ? (
            <Button size="sm" variant="ghost" onClick={() => setLive((v) => !v)}>
              {live ? "Show the captcha picture" : "Show live view"}
            </Button>
          ) : (
            <span />
          )}
          <Button
            size="sm"
            variant="ghost"
            loading={busy === "skip"}
            disabled={busy !== null}
            onClick={() => void act("skip", () => skipCaptcha(captcha.id), onGone)}
          >
            Skip this account
          </Button>
        </div>
      </div>
    </Card>
  );
}

function StatusLine({ data }: { data: CaptchaList }) {
  const h = data.last_hour;
  return (
    <div className="flex flex-col gap-1 text-[13px] text-ink-2">
      <p>
        Asking a person: <span className="font-medium text-ink">{data.ask ? "on" : "off"}</span> · each captcha waits{" "}
        {data.wait_seconds} s · a run stops starting accounts after {data.stop_count} captchas in {data.stop_minutes} min
      </p>
      <p>
        Last hour: answered {h.answered ?? 0} · skipped {h.skipped ?? 0} · timed out {h.expired ?? 0} · blocked{" "}
        {h.blocked ?? 0} · waiting {h.pending ?? 0}
      </p>
    </div>
  );
}

const OUTCOMES: Record<string, { status: string; label: string }> = {
  pending: { status: "pending", label: "Waiting" },
  answered: { status: "done", label: "Answered" },
  skipped: { status: "skipped", label: "Skipped" },
  expired: { status: "skipped", label: "Timed out" },
  blocked: { status: "failed", label: "Blocked" },
};

function Outcome({ status }: { status: string }) {
  const meta = OUTCOMES[status];
  return meta ? <StatusBadge status={meta.status} label={meta.label} /> : <StatusBadge status={status} />;
}

const HISTORY_COLUMNS: Column<CaptchaHistoryRow>[] = [
  { key: "created_at", header: "Time", render: (row) => <span className="whitespace-nowrap">{formatDateTime(row.created_at)}</span> },
  { key: "site", header: "Site", render: (row) => siteLabel(row.site) },
  { key: "step", header: "Step" },
  { key: "account_id", header: "Account", render: (row) => (row.account_id !== null ? `#${row.account_id}` : "—") },
  { key: "job_id", header: "Run", render: (row) => (row.job_id !== null ? `#${row.job_id}` : "—") },
  { key: "status", header: "Outcome", render: (row) => <Outcome status={row.status} /> },
  { key: "answered_by", header: "Answered by", render: (row) => row.answered_by ?? "—" },
  { key: "detail", header: "Detail", render: (row) => <span className="break-words">{row.detail ?? "—"}</span> },
];

function notificationsSupported(): boolean {
  return typeof window !== "undefined" && "Notification" in window;
}

/** Captchas: the ones waiting for a person, answered here, and what happened to the last ones. */
export default function Captchas() {
  const { data, error, refresh } = useCaptchaWatch();
  const [gone, setGone] = useState<Set<number>>(() => new Set());
  const [history, setHistory] = useState<CaptchaHistoryRow[] | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [permission, setPermission] = useState<string>(() =>
    notificationsSupported() ? Notification.permission : "unsupported",
  );

  useEffect(() => {
    document.title = "Captchas — Ordering Suite";
  }, []);

  const drop = useCallback(
    (id: number) => {
      setGone((prev) => new Set(prev).add(id));
      refresh();
    },
    [refresh],
  );

  const open = (data?.open ?? [])
    .filter((c) => !gone.has(c.id))
    .sort((a, b) => a.created_at.localeCompare(b.created_at) || a.id - b.id);

  // The history follows the list: refetched whenever what is open, or the last hour's counts, change.
  const listKey = data ? `${data.open.map((c) => c.id).join(",")}|${JSON.stringify(data.last_hour)}` : "";
  useEffect(() => {
    if (!data) return;
    let alive = true;
    captchaHistory(50).then(
      (rows) => {
        if (!alive) return;
        setHistory(rows);
        setHistoryError(null);
      },
      (err: unknown) => {
        if (alive) setHistoryError(errorMessage(err));
      },
    );
    return () => {
      alive = false;
    };
  }, [listKey]);

  const askPermission = () => {
    if (!notificationsSupported()) return;
    void Promise.resolve(Notification.requestPermission()).then(
      (answer) => setPermission(answer),
      () => undefined,
    );
  };

  return (
    <div className="flex min-w-0 flex-col gap-5">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-[20px] font-semibold tracking-tight text-ink">Captchas</h1>
          <p className="mt-1 max-w-prose text-[13px] text-ink-2">
            When a run meets a captcha, its browser waits here for a person to answer. Other accounts keep running. If
            nobody answers in time, the account is skipped.
          </p>
        </div>
        {permission === "default" ? (
          <Button size="sm" onClick={askPermission}>
            Turn on notifications
          </Button>
        ) : permission === "denied" ? (
          <p className="text-[12px] text-ink-2">Notifications are blocked in this browser's site settings.</p>
        ) : null}
      </header>

      {data ? <StatusLine data={data} /> : null}
      {error ? <Alert tone="error">Could not check for captchas: {error}</Alert> : null}

      {data === null && !error ? (
        <p className="text-[13px] text-ink-2">Loading…</p>
      ) : data !== null && open.length === 0 ? (
        <EmptyState title="No captchas waiting." description="When a run meets one, it shows here with a beep." />
      ) : (
        <div className="grid min-w-0 grid-cols-1 gap-4 lg:grid-cols-2">
          {open.map((captcha, index) => (
            <CaptchaCard key={captcha.id} captcha={captcha} autoFocus={index === 0} onGone={() => drop(captcha.id)} />
          ))}
        </div>
      )}

      <Card flush title="Recent captchas">
        {historyError ? (
          <div className="px-5 pb-4">
            <Alert tone="error">{historyError}</Alert>
          </div>
        ) : null}
        <Table
          caption="Recent captchas"
          columns={HISTORY_COLUMNS}
          rows={history ?? []}
          rowKey={(row) => row.id}
          dense
          minWidth="900px"
          empty={history === null ? "Loading…" : "No captchas yet."}
        />
      </Card>
    </div>
  );
}
