# VAYUNX Profiler Service (sampling + instrumentation)

> **Standalone proof of concept. Not production-ready.** No auth, no multi-tenancy, no deployment hardening.
> Separate from `vayunx-perf-demo`: it neither imports nor modifies it.

## Why this exists

The Migration Performance Impact module (`vayunx-perf-demo`) can tell you **that** a before/after number changed.
It measures network-observable Tier 1 metrics, plus mocked Tier 2 system metrics.
It cannot tell you **which function, inside which code path**, caused the change.

This Profiler Service closes that gap by instrumenting code directly. It has three parts:
- **Profiler Service** (FastAPI + SQLAlchemy): language-agnostic ingestion, storage, baseline-vs-remediated comparison and a flame-graph report.
- **Python SDK** (`vayunx_profiler_sdk`): a thin wrapper over the Service's plain HTTP/JSON API. Other languages can get SDKs later without Service changes (see [Wire format](#wire-format-v1)).
- **Demo scripts** that prove the concept on self-contained code only.

## Terminology (read first)

| Term | Meaning in this project |
|---|---|
| **Instrumentation** | Explicit, manually added timing of **named spans** with parent/child nesting (e.g. `login` → `hash_password` → `sanitize_input` / `compute_digest`). Each span records start, end and duration. It needs SDK calls in the target code, but no infrastructure access. |
| **Sampling** | **Periodic system-resource metrics** of a running process: CPU %, memory, thread count, collected at a fixed interval by a background thread (psutil). No code changes beyond start/stop. |
| **Category** | A tag on every span and sample: `general` (ordinary code, e.g. file reads) or `cryptographic` (the same mechanism applied to crypto operations). One code path serves both. |

> ⚠️ **Naming collision (REVIEW NEEDED, item 1).**
> In classical profiling, "sampling" usually means **statistical call-stack sampling**: periodically interrupting a program to record its stack, as `py-spy` or `async-profiler` do. **That is not built here.**
> "Sampling" in this project is also conceptually close to the Performance Module's **Tier 2** (CloudWatch-style CPU/memory/network). These are three different things sharing one word. Don't assume one when reading about another.

## Quick start

```powershell
cd D:\Downloads\Vayunx_Performance_Application\vayunx-profiler-service
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e .          # makes vayunx_profiler_sdk importable, like a consumer would

# Everything: starts the Service if needed, runs all demos, prints summaries + report URLs
.\.venv\Scripts\python.exe demos\run_demo.py --keep-service

# Or step by step
.\.venv\Scripts\python.exe -m profiler_service            # http://127.0.0.1:8010/
.\.venv\Scripts\python.exe demos\crypto_baseline.py       # prints RUN_ID=...
.\.venv\Scripts\python.exe demos\crypto_remediated.py     # prints RUN_ID=...
.\.venv\Scripts\python.exe demos\general_file_read.py     # prints BASELINE_RUN_ID=... / REMEDIATED_RUN_ID=...
# then open http://127.0.0.1:8010/ and pick a baseline + remediated run, or:
#   http://127.0.0.1:8010/report?baseline_run_id=<id>&remediated_run_id=<id>

.\.venv\Scripts\python.exe -m pytest -q                   # unit + end-to-end tests
```

**Configuration:**

| Variable | Default | Purpose |
|---|---|---|
| `VAYUNX_PROFILER_DB_URL` | `sqlite:///profiler.db` | Database. Postgres later is a connection-string change. |
| `VAYUNX_PROFILER_HOST` | `127.0.0.1` | Listen address. |
| `VAYUNX_PROFILER_PORT` | `8010` | Listen port. |

The report page loads d3 7.9.0 and d3-flame-graph 4.1.3 from jsdelivr. Without internet access it says so and shows a text tree instead.

## Layout

```
profiler_service/       Part 1  FastAPI app (api.py), models/db (SQLAlchemy), schemas.py (wire format),
                                comparison.py (trees, deltas, summary), report_html.py (Part 3/5 pages)
vayunx_profiler_sdk/    Part 2  client.py (runs + spans), sampling.py (psutil thread), transport.py (stdlib HTTP)
demos/                  Part 4  crypto_baseline.py (MD5), crypto_remediated.py (Argon2id),
                                general_file_read.py (general category), run_demo.py (orchestrator)
tests/                          test_comparison.py (pure logic), test_service_e2e.py (live server + SDK)
```

