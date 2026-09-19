"use client";

import { useId, useState } from "react";
import type { Flag } from "@/lib/types";

/** Weak-data marker placed next to the number it affects (no banners). Click or Enter reveals the reason. */
export default function FlagNote({ flags }: { flags: Flag[] }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  if (!flags.length) return null;
  return (
    <span style={{ display: "inline" }}>
      <button type="button" className="flag" aria-expanded={open} aria-controls={id}
              aria-label={`Weak data (${flags.length}). ${flags.map((f) => f.message).join(" ")}`}
              onClick={() => setOpen((o) => !o)}>
        ⚠ weak data
      </button>
      {open && (
        <span id={id} role="note" style={{ display: "block", fontSize: 12, color: "var(--ink-2)", maxWidth: 280, whiteSpace: "normal" }}>
          {flags.map((f) => <span key={f.code} style={{ display: "block" }}>{f.message}</span>)}
        </span>
      )}
    </span>
  );
}

export function flagsFor(flags: Flag[], field: string): Flag[] {
  return flags.filter((f) => f.affects.includes(field));
}
