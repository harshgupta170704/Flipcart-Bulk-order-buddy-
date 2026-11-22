import { useEffect, useId, useRef, useState } from "react";
import { formatCount, formatPercent } from "../lib/format";
import { statusMeta } from "./StatusBadge";

export interface StatusBarDatum {
  /**
   * Status key. The bar colour comes from `statusMeta(status).colorVar`, so a
   * bar and its badge can never disagree.
   */
  status: string;
  /** Category name. Defaults to the status's own label. */
  label?: string;
  value: number;
}

export interface DashboardStatusChartProps {
  data: StatusBarDatum[];
  /** Accessible name for the chart. */
  title?: string;
  /** Unit word used in the tooltip and the screen-reader description. */
  unit?: string;
  className?: string;
}

/* --- geometry -------------------------------------------------------------
   One row per category. BAND - BAR_H leaves bare card surface between
   adjacent bars (16px here, far more than the 2px minimum), so neighbouring
   bars never touch.
------------------------------------------------------------------------- */

const PAD_TOP = 6;
const BAND = 36;
const BAR_H = 20;
const AXIS_H = 28;
/** Rounded data end. The baseline end stays square — it is anchored to zero. */
const RADIUS = 4;
const VALUE_GAP = 8;
/** Room kept clear at the right so the direct value label always fits. */
const VALUE_W = 56;
const LABEL_GAP = 12;

const FALLBACK_W = 760;
const MIN_W = 260;
const MAX_W = 1400;

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

/** A whole-number axis: 0 .. max in 1/2/5 x 10^n steps. */
function axisScale(max: number, steps = 4): { max: number; step: number } {
  if (max <= 0) return { max: steps, step: 1 };
  const raw = max / steps;
  const magnitude = Math.pow(10, Math.floor(Math.log10(raw)));
  const normalised = raw / magnitude;
  const base = normalised <= 1 ? 1 : normalised <= 2 ? 2 : normalised <= 5 ? 5 : 10;
  const step = Math.max(1, Math.round(base * magnitude));
  return { max: Math.ceil(max / step) * step, step };
}

/** A bar with only its right end rounded, so the baseline end stays at zero. */
function barPath(x: number, y: number, width: number, height: number): string {
  const r = Math.min(RADIUS, width, height / 2);
  if (r <= 0) return `M${x} ${y}h${width}v${height}h${-width}Z`;
  return (
    `M${x} ${y}h${width - r}` +
    `a${r} ${r} 0 0 1 ${r} ${r}` +
    `v${height - 2 * r}` +
    `a${r} ${r} 0 0 1 ${-r} ${r}` +
    `h${-(width - r)}Z`
  );
}

/**
 * Orders by status: a horizontal bar chart in hand-written SVG.
 *
 * One axis (values along the bottom), categories as text, a direct value label
 * at every bar end, recessive gridlines and a per-bar tooltip. The chart is
 * laid out in CSS pixels and re-measured on resize, so the type stays the same
 * size at every width instead of scaling with the viewBox.
 */
