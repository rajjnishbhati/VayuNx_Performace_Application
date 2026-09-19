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
};

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
  other_operations?: string[];
  runs?: RunItem[];
};

export type Timeseries = {
  t0: string | null;
  metrics: Record<string, { unit: string; points: [number, number, string | null][] }>;
  regions: { start_s: number; end_s: number; preset: string; variant: string; trial: number; noisy: boolean }[];
  note: string;
};

export type Page<T> = { items: T[]; total: number; limit: number; offset: number };
