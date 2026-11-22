import { useCallback, useEffect, useId, useMemo, useState, type ReactNode } from "react";
import { ApiError, errorMessage, getSettings, testOtpInbox, updateSettings } from "../lib/api";
import type { AppSettings, OtpInboxTestResult, SettingsUpdate } from "../lib/types";
import { Alert } from "../components/Alert";
import { Button } from "../components/Button";
import { Card } from "../components/Card";
import { Checkbox, Field, NumberInput, TextInput } from "../components/Field";
import { RunPageHeader } from "../components/RunPageHeader";
import { Spinner } from "../components/Spinner";

/* ===========================================================================
   Helpers
   =========================================================================== */

function shallowEqual<T extends object>(a: T, b: T): boolean {
  const keys = Object.keys(a) as (keyof T)[];
  if (keys.length !== Object.keys(b).length) return false;
  return keys.every((key) => a[key] === b[key]);
}

/**
 * Turn a FastAPI 422 body into { field: message } so the message lands under
 * the input that caused it instead of in a wall of text at the top.
 */
function fieldErrorsFrom(detail: unknown): Record<string, string> {
  const out: Record<string, string> = {};
  if (!Array.isArray(detail)) return out;
  for (const entry of detail) {
    if (!entry || typeof entry !== "object") continue;
    const record = entry as Record<string, unknown>;
    const message = typeof record.msg === "string" ? record.msg : "";
    if (message === "") continue;
    const loc = Array.isArray(record.loc) ? record.loc : [];
    const names = loc.filter(
      (part): part is string =>
        typeof part === "string" && part !== "body" && part !== "query" && part !== "path",
    );
    const field = names[names.length - 1];
    if (field !== undefined && !(field in out)) out[field] = message;
  }
  return out;
}

function isPositiveInt(value: string): boolean {
  const n = Number(value);
  return value.trim() !== "" && Number.isInteger(n) && n >= 1;
}

/** A whole number within the server's own bounds for the setting
 *  (app.core.app_settings), so a slip is caught where it is typed. */
function isWholeBetween(value: string, low: number, high: number): boolean {
  const n = Number(value);
  return value.trim() !== "" && Number.isInteger(n) && n >= low && n <= high;
}

/** The most accounts one run works at once (the server's ceiling). */
const MAX_CONCURRENCY = 50;

/* ===========================================================================
   One group = one draft, one validator, one patch builder.
   Defined at module scope so their identities are stable across renders.
   =========================================================================== */

interface GroupSpec<D extends object> {
  toDraft: (settings: AppSettings) => D;
  validate: (draft: D) => Record<string, string>;
  /** The save's body. `loaded` is the draft as the server last gave it, for a
   *  field that must only be sent when it was changed. */
  toPatch: (draft: D, loaded: D) => SettingsUpdate;
}

interface PerformanceDraft {
  max_concurrency: string;
  browser_headless: boolean;
  proxy_server: string;
  sync_every_hours: string;
}

function isHours(value: string): boolean {
  const n = Number(value);
  return value.trim() !== "" && Number.isInteger(n) && n >= 0 && n <= 168;
}

const PERFORMANCE: GroupSpec<PerformanceDraft> = {
  toDraft: (settings) => ({
    max_concurrency: String(settings.max_concurrency ?? 1),
    browser_headless: Boolean(settings.browser_headless),
    proxy_server: settings.proxy_server ?? "",
    sync_every_hours: String(settings.sync_every_hours ?? 0),
  }),
  validate: (draft) => {
    const errors: Record<string, string> = {};
    if (!isWholeBetween(draft.max_concurrency, 1, MAX_CONCURRENCY)) {
      errors.max_concurrency = `Enter a whole number from 1 to ${MAX_CONCURRENCY}.`;
    }
    if (draft.proxy_server.includes("***@")) {
      errors.proxy_server =
        "This still has *** where the proxy's sign-in goes. Type the whole address again, or press Reset to keep the saved one.";
    }
    if (!isHours(draft.sync_every_hours)) {
      errors.sync_every_hours = "Enter a whole number of hours, 0 (off) to 168 (a week).";
    }
    return errors;
  },
  toPatch: (draft, loaded) => {
    const patch: SettingsUpdate = {
      max_concurrency: Number(draft.max_concurrency),
      browser_headless: draft.browser_headless,
      sync_every_hours: Number(draft.sync_every_hours),
    };
    // The saved proxy is shown with its sign-in masked ("http://***@host").
    // Sent only when it was edited, so saving another field of this card can
    // never write the mask over the real address.
    if (draft.proxy_server.trim() !== loaded.proxy_server.trim()) patch.proxy_server = draft.proxy_server.trim();
    return patch;
  },
};

