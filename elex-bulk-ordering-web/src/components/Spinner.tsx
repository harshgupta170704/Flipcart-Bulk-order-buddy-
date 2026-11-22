export interface SpinnerProps {
  /** Diameter in px. Default 16. */
  size?: number;
  className?: string;
  /** Screen-reader label. Omit when visible text already says what is happening. */
  label?: string;
}

/** A hand-rolled indeterminate spinner in the current text color. */
export function Spinner({ size = 16, className = "", label }: SpinnerProps) {
  return (
    <span
      className={`inline-flex shrink-0 items-center justify-center ${className}`}
      role={label ? "status" : undefined}
      aria-hidden={label ? undefined : true}
    >
      <svg
        width={size}
        height={size}
        viewBox="0 0 16 16"
        fill="none"
        className="animate-spin"
        focusable="false"
      >
        <circle cx="8" cy="8" r="6.5" stroke="currentColor" strokeOpacity="0.22" strokeWidth="2" />
        <path
          d="M14.5 8A6.5 6.5 0 0 0 8 1.5"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
        />
      </svg>
      {label ? <span className="sr-only">{label}</span> : null}
    </span>
  );
}

export default Spinner;
