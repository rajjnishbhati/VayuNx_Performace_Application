"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { PROJECT_KEY, api } from "@/lib/api";
import type { Me, ProjectItem } from "@/lib/types";
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
  const [me, setMe] = useState<Me | null>(null);
  useEffect(() => { api.me().then(setMe).catch(() => undefined); }, []);
  const [projects, setProjects] = useState<ProjectItem[]>([]);
  const project = useStoredString(PROJECT_KEY, "");
  useEffect(() => {
    api.projects().then((ps) => {
      setProjects(ps);
      // a remembered project that is gone or no longer visible falls back to "all projects"
      if (project && !ps.some((p) => p.project_id === project)) { writeStored(PROJECT_KEY, ""); window.location.reload(); }
    }).catch(() => undefined);
  }, [project]);
  const chooseProject = (id: string) => {
    writeStored(PROJECT_KEY, id);
    window.location.reload(); // every list and comparison on the page follows the new project
  };

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
          <select aria-label="Project" value={project} onChange={(e) => chooseProject(e.target.value)}>
            <option value="">All projects</option>
            {projects.map((p) => <option key={p.project_id} value={p.project_id}>{p.name}{p.my_role && me?.auth === "oidc" ? ` (${p.my_role})` : ""}</option>)}
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
        {me?.auth === "oidc" && me.signed_in && (
          <span className="row" style={{ gap: 8 }}>
            <span className="muted" style={{ fontSize: 13 }}>{me.name || me.email}{me.is_admin ? " · admin" : ""}</span>
            {/* a full reload on purpose, so no signed-in state survives in memory */}
            {/* eslint-disable-next-line @next/next/no-location-assign-relative-destination */}
            <button onClick={() => { api.signOut().finally(() => window.location.assign("/")); }}>Sign out</button>
          </span>
        )}
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