export function DashboardStatusChart({
  data,
  title = "Orders by status",
  unit = "orders",
  className = "",
}: DashboardStatusChartProps) {
  const uid = useId();
  const titleId = `chart-title-${uid}`;
  const descId = `chart-desc-${uid}`;

  const boxRef = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(FALLBACK_W);
  const [active, setActive] = useState<number | null>(null);

  useEffect(() => {
    const el = boxRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) return;
      const next = clamp(Math.round(entry.contentRect.width), MIN_W, MAX_W);
      if (next > 0) setWidth((prev) => (prev === next ? prev : next));
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const rows = data.map((datum) => {
    const meta = statusMeta(datum.status);
    return {
      label: datum.label ?? meta.label,
      value: Math.max(0, datum.value),
      color: meta.colorVar,
    };
  });

  const total = rows.reduce((sum, row) => sum + row.value, 0);
  const axis = axisScale(Math.max(...rows.map((row) => row.value), 0));

  const height = PAD_TOP + BAND * rows.length + AXIS_H;
  const labelW = clamp(Math.round(width * 0.24), 84, 148);
  const plotX = labelW;
  const plotW = Math.max(48, width - labelW - VALUE_W - 8);
  const plotBottom = PAD_TOP + BAND * rows.length;
  const scale = (value: number) => (axis.max <= 0 ? 0 : (value / axis.max) * plotW);

  const ticks: number[] = [];
  for (let tick = 0; tick <= axis.max + 1e-9; tick += axis.step) ticks.push(Math.round(tick));

  const description = `${title}. ${rows
    .map((row) => `${row.label} ${formatCount(row.value)}`)
    .join(", ")}. ${formatCount(total)} ${unit} in total.`;

  const activeRow = active === null ? null : rows[active];
  const activeAnchor =
    active === null ? 0 : plotX + scale(rows[active].value) + VALUE_GAP + VALUE_W / 2;
  // Below the row, except on the last one — there the axis ticks live.
  const tipAbove = active !== null && active === rows.length - 1 && rows.length > 1;
  const activeTop =
    active === null ? 0 : PAD_TOP + active * BAND + (tipAbove ? 2 : BAND - 2);

  return (
    <div ref={boxRef} className={`relative w-full ${className}`}>
      <svg
        role="img"
        aria-labelledby={`${titleId} ${descId}`}
        width="100%"
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="xMidYMid meet"
        style={{ display: "block" }}
        focusable="false"
      >
        <title id={titleId}>{title}</title>
        <desc id={descId}>{description}</desc>

        {/* gridlines: recessive, behind the data */}
        {ticks.map((tick) =>
          tick === 0 ? null : (
            <line
              key={`grid-${tick}`}
              x1={plotX + scale(tick)}
              x2={plotX + scale(tick)}
              y1={PAD_TOP}
              y2={plotBottom}
              style={{ stroke: "var(--grid)" }}
              strokeWidth={1}
              shapeRendering="crispEdges"
            />
          ),
        )}

        {/* the single axis: a left baseline the bars grow from, and the value
            scale underneath it */}
        <line
          x1={plotX + 0.5}
          x2={plotX + 0.5}
          y1={PAD_TOP}
          y2={plotBottom}
          style={{ stroke: "var(--axis)" }}
          strokeWidth={1}
          shapeRendering="crispEdges"
        />
        <line
          x1={plotX}
          x2={plotX + plotW}
          y1={plotBottom + 0.5}
          y2={plotBottom + 0.5}
          style={{ stroke: "var(--axis)" }}
          strokeWidth={1}
          shapeRendering="crispEdges"
        />
        {ticks.map((tick) => (
          <text
            key={`tick-${tick}`}
            x={plotX + scale(tick)}
            y={plotBottom + 17}
            textAnchor={tick === 0 ? "start" : "middle"}
            style={{
              fill: "var(--ink-muted)",
              fontSize: 11,
              fontVariantNumeric: "tabular-nums",
            }}
          >
            {formatCount(tick)}
          </text>
        ))}

        {rows.map((row, index) => {
          const rowTop = PAD_TOP + index * BAND;
          const barY = rowTop + (BAND - BAR_H) / 2;
          const barW = scale(row.value);
          const centreY = barY + BAR_H / 2;
          const share = total > 0 ? (row.value / total) * 100 : 0;
          const readout =
            total > 0
              ? `${row.label}: ${formatCount(row.value)} ${unit} · ${formatPercent(share)} of ${formatCount(total)}`
              : `${row.label}: ${formatCount(row.value)} ${unit}`;

          return (
            <g key={row.label}>
              <title>{readout}</title>

              {active === index ? (
                <rect
                  x={0}
                  y={rowTop}
                  width={width}
                  height={BAND}
                  rx={6}
                  style={{ fill: "var(--page)" }}
                />
              ) : null}

              {/* category name, always as text — never colour alone */}
              <text
                x={plotX - LABEL_GAP}
                y={centreY}
                textAnchor="end"
                dominantBaseline="middle"
                style={{ fill: "var(--ink-secondary)", fontSize: 12 }}
              >
                {row.label}
              </text>

              {barW > 0 ? (
                <path d={barPath(plotX, barY, barW, BAR_H)} style={{ fill: row.color }} />
              ) : null}

              {/* direct value label at the data end */}
              <text
                x={plotX + barW + VALUE_GAP}
                y={centreY}
                dominantBaseline="middle"
                style={{
                  fill: "var(--ink-primary)",
                  fontSize: 12,
                  fontWeight: 600,
                  fontVariantNumeric: "tabular-nums",
                }}
              >
                {formatCount(row.value)}
              </text>

              {/* hover target for the whole row */}
              <rect
                x={0}
                y={rowTop}
                width={width}
                height={BAND}
                style={{ fill: "transparent" }}
                pointerEvents="all"
                onMouseEnter={() => setActive(index)}
                onMouseLeave={() => setActive((prev) => (prev === index ? null : prev))}
              />
            </g>
          );
        })}
      </svg>

      {activeRow ? (
        <div
          aria-hidden="true"
          className="pointer-events-none absolute z-10 rounded-control border border-line bg-surface px-2.5 py-1.5 text-[12px] leading-snug whitespace-nowrap text-ink"
          style={{
            left: `${clamp((activeAnchor / width) * 100, 10, 90)}%`,
            top: `${(activeTop / height) * 100}%`,
            transform: `translate(-50%, ${tipAbove ? "-100%" : "0"})`,
          }}
        >
          <span className="font-semibold">{activeRow.label}</span>
          <span className="text-ink-2">
            {" · "}
            <span style={{ fontVariantNumeric: "tabular-nums" }}>
              {formatCount(activeRow.value)}
            </span>{" "}
            {unit}
            {total > 0 ? (
              <>
                {" · "}
                <span style={{ fontVariantNumeric: "tabular-nums" }}>
                  {formatPercent((activeRow.value / total) * 100)}
                </span>{" "}
                of {formatCount(total)}
              </>
            ) : null}
          </span>
        </div>
      ) : null}
    </div>
  );
}

export default DashboardStatusChart;