interface OtpDraft {
  otp_imap_host: string;
  otp_imap_port: string;
  otp_imap_user: string;
  /** Write-only. The baseline is always "" — the API never sends a password. */
  otp_imap_password: string;
  otp_imap_mailbox: string;
  otp_imap_use_ssl: boolean;
  otp_poll_interval: string;
  otp_fetch_timeout: string;
}

const OTP: GroupSpec<OtpDraft> = {
  toDraft: (settings) => ({
    otp_imap_host: settings.otp_imap_host ?? "",
    otp_imap_port: String(settings.otp_imap_port ?? 993),
    otp_imap_user: settings.otp_imap_user ?? "",
    otp_imap_password: "",
    otp_imap_mailbox: settings.otp_imap_mailbox ?? "",
    otp_imap_use_ssl: Boolean(settings.otp_imap_use_ssl),
    otp_poll_interval: String(settings.otp_poll_interval ?? 0),
    otp_fetch_timeout: String(settings.otp_fetch_timeout ?? 0),
  }),
  validate: (draft) => {
    const errors: Record<string, string> = {};
    const port = Number(draft.otp_imap_port);
    if (!isPositiveInt(draft.otp_imap_port) || port > 65535) {
      errors.otp_imap_port = "A port is a whole number between 1 and 65535.";
    }
    if (!isWholeBetween(draft.otp_poll_interval, 1, 60)) {
      errors.otp_poll_interval = "Enter how many seconds to wait between checks: a whole number from 1 to 60.";
    }
    if (!isWholeBetween(draft.otp_fetch_timeout, 5, 600)) {
      errors.otp_fetch_timeout = "Enter how many seconds to keep waiting for a code: a whole number from 5 to 600.";
    }
    return errors;
  },
  toPatch: (draft) => {
    const patch: SettingsUpdate = {
      otp_imap_host: draft.otp_imap_host.trim(),
      otp_imap_port: Number(draft.otp_imap_port),
      otp_imap_user: draft.otp_imap_user.trim(),
      otp_imap_mailbox: draft.otp_imap_mailbox.trim(),
      otp_imap_use_ssl: draft.otp_imap_use_ssl,
      otp_poll_interval: Number(draft.otp_poll_interval),
      otp_fetch_timeout: Number(draft.otp_fetch_timeout),
    };
    // Only ever sent when the user typed something new.
    if (draft.otp_imap_password !== "") patch.otp_imap_password = draft.otp_imap_password;
    return patch;
  },
};

/* ===========================================================================
   Group state
   =========================================================================== */

interface GroupState<D extends object> {
  draft: D;
  dirty: boolean;
  saving: boolean;
  saved: boolean;
  error: string | null;
  fieldErrors: Record<string, string>;
  set: (patch: Partial<D>) => void;
  save: () => void;
  reset: () => void;
}

/**
 * Each group saves on its own, so a bad value in one group can never block the
 * rest. A local draft of `null` means "no edits" and reads straight through to
 * the loaded values, which keeps the two in sync with no effect to misfire.
 */
