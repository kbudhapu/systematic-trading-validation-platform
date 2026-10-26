"use client";

/**
 * Chrome-tab-style selector for the registry, grouped by the artifact's existing `kind`
 * (EXPERIMENT / DOCTRINE / STANDING / QUARANTINED / PREAMBLE / …).
 *
 * PURE DISPLAY. The grouping and the panels themselves are built server-side and handed in as
 * `content`; this component only decides which one is visible. No registry content is read,
 * derived, filtered or reordered here.
 *
 * Why tabs: the page previously stacked every kind vertically, so reaching Standing or Preamble
 * meant scrolling past the whole experiment list. Tabs make the set navigable in one screen while
 * keeping each panel's own SourcedPanel provenance header intact.
 *
 * All panels stay MOUNTED and are hidden with the `hidden` attribute rather than unmounted, so
 * switching tabs never re-runs a child's effects or loses its scroll position, and in-page browser
 * find (ctrl-F) still reaches the rendered markup of the active tab.
 */

import { useState } from "react";

export type RegistryTab = {
  key: string;
  label: string;
  count: number;
  content: React.ReactNode;
};

export function RegistryTabs({ tabs }: { tabs: RegistryTab[] }) {
  const [active, setActive] = useState<string>(tabs[0]?.key ?? "");
  if (tabs.length === 0) return null;
  const current = tabs.some((t) => t.key === active) ? active : tabs[0].key;

  return (
    <div className="space-y-0">
      {/* tab strip — sits on the panel below it, Chrome-style */}
      <div
        role="tablist"
        aria-label="Registry kind"
        className="flex flex-wrap items-end gap-1 border-b border-[var(--border)] px-1"
      >
        {tabs.map((t) => {
          const on = t.key === current;
          return (
            <button
              key={t.key}
              type="button"
              role="tab"
              id={`registry-tab-${t.key}`}
              aria-selected={on}
              aria-controls={`registry-panel-${t.key}`}
              onClick={() => setActive(t.key)}
              className={[
                "relative -mb-px rounded-t-lg border px-3 py-1.5 text-xs transition-colors",
                on
                  ? "border-[var(--border)] border-b-transparent bg-[var(--card)] font-semibold text-[var(--text)]"
                  : "border-transparent text-[var(--muted)] hover:bg-[var(--card-2)] hover:text-[var(--text)]",
              ].join(" ")}
            >
              {t.label}
              <span
                className={[
                  "ml-1.5 rounded px-1.5 py-0.5 text-[0.62rem] tnum",
                  on ? "bg-[var(--accent-soft)] text-[var(--text)]" : "text-[var(--dim)]",
                ].join(" ")}
              >
                {t.count}
              </span>
            </button>
          );
        })}
      </div>

      <div className="pt-3">
        {tabs.map((t) => (
          <div
            key={t.key}
            role="tabpanel"
            id={`registry-panel-${t.key}`}
            aria-labelledby={`registry-tab-${t.key}`}
            hidden={t.key !== current}
          >
            {t.content}
          </div>
        ))}
      </div>
    </div>
  );
}