## MVP demo scenario

| Script | Category | What it measures |
|---|---|---|
| `crypto_baseline.py` | cryptographic + general | 25 users sign up, then log in; password hashing is **unsalted single-pass MD5** (**deliberately insecure** fixture) |
| `crypto_remediated.py` | cryptographic + general | the identical flow with **salted Argon2id** |
| `general_file_read.py` | general only | reading a 32 MiB temp file 5× with 4 KiB chunks (baseline) vs 1 MiB chunks (remediated); no cryptography |

Span tree in both crypto scripts (sampling runs for the whole script):

```
signup (general)                        login (general)
├─ hash_password (cryptographic)        ├─ load_user (general)
│  ├─ sanitize_input (cryptographic)    ├─ hash_password (cryptographic)
│  └─ compute_digest (cryptographic)    │  ├─ sanitize_input (cryptographic)
└─ store_user (general)                 │  └─ compute_digest (cryptographic)
                                        └─ check_credentials (cryptographic)
```

**Why `compute_digest`, not `md5_hash` / `argon2id_hash`.** Spans are matched across runs by **name + position**.
If each script used its algorithm name, the key hashing span would show up only as "present in baseline only" / "present in remediated only", with no delta.
Both scripts therefore use the neutral name `compute_digest` and record the algorithm as a span attribute (`{"algorithm": "MD5"}` vs `{"algorithm": "Argon2id", "params": ...}`).
The report shows those attributes next to each delta.

### Hashing algorithm choice (REVIEW NEEDED, item 3): **Argon2id**

The remediated demo uses **Argon2id** with `time_cost=3`, `memory_cost=65536 KiB (64 MiB)`, `parallelism=4`, `hash_len=32` and a 16-byte random salt.
These parameters are RFC 9106 §4's second recommended option (argon2-cffi's `RFC_9106_LOW_MEMORY` profile). Rationale:
- It is the first-choice password hash in OWASP's Password Storage Cheat Sheet and is standardized in RFC 9106 (Password Hashing Competition winner).
- It is **memory-hard**, which is exactly the resource trade-off a CPU/memory profiler should make visible. bcrypt is not memory-hard, so the memory metric would show little.
- bcrypt also silently truncates passwords at 72 bytes.

bcrypt remains a valid alternative. This is a deliberate choice for this demo, not a VAYUNX-wide recommendation.

### Illustrative figures are **not** used

The source conversation for this project contained illustrative mockup figures for MD5 vs Argon2id response time, CPU and memory.
They were AI-generated concept-explanation numbers, not measurements, and **they appear nowhere in this build**: not in code, seed data, tests or example output.
Every number in a report, including the summary sentence, is computed from spans and samples the demo scripts actually produced on the machine they ran on.
Real numbers differ from any illustrative figures, and between machines. That is expected.

## Wire format (v1)

Plain JSON over HTTP. Nothing assumes a Python sender, so this section is the full contract for a future Java/C#/Node SDK.
The interactive schema is at `/docs` (OpenAPI).

**Conventions:**
- **Timestamps:** RFC 3339 **with an offset** (`"2026-09-17T10:15:30.123456Z"` or `+00:00`). Naive timestamps are rejected.
- **IDs** (`run_id`, `span_id`, `parent_span_id`): 1–64 characters from `[A-Za-z0-9._:-]`.
- **Allowed values:** `category` is `general` or `cryptographic`. `phase` is `baseline` or `remediated` (not pre/post, so it generalizes beyond crypto migration).
- **Floats** must be finite. NaN and Infinity are rejected with 422.
- **Unknown fields** are rejected (422), so typos don't pass silently.

### Endpoints

