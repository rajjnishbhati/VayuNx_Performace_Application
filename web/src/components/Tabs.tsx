"use client";

import { useId, useRef } from "react";

export type TabDef = { id: string; label: string; content: React.ReactNode };

/** ARIA tabs: click, or Left/Right/Home/End to move between tabs. */
export default function Tabs({ tabs, active, onChange }: { tabs: TabDef[]; active: string; onChange: (id: string) => void }) {
  const base = useId();
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const idx = Math.max(0, tabs.findIndex((t) => t.id === active));
  const move = (i: number) => {
    const n = (i + tabs.length) % tabs.length;
    onChange(tabs[n].id);
    refs.current[n]?.focus();
  };
  return (
    <div className="tabs">
      <div role="tablist" aria-label="Result views">
        {tabs.map((t, i) => (
          <button key={t.id} ref={(el) => { refs.current[i] = el; }} role="tab" id={`${base}-${t.id}`}
                  aria-selected={i === idx} aria-controls={`${base}-${t.id}-panel`} tabIndex={i === idx ? 0 : -1}
                  onClick={() => onChange(t.id)}
                  onKeyDown={(e) => {
                    if (e.key === "ArrowRight") move(i + 1);
                    else if (e.key === "ArrowLeft") move(i - 1);
                    else if (e.key === "Home") move(0);
                    else if (e.key === "End") move(tabs.length - 1);
                    else return;
                    e.preventDefault();
                  }}>
            {t.label}
          </button>
        ))}
      </div>
      <div role="tabpanel" id={`${base}-${tabs[idx].id}-panel`} aria-labelledby={`${base}-${tabs[idx].id}`} tabIndex={0}>
        {tabs[idx].content}
      </div>
    </div>
  );
}
