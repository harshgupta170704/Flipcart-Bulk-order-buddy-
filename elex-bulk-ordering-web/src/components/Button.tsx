import type { ButtonHTMLAttributes, ReactNode } from "react";
import { Spinner } from "./Spinner";

export type ButtonVariant = "primary" | "secondary" | "danger" | "ghost";
export type ButtonSize = "sm" | "md";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** While true the button is disabled, shows a spinner and is aria-busy. */
  loading?: boolean;
  children?: ReactNode;
}

const BASE =
  "inline-flex items-center justify-center gap-2 rounded-control border font-medium " +
  "whitespace-nowrap transition-colors disabled:cursor-not-allowed disabled:opacity-55";

const SIZES: Record<ButtonSize, string> = {
  sm: "h-8 px-3 text-[13px]",
  md: "h-[38px] px-4 text-sm",
};

const VARIANTS: Record<ButtonVariant, string> = {
  primary: "border-transparent bg-accent text-accent-ink hover:brightness-110",
  secondary: "border-line bg-surface text-ink hover:bg-page",
  danger: "border-transparent bg-critical text-white hover:brightness-110",
  ghost: "border-transparent bg-transparent text-ink-2 hover:bg-page hover:text-ink",
};

export function Button({
  variant = "secondary",
  size = "md",
  loading = false,
  disabled,
  className = "",
  children,
  type = "button",
  ...rest
}: ButtonProps) {
  return (
    <button
      {...rest}
      type={type}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={`${BASE} ${SIZES[size]} ${VARIANTS[variant]} ${className}`}
    >
      {loading ? <Spinner size={size === "sm" ? 13 : 15} /> : null}
      {children}
    </button>
  );
}

export default Button;