function useSettingsGroup<D extends object>(
  settings: AppSettings,
  spec: GroupSpec<D>,
  onSaved: (next: AppSettings) => void,
): GroupState<D> {
  const [local, setLocal] = useState<D | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});

  const baseline = useMemo(() => spec.toDraft(settings), [settings, spec]);
  const draft = local ?? baseline;
  const dirty = local !== null && !shallowEqual(local, baseline);

  const set = useCallback(
    (patch: Partial<D>) => {
      setLocal((previous) => ({ ...(previous ?? baseline), ...patch }));
      setSaved(false);
      setError(null);
      setFieldErrors({});
    },
    [baseline],
  );

  const reset = useCallback(() => {
    setLocal(null);
    setSaved(false);
    setError(null);
    setFieldErrors({});
  }, []);

  const save = useCallback(() => {
    const invalid = spec.validate(draft);
    if (Object.keys(invalid).length > 0) {
      setFieldErrors(invalid);
      setError(null);
      setSaved(false);
      return;
    }
    setSaving(true);
    setError(null);
    setFieldErrors({});
    setSaved(false);
    void (async () => {
      try {
        const next = await updateSettings(spec.toPatch(draft, baseline));
        if (next) onSaved(next);
        // Drop the draft: the loaded values are now the truth, and the
        // write-only password field goes back to empty.
        setLocal(null);
        setSaved(true);
      } catch (err) {
        setError(errorMessage(err));
        if (err instanceof ApiError && err.status === 422) {
          setFieldErrors(fieldErrorsFrom(err.detail));
        }
      } finally {
        setSaving(false);
      }
    })();
  }, [draft, baseline, spec, onSaved]);

  return { draft, dirty, saving, saved, error, fieldErrors, set, save, reset };
}

/* ===========================================================================
   Chrome
   =========================================================================== */

function UnsavedChip() {
  return (
    <span
      className="inline-flex items-center gap-1.5 whitespace-nowrap rounded-control border px-2 py-[3px] text-[12px] font-medium leading-5 text-ink"
      style={{
        borderColor: "color-mix(in oklab, var(--status-warning) 45%, transparent)",
        backgroundColor: "color-mix(in oklab, var(--status-warning) 16%, var(--surface-1))",
      }}
    >
      <span aria-hidden="true" className="font-semibold">
        !
      </span>
      Unsaved changes
    </span>
  );
}

interface GroupCardProps {
  title: string;
  description: ReactNode;
  dirty: boolean;
  saving: boolean;
  saved: boolean;
  error: string | null;
  onSave: () => void;
  onReset: () => void;
  children: ReactNode;
}

/**
 * A group with its own Save and Reset. Save lives in the header but is wired to
 * the body's form with `form=`, so Enter in any field saves that group only.
 */
function GroupCard({
  title,
  description,
  dirty,
  saving,
  saved,
  error,
  onSave,
  onReset,
  children,
}: GroupCardProps) {
  const formId = `settings-form-${useId()}`;
  return (
    <Card
      title={title}
      description={description}
      actions={
        <>
          {dirty ? <UnsavedChip /> : null}
          <Button size="sm" variant="ghost" onClick={onReset} disabled={!dirty || saving}>
            Reset
          </Button>
          <Button
            size="sm"
            variant="primary"
            type="submit"
            form={formId}
            loading={saving}
            disabled={!dirty}
          >
            {saving ? "Saving…" : "Save"}
          </Button>
        </>
      }
    >
      <form
        id={formId}
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          onSave();
        }}
        className="flex flex-col gap-5"
      >
        {children}
        <div aria-live="polite" className="flex flex-col gap-3">
          {error ? (
            <Alert tone="error" title="Not saved">
              {error}
            </Alert>
          ) : null}
          {saved ? <Alert tone="success">Saved.</Alert> : null}
        </div>
      </form>
    </Card>
  );
}

/* ===========================================================================
   The groups
   =========================================================================== */