| Method & path | Body | Result |
|---|---|---|
| `GET /healthz` | - | `{"status":"ok","version":...,"wire_schema_version":"1"}` |
| `POST /v1/runs` | `RunCreate` | 201 `Run`. 409 if `run_id` already exists. |
| `GET /v1/runs?service=&phase=&limit=` | - | `[Run]`, newest first |
| `GET /v1/runs/{run_id}` | - | `Run` (with `span_count`, `sample_count`) |
| `POST /v1/runs/{run_id}/complete` | `{}` or `{"completed_at": ts}` | `Run`. After this, the run accepts no more spans/samples (409). |
| `POST /v1/spans` | one `Span` **or** an array of `Span` (≤ 10,000) | 201 `{"accepted": n}`. The batch is all-or-nothing. |
| `POST /v1/samples` | one `Sample` **or** an array of `Sample` (≤ 10,000) | 201 `{"accepted": n}` |
| `GET /v1/comparison?baseline_run_id=&remediated_run_id=` | - | Comparison report JSON (below), in one call |
| `GET /report?baseline_run_id=&remediated_run_id=` | - | HTML report |
| `GET /` | - | HTML run list + pair picker |

**Errors** use `{"detail": ...}`:
- **404:** unknown run.
- **409:** duplicate `span_id` within a run, a completed run, or a duplicate `run_id`.
- **413:** batch too large.
- **422:** validation failure. For batch items, `detail` includes the failing `index`. This covers: service not matching the run's service, empty batch, same run passed twice, wrong phases, or runs from different services in a comparison.

### Objects

```jsonc
// RunCreate
{ "run_id": "optional-client-id", "service": "crypto-demo", "label": "md5-baseline", "phase": "baseline",
  "metadata": { "sdk": "vayunx-profiler-sdk-java/0.1.0", "runtime": "java 21", "platform": "...", "hostname": "...", "pid": 1234, "cpu_count": 8 } }

// Span  (span_id is chosen by the sender and must be unique within the run)
{ "run_id": "…", "service": "crypto-demo", "category": "cryptographic",
  "span_id": "a1b2c3", "parent_span_id": "f00d42",            // null for a root span
  "span_name": "compute_digest",
  "start_time": "2026-09-17T10:15:30.100000Z", "end_time": "2026-09-17T10:15:30.160000Z",
  "duration_ms": 60.0,
  "attributes": { "algorithm": "Argon2id" } }                 // optional; ≤ 32 keys; string/number/bool values

// Sample
{ "run_id": "…", "service": "crypto-demo", "category": "cryptographic",
  "metric_name": "memory_mb", "value": 97.4, "unit": "MiB", "timestamp": "2026-09-17T10:15:30.200000Z" }
```

**Rules for SDK authors:**
1. **Create the run first** (`POST /v1/runs`) and use the returned `run_id`.
2. **Span IDs.** Generate a unique `span_id` per span, and set `parent_span_id` to the enclosing open span's ID.
3. **Ordering.** Spans may arrive in any order and in any batch. Children usually end, and are sent, before their parents.
4. **Duration.** `duration_ms` is authoritative. Measure it with a monotonic clock, and use wall-clock time only for `start_time`, setting `end_time = start_time + duration`. If `end_time − start_time` differs from `duration_ms` by more than max(1 ms, 1 %), the report shows a warning.
5. **Flush, then complete.** Flush all buffered spans and samples, then call `/complete`. Anything sent after completion is rejected.
6. **Metadata.** Put environment info in `metadata`. The report warns when `hostname`, `platform`, `runtime` or `cpu_count` differ between the two compared runs.

### Comparison report JSON

