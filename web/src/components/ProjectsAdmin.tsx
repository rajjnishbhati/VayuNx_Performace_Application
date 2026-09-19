"use client";

import { useCallback, useEffect, useState } from "react";
import { ErrorState } from "@/components/States";
import { ApiError, api } from "@/lib/api";
import type { Me, ProjectItem, Role, TeamItem } from "@/lib/types";

/** Settings: projects, who gets which role, and teams. What is shown follows what this person may change. */
export default function ProjectsAdmin({ me }: { me: Me }) {
  const signIn = me.auth === "oidc";
  const globalAdmin = !signIn || !!me.is_admin;
  const [projects, setProjects] = useState<ProjectItem[] | null>(null);
  const [teams, setTeams] = useState<TeamItem[]>([]);
  const [error, setError] = useState<ApiError | null>(null);
  const [newProject, setNewProject] = useState("");
  const [newTeam, setNewTeam] = useState("");
  const [member, setMember] = useState<Record<string, string>>({});
  const [grant, setGrant] = useState<Record<string, { team: string; role: Role }>>({});

  const load = useCallback(() => {
    api.projects().then(setProjects).catch(setError);
    if (signIn) api.teams().then(setTeams).catch(setError);
  }, [signIn]);
  useEffect(() => { load(); }, [load]);
  const run = (p: Promise<unknown>) => p.then(load).catch((e) => setError(e as ApiError));

  return (
    <section className="card" aria-labelledby="projects-title">
      <h2 id="projects-title">Projects{signIn ? " & access" : ""}</h2>
      <p className="ink2">
        Runs and experiments belong to a project; the header picks which one you are looking at.
        {signIn
          ? " Roles: viewer (read), editor (run the Lab, send data), admin (manage who has access). Everyone signed in gets a project's default role; teams can be granted more."
          : " Sign-in is off, so projects organise data but do not restrict it."}
      </p>
      {error && <ErrorState error={error.message} fix={error.fix} onRetry={() => { setError(null); load(); }} />}

      <div className="scroll-x">
        <table className="data">
          <thead>
            <tr><th scope="col">Project</th>{signIn && <><th scope="col">Your role</th><th scope="col">Everyone signed in</th><th scope="col">Teams</th></>}</tr>
          </thead>
          <tbody>
            {!projects && <tr><td colSpan={4}><div className="skeleton" style={{ height: 18 }} aria-hidden /></td></tr>}
            {projects?.map((p) => {
              const canAdmin = p.my_role === "admin";
              const g = grant[p.project_id] ?? { team: teams[0]?.team_id ?? "", role: "viewer" as Role };
              return (
                <tr key={p.project_id}>
                  <th scope="row" style={{ fontWeight: 500 }}>{p.name}<div className="muted mono">{p.project_id}</div></th>
                  {signIn && (
                    <>
                      <td>{p.my_role ?? "–"}</td>
                      <td>
                        {canAdmin ? (
                          <select aria-label={`Default role on ${p.name}`} value={p.default_role ?? "none"}
                                  onChange={(e) => run(api.setDefaultRole(p.project_id, e.target.value as "none" | "viewer" | "editor"))}>
                            <option value="none">no access</option><option value="viewer">viewer</option><option value="editor">editor</option>
                          </select>
                        ) : (p.default_role ?? "no access")}
                      </td>
                      <td>
                        {(p.grants ?? []).map((gr) => (
                          <div key={gr.team_id} className="row" style={{ gap: 6 }}>
                            <span>{gr.team}: {gr.role}</span>
                            {canAdmin && <button onClick={() => run(api.revokeGrant(p.project_id, gr.team_id))} aria-label={`Remove ${gr.team} from ${p.name}`}>Remove</button>}
                          </div>
                        ))}
                        {canAdmin && teams.length > 0 && (
                          <div className="row" style={{ gap: 6, marginTop: 4 }}>
                            <select aria-label={`Team to grant on ${p.name}`} value={g.team}
                                    onChange={(e) => setGrant({ ...grant, [p.project_id]: { ...g, team: e.target.value } })}>
                              {teams.map((t) => <option key={t.team_id} value={t.team_id}>{t.name}</option>)}
                            </select>
                            <select aria-label={`Role to grant on ${p.name}`} value={g.role}
                                    onChange={(e) => setGrant({ ...grant, [p.project_id]: { ...g, role: e.target.value as Role } })}>
                              <option value="viewer">viewer</option><option value="editor">editor</option><option value="admin">admin</option>
                            </select>
                            <button onClick={() => g.team && run(api.grant(p.project_id, g.team, g.role))}>Grant</button>
                          </div>
                        )}
                      </td>
                    </>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {globalAdmin && (
        <form className="row" style={{ gap: 8, alignItems: "end", marginTop: 12 }}
              onSubmit={(e) => { e.preventDefault(); if (newProject.trim()) run(api.createProject(newProject.trim()).then(() => setNewProject(""))); }}>
          <label className="field">New project
            <input value={newProject} maxLength={128} onChange={(e) => setNewProject(e.target.value)} placeholder="e.g. Payments API" />
          </label>
          <button className="btn-primary" type="submit" disabled={!newProject.trim()}>Create project</button>
        </form>
      )}

      {signIn && (
        <>
          <h3 style={{ marginTop: 20 }}>Teams</h3>
          {teams.length === 0 && <p className="muted">No teams{globalAdmin ? " yet" : " that you are in"}.</p>}
          {teams.map((t) => (
            <div key={t.team_id} className="card" style={{ padding: 12 }}>
              <strong>{t.name}</strong>
              <ul style={{ margin: "6px 0" }}>
                {t.members.map((m) => (
                  <li key={m.user_id}>
                    {m.name || m.email} <span className="muted">{m.email}</span>{" "}
                    {globalAdmin && <button onClick={() => run(api.removeMember(t.team_id, m.user_id))} aria-label={`Remove ${m.email} from ${t.name}`}>Remove</button>}
                  </li>
                ))}
              </ul>
              {globalAdmin && (
                <form className="row" style={{ gap: 8, alignItems: "end" }}
                      onSubmit={(e) => { e.preventDefault(); const em = (member[t.team_id] ?? "").trim(); if (em) run(api.addMember(t.team_id, em).then(() => setMember({ ...member, [t.team_id]: "" }))); }}>
                  <label className="field">Add member (email; they must have signed in once)
                    <input type="email" value={member[t.team_id] ?? ""} onChange={(e) => setMember({ ...member, [t.team_id]: e.target.value })} />
                  </label>
                  <button type="submit">Add</button>
                </form>
              )}
            </div>
          ))}
          {globalAdmin && (
            <form className="row" style={{ gap: 8, alignItems: "end", marginTop: 8 }}
                  onSubmit={(e) => { e.preventDefault(); if (newTeam.trim()) run(api.createTeam(newTeam.trim()).then(() => setNewTeam(""))); }}>
              <label className="field">New team
                <input value={newTeam} maxLength={128} onChange={(e) => setNewTeam(e.target.value)} placeholder="e.g. payments-team" />
              </label>
              <button type="submit" disabled={!newTeam.trim()}>Create team</button>
            </form>
          )}
        </>
      )}
    </section>
  );
}
