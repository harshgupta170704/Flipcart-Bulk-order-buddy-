import { useId } from "react";
import { formatCount } from "../lib/format";
import type { FilterOptions } from "../lib/types";

/**
 * Email Domains, Subscription Types and Custom Tags as checkbox lists, two
 * columns each with Clear All / Select All: the order form's Email ID Filters.
 * Nothing ticked in a group means that group does not narrow at all; ticking
 * several means an account matching any of them is in.
 */
export interface FilterChecklistsProps {
  options: FilterOptions | null;
  tags: (number | "untagged")[];
  subscriptions: string[];
  domains: string[];
  onTags: (next: (number | "untagged")[]) => void;
  onSubscriptions: (next: string[]) => void;
  onDomains: (next: string[]) => void;
  disabled?: boolean;
}

interface Item<T> {
  value: T;
  label: string;
  count?: number;
}

function Group<T extends string | number>({
  title,
  items,
  chosen,
  onChange,
  disabled,
}: {
  title: string;
  items: Item<T>[];
  chosen: T[];
  onChange: (next: T[]) => void;
  disabled?: boolean;
}) {
  const id = useId();
  if (items.length === 0) return null;
  const toggle = (value: T) =>
    onChange(chosen.includes(value) ? chosen.filter((v) => v !== value) : [...chosen, value]);
  return (
    <fieldset className="flex flex-col gap-2" disabled={disabled}>
      <div className="flex items-center justify-between gap-2">
        <legend className="text-[13px] font-medium text-ink">{title}</legend>
        <div className="flex gap-3 text-[12px]">
          <button type="button" className="text-ink-2 hover:text-ink" onClick={() => onChange([])}>
            Clear All
          </button>
          <button
            type="button"
            className="text-ink-2 hover:text-ink"
            onClick={() => onChange(items.map((item) => item.value))}
          >
            Select All
          </button>
        </div>
      </div>
      <div className="grid gap-x-6 gap-y-1.5 sm:grid-cols-2">
        {items.map((item) => {
          const inputId = `${id}-${String(item.value)}`;
          return (
            <div key={String(item.value)} className="flex items-center gap-2">
              <input
                id={inputId}
                type="checkbox"
                checked={chosen.includes(item.value)}
                onChange={() => toggle(item.value)}
                className="size-4 shrink-0 accent-accent"
              />
              <label htmlFor={inputId} className="min-w-0 truncate text-[13px] text-ink">
                {item.label}
                {item.count !== undefined ? (
                  <span className="ml-1 text-[11.5px] text-muted">({formatCount(item.count)})</span>
                ) : null}
              </label>
            </div>
          );
        })}
      </div>
    </fieldset>
  );
}

export function FilterChecklists({
  options,
  tags,
  subscriptions,
  domains,
  onTags,
  onSubscriptions,
  onDomains,
  disabled,
}: FilterChecklistsProps) {
  if (options === null) return <p className="text-[12px] text-ink-2">Loading filters…</p>;
  const tagItems: Item<number | "untagged">[] = [
    { value: "untagged", label: "Untagged", count: options.untagged },
    ...options.tags.map((tag) => ({ value: tag.id, label: tag.name, count: tag.count })),
  ];
  return (
    <div className="flex flex-col gap-4">
      <Group
        title="Email Domains"
        items={(options.domains ?? []).map((d) => ({ value: d.value, label: d.value, count: d.count }))}
        chosen={domains}
        onChange={onDomains}
        disabled={disabled}
      />
      <Group
        title="Subscription Types"
        items={options.subscriptions.map((s) => ({ value: s, label: s }))}
        chosen={subscriptions}
        onChange={onSubscriptions}
        disabled={disabled}
      />
      <Group
        title="Custom Tags"
        items={tagItems as Item<string | number>[]}
        chosen={tags as (string | number)[]}
        onChange={(next) => onTags(next as (number | "untagged")[])}
        disabled={disabled}
      />
    </div>
  );
}

export default FilterChecklists;
