"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { useStoredString, writeStored } from "@/lib/useStored";

const NAV = [
  { href: "/", label: "Compare" },
  { href: "/runs", label: "Runs" },
  { href: "/algorithms", label: "Algorithms" },
  { href: "/settings", label: "Settings" },
];

type Theme = "system" | "light" | "dark";

function applyTheme(t: Theme) {
  if (t === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
  writeStored("vx-theme", t);
}

export default function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const stored = useStoredString("vx-theme", "system");
  const theme: Theme = stored === "light" || stored === "dark" ? stored : "system";
  const [machine, setMachine] = useState<string>("This machine");

  useEffect(() => {
    // The machine selector shows where Lab numbers come from (the Service's host). Multi-machine comes with Phase 4.
    api.experiments("?limit=1").then((p) => {
      const env = p.items[0]?.env;
      if (env) setMachine(`${env.cpu_model.replace(/\(R\)|\(TM\)|CPU /g, "").replace(/\s+/g, " ").trim()} · ${env.cpu_count_logical} cores`);
    }).catch(() => undefined);
  }, []);

  return (
    <div className="shell">
      <a href="#main" className="skip-link">Skip to content</a>
      <header className="topbar">
        <span className="brand">VAYUNX Crypto Profiler</span>
        <label>
          Project
          <select aria-label="Project" defaultValue="default">
            <option value="default">Default project</option>
          </select>
        </label>
        <label>
          Machine
          <select aria-label="Machine" defaultValue="local">
            <option value="local">{machine}</option>
          </select>
        </label>
        <span className="spacer" />
        <label>
          Theme
          <select aria-label="Theme" value={theme} onChange={(e) => applyTheme(e.target.value as Theme)}>
            <option value="system">System</option>
            <option value="light">Light</option>
            <option value="dark">Dark</option>
          </select>
        </label>
      </header>
      <nav className="leftnav" aria-label="Main">
        {NAV.map((n) => (
          <Link key={n.href} href={n.href} aria-current={pathname === n.href ? "page" : undefined}>
            {n.label}
          </Link>
        ))}
      </nav>
      <main id="main">{children}</main>
    </div>
  );
}
