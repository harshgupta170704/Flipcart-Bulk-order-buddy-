import type { ReactNode } from "react";

export interface CardProps {
  title?: ReactNode;
  description?: ReactNode;
  /** Buttons or links on the right of the header; a long row wraps onto further lines. */
  actions?: ReactNode;
  children?: ReactNode;
  className?: string;
  /** Drop the body padding — for a Table that should meet the card edges. */
  flush?: boolean;
}

/** A flat titled panel: hairline border, 12px radius, no shadow. */
export function Card({ title, description, actions, children, className = "", flush }: CardProps) {
  const hasHeader = Boolean(title || description || actions);
  return (
    <section className={`rounded-card border border-line bg-surface ${className}`}>
      {hasHeader ? (
        <header className="flex flex-wrap items-start justify-between gap-3 px-5 pt-5">
          <div className="min-w-0">
            {title ? (
              <h2 className="text-[15px] font-semibold leading-tight text-ink">{title}</h2>
            ) : null}
            {description ? (
              <p className="mt-1 max-w-prose text-[13px] leading-relaxed text-ink-2">{description}</p>
            ) : null}
          </div>
          {/*
            The actions box must be allowed to shrink: `shrink-0` would fix it
            at the one-line width of every button, so its own `flex-wrap` could
            never fire and a long toolbar (the ledger's ten controls) ran off
            the card, clipped by the page's overflow-x. Left to its automatic
            minimum it shrinks to the widest single button and wraps the rest;
            `ml-auto` keeps a row that drops under the title on the right.
          */}
          {actions ? <div className="ml-auto flex flex-wrap items-center justify-end gap-2">{actions}</div> : null}
        </header>
      ) : null}
      {children !== undefined && children !== null ? (
        <div className={flush ? (hasHeader ? "mt-4" : "") : hasHeader ? "px-5 pb-5 pt-4" : "p-5"}>
          {children}
        </div>
      ) : null}
    </section>
  );
}

export default Card;
