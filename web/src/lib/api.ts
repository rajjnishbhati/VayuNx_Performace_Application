// Typed client for the Profiler Service. Requests go to /api/*, which next.config.ts rewrites to the service.
import type { ApiTokenItem, CompareResult, Experiment, Me, Page, Preset, ProjectItem, RetentionReport, RunItem, ShareItem, TeamItem, Timeseries } from "./types";

export class ApiError extends Error {
  constructor(public status: number, message: string, public fix: string) {
    super(message);
  }
}

const SERVICE_DOWN = new ApiError(
  0,
  "Can't reach the Profiler Service.",
  "Start it with `python -m profiler_service` (it serves http://127.0.0.1:8010), then retry.",
);

/** The project chosen in the header ("" = all projects I can see). Lists are filtered by it; new Lab
 *  experiments are stored in it (Default when "all"). */
export const PROJECT_KEY = "vx-project";
export function currentProject(): string {
  try {
    return localStorage.getItem(PROJECT_KEY) ?? "";
  } catch {
    return "";
  }
}

function withProject(qs: string): string {
  const p = currentProject();
  return p ? `${qs}${qs.includes("?") ? "&" : "?"}project=${encodeURIComponent(p)}` : qs;
}

const json = (method: string, body?: unknown): RequestInit => ({ method, body: body === undefined ? undefined : JSON.stringify(body) });

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`/api${path}`, { cache: "no-store", headers: { "Content-Type": "application/json" }, ...init });
  } catch {
    throw SERVICE_DOWN;
  }
  if (res.status === 502 || res.status === 503 || res.status === 504 || res.status === 500 && !res.headers.get("content-type")?.includes("json")) {
    throw SERVICE_DOWN;
  }
  const body = await res.json().catch(() => null);
  const onSharedPage = typeof window !== "undefined" && window.location.pathname.startsWith("/shared/");
  if (res.status === 401 && body?.detail?.login_url && typeof window !== "undefined" && !onSharedPage) {
    // sign-in required (VAYUNX_AUTH=oidc): go through the identity provider and come back to this page.
    // A full navigation on purpose: /api/auth/login is the Service (proxied), not a Next.js page.
    // eslint-disable-next-line @next/next/no-location-assign-relative-destination
    window.location.assign(`/api${body.detail.login_url}?next=${encodeURIComponent(window.location.pathname + window.location.search)}`);
    throw new ApiError(401, "Signing in…", "");
  }
  if (!res.ok) {
    const d = body?.detail;
    if (d && typeof d === "object" && "error" in d) throw new ApiError(res.status, d.error, d.fix ?? "");
    const msg = Array.isArray(d) ? d.map((e: { msg?: string }) => e.msg).join("; ") : String(d ?? res.statusText);
    throw new ApiError(res.status, msg, "Check the request and try again.");
  }
  return body as T;
}

export const api = {
  presets: () => request<Preset[]>("/v2/presets"),
  startLab: (body: { presets: string[]; reference?: string; trials: number; duration_s: number }) =>
    request<{ experiment_id: string }>("/v2/lab/runs", json("POST", { ...body, project: currentProject() || undefined })),
  experiment: (id: string) => request<Experiment>(`/v2/experiments/${encodeURIComponent(id)}`),
  experiments: (qs = "") => request<Page<Experiment>>(`/v2/experiments${withProject(qs)}`),
  cancel: (id: string) => request<{ status: string }>(`/v2/experiments/${encodeURIComponent(id)}/cancel`, { method: "POST" }),
  compareExperiment: (id: string, reference?: string) =>
    request<CompareResult>(`/v2/compare?experiment_id=${encodeURIComponent(id)}${reference ? `&reference=${encodeURIComponent(reference)}` : ""}`),
  compareRuns: (ids: string[], reference?: string) =>
    request<CompareResult>(`/v2/compare?run_ids=${ids.map(encodeURIComponent).join(",")}${reference ? `&reference=${encodeURIComponent(reference)}` : ""}`),
  timeseries: (id: string) => request<Timeseries>(`/v2/experiments/${encodeURIComponent(id)}/timeseries`),
  runs: (qs = "") => request<Page<RunItem>>(`/v2/runs${withProject(qs)}`),
  projects: () => request<ProjectItem[]>("/v2/projects"),
  createProject: (name: string) => request<ProjectItem>("/v2/projects", json("POST", { name })),
  setDefaultRole: (id: string, role: "none" | "viewer" | "editor") =>
    request<ProjectItem>(`/v2/projects/${encodeURIComponent(id)}`, json("PATCH", { default_role: role })),
  grant: (id: string, team_id: string, role: string) =>
    request<unknown>(`/v2/projects/${encodeURIComponent(id)}/grants`, json("PUT", { team_id, role })),
  revokeGrant: async (id: string, teamId: string) => {
    const r = await fetch(`/api/v2/projects/${encodeURIComponent(id)}/grants/${encodeURIComponent(teamId)}`, { method: "DELETE" });
    if (!r.ok) throw new ApiError(r.status, "Could not remove the grant.", "Reload the page and try again.");
  },
  retentionPreview: (id: string, days: number) =>
    request<RetentionReport>(`/v2/projects/${encodeURIComponent(id)}/retention/preview?days=${days}`),
  setRetention: (id: string, days: number | null) =>
    request<ProjectItem>(`/v2/projects/${encodeURIComponent(id)}/retention`, json("PUT", { days })),
  share: (body: { experiment_id?: string; run_ids?: string[]; reference: string; expires_days: number }) =>
    request<ShareItem & { url: string }>("/v2/shares", json("POST", body)),
  shared: (token: string) => request<CompareResult & { shared: { expires_at: string; created_at: string } }>(`/v2/shared/${encodeURIComponent(token)}`),
  sharedTimeseries: (token: string) => request<Timeseries>(`/v2/shared/${encodeURIComponent(token)}/timeseries`),
  teams: () => request<TeamItem[]>("/v2/teams"),
  createTeam: (name: string) => request<TeamItem>("/v2/teams", json("POST", { name })),
  addMember: (teamId: string, email: string) => request<unknown>(`/v2/teams/${encodeURIComponent(teamId)}/members`, json("POST", { email })),
  removeMember: async (teamId: string, userId: string) => {
    const r = await fetch(`/api/v2/teams/${encodeURIComponent(teamId)}/members/${encodeURIComponent(userId)}`, { method: "DELETE" });
    if (!r.ok) throw new ApiError(r.status, "Could not remove the member.", "Reload the page and try again.");
  },
  me: () => request<Me>("/auth/me"),
  signOut: () => request<{ signed_out: boolean }>("/auth/logout", { method: "POST" }),
  tokens: () => request<ApiTokenItem[]>("/v2/tokens"),
  createToken: (name: string) =>
    request<ApiTokenItem & { token: string }>("/v2/tokens", { method: "POST", body: JSON.stringify({ name }) }),
  revokeToken: async (id: string) => {
    const r = await fetch(`/api/v2/tokens/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (!r.ok) throw new ApiError(r.status, "Could not revoke the token.", "Reload the page and try again.");
  },
};
