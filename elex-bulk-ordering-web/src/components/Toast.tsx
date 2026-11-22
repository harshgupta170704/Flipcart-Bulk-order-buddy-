import { useCallback, useEffect, useState, type ReactNode } from "react";

export type ToastTone = "success" | "error" | "info";

export interface ToastMessage {
  id: number;
  title: ReactNode;
  description?: ReactNode;
  tone?: ToastTone;
}

const TONE_VAR: Record<ToastTone, string> = {
  success: "var(--status-good)",
  error: "var(--status-critical)",
  info: "var(--accent)",
};
const TONE_GLYPH: Record<ToastTone, string> = { success: "✓", error: "✕", info: "i" };

/** How long a toast stays before it goes on its own. */
const TOAST_MS = 5000;

let nextId = 1;

/** The toast list and the two things a page does with it. */
export function useToasts() {
  const [toasts, setToasts] = useState<ToastMessage[]>([]);
  const dismiss = useCallback((id: number) => {
    setToasts((current) => current.filter((toast) => toast.id !== id));
  }, []);
  const push = useCallback((toast: Omit<ToastMessage, "id">) => {
    const id = nextId++;
    setToasts((current) => [...current, { ...toast, id }]);
    return id;
  }, []);
  return { toasts, push, dismiss };
}

function ToastItem({ toast, onDismiss }: { toast: ToastMessage; onDismiss: (id: number) => void }) {
  const tone = toast.tone ?? "info";
  useEffect(() => {
    const handle = window.setTimeout(() => onDismiss(toast.id), TOAST_MS);
    return () => window.clearTimeout(handle);
  }, [toast.id, onDismiss]);
  return (
    <div
      role={tone === "error" ? "alert" : "status"}
      className="flex w-[320px] items-start gap-3 rounded-card border border-line bg-surface px-4 py-3 shadow-lg"
      style={{ borderLeftWidth: 3, borderLeftColor: TONE_VAR[tone] }}
    >
      <span aria-hidden="true" className="mt-px text-[13px] font-semibold text-ink">
        {TONE_GLYPH[tone]}
      </span>
      <div className="min-w-0 flex-1">
        <p className="text-[13px] font-semibold text-ink">{toast.title}</p>
        {toast.description ? <p className="mt-0.5 text-[12px] text-ink-2">{toast.description}</p> : null}
      </div>
      <button
        type="button"
        onClick={() => onDismiss(toast.id)}
        aria-label="Dismiss"
        className="text-[14px] leading-none text-muted hover:text-ink"
      >
        ×
      </button>
    </div>
  );
}

/** Bottom-right stack of short confirmations ("CSV downloaded"). */
export function Toaster({ toasts, onDismiss }: { toasts: ToastMessage[]; onDismiss: (id: number) => void }) {
  if (toasts.length === 0) return null;
  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex flex-col gap-2">
      {toasts.map((toast) => (
        <div key={toast.id} className="pointer-events-auto">
          <ToastItem toast={toast} onDismiss={onDismiss} />
        </div>
      ))}
    </div>
  );
}
