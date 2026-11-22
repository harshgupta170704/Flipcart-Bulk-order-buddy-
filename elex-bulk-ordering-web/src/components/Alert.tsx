import type { ReactNode } from "react";

export type AlertTone = "info" | "error" | "success" | "warning";

export interface AlertProps {
  tone: AlertTone;
  /** Optional bold first line. */
  title?: ReactNode;
  children?: ReactNode;
  className?: string;
}

interface ToneSpec {
  color: string;
  glyph: string;
  role: "status" | "alert";
}

const TONES: Record<AlertTone, ToneSpec> = {
  info: { color: "var(--accent)", glyph: "i", role: "status" },
  success: { color: "var(--status-good)", glyph: "✓", role: "status" },
  warning: { color: "var(--status-warning)", glyph: "!", role: "status" },
  error: { color: "var(--status-critical)", glyph: "✕", role: "alert" },
};

/** Inline message. The glyph pairs with the color; the text is always in ink. */
export function Alert({ tone, title, children, className = "" }: AlertProps) {
  const spec = TONES[tone];
  return (
    <div
      role={spec.role}
      className={`flex items-start gap-2.5 rounded-control border px-3.5 py-3 text-[13px] leading-relaxed text-ink ${className}`}
      style={{
        borderColor: `color-mix(in oklab, ${spec.color} 42%, transparent)`,
        backgroundColor: `color-mix(in oklab, ${spec.color} 12%, var(--surface-1))`,
      }}
    >
      <span
        aria-hidden="true"
        className="mt-px inline-flex size-[18px] shrink-0 items-center justify-center rounded-full border text-[11px] font-bold"
        style={{
          borderColor: `color-mix(in oklab, ${spec.color} 60%, transparent)`,
          backgroundColor: `color-mix(in oklab, ${spec.color} 30%, var(--surface-1))`,
        }}
      >
        {spec.glyph}
      </span>
      <div className="min-w-0">
        {title ? <div className="font-semibold">{title}</div> : null}
        {children ? <div className={title ? "mt-0.5" : ""}>{children}</div> : null}
      </div>
    </div>
  );
}

export default Alert;
