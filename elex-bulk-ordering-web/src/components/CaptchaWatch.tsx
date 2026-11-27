/**
 * The captchas waiting for a person — polled once, shared by the nav badge and
 * the Captchas page.
 *
 * Mounted inside the signed-in shell (Layout), so it polls only while someone
 * is signed in. Every few seconds it asks GET /captchas; when a captcha it has
 * not seen before turns up it beeps, shows a browser notification (when
 * allowed) and puts the count in the tab title, so a person answers it before
 * the account is skipped.
 */
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { errorMessage, listCaptchas } from "../lib/api";
import type { CaptchaList, OpenCaptcha } from "../lib/types";

/** How often the open captchas are asked for. */
export const CAPTCHA_POLL_MS = 3_000;

export interface CaptchaWatchState {
  /** null until the first answer. */
  data: CaptchaList | null;
  error: string | null;
  /** Ask again now (after an answer, a skip, or a captcha found gone). */
  refresh: () => void;
}

const CaptchaWatchContext = createContext<CaptchaWatchState | null>(null);

export function siteLabel(site: string): string {
  if (site === "flipkart") return "Flipkart";
  if (site === "hdfc") return "HDFC CorpAssist";
  return site;
}

/** A short beep from an oscillator: no audio file to ship or load. */
let audio: AudioContext | null = null;
export function beep(): void {
  try {
    const Ctor =
      window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Ctor) return;
    audio ??= new Ctor();
    const ctx = audio;
    if (ctx.state === "suspended") void ctx.resume().catch(() => undefined);
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.value = 880;
    const start = ctx.currentTime;
    gain.gain.setValueAtTime(0.0001, start);
    gain.gain.exponentialRampToValueAtTime(0.25, start + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.35);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start(start);
    osc.stop(start + 0.4);
  } catch {
    /* no sound available: the badge and the title still say it */
  }
}

function notify(captcha: OpenCaptcha): void {
  try {
    if (typeof Notification === "undefined" || Notification.permission !== "granted") return;
    new Notification(`Captcha waiting — ${siteLabel(captcha.site)}, ${captcha.step}`, {
      body: captcha.account_email ?? undefined,
      tag: `captcha-${captcha.id}`,
    });
  } catch {
    /* some browsers only allow notifications from a service worker */
  }
}

/** "(2) Orders — Ordering Suite": the count in front of whatever title the page set. */
const COUNT_PREFIX = /^\(\d+\) /;
function titleWithCount(count: number): void {
  const base = document.title.replace(COUNT_PREFIX, "");
  const next = count > 0 ? `(${count}) ${base}` : base;
  if (document.title !== next) document.title = next;
}

export function CaptchaWatchProvider({ children }: { children: ReactNode }) {
  const [data, setData] = useState<CaptchaList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const seen = useRef<Set<number>>(new Set());
  const ticket = useRef(0);

  const load = useCallback(async () => {
    ticket.current += 1;
    const mine = ticket.current;
    try {
      const next = await listCaptchas();
      if (mine !== ticket.current) return; // a newer ask has been made: its answer wins
      const fresh = next.open.filter((c) => !seen.current.has(c.id));
      for (const c of fresh) seen.current.add(c.id);
      if (fresh.length > 0) {
        beep();
        for (const c of fresh) notify(c);
      }
      setData(next);
      setError(null);
    } catch (err) {
      if (mine !== ticket.current) return;
      setError(errorMessage(err)); // keep the last list: a stale card beats none
    }
  }, []);

  useEffect(() => {
    void load();
    const handle = window.setInterval(() => void load(), CAPTCHA_POLL_MS);
    return () => window.clearInterval(handle);
  }, [load]);

  const count = data?.open.length ?? 0;
  useEffect(() => {
    titleWithCount(count);
  });
  useEffect(() => () => titleWithCount(0), []);

  const refresh = useCallback(() => void load(), [load]);

  return <CaptchaWatchContext.Provider value={{ data, error, refresh }}>{children}</CaptchaWatchContext.Provider>;
}

export function useCaptchaWatch(): CaptchaWatchState {
  const state = useContext(CaptchaWatchContext);
  if (!state) throw new Error("useCaptchaWatch needs a CaptchaWatchProvider above it.");
  return state;
}

/** The open count beside "Captchas" in the nav. Nothing at zero. */
export function OpenCaptchasBadge() {
  const state = useContext(CaptchaWatchContext);
  const count = state?.data?.open.length ?? 0;
  if (count <= 0) return null;
  const hue = "var(--status-critical)";
  const label = `${count} captcha${count === 1 ? "" : "s"} waiting`;
  return (
    <span
      className="ml-auto inline-flex min-w-[22px] items-center justify-center rounded-full border px-1.5 py-px text-[11px] font-semibold tabular-nums text-ink"
      style={{
        borderColor: `color-mix(in oklab, ${hue} 45%, transparent)`,
        backgroundColor: `color-mix(in oklab, ${hue} 18%, var(--surface-1))`,
      }}
      title={label}
      data-testid="captcha-badge"
    >
      <span aria-hidden="true">{count}</span>
      <span className="sr-only">{label}</span>
    </span>
  );
}

/**
 * Below the wide layout the sections fold into a menu, so while a captcha is
 * waiting a link to it sits in the top bar itself: one tap away at any width.
 */
export function CaptchaHeaderLink({ className = "" }: { className?: string }) {
  const state = useContext(CaptchaWatchContext);
  const count = state?.data?.open.length ?? 0;
  if (count <= 0) return null;
  return (
    <Link
      to="/captchas"
      className={`inline-flex h-8 items-center gap-1.5 rounded-control border border-line px-2 text-[12.5px] font-medium text-ink hover:bg-page ${className}`}
      aria-label={`Captchas: ${count} waiting`}
    >
      {/* Only the count on a phone: the top bar has no room for the word too. */}
      <span className="hidden sm:inline">Captchas</span>
      <OpenCaptchasBadge />
    </Link>
  );
}
