import {
  createContext,
  useContext,
  useId,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";

/** What Field hands down to whatever control sits inside it. */
export interface FieldControl {
  id: string;
  /** True inside a boxed field: the control leaves room for the label inside it. */
  boxed?: boolean;
  /** ids of the help and error text, for aria-describedby. */
  describedBy: string | undefined;
  invalid: boolean;
}

const FieldControlContext = createContext<FieldControl | null>(null);

/**
 * How fields draw themselves. "stacked" (the default) puts the label above the
 * control and the help under it. "boxed" is the compact order-form look: the
 * label sits inside the control's border, long help hides behind an info icon,
 * a short `hint` stays under the box, and a Checkbox becomes a switch row.
 */
export type FieldLook = "stacked" | "boxed";
const FieldLookContext = createContext<FieldLook>("stacked");
export const FieldLookProvider = FieldLookContext.Provider;
export function useFieldLook(): FieldLook {
  return useContext(FieldLookContext);
}

/** For a custom control that needs the generated id. */
export function useFieldControl(): FieldControl | null {
  return useContext(FieldControlContext);
}

export interface FieldProps {
  label: ReactNode;
  /** Explicit control id. One is generated when omitted. */
  id?: string;
  /** Helper text under the control. */
  help?: ReactNode;
  /** Error text under the control. Its presence marks the control invalid. */
  error?: ReactNode;
  required?: boolean;
  /** Keep the label for screen readers but hide it visually. */
  labelHidden?: boolean;
  /** A short line always shown under a boxed field ("Min: 25"). */
  hint?: ReactNode;
  /** Overrides the surrounding FieldLookProvider for this one field. */
  look?: FieldLook;
  className?: string;
  /**
   * The control. TextInput / NumberInput / Select / FileInput / Textarea pick
   * up the id and aria-describedby automatically; pass a function for anything
   * that needs them by hand.
   */
  children: ReactNode | ((control: FieldControl) => ReactNode);
}

/** A real <label for>, a control, and optional help / error text. */
export function Field({
  label,
  id,
  help,
  error,
  required,
  labelHidden,
  hint,
  look,
  className = "",
  children,
}: FieldProps) {
  const generated = useId();
  const contextLook = useFieldLook();
  const controlId = id ?? `f-${generated}`;
  const helpId = help ? `${controlId}-help` : undefined;
  const errorId = error ? `${controlId}-error` : undefined;
  const describedBy = [helpId, errorId].filter(Boolean).join(" ") || undefined;
  const boxed = (look ?? contextLook) === "boxed" && !labelHidden;
  const control: FieldControl = { id: controlId, describedBy, invalid: Boolean(error), boxed };

  if (boxed) {
    return (
      <div className={`flex min-w-0 flex-col gap-1 ${className}`}>
        <div className="relative">
          <label
            htmlFor={controlId}
            className="pointer-events-none absolute left-3 top-[5px] z-[1] max-w-[calc(100%-2.5rem)] truncate text-[11px] leading-tight text-ink-2"
          >
            {label}
            {required ? (
              <span className="ml-0.5" aria-hidden="true">
                *
              </span>
            ) : null}
          </label>
          <FieldControlContext.Provider value={control}>
            {typeof children === "function" ? children(control) : children}
          </FieldControlContext.Provider>
          {help ? <InfoTip id={helpId}>{help}</InfoTip> : null}
        </div>
        {hint ? <p className="text-[11.5px] leading-snug text-muted">{hint}</p> : null}
        <FieldError id={errorId} error={error} />
      </div>
    );
  }

  return (
    <div className={`flex flex-col gap-1.5 ${className}`}>
      <label
        htmlFor={controlId}
        className={
          labelHidden
            ? "sr-only"
            : "text-[13px] font-medium leading-none text-ink"
        }
      >
        {label}
        {required ? (
          <span className="ml-1 text-muted" aria-hidden="true">
            *
          </span>
        ) : null}
      </label>

      <FieldControlContext.Provider value={control}>
        {typeof children === "function" ? children(control) : children}
      </FieldControlContext.Provider>

      {help ? (
        <p id={helpId} className="text-[12px] leading-snug text-ink-2">
          {help}
        </p>
      ) : null}
      <FieldError id={errorId} error={error} />
    </div>
  );
}

function FieldError({ id, error }: { id: string | undefined; error: ReactNode }) {
  if (!error) return null;
  return (
    <p id={id} role="alert" className="flex items-start gap-1.5 text-[12px] leading-snug text-ink">
      <span aria-hidden="true" style={{ color: "var(--status-critical)" }} className="font-semibold">
        {"✕"}
      </span>
      <span>{error}</span>
    </p>
  );
}

/**
 * An info icon at the top right of a boxed control; its text shows on hover or
 * keyboard focus. The text is always in the page, so it still describes the
 * control to a screen reader (aria-describedby) and to the tests.
 */
export function InfoTip({ id, children, className = "" }: { id?: string; children: ReactNode; className?: string }) {
  return (
    <span className={`absolute right-2 top-[5px] z-[2] ${className}`}>
      <span
        tabIndex={0}
        aria-hidden="true"
        className="peer flex size-4 cursor-help items-center justify-center rounded-full border border-line text-[10px] font-semibold leading-none text-muted outline-none hover:text-ink focus-visible:ring-2 focus-visible:ring-accent"
      >
        i
      </span>
      <span
        id={id}
        role="tooltip"
        className="invisible absolute right-0 top-6 z-30 w-72 rounded-control border border-line bg-surface p-2.5 text-[12px] font-normal leading-snug text-ink-2 opacity-0 shadow-lg transition-opacity peer-hover:visible peer-hover:opacity-100 peer-focus:visible peer-focus:opacity-100"
      >
        {children}
      </span>
    </span>
  );
}

/* --- shared control styling ---------------------------------------------- */

const CONTROL =
  "w-full rounded-control border bg-surface px-3 text-sm text-ink " +
  "transition-colors placeholder:text-muted disabled:cursor-not-allowed disabled:opacity-60";

const CONTROL_H = "h-[38px]";

function borderClass(invalid: boolean): string {
  return invalid ? "border-critical" : "border-line";
}

/** A boxed field's control is taller, with its text below the label. */
const BOXED_H = "h-[46px] pt-[17px] pb-[3px] pr-7";

function useWired(
  id: string | undefined,
  describedBy: string | undefined,
): { id: string | undefined; describedBy: string | undefined; invalid: boolean; height: string } {
  const ctx = useFieldControl();
  return {
    id: id ?? ctx?.id,
    describedBy: describedBy ?? ctx?.describedBy,
    invalid: ctx?.invalid ?? false,
    height: ctx?.boxed ? BOXED_H : CONTROL_H,
  };
}

/* --- text ---------------------------------------------------------------- */

export type TextInputProps = InputHTMLAttributes<HTMLInputElement>;

export function TextInput({ className = "", id, "aria-describedby": described, ...rest }: TextInputProps) {
  const wired = useWired(id, described);
  return (
    <input
      {...rest}
      type={rest.type ?? "text"}
      id={wired.id}
      aria-describedby={wired.describedBy}
      aria-invalid={wired.invalid || undefined}
      className={`${CONTROL} ${wired.height} ${borderClass(wired.invalid)} ${className}`}
    />
  );
}

/* --- number -------------------------------------------------------------- */

export type NumberInputProps = InputHTMLAttributes<HTMLInputElement>;

export function NumberInput({
  className = "",
  id,
  "aria-describedby": described,
  ...rest
}: NumberInputProps) {
  const wired = useWired(id, described);
  return (
    <input
      {...rest}
      type="number"
      inputMode={rest.inputMode ?? "decimal"}
      id={wired.id}
      aria-describedby={wired.describedBy}
      aria-invalid={wired.invalid || undefined}
      className={`${CONTROL} ${wired.height} ${borderClass(wired.invalid)} tabular-nums ${className}`}
    />
  );
}

/* --- select -------------------------------------------------------------- */

export type SelectProps = SelectHTMLAttributes<HTMLSelectElement>;

export function Select({ className = "", id, "aria-describedby": described, children, ...rest }: SelectProps) {
  const wired = useWired(id, described);
  return (
    <select
      {...rest}
      id={wired.id}
      aria-describedby={wired.describedBy}
      aria-invalid={wired.invalid || undefined}
      className={`${CONTROL} ${wired.height} ${borderClass(wired.invalid)} pr-8 ${className}`}
    >
      {children}
    </select>
  );
}

/* --- textarea ------------------------------------------------------------ */

export type TextareaProps = TextareaHTMLAttributes<HTMLTextAreaElement>;

export function Textarea({ className = "", id, "aria-describedby": described, ...rest }: TextareaProps) {
  const wired = useWired(id, described);
  return (
    <textarea
      {...rest}
      id={wired.id}
      aria-describedby={wired.describedBy}
      aria-invalid={wired.invalid || undefined}
      rows={rest.rows ?? 3}
      className={`${CONTROL} ${borderClass(wired.invalid)} py-2 ${className}`}
    />
  );
}

/* --- file ---------------------------------------------------------------- */

export type FileInputProps = InputHTMLAttributes<HTMLInputElement>;

export function FileInput({ className = "", id, "aria-describedby": described, ...rest }: FileInputProps) {
  const wired = useWired(id, described);
  return (
    <input
      {...rest}
      type="file"
      id={wired.id}
      aria-describedby={wired.describedBy}
      aria-invalid={wired.invalid || undefined}
      className={
        `w-full rounded-control border bg-surface py-2 pr-3 text-sm text-ink-2 ` +
        `file:mr-3 file:cursor-pointer file:rounded-l-control file:border-0 file:border-r ` +
        `file:border-line file:bg-page file:px-3 file:py-2 file:text-sm file:font-medium file:text-ink ` +
        `${borderClass(wired.invalid)} ${className}`
      }
    />
  );
}

/* --- checkbox (brings its own label) ------------------------------------- */

export interface CheckboxProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "type"> {
  label: ReactNode;
  help?: ReactNode;
}

/** The checkbox drawn as a switch: same element, same role, same keyboard. */
const SWITCH =
  "relative h-5 w-9 shrink-0 cursor-pointer appearance-none rounded-full bg-[var(--grid)] transition-colors " +
  "before:absolute before:left-0.5 before:top-0.5 before:size-4 before:rounded-full before:bg-white before:shadow-sm " +
  "before:transition-transform checked:bg-accent checked:before:translate-x-4 disabled:cursor-not-allowed disabled:opacity-60 " +
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent";

export function Checkbox({ label, help, className = "", id, ...rest }: CheckboxProps) {
  const generated = useId();
  const look = useFieldLook();
  const inputId = id ?? `c-${generated}`;
  const helpId = help ? `${inputId}-help` : undefined;
  if (look === "boxed") {
    return (
      <div
        className={`relative flex min-h-[42px] items-center justify-between gap-3 rounded-control border border-line bg-surface py-2 pl-3 ${
          help ? "pr-8" : "pr-3"
        } ${className}`}
      >
        <label htmlFor={inputId} className="cursor-pointer text-[13px] leading-snug text-ink">
          {label}
        </label>
        <input {...rest} id={inputId} type="checkbox" aria-describedby={helpId} className={SWITCH} />
        {help ? (
          <InfoTip id={helpId} className="!top-1/2 -translate-y-1/2">
            {help}
          </InfoTip>
        ) : null}
      </div>
    );
  }
  return (
    <div className={`flex flex-col gap-1 ${className}`}>
      <div className="flex items-start gap-2.5">
        <input
          {...rest}
          id={inputId}
          type="checkbox"
          aria-describedby={helpId}
          className="mt-[2px] size-4 shrink-0 accent-accent"
        />
        <label htmlFor={inputId} className="text-[13px] leading-snug text-ink">
          {label}
        </label>
      </div>
      {help ? (
        <p id={helpId} className="pl-[26px] text-[12px] leading-snug text-ink-2">
          {help}
        </p>
      ) : null}
    </div>
  );
}

export default Field;