function PerformanceGroup({
  settings,
  onSaved,
}: {
  settings: AppSettings;
  onSaved: (next: AppSettings) => void;
}) {
  const group = useSettingsGroup(settings, PERFORMANCE, onSaved);
  const { draft, fieldErrors, set } = group;
  const concurrency = Number(draft.max_concurrency);
  const pushingIt = Number.isFinite(concurrency) && concurrency > 8;

  return (
    <GroupCard
      title="Performance"
      description="How hard the tool pushes, and how the browser behaves while it runs."
      dirty={group.dirty}
      saving={group.saving}
      saved={group.saved}
      error={group.error}
      onSave={group.save}
      onReset={group.reset}
    >
      <Field
        label="Max concurrency"
        error={fieldErrors.max_concurrency}
        className="sm:max-w-[200px]"
        help={
          <>
            <span className="block">How many accounts run in parallel.</span>
            <span className="block">
              Pushing this too high trips Flipkart&rsquo;s rate limiting and bot checks, which makes
              the whole run slower rather than faster. Raise it a little at a time and watch the
              failure rate.
            </span>
          </>
        }
      >
        <NumberInput
          min={1}
          max={MAX_CONCURRENCY}
          step={1}
          inputMode="numeric"
          value={draft.max_concurrency}
          onChange={(event) => set({ max_concurrency: event.target.value })}
        />
      </Field>

      {pushingIt ? (
        <Alert tone="warning" title={`${concurrency} accounts at once is a lot`}>
          Past roughly eight parallel accounts, Flipkart starts rate-limiting and challenging the
          sessions. Try a smaller step up and compare the failure rate before going further.
        </Alert>
      ) : null}

      <Checkbox
        label="Run the browser headless"
        checked={draft.browser_headless}
        onChange={(event) => set({ browser_headless: event.target.checked })}
        help="Off opens a real browser window per account — handy when something looks wrong, much heavier when many accounts run at once."
      />

      <Field
        label="Read orders back every (hours)"
        error={fieldErrors.sync_every_hours}
        className="sm:max-w-[200px]"
        help={
          <>
            <span className="block">
              0 means only when someone presses the read-back button. Any other number starts a
              read-back of every signed-in account that often, so deliveries, invoices and alerts
              keep up by themselves.
            </span>
            <span className="block">
              A read-back opens each account on Flipkart, so every few hours is plenty; once a day
              suits a quiet week.
            </span>
          </>
        }
      >
        <NumberInput
          min={0}
          max={168}
          step={1}
          inputMode="numeric"
          value={draft.sync_every_hours}
          onChange={(event) => set({ sync_every_hours: event.target.value })}
        />
      </Field>

      <Field
        label="Proxy server"
        error={fieldErrors.proxy_server}
        help="Optional. A proxy spreads the run's traffic across addresses, which matters when many accounts order from one machine. Leave blank to use this server's own address. A saved proxy's sign-in is shown as ***; leave the field as it is to keep it."
      >
        <TextInput
          value={draft.proxy_server}
          autoComplete="off"
          spellCheck={false}
          placeholder="http://user:password@host:port"
          onChange={(event) => set({ proxy_server: event.target.value })}
        />
      </Field>
    </GroupCard>
  );
}

/** Checks the saved OTP inbox actually connects.

Without this the only feedback is a session run that silently never finds a
code, which is a miserable way to discover a wrong password. */
function OtpInboxTester() {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<OtpInboxTestResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = async () => {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      setResult(await testOtpInbox());
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-3">
      <div>
        <Button type="button" variant="secondary" onClick={run} loading={busy}>
          Test connection
        </Button>
      </div>
      <div aria-live="polite">
        {error ? <Alert tone="error" title="Could not run the test">{error}</Alert> : null}
        {result ? (
          <Alert
            tone={result.ok ? "success" : "error"}
            title={result.ok ? "Inbox reachable" : "Inbox not reachable"}
          >
            {result.message}
            {result.ok && result.message_count !== null ? (
              <span className="mt-1 block text-ink-2">
                {result.message_count} message{result.message_count === 1 ? "" : "s"} in the mailbox.
              </span>
            ) : null}
          </Alert>
        ) : null}
      </div>
    </div>
  );
}

