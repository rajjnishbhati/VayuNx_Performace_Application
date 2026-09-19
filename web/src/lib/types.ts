// Shapes of the Profiler Service /v2 JSON API (profiler_service/api_v2.py, compare_v2.py).

export type Security = {
  algorithm: string;
  params: string;
  safe_for_passwords: boolean;
  meets_owasp_minimum: boolean | null;
  summary: string;
  reference: string;
};

export type Preset = {
  id: string;
  label: string;
  algorithm: string;
  params: string;
  family: "fast-hash" | "password-hash";
  salted: boolean;
  library: string;
  security: Security;
  runtime?: "python" | "node";
  /** the same preset run by the Node.js Lab worker (id "<id>@node"); availability as reported by that worker */
  node?: { id: string; label: string; available: boolean; library: string | null; reason: string | null };
};

/** Python presets followed by their Node.js twins that can run on this machine. */
export function withNodeVariants(presets: Preset[]): Preset[] {
  const twins = presets.filter((p) => p.node?.available).map((p) => ({
    ...p, id: p.node!.id, label: p.node!.label, library: p.node!.library ?? "node:crypto", runtime: "node" as const, node: undefined,
  }));
  return [...presets.map((p) => ({ ...p, runtime: p.runtime ?? ("python" as const) })), ...twins];
}

export type Flag = { code: string; message: string; affects: string[] };

export type PerTrial = {
  trial_index: number;
  ops: number;
  p50: number;
  p95: number;
  p99: number;
  mean: number;
  iqr: number | null;
  wall_s: number | null;
  noisy: boolean;
  exact: boolean;
};

export type Capacity = {
  estimate: true;
  rate_per_s: number;
  cores_total: number;
  cores_needed: number | null;
  share_of_machine: number | null;
  ram_in_flight_bytes: number | null;
  latency_ns: number | null;
  added_latency_ns: number | null;
};

export type Variant = {
  key: string;
  label: string;
  family: string | null;
  security: Security | null;
  trials: number;
  is_reference: boolean;
  time_per_call: {
    median_ns: number | null;
    p50_ns: number | null;
    p95_ns: number | null;
    p99_ns: number | null;
    mean_ns: number | null;
    iqr_ns: number | null;
    trial_medians_ns: number[];
    range_ns: [number, number] | null;
    ci95_ns: [number, number] | null;
    percentile_method: string;
  };
  cpu: { cpu_s_per_op: number | null; ops_per_s_per_core: number | null; cores_busy: number | null };
  memory: { peak_rss_bytes: number | null; mem_per_op_bytes: number | null; approximate: boolean };
  throughput: { ops_per_s: number | null };
  vs_reference: {
    time_ratio: number | null;
    time_change: string;
    significant: boolean | null;
    cpu_change: string;
    memory_change: string;
  } | null;
  flags: Flag[];
  capacity: Capacity;
  per_trial: PerTrial[];
};

export type Env = {
  id: string;
  cpu_model: string;
  cpu_count_physical: number | null;
  cpu_count_logical: number | null;
  ram_total_bytes: number;
  os: string;
  python: string;
  openssl: string;
  runtime: string;
  libraries: Record<string, string | null>;
};

export type Experiment = {
  experiment_id: string;
  source: "lab" | "app";
  label: string;
  status: "queued" | "running" | "complete" | "failed" | "cancelled" | "cancelling";
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  params: { presets: string[]; trials: number; duration_s: number; concurrency: number };
  reference_preset: string | null;
  progress: {
    done: number;
    total: number;
    percent: number;
    current: { trial?: number; trials?: number; preset?: string; variant?: string; stage?: string };
    eta_s: number | null;
  };
  error: string | null;
  env: Env | null;
};

export type RunItem = {
  run_id: string;
  service: string;
  label: string;
  phase: string;
  created_at: string;
  completed_at: string | null;
  metadata: Record<string, unknown>;
  span_count: number;
  sample_count: number;
  op_stat_count: number;
  variant: string | null;
  experiment_id: string | null;
  trial_index: number | null;
  source: "lab" | "app";
  project_id?: string;
};

export type CompareResult = {
  schema_version: string;
  source: "lab" | "app";
  reference: string;
  verdict: string;
  variants: Variant[];
  capacity_defaults: { rate_per_s: number; cores_total: number };
  method: Record<string, string>;
  experiment?: Experiment;
  operation?: string;
  /** "span": every crypto call inside the app's span of that name; "operation": one crypto.operation */
  operation_kind?: "span" | "operation";
  /** per variant: what that code path actually ran, largest time first, e.g. ["verify Argon2id"] */
  operation_detail?: Record<string, string[]>;
  other_operations?: string[];
  measurement_notes?: string[];
  runs?: RunItem[];
};

export type Timeseries = {
  t0: string | null;
  metrics: Record<string, { unit: string; points: [number, number, string | null][] }>;
  regions: { start_s: number; end_s: number; preset: string; variant: string; trial: number; noisy: boolean }[];
  note: string;
};

export type Page<T> = { items: T[]; total: number; limit: number; offset: number };

/** GET /auth/me: sign-in state (auth "off" = open, no accounts). */
export type Me = {
  auth: "off" | "oidc"; signed_in: boolean; login_url?: string; user_id?: string; email?: string | null;
  name?: string | null; is_admin?: boolean; via?: "session" | "token";
};

export type ApiTokenItem = {
  token_id: string; name: string; prefix: string; created_at: string; last_used_at: string | null; revoked: boolean; user_id: string; project_id?: string | null;
};

export type Role = "viewer" | "editor" | "admin";

export type ProjectItem = {
  project_id: string; name: string; default_role: "viewer" | "editor" | null; my_role: Role | null;
  retention_days: number | null; retention_last_purge: RetentionReport | null;
  grants?: { team_id: string; team: string; role: Role }[];
};

export type TeamItem = { team_id: string; name: string; members: { user_id: string; email: string | null; name: string | null }[] };

/** What a retention purge deleted - or, with dry_run, would delete. */
export type RetentionReport = {
  project_id: string; retention_days: number | null; cutoff?: string; dry_run: boolean; runs: number; spans?: number;
  samples?: number; op_stats?: number; trial_results?: number; experiments?: number; purged_at?: string;
};

export type ShareItem = {
  share_id: string; project_id: string; created_at: string; expires_at: string; revoked: boolean; views: number;
  target: { experiment_id: string | null; run_ids: string[] | null; reference: string | null };
};

export type InventoryItem = {
  key: string; operation: string; algorithm: string; params: string | null; library: string | null; runtime: string;
  calls: number; runs: number; variants: string[]; scopes: string[]; projects: string[]; first_seen: string; last_seen: string;
  safe_for_passwords: boolean | null; meets_owasp_minimum: boolean | null; owasp_note: string; security_summary: string | null;
};

export type Inventory = {
  generated_at: string; note: string;
  apps: { service: string; algorithms: InventoryItem[]; summary: { algorithms: number; not_for_passwords: number; below_owasp_minimum: number } }[];
};