```jsonc
{ "schema_version": "1", "service": "crypto-demo",
  "baseline":   { "run_id", "label", "phase", "created_at", "completed_at", "metadata", "categories": [...],
                  "span_count", "sample_count",
                  "flame_graph": { "name": "baseline: md5-baseline", "value": <total ms>, "synthetic_root": true,
                                   "children": [ { "name", "value": <inclusive total ms>, "count", "mean_ms", "min_ms", "max_ms",
                                                   "category", "attributes": {k: [values]}, "children": [...] } ] } },
  "remediated": { ...same shape... },
  "span_comparison": [ { "path": ["login","hash_password"], "span_name", "depth",
                         "status": "matched" | "baseline_only" | "remediated_only",
                         "baseline": {count,total_ms,mean_ms,min_ms,max_ms} | null, "remediated": {...} | null,
                         "delta_total_ms", "pct_total", "delta_mean_ms", "pct_mean",      // null unless matched
                         "category_baseline", "category_remediated", "attributes_baseline", "attributes_remediated", "notes": [...] } ],
  "sampling_comparison": [ { "metric_name", "category", "unit", "status", "baseline": {count,avg,min,max} | null, "remediated": ...,
                             "delta_avg", "pct_avg", "delta_min", "pct_min", "delta_max", "pct_max", "notes": [...] } ],
  "summary": "<plain-language sentence generated from the numbers above>",
  "warnings": [...], "thresholds": { "material_change_pct": 5.0, "min_samples_per_metric": 3 } }
```

The `flame_graph` trees are in d3-flame-graph's `{name, value, children}` shape, with `value` as the **inclusive** duration in ms.

## Python SDK

```python
from vayunx_profiler_sdk import ProfilerClient

profiler = ProfilerClient(service_url="http://localhost:8010", service_name="crypto-demo")

with profiler.run(label="md5-baseline", phase="baseline") as run:
    profiler.start_sampling(interval_ms=500, metrics=["cpu_pct", "memory_mb"], category="cryptographic")
    with run.span("hash_password", category="cryptographic", attributes={"algorithm": "MD5"}):
        with run.span("sanitize_input", category="cryptographic"):
            ...
        with run.span("compute_digest", category="cryptographic"):
            ...
    profiler.stop_sampling()      # optional: run exit stops sampling for that run anyway
```

- **Nesting is automatic.** The innermost open span is tracked in a `ContextVar`, so a span opened inside another becomes its child.
  This tracking is per thread / asyncio task. A span opened in a **different thread** becomes a root.
- **Batching.** Spans are buffered and sent in batches (every 500 spans, and at run exit).
  On exit the run stops its sampler, flushes, and calls `/complete`, even if the block raised.
  An exception inside a span adds `attributes.error = "<ExceptionType>"` and is never swallowed.
- **Sampling** runs a daemon thread that samples **the current process** with psutil. Supported metrics:

  | Metric | Unit | Meaning |
  |---|---|---|
  | `cpu_pct` | `%` | Percent of **one** logical CPU; can exceed 100 with multiple threads |
  | `memory_mb` | `MiB` | RSS |
  | `num_threads` | `count` | Thread count |

  Memory is sampled at start, each interval and at stop. CPU is sampled each interval and at stop (the start call only primes psutil).
  Samples are buffered and sent about every 2 s. Sampling errors never crash the application; `stop_sampling()` returns `{"samples_sent", "errors"}`.
- **Overhead.** The sampler thread and HTTP flushes run inside the measured process and are included in what they measure. This is small but not zero.
- **Dependencies.** HTTP uses only the standard library (`urllib`); the only third-party dependency is `psutil`.
- **Manual only.** There is no automatic instrumentation: no monkey-patching or bytecode hooks.

## How comparison works

- **Explicit pairing.** The caller names both runs. The baseline must have phase `baseline`, the remediated run `remediated`, and both must belong to the same service.
- **Span matching.** Span IDs differ between runs, so spans are matched by **the path of span names from the root** (name + position).
  Same-name siblings at the same path are **merged** (count, total, mean, min, max), as a flame graph does.
  Deltas are given for the total and the mean. A count difference is noted.
- **Nothing missing is shown as zero.** A path present in one run only gets status `baseline_only` / `remediated_only` and null deltas.
  % change is null when the baseline value is 0.
- **Sampling.** Samples are grouped by (metric, category): avg/min/max, then absolute and % deltas.
  Differing units mean no deltas are computed.
  Fewer than 3 samples on either side is flagged as *indicative only*. This happens with the MD5 baseline, which finishes within one sampling interval.
- **Summary sentence.** Generated only from the computed rows:
  - total matched top-level time
  - the largest per-span change
  - average CPU and peak memory