function OtpGroup({
  settings,
  onSaved,
}: {
  settings: AppSettings;
  onSaved: (next: AppSettings) => void;
}) {
  const group = useSettingsGroup(settings, OTP, onSaved);
  const { draft, fieldErrors, set } = group;
  const passwordOnFile = settings.otp_imap_password_set;

  return (
    <GroupCard
      title="OTP inbox"
      description="How login codes are read automatically, so a session run never waits for someone to type one."
      dirty={group.dirty}
      saving={group.saving}
      saved={group.saved}
      error={group.error}
      onSave={group.save}
      onReset={group.reset}
    >
      <Alert tone="info" title="One inbox, any number of email domains">
        Codes are matched on the recipient address, so the domain your accounts use can change from
        run to run without touching anything here — as long as the mail still lands in this inbox.
      </Alert>

      {settings.otp_extra_inboxes && settings.otp_extra_inboxes.length > 0 ? (
        <Alert
          tone="info"
          title={
            settings.otp_extra_inboxes.length === 1
              ? "A second inbox is read from the server's settings file"
              : `${settings.otp_extra_inboxes.length} more inboxes are read from the server's settings file`
          }
        >
          <ul className="flex flex-col gap-0.5">
            {settings.otp_extra_inboxes.map((inbox) => (
              <li key={inbox.number}>
                <span className="font-medium text-ink">{inbox.user}</span>
                <span className="text-ink-2"> on {inbox.host} — login codes for {inbox.serves.join(", ")}</span>
              </li>
            ))}
          </ul>
          <p className="mt-1 text-[12px] text-ink-2">
            An account listed there gets its login code from that inbox; every other account uses the
            inbox below. These lines live in the server's .env file (ORDERING_OTP_IMAP_USER_2 and
            ORDERING_OTP_IMAP_PASSWORD_2, then _3 …), not on this page.
          </p>
        </Alert>
      ) : null}

      {settings.otp_unused_inboxes && settings.otp_unused_inboxes.length > 0 ? (
        <Alert
          tone="warning"
          title={
            settings.otp_unused_inboxes.length === 1
              ? "An inbox in the server's settings file is not used"
              : `${settings.otp_unused_inboxes.length} inboxes in the server's settings file are not used`
          }
        >
          <ul className="flex flex-col gap-0.5">
            {settings.otp_unused_inboxes.map((inbox) => (
              <li key={inbox.number}>
                <span className="font-medium text-ink">
                  Inbox {inbox.number}
                  {inbox.user ? ` (${inbox.user})` : ""}
                </span>
                <span className="text-ink-2">: {inbox.problem}.</span>
              </li>
            ))}
          </ul>
          <p className="mt-1 text-[12px] text-ink-2">
            Until it is fixed in the server's .env file, the accounts it was meant for get their login
            code from the inbox below, where it is unlikely to arrive.
          </p>
        </Alert>
      ) : null}

      <OtpInboxTester />

      <p className="-mt-1 text-[12px] text-ink-2">
        Save your changes first — the test checks what is stored, not what is typed above.
      </p>

      <div className="grid gap-5 sm:grid-cols-2">
        <Field label="IMAP host" error={fieldErrors.otp_imap_host}>
          <TextInput
            value={draft.otp_imap_host}
            autoComplete="off"
            spellCheck={false}
            placeholder="imap.example.com"
            onChange={(event) => set({ otp_imap_host: event.target.value })}
          />
        </Field>

        <Field
          label="Port"
          error={fieldErrors.otp_imap_port}
          help="993 for IMAP over SSL, 143 for plain IMAP."
          className="sm:max-w-[160px]"
        >
          <NumberInput
            min={1}
            max={65535}
            step={1}
            inputMode="numeric"
            value={draft.otp_imap_port}
            onChange={(event) => set({ otp_imap_port: event.target.value })}
          />
        </Field>

        <Field
          label="Username"
          error={fieldErrors.otp_imap_user}
          help="The mailbox the tool signs into to read the codes."
        >
          <TextInput
            value={draft.otp_imap_user}
            autoComplete="off"
            spellCheck={false}
            onChange={(event) => set({ otp_imap_user: event.target.value })}
          />
        </Field>

        <Field
          label="Password"
          error={fieldErrors.otp_imap_password}
          help={
            passwordOnFile
              ? "A password is already saved. Leave this blank to keep it — type a new one only to replace it."
              : "Not set yet. The tool cannot read codes until a password is saved here."
          }
        >
          <TextInput
            type="password"
            value={draft.otp_imap_password}
            autoComplete="new-password"
            placeholder={passwordOnFile ? "••••••••" : ""}
            onChange={(event) => set({ otp_imap_password: event.target.value })}
          />
        </Field>

        <Field
          label="Mailbox"
          error={fieldErrors.otp_imap_mailbox}
          help="Which folder to search. Usually INBOX."
        >
          <TextInput
            value={draft.otp_imap_mailbox}
            autoComplete="off"
            spellCheck={false}
            placeholder="INBOX"
            onChange={(event) => set({ otp_imap_mailbox: event.target.value })}
          />
        </Field>

        <div className="flex items-end">
          <Checkbox
            label="Connect over SSL"
            checked={draft.otp_imap_use_ssl}
            onChange={(event) => set({ otp_imap_use_ssl: event.target.checked })}
            help="Leave this on unless the server only offers plain IMAP."
          />
        </div>

        <Field
          label="Poll interval (seconds)"
          error={fieldErrors.otp_poll_interval}
          help="How long to wait between checks while a code is expected."
        >
          <NumberInput
            min={1}
            max={60}
            step={1}
            inputMode="numeric"
            value={draft.otp_poll_interval}
            onChange={(event) => set({ otp_poll_interval: event.target.value })}
          />
        </Field>

        <Field
          label="Fetch timeout (seconds)"
          error={fieldErrors.otp_fetch_timeout}
          help="How long to keep waiting for a code before giving up on that account and moving on."
        >
          <NumberInput
            min={5}
            max={600}
            step={1}
            inputMode="numeric"
            value={draft.otp_fetch_timeout}
            onChange={(event) => set({ otp_fetch_timeout: event.target.value })}
          />
        </Field>
      </div>
    </GroupCard>
  );
}

