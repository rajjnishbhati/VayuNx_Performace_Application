// Typed client for the Profiler Service. Requests go to /api/*, which next.config.ts rewrites to the service.
import type { ApiTokenItem, CompareResult, Experiment, Me, Page, Preset, RunItem, Timeseries } from "./types";

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
  if (res.status === 401 && body?.detail?.login_url && typeof window !== "undefined") {
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
    request<{ experiment_id: string }>("/v2/lab/runs", { method: "POST", body: JSON.stringify(body) }),
  experiment: (id: string) => request<Experiment>(`/v2/experiments/${encodeURIComponent(id)}`),
  experiments: (qs = "") => request<Page<Experiment>>(`/v2/experiments${qs}`),
  cancel: (id: string) => request<{ status: string }>(`/v2/experiments/${encodeURIComponent(id)}/cancel`, { method: "POST" }),
  compareExperiment: (id: string, reference?: string) =>
    request<CompareResult>(`/v2/compare?experiment_id=${encodeURIComponent(id)}${reference ? `&reference=${encodeURIComponent(reference)}` : ""}`),
  compareRuns: (ids: string[], reference?: string) =>
    request<CompareResult>(`/v2/compare?run_ids=${ids.map(encodeURIComponent).join(",")}${reference ? `&reference=${encodeURIComponent(reference)}` : ""}`),
  timeseries: (id: string) => request<Timeseries>(`/v2/experiments/${encodeURIComponent(id)}/timeseries`),
  runs: (qs = "") => request<Page<RunItem>>(`/v2/runs${qs}`),
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