- **Interpretation clauses in the summary.**
  - **Slow, memory-hard:** added only when the largest change is a cryptographic span, it slowed by ≥ 5 %, **and** peak memory rose by ≥ 5 %.
  - **Memory not evidenced:** if the span slowed but memory didn't rise materially, the summary says memory-hardness is **not** evidenced by the data.
  - **No material change:** used for changes within ±5 %.
- **Warnings on every report:**
  - orphan or cyclic parent links
  - duration inconsistencies
  - children longer than their parent (overlapping spans)
  - incomplete runs
  - environment differences between runs
  - a standing note that this is a single run against a single run, with no repeated trials

**Report page:**
- Shows service, categories, both runs' labels, phases, run IDs and environment.
- Stacks two clearly labeled flame graphs: blue **BASELINE**, red **REMEDIATED**. Frame colour shows category: blue for general, orange for cryptographic.
- A toggle switches between "fit each graph" and a **shared time scale** (widths proportional to each run's total).
- Hovering shows total, count, mean and attributes.
- Below the graphs: the span delta table, the sampling delta table, and the generated summary.

## Limitations

- **Single run vs single run.** There are no repeated trials, so noise is not controlled. Aggregating repeated spans at one path reduces, but does not remove, it.
- **Short runs sample poorly.** Sampling is interval-based, so a run shorter than the interval gets only start/stop samples, and the report says so.
- **Process-wide metrics.** CPU/memory cover the whole process, so they can't be attributed to individual spans. Only the timing is per span.
- **Two processes vs one.** The crypto demos run baseline and remediated in **separate processes**, for a clean memory baseline. The general demo runs both in **one process**, so its memory samples share interpreter state.
- **Thread-local nesting.** SDK span nesting does not cross threads.
- **Demo-grade service.** No auth, retention or rate limiting. Services bind to 127.0.0.1 by default.

## Explicit non-goals (not implemented)

- **Statistical call-stack sampling** (`py-spy` / `async-profiler` style).
- **Non-Python SDKs.** The wire protocol is kept language-agnostic so one can be added later.
- **Integration** with `vayunx-perf-demo`, real VAYUNX, CryptoSPM or Abhed code.
- **Production hardening:** auth, multi-tenancy, deployment.
- **Automatic / zero-code instrumentation.**

## REVIEW NEEDED (open items: deliberately not resolved here)

1. **Sampling terminology.** "Sampling" here means periodic system metrics, not classical statistical stack sampling. The team needs to confirm this name is acceptable, or rename it, before wider use.
2. **Overlap with the Performance Module's Tier 2.** It is undecided whether this Service's sampling becomes the same subsystem as `vayunx-perf-demo`'s Tier 2 (CloudWatch-style metrics) or stays separate. They are **not** merged in this build.
3. **Hashing algorithm choice.** Argon2id (with the parameters and rationale above) was chosen deliberately over bcrypt for the remediated demo. Confirm it is the preferred reference.
4. **Report format.** Reports are HTML/JSON now. Whether a downloadable PDF is needed later is open.
5. **SDK distribution / packaging.** The SDK is installable today via `pip install -e .` from this repository (`pyproject.toml`). How it reaches other teams (internal index, git dependency, vendoring) is undecided.
6. **Real integration into CryptoSPM / Abhed** is out of scope. The plan is to prove the Service and SDK on the self-contained demos first, then hand the SDK to application teams.
7. **New labels vs VAYUNX's existing taxonomy.** This build introduces demo-local labels that are **not** VAYUNX severities (Critical/High/Medium/Low) or verdicts (MATCH/PARTIAL/MISSING):
   - span/sample match status: `matched` / `baseline_only` / `remediated_only`
   - summary wording: "slower / faster / no material change (±5 %)"
   - "indicative only" for low sample counts

   Reconcile these with the existing taxonomy before this feeds any VAYUNX findings or Impact Analysis output.
8. **Wire-format additions beyond the original field list.** Three additions need confirming as the intended contract:
   - `span_id`, which `parent_span_id` needs something to refer to
   - optional span `attributes`, used to keep span names identical across algorithm swaps
   - optional `metadata` on runs

   There is also one decision to confirm: spans and samples whose `service` differs from their run's `service` are **rejected**, so one run cannot hold several services (distributed traces are out of scope).