/* ===========================================================================
   Group 3 — HDFC CorpAssist Portal & Cards (stored in DB, no .env editing)
   =========================================================================== */

interface HdfcDraft {
  hdfc_portal_url: string;
  hdfc_portal_user: string;
  hdfc_portal_password: string;
  hdfc_portal_otp_mobile: string;
  hdfc_portal_otp_email: string;
  hdfc_portal_otp_sender: string;
  hdfc_portal_step_seconds: string;
  hdfc_sender_filter: string;
  hdfc_since_days: string;
  hdfc_auto_fetch_minutes: string;
  hdfc_batch_wait_minutes: string;
}

const HDFC_GROUP: GroupSpec<HdfcDraft> = {
  toDraft: (settings) => ({
    hdfc_portal_url: settings.hdfc_portal_url ?? "https://corpcardassist.hdfc.bank.in",
    hdfc_portal_user: settings.hdfc_portal_user ?? "",
    hdfc_portal_password: "",
    hdfc_portal_otp_mobile: settings.hdfc_portal_otp_mobile ?? "",
    hdfc_portal_otp_email: settings.hdfc_portal_otp_email ?? "",
    hdfc_portal_otp_sender: settings.hdfc_portal_otp_sender ?? "HDFC",
    hdfc_portal_step_seconds: String(settings.hdfc_portal_step_seconds ?? 60),
    hdfc_sender_filter: settings.hdfc_sender_filter ?? "hdfc",
    hdfc_since_days: String(settings.hdfc_since_days ?? 30),
    hdfc_auto_fetch_minutes: String(settings.hdfc_auto_fetch_minutes ?? 0),
    hdfc_batch_wait_minutes: String(settings.hdfc_batch_wait_minutes ?? 60),
  }),
  validate: (draft) => {
    const errors: Record<string, string> = {};
    if (!isWholeBetween(draft.hdfc_portal_step_seconds, 10, 300)) {
      errors.hdfc_portal_step_seconds = "Enter a whole number from 10 to 300 seconds.";
    }
    if (!isWholeBetween(draft.hdfc_since_days, 1, 365)) {
      errors.hdfc_since_days = "Enter a whole number from 1 to 365 days.";
    }
    if (!isWholeBetween(draft.hdfc_auto_fetch_minutes, 0, 1440)) {
      errors.hdfc_auto_fetch_minutes = "Enter 0 (manual) to 1440 minutes.";
    }
    if (!isWholeBetween(draft.hdfc_batch_wait_minutes, 1, 1440)) {
      errors.hdfc_batch_wait_minutes = "Enter 1 to 1440 minutes.";
    }
    return errors;
  },
  toPatch: (draft) => {
    const patch: SettingsUpdate = {
      hdfc_portal_url: draft.hdfc_portal_url.trim(),
      hdfc_portal_user: draft.hdfc_portal_user.trim(),
      hdfc_portal_otp_mobile: draft.hdfc_portal_otp_mobile.trim(),
      hdfc_portal_otp_email: draft.hdfc_portal_otp_email.trim(),
      hdfc_portal_otp_sender: draft.hdfc_portal_otp_sender.trim() || "HDFC",
      hdfc_portal_step_seconds: Number(draft.hdfc_portal_step_seconds),
      hdfc_sender_filter: draft.hdfc_sender_filter.trim() || "hdfc",
      hdfc_since_days: Number(draft.hdfc_since_days),
      hdfc_auto_fetch_minutes: Number(draft.hdfc_auto_fetch_minutes),
      hdfc_batch_wait_minutes: Number(draft.hdfc_batch_wait_minutes),
    };
    if (draft.hdfc_portal_password.trim() !== "") {
      patch.hdfc_portal_password = draft.hdfc_portal_password;
    }
    return patch;
  },
};

function HdfcGroup({ settings, onSaved }: { settings: AppSettings; onSaved: (next: AppSettings) => void }) {
  const { draft, set, dirty, saving, saved, error, fieldErrors, save, reset } = useSettingsGroup(
    settings,
    HDFC_GROUP,
    onSaved,
  );
  const id = useId();
  return (
    <GroupCard
      title="HDFC CorpAssist Portal & Cards"
      description="Saved in the database (no .env edits needed). Leave portal login blank to use manual sheet downloads only."
      dirty={dirty}
      saving={saving}
      saved={saved}
      error={error}
      onSave={save}
      onReset={reset}
    >
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <Field id={`${id}-url`} label="Portal URL" error={fieldErrors.hdfc_portal_url}>
          <TextInput value={draft.hdfc_portal_url} onChange={(e) => set({ hdfc_portal_url: e.target.value })} />
        </Field>
        <Field id={`${id}-user`} label="Portal Username" error={fieldErrors.hdfc_portal_user}>
          <TextInput value={draft.hdfc_portal_user} onChange={(e) => set({ hdfc_portal_user: e.target.value })} />
        </Field>
        <Field
          id={`${id}-pass`}
          label="Portal Password"
          hint={settings.hdfc_portal_password_set ? "A password is saved — leave blank to keep it." : "Not saved yet."}
          error={fieldErrors.hdfc_portal_password}
        >
          <TextInput
            type="password"
            autoComplete="new-password"
            placeholder={settings.hdfc_portal_password_set ? "••••••••" : ""}
            value={draft.hdfc_portal_password}
            onChange={(e) => set({ hdfc_portal_password: e.target.value })}
          />
        </Field>
        <Field
          id={`${id}-mob`}
          label="Sign-in OTP Mobile (10 digits)"
          hint="Must match an Employee Phone. Set either mobile OR email."
          error={fieldErrors.hdfc_portal_otp_mobile}
        >
          <TextInput value={draft.hdfc_portal_otp_mobile} onChange={(e) => set({ hdfc_portal_otp_mobile: e.target.value })} />
        </Field>
        <Field
          id={`${id}-email`}
          label="Sign-in OTP Email"
          hint="Alternative to OTP Mobile — read from the OTP inbox."
          error={fieldErrors.hdfc_portal_otp_email}
        >
          <TextInput value={draft.hdfc_portal_otp_email} onChange={(e) => set({ hdfc_portal_otp_email: e.target.value })} />
        </Field>
        <Field id={`${id}-otpsender`} label="OTP SMS Sender Match">
          <TextInput value={draft.hdfc_portal_otp_sender} onChange={(e) => set({ hdfc_portal_otp_sender: e.target.value })} />
        </Field>
        <Field id={`${id}-sender`} label="Card Email Sender Filter">
          <TextInput value={draft.hdfc_sender_filter} onChange={(e) => set({ hdfc_sender_filter: e.target.value })} />
        </Field>
        <Field id={`${id}-since`} label="Fetch Lookback (days)" error={fieldErrors.hdfc_since_days}>
          <NumberInput min={1} max={365} value={draft.hdfc_since_days} onChange={(e) => set({ hdfc_since_days: e.target.value })} />
        </Field>
        <Field
          id={`${id}-auto`}
          label="Auto-Fetch Interval (minutes)"
          hint="0 = Fetch by hand; e.g. 15 = fetch active user IDs every 15m."
          error={fieldErrors.hdfc_auto_fetch_minutes}
        >
          <NumberInput min={0} max={1440} value={draft.hdfc_auto_fetch_minutes} onChange={(e) => set({ hdfc_auto_fetch_minutes: e.target.value })} />
        </Field>
        <Field id={`${id}-wait`} label="Batch Wait Timeout (minutes)" error={fieldErrors.hdfc_batch_wait_minutes}>
          <NumberInput min={1} max={1440} value={draft.hdfc_batch_wait_minutes} onChange={(e) => set({ hdfc_batch_wait_minutes: e.target.value })} />
        </Field>
        <Field id={`${id}-step`} label="Portal Step Timeout (seconds)" error={fieldErrors.hdfc_portal_step_seconds}>
          <NumberInput min={10} max={300} value={draft.hdfc_portal_step_seconds} onChange={(e) => set({ hdfc_portal_step_seconds: e.target.value })} />
        </Field>
      </div>
    </GroupCard>
  );
}

/* ===========================================================================
   Page
   =========================================================================== */

export default function Settings() {
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    document.title = "Settings — Ordering Suite";
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await getSettings();
      if (!data) throw new Error("The server did not return any settings.");
      setSettings(data);
      setLoadError(null);
    } catch (err) {
      setLoadError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const onSaved = useCallback((next: AppSettings) => {
    setSettings(next);
  }, []);

  return (
    <div className="flex flex-col gap-5">
      <RunPageHeader
        title="Settings"
        lead="Shared defaults for every run. Each group saves on its own, so one value the server rejects never blocks the rest."
        actions={
          <Button size="sm" onClick={() => void load()} loading={loading}>
            Reload
          </Button>
        }
      />

      {loading && settings === null ? (
        <Card>
          <p className="flex items-center justify-center gap-2 py-6 text-[13px] text-ink-2" aria-live="polite">
            <Spinner size={15} />
            Loading settings…
          </p>
        </Card>
      ) : loadError !== null && settings === null ? (
        <Alert tone="error" title="Could not load settings">
          <span className="block">{loadError}</span>
          <Button size="sm" className="mt-2" onClick={() => void load()} loading={loading}>
            Try again
          </Button>
        </Alert>
      ) : settings !== null ? (
        <>
          {loadError !== null ? (
            <Alert tone="error" title="Could not refresh settings">
              <span className="block">{loadError}</span>
              <span className="block text-ink-2">The values below are the ones that last loaded.</span>
              <Button size="sm" className="mt-2" onClick={() => void load()} loading={loading}>
                Try again
              </Button>
            </Alert>
          ) : null}
          <PerformanceGroup settings={settings} onSaved={onSaved} />
          <OtpGroup settings={settings} onSaved={onSaved} />
          <HdfcGroup settings={settings} onSaved={onSaved} />
        </>
      ) : null}
    </div>
  );
}
