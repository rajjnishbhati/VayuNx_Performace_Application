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

**PostgreSQL (Phase 4).** Point `VAYUNX_PROFILER_DB_URL` at Postgres, e.g.
`postgresql+psycopg://vayunx:secret@db-host:5432/vayunx`. The schema is managed by Alembic
(`profiler_service/migrations`): the Service upgrades the database to the newest revision when it starts,
and a pre-Alembic SQLite file (Phases 0-3) is brought up to date and stamped, keeping its rows. To move the
local SQLite data into an **empty** Postgres database (the source is only read, row counts are checked):

```powershell
.\.venv\Scripts\python.exe -m profiler_service.copy_db --from sqlite:///profiler.db --to postgresql+psycopg://vayunx:secret@db-host:5432/vayunx
```

Rehearsed on this machine with the real `profiler.db` (98,093 rows: 28 runs, 73,092 spans, 24,842 samples)
into PostgreSQL 17.6: 6.1 s, identical comparison output. The whole test suite also runs on Postgres:
`$env:VAYUNX_TEST_DB="postgres"` plus `VAYUNX_PG_BIN` (a folder with `initdb`/`pg_ctl`, e.g. the portable
EnterpriseDB zip) or `VAYUNX_TEST_PG_URL` (a server you own); see `tests/pgtools.py`.

**Sign-in (Phase 4).** Off by default: anyone who can reach the Service can use it, as before. To require
sign-in, register VAYUNX as an OpenID Connect client with your identity provider (Microsoft Entra ID, Okta,
Google, Keycloak...). Use redirect URI `<public URL>/auth/callback`, for example
`http://vayunx.example:3000/api/auth/callback` behind the UI. Then set:

| Variable | Meaning |
|---|---|
| `VAYUNX_AUTH=oidc` | require sign-in |
| `VAYUNX_OIDC_ISSUER`, `VAYUNX_OIDC_CLIENT_ID` | the provider (discovery at `<issuer>/.well-known/openid-configuration`) and this app's client id |
| `VAYUNX_OIDC_CLIENT_SECRET` | only for confidential clients; PKCE is always used |
| `VAYUNX_PUBLIC_URL` | where browsers reach the Service, e.g. `http://vayunx.example:3000/api` behind the UI |
| `VAYUNX_ADMIN_EMAILS` | comma-separated administrators (checked at every sign-in) |
| `VAYUNX_SESSION_SECRET`, `VAYUNX_SESSION_HOURS` | signs the short-lived sign-in cookie (set it when running several instances); session length, default 12 h |

- **People:** authorization-code flow with PKCE, state and nonce. The id_token's signature (provider JWKS),
  issuer, audience, expiry and nonce are all checked. After sign-in there is a server-side session in an
  HttpOnly, SameSite=Lax cookie; only its SHA-256 is stored, so sign-out really ends it.
- **Cross-site writes:** writes made with that cookie must come from the app's own origin.
- **SDKs and CI:** create an API token under **Settings → API tokens** and set `VAYUNX_API_TOKEN` where the
  SDK or CI runs (Python, Node.js, Next.js and Edge all send it). A token is shown once and stored as a SHA-256.
  Tokens can be revoked, and they cannot create tokens.
- **Tested** against a local test identity provider (`tests/fake_oidc.py`, real RS256 tokens): bad audience,
  issuer, expiry, nonce and signature are each refused. The full browser flow through the UI proxy was
  checked in Edge (`web/scripts/auth-check.mjs`). Not yet tried with a real company identity provider.

**Projects, teams and roles (Phase 4).** Every run and experiment belongs to a project. Everything recorded
before Phase 4 is in **Default project**. The header's Project selector filters lists and decides where new Lab
experiments go; **Settings → Projects & access** manages them.

- **Roles:** `viewer` (read) < `editor` (run the Lab, send data) < `admin` (manage the project's access).
- **Who gets which role:** a person's role is the highest of the project's *default role*, which everyone
  signed in gets (Default project: editor; new projects: none), and the roles granted to their teams.
  Administrators from `VAYUNX_ADMIN_EMAILS` can do everything, including creating projects and teams.
- **Out of scope looks like missing:** anything you cannot view answers 404, the same as something that
  does not exist.
- **Where new data goes:** a project named in the request (`"project"` in `POST /v1/runs` or `/v2/lab/runs`,
  or the resource attribute `vayunx.project` over OTLP), else the API token's project, else Default. It needs
  the editor role there.
- **Project-bound tokens:** an API token can be bound to one project (`{"name": "ci", "project": "payments"}`),
  and then reaches only that project.
- **Sign-in off:** projects still organise data, but everyone can see and change everything.

API: `GET/POST /v2/projects`, `PATCH /v2/projects/{id}` (name, default role),
`PUT/DELETE /v2/projects/{id}/grants[/{team}]`, `GET/POST /v2/teams`,
`POST/DELETE /v2/teams/{id}/members[/{user}]`, `GET /v2/users`. List endpoints accept `?project=`.

The report page loads d3 7.9.0 and d3-flame-graph 4.1.3 from jsdelivr. Without internet access it says so and shows a text tree instead.

## Crypto Lab and compare screen (Phase 2)

The main question the product answers: **"If we replace crypto algorithm A with B, what changes?"**

```powershell
.\.venv\Scripts\python.exe -m profiler_service                 # service, http://127.0.0.1:8010
cd web; npm install; npm run dev                                 # UI, http://localhost:3000
```

In the UI, click **▶ MD5 → Argon2id** and wait about 2 minutes for 5 trials × 10 s per algorithm. The result then shows:
- a verdict and a scorecard
- the App view, Machine view and Security tabs
- a what-if capacity estimate

The UI is documented in [`web/README.md`](web/README.md).

The Lab works without the UI too:

```powershell
.\.venv\Scripts\python.exe -m vayunx_lab presets
.\.venv\Scripts\python.exe -m vayunx_lab run --presets md5,argon2id-rfc9106-low --trials 5 --duration 10
```

### Crypto Lab (`vayunx_lab/`)

**Presets (an allow-list; the Service can start only these):**

| Preset | Parameters |
|---|---|
| MD5, SHA-256 | – |
| PBKDF2-HMAC-SHA256 | 600,000 iterations |
| bcrypt | cost 10 and 12 |
| scrypt | N = 2^17, r = 8, p = 1 (`maxmem` 256 MiB) |
| Argon2id, OWASP minimum | m = 19 MiB, t = 2, p = 1 |
| Argon2id, RFC 9106 low-memory | m = 64 MiB, t = 3, p = 4 |

Every operation hashes a synthetic password with a fresh 16-byte salt.

**Protocol for each trial:**
- It runs in a **fresh subprocess**, with a warm-up first.
- It lasts a fixed duration (default 10 s), with a minimum of 10 operations for slow presets.
- Variants are **interleaved** with alternating order (A B, B A, …).
- A **quiet-machine check** runs first. It waits a bounded time for machine CPU to drop below 25 %, and if it never does, the trial is flagged rather than blocked.
- Concurrency of 1, 2, 4 or 8 workers is optional.

**What each trial records:**
- per-operation timing. Up to 200,000 operations per trial keep raw durations, which gives **exact** percentiles; beyond that, a histogram accurate to ±6.25 %.
- CPU time per operation
- peak RSS: psutil `peak_wset` on Windows, `ru_maxrss` elsewhere
- threads and context switches
- the worker's own timer overhead
- an environment fingerprint: CPU model, cores, RAM, OS, Python, OpenSSL and library versions

**Noise check.** While a trial runs, the runner samples the worker process and the machine every 100 ms; that series feeds the Machine view.
The noisy/quiet decision uses **cumulative counters** instead: (machine busy CPU-seconds − benchmark CPU-seconds) / wall time over the measured window.

### Sampler v2 (SDK, spec C)

**Default metrics** from `start_sampling()`:
- **Process cost:** cores busy, CPU time, RSS (plus peak RSS on Windows), threads, context switches.
- **Machine noise:** CPU (average and busiest core), available RAM, swap, load, process count, CPU frequency, and "other cores busy".

**Timing:**
- The interval adapts: about 10 ms target while a cryptographic span or op runs, 1 s otherwise. Crypto activity wakes the sampler immediately.
- Samples are tagged `cryptographic` only while crypto runs, and use the same clock as spans.
- Measured on an i5-8400H: the real fast interval was about 16 ms with the app waiting and about 32 ms with it busy-looping (the Windows timer floor and the GIL). The sampler itself cost about 0.4–0.6 ms per tick.
- The CPU-cycles flag via Linux `perf` is not implemented; CPU time is used everywhere.

### Compare engine v2 and `/v2` API (spec D)

**Statistics:**
- **Comparisons:** 2 or more variants, and any of them can be the reference.
- **Per call:** the median of the trial medians, plus p95, p99, mean and IQR.
- **Spread:** the min–max of the trial medians and a seeded bootstrap 95 % CI.
- **Significance:** "not significant" when the variants' ranges overlap.

**Estimates:** CPU-seconds per operation, operations per second per core, and capacity at a given rate:
- cores = rate × CPU-seconds per operation
- RAM in flight = rate × latency × memory per operation

**Weak-data flags**, each naming the numbers it affects:
- too few trials, or too few operations
- a noisy machine
- unstable trials
- timer overhead
- short runs
- approximate memory
- concurrency

**Also in every result:** security notes per preset (spec G, OWASP, checked 2026-09-19), and a verdict built only from computed rows.

**Endpoints:**

| Endpoint | Purpose |
|---|---|
| `GET /v2/presets` | Built-in presets with their security notes and Node.js availability |
| `POST /v2/lab/runs` | Start a Lab experiment. One at a time; returns 409 while another is running. |
| `GET /v2/experiments[/{id}]` | Experiment list, or one experiment with progress and ETA |
| `POST /v2/experiments/{id}/cancel` | Cancel a queued or running experiment |
| `GET /v2/experiments/{id}/timeseries` | Machine data, with shaded "crypto ran here" regions |
| `GET /v2/compare?experiment_id=` or `?run_ids=a,b` | Compare a Lab experiment, or app runs (matched by span name, else `crypto.operation`; see Phase 3) |
| `GET /v2/runs` | Run search with filters and pagination |

Errors are `{"detail": {"error", "fix"}}`. Every `/v1` endpoint is unchanged.

**Data model:**
- `Run` gains `variant`, `experiment_id`, `trial_index`, `source` (lab/app) and `env_json`.
- New tables: `experiments` and `trial_results`.
- Existing SQLite files gain the new columns automatically, keeping their rows.

### Measured on this machine (one experiment)

Machine: Intel Core i5-8400H (4 cores / 8 threads), Windows 11 Pro 10.0.26200, Python 3.13.9, OpenSSL 3.5.6, argon2-cffi 25.1.0.
Run: 5 interleaved trials × 10 s per algorithm, started from the UI; 121 s total.

| Algorithm | Time per call (median) | p95 | Cores busy | Peak RSS | Flags |
|---|---|---|---|---|---|
| MD5 | 991 ns | 1.09 µs | 1.00 | 37.6 MiB | timer overhead (100 ns, 10 % of the call) |
| Argon2id m=64 MiB t=3 p=4 | 55.6 ms | 60.5 ms | 3.4 | 97.6 MiB | 1 of 5 trials noisy |

- **Change:** about 56,000× slower per hash, which is expected: password hashes are slow on purpose.
- **Estimate at 100 logins/s:** about 19 cores (233 % of this 8-thread machine) and 359 MiB RAM in flight.
- **Earlier runs on the same machine** gave Argon2id medians of 56.6 ms and 61.2 ms. Numbers vary between runs, and a different machine will differ more.

## Profile your own app: SDKs on OpenTelemetry (Phase 3)

Phase 3 answers the same question for a **real application**: run it once with MD5 and once with
Argon2id, and see what the switch costs in that app, under "My app" on the compare screen.

### Python: no code changes

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[otel]"        # the `vayunx` package and the vayunx-run launcher
$env:VAYUNX_VARIANT="md5";      .\.venv\Scripts\vayunx-run.exe -- python -m uvicorn app:app
$env:VAYUNX_VARIANT="argon2id"; .\.venv\Scripts\vayunx-run.exe -- python -m uvicorn app:app
```

`vayunx-run` (or `python -m vayunx run -- <cmd>`) puts a bootstrap `sitecustomize` on `PYTHONPATH`, so hooks
are installed before the app imports anything (even `from hashlib import md5` is captured). An existing
`sitecustomize` still runs. Or call it yourself, as early as possible:

```python
import vayunx
vayunx.init(service="my-api", variant="md5")      # endpoint defaults to http://127.0.0.1:8010

@vayunx.measure("login")                          # optional: label a code path (see "Matching" below)
def login(...): ...

with vayunx.span("checkout"): ...
vayunx.status()                                   # hooks, findings, counters
```

**Hooked (Python):** `hashlib` constructors, `new`, `pbkdf2_hmac`, `scrypt`; `hmac.new/digest`;
`bcrypt.hashpw/checkpw/kdf`; argon2-cffi `PasswordHasher.hash/verify` and `low_level`; passlib
`CryptContext.hash/verify`; PyNaCl `pwhash` and Ed25519 signing; PyJWT `encode/decode`; cryptography
`Fernet`, `PBKDF2HMAC.derive`, `Scrypt.derive`, `AESGCM`, `ChaCha20Poly1305`. Libraries imported later are
hooked on import. A hooked call made inside another (SHA-256 inside HMAC, PBKDF2 inside passlib) is
recorded once, as the outer call. cryptography's Rust KDF objects expose no parameters, so those calls are
reported as `PBKDF2-HMAC` / `scrypt` without them.

### Node.js and Next.js

See [`sdk-node/README.md`](sdk-node/README.md): `node --require @vayunx/profiler/register app.js`,
`vayunx-node run -- <cmd>`, or `registerVayunx()` in a Next.js `instrumentation.ts` (plus an Edge light
mode). It hooks `node:crypto` (including ESM named imports and WebCrypto) and bcrypt, bcryptjs, argon2 and
jsonwebtoken loaded through CommonJS.

**Shared configuration** (both SDKs): `VAYUNX_ENDPOINT`, `VAYUNX_SERVICE`, `VAYUNX_VARIANT`,
`VAYUNX_RUN_ID` (32 hex), `VAYUNX_PHASE`, `VAYUNX_SLOW_MS` (default 1), export interval
(`VAYUNX_EXPORT_INTERVAL_S` / `_MS`, default 5 s), `VAYUNX_GAUGES=0`, `VAYUNX_HOOKS=0`, `VAYUNX_DISABLE=1`.

### What is recorded

| Signal | How | Sent as |
|---|---|---|
| Every hooked call | Latency into an in-process `log2x8-ns` histogram per (operation, algorithm, parameters, library, scope) | OTLP metric `vayunx.crypto.duration`: explicit-bucket histogram, delta, bucket bounds equal to ours, so nothing is lost |
| Calls ≥ `slow_ms` | An OpenTelemetry span: `crypto.operation/algorithm/params/library/input_bytes/sync`, per-call thread CPU `vayunx.cpu_time_ms` where measurable, `vayunx.scope` | OTLP traces |
| Process and machine, every 1 s | Cores busy, CPU time, RSS, peak RSS, machine CPU, other cores busy, available memory; Node.js also event-loop delay p99, event-loop utilisation and threadpool wait (a 1-byte `randomFill` probe) | OTLP metrics (gauges), tagged `cryptographic` while crypto ran in the last 50 ms |

Why our own histograms rather than the OTel metrics SDK: one OTel Python `Histogram.record()` costs about
6.5 µs on this machine, about 9× an MD5 call. The shipped format is still standard OTLP.

**Findings:**
- `blocking_event_loop`: a slow synchronous crypto call on a thread that is running an asyncio loop
  (Python), or any slow synchronous crypto call (Node.js).
- `threadpool_queue` (Node.js): an async threadpool crypto call started while `UV_THREADPOOL_SIZE` of ours
  were already running.

**Safety (spec A)** is the same contract as the v1 SDK: never raise into the host, never mask its
exceptions, no network I/O on its threads, bounded memory (drops are counted), bounded shutdown
(`shutdown(timeout)`), quiet when the Service is down, and only sizes, parameters and timings are
recorded. Describers read public cost fields only (bcrypt cost digits, the `m=,t=,p=` field of a PHC
string) and the receiver scrubs attributes again. `shutdown()` restores every patched function.

**CPU time on Windows:** `thread_time()` there is `GetThreadTimes`, which advances only on the 15.6 ms
scheduler tick: an 8.7 ms PBKDF2 call measured 0.0 ms. The Python SDK uses `QueryThreadCycleTime`,
calibrated once against `perf_counter` (2.496 cycles/ns on the i5-8400H, matching its 2.5 GHz TSC).

**Measured overhead** (i5-8400H, Windows 11; from the tests):

| Call | Overhead per call |
|---|---|
| Python `hashlib.md5(pw).digest()` | 1.0–1.2 µs (the call itself: 0.84 µs; minimum over 15 interleaved rounds) |
| Node.js `createHash('md5').update(pw).digest()` | 0.7–0.8 µs |
| Slow calls (password hashes, KDFs) | adds two ~1.4 µs cycle-counter reads (Python, Windows) and one span |

### OTLP receiver

`POST /v1/traces` and `POST /v1/metrics` accept OTLP/HTTP protobuf or JSON, optionally gzipped, up to 16 MiB.
- **Resource attributes → run:** `service.name`, `vayunx.variant`, `vayunx.run_id`, `vayunx.phase`.
- **Spans with `crypto.*` →** category `cryptographic`.
- **`vayunx.crypto.duration` →** op-stats. Delta points add up; a cumulative point replaces its series.
- **`vayunx.process.*`, `vayunx.machine.*`, `vayunx.node.*` gauges →** samples.

Any OpenTelemetry SDK can send to it.

### Matching app runs ("My app")

- Crypto recorded inside a span the app named (`vayunx.span("login")`) is matched **by that name**. Every
  crypto call in the code path counts, whatever its operation: MD5 re-hashes at login, while Argon2id verifies.
- Named code paths shared by all runs win over unscoped operations, so incidental hashing elsewhere in the
  app (ETags, caches) does not decide what is compared.
- **CPU per call** comes only from per-call CPU-timed spans that cover at least half of the calls. It is
  never whole-process CPU divided by the call count, which would count request handling as crypto.
- **Memory per call** is not measured for apps; peak process memory is shown instead.
- The response says what each variant ran (`operation_detail`) and states these rules (`measurement_notes`).

### Example apps

[`examples/`](examples/README.md) has a FastAPI and a Next.js login service. Each switches MD5 → Argon2id
through `VAYUNX_VARIANT`. `examples/run_demo.py fastapi|next` runs both variants under load and prints the
**My app** link.

**Measured on this machine** (same machine as below; 4 concurrent clients on the same machine, 20 s per
variant, 50 users; service on :8010):

| App | Variant | Logins/s (client) | Client p50 | Crypto inside `login` (median) | CPU per call | Cores at 100 logins/s |
|---|---|---|---|---|---|---|
| FastAPI | md5 | 676.7 | 5.26 ms | hash MD5: 5.89 µs | not measured (fast calls) | – |
| FastAPI | argon2id | 55.0 | 68.85 ms | verify Argon2id: 65.0 ms | 60.6 ms | about 6.1 |
| Next.js | md5 | 541.1 | 6.99 ms | hash MD5: 10.8 µs | not measured (fast calls) | – |
| Next.js | argon2id | 60.5 | 63.84 ms | verify Argon2id: 60.8 ms | not measured (async, threadpool) | – |

- In the apps, MD5 takes 5.9–10.8 µs per call against about 1 µs in the Lab. The in-app time includes
  contention: 4 request threads share the GIL in Python and the event loop in Node.js.
- Argon2id in FastAPI used 60.6 ms of thread CPU per verify, against 32.5 ms per hash in the Lab
  (single worker). Four verifies ran at once on 4 cores / 8 hardware threads.
- Every run is flagged `few_trials` (one run per variant) and `noisy_machine`: the load generator ran on the
  same machine.

### Node.js Lab runner

Every preset also runs in Node.js as `<preset>@node`. It has the same parameters, synthetic password and
per-operation salt, and uses the same protocol and histogram (`vayunx_lab/node/worker.mjs`): `node:crypto`,
including the built-in `argon2Sync` (Node.js ≥ 24.7), and the bcrypt npm package. Node.js variants run with
concurrency 1. The runner reads their thread count and context switches with psutil, because libuv reports
no context switches on Windows. `GET /v2/presets` reports, per preset, whether this machine's Node.js can
run it; the UI shows a Node.js chip row and a **▶ Argon2id: Python vs Node.js** quick pick.

**Measured on this machine** (i5-8400H, Windows 11 Pro 10.0.26200; Python 3.13.9 with argon2-cffi 25.1.0
and OpenSSL 3.5.6; Node.js 24.15.0 with OpenSSL 3.5.5). Argon2id at the OWASP minimum (m = 19 MiB, t = 2,
p = 1), 5 interleaved trials × 10 s each, started as the quick pick does:

| Runtime | Time per hash (median) | Trial medians | p95 | CPU per hash | Peak RSS |
|---|---|---|---|---|---|
| Python, argon2-cffi | 31.8 ms | 30.9–32.1 ms | 39.5 ms | 32.5 ms | 52.4 MiB |
| Node.js, `crypto.argon2Sync` | 71.4 ms | 70.2–76.8 ms | 93.4 ms | 72.7 ms | 63.7 MiB |

- **Result:** Node.js was 2.2× slower per hash and used 2.2× more CPU. The trial ranges do not overlap, so
  the difference is significant.
- **Why:** not measured here. Both run on one core, so the difference is in the implementations
  (libargon2 in argon2-cffi against OpenSSL's Argon2 KDF).
- **Weak data:** all 10 trials were flagged noisy (other work above 0.5 cores; see item 10). The desktop
  was in normal use.

## Layout

```
profiler_service/       Part 1  FastAPI app (api.py), models/db (SQLAlchemy), schemas.py (wire format),
                                comparison.py (trees, deltas, summary), report_html.py (Part 3/5 pages)
vayunx_profiler_sdk/    Part 2  client.py (runs + spans), sampling.py (psutil thread), transport.py (stdlib HTTP)
demos/                  Part 4  crypto_baseline.py (MD5), crypto_remediated.py (Argon2id),
                                general_file_read.py (general category), run_demo.py (orchestrator)
tests/                          test_comparison.py (pure logic), test_service_e2e.py (live server + SDK)
vayunx/                 Phase 3 Python SDK on OpenTelemetry: _hooks.py (automatic hooks), _core.py (init/span/measure),
                                _ship.py (OTLP histograms), _otel.py (spans, gauges), __main__.py + _bootstrap/ (vayunx-run)
sdk-node/               Phase 3 Node.js / Next.js SDK (@vayunx/profiler, private): src/hooks.ts, core.ts, next.ts, edge.ts, cli.ts
vayunx_lab/node/        Phase 3 Node.js Lab worker (worker.mjs); vayunx_lab/node_runtime.py finds Node.js
profiler_service/otlp.py        Phase 3 OTLP/HTTP receiver (/v1/traces, /v1/metrics)
examples/               Phase 3 FastAPI and Next.js login apps, loadgen.py, run_demo.py
web/                    Phase 2 UI (Next.js); scripts/dod-check.mjs, scripts/phase3-check.mjs (browser checks)
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
| `GET /v1/runs?service=&phase=&limit=&offset=` | - | `[Run]`, newest first; `limit` 1–1000 (default 200), `offset` ≥ 0 |
| `GET /v1/runs/{run_id}` | - | `Run` (with `span_count`, `sample_count`, `op_stat_count`) |
| `POST /v1/runs/{run_id}/complete` | `{}` or `{"completed_at": ts}` | `Run`. After this, the run accepts no more spans/samples (409). |
| `POST /v1/spans` | one `Span` **or** an array of `Span` (≤ 10,000) | 201 `{"accepted": n}`. The batch is all-or-nothing. |
| `POST /v1/samples` | one `Sample` **or** an array of `Sample` (≤ 10,000) | 201 `{"accepted": n}` |
| `POST /v1/op-stats` | one `OpStats` **or** an array (≤ 10,000) | 201 `{"accepted": n}`. Fast-path summaries (added in Phase 1; additive). |
| `GET /v1/runs/{run_id}/op-stats` | - | `[OpStats]` for the run, oldest interval first |
| `GET /v1/comparison?baseline_run_id=&remediated_run_id=` | - | Comparison report JSON (below), in one call |
| `GET /report?baseline_run_id=&remediated_run_id=` | - | HTML report. Errors are a plain HTML page (cause, fix, link back), not JSON. |
| `GET /?page=&page_size=` | - | HTML run list (paged, 50 per page by default) + pair picker |

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

// OpStats  (one interval of a fast-path operation: many calls aggregated in the SDK)
{ "run_id": "…", "service": "crypto-demo", "category": "cryptographic",
  "op_name": "md5_hash", "attributes": { "crypto.algorithm": "MD5" },
  "interval_start": "…Z", "interval_end": "…Z",
  "count": <int ≥ 1>, "sum_ns": <int>, "min_ns": <int>, "max_ns": <int>,   // exact
  "p50_ns": <int>, "p95_ns": <int>, "p99_ns": <int>,                      // from the histogram (±6.25 %)
  "histogram": { "scheme": "log2x8-ns", "count": <same as count>, "sum_ns": <same as sum_ns>,
                 "min_ns": <int>, "max_ns": <int>,
                 "buckets": { "<bucket index>": <count>, ... } },          // counts must add up to count
  "sdk_overhead_ns": <float> }                                           // SDK's own measured cost per call
```

The histogram scheme `log2x8-ns` is fully specified in `vayunx_profiler_sdk/histogram.py` (0–7 ns exact; above that, 8 equal sub-buckets per power of two, so any SDK can produce identical buckets).

**Rules for SDK authors:**
1. **Create the run first** (`POST /v1/runs`). Generating `run_id` on the client (recommended) means starting a run needs no round trip, so the host app never waits on the service.
2. **Span IDs.** Generate a unique `span_id` per span, and set `parent_span_id` to the enclosing open span's ID.
3. **Ordering.** Spans may arrive in any order and in any batch. Children usually end, and are sent, before their parents.
4. **Duration.** `duration_ms` is authoritative. Measure it with a monotonic clock, and use wall-clock time only for `start_time`, setting `end_time = start_time + duration`. If `end_time − start_time` differs from `duration_ms` by more than max(1 ms, 1 %), the report shows a warning.
5. **Flush, then complete.** Flush all buffered spans and samples, then call `/complete`. Anything sent after completion is rejected.
6. **Metadata.** Put environment info in `metadata`. The report warns when `hostname`, `platform`, `runtime` or `cpu_count` differ between the two compared runs.
7. **Be fail-safe** (the Python SDK's contract; see [SDK safety](#sdk-safety)). Never raise into the host application, never send on the host's thread, bound every buffer, and never record secrets.
8. **Retry only what can succeed.** Retry connection failures, timeouts, 5xx, 408 and 429 with backoff. Treat other 4xx answers as permanent: drop that batch and count it. A 409 on `POST /v1/runs` or `/complete` means an earlier attempt already succeeded.

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
                         "change_per_call": "about 30,000× slower",                       // null unless matched
                         "category_baseline", "category_remediated", "attributes_baseline", "attributes_remediated", "notes": [...] } ],
  "sampling_comparison": [ { "metric_name", "category", "unit", "status", "baseline": {count,avg,min,max} | null, "remediated": ...,
                             "delta_avg", "pct_avg", "delta_min", "pct_min", "delta_max", "pct_max",
                             "change_avg": "3.7× higher", "change_peak": "2.9× more", "notes": [...] } ],
  "summary": "<plain-language sentence generated from the numbers above>",
  "warnings": [...], "thresholds": { "material_change_pct": 5.0, "min_samples_per_metric": 3 } }
```

The `flame_graph` trees are in d3-flame-graph's `{name, value, children}` shape, with `value` as the **inclusive** duration in ms.

**Number formatting** (`profiler_service/formatting.py`, used by the summary, the `change_*` fields and the HTML):
- **Units:** durations and sizes choose their own unit and show 3 significant digits (`580 ns`, `1.10 µs`, `38.3 ms`, `3.24 s`; `512 B`, `99.4 MiB`).
- **Changes:** a change of 2× or more in either direction is a ratio (`2.5× slower`, `about 30,000× slower`, `2.0× faster`). A smaller one is a percentage (`-34.4%`). Ratios of 100× or more are rounded to 2 significant digits and marked "about".
- **CPU:** shown as **cores busy** = CPU % ÷ 100, because psutil's CPU % is a share of **one** core.
- **Neutral words:** "slower" and "more" are descriptions, not verdicts. For password hashing, slower is the point.

## Python SDK (v1, manual)

> For automatic hooks with no code changes, use the Phase 3 `vayunx` package instead (see above).

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
    # Fast primitives (e.g. MD5, ~1 µs): aggregate into a histogram instead of one span per call
    md5 = run.op("md5_hash", attributes={"crypto.algorithm": "MD5"})
    for pw in passwords:
        with md5:
            hashlib.md5(pw).digest()
    profiler.stop_sampling()      # optional: run exit stops sampling for that run anyway

print(profiler.stats())           # recorded / sent / dropped counters, offline flag, measured op overhead
```

- **Nesting is automatic.** The innermost open span is tracked in a `ContextVar`, so a span opened inside another becomes its child.
  This tracking is per thread / asyncio task. A span opened in a **different thread** becomes a root.
- **Background delivery.** Spans, samples and op summaries go into a bounded queue and are sent by one background thread, in batches of 500 or every second.
  On exit the run stops its sampler, flushes (for at most 2 s), and queues `/complete`, even if the block raised.
  An exception inside a span adds `attributes.error = "<ExceptionType>"` and is never swallowed.
  Delivery is asynchronous: `run.spans_sent` / `run.samples_sent` / `run.op_stats_sent` are the counts the Service accepted, final after the run exits.
- **Spans vs ops.** Use `run.span()` for work that takes milliseconds, `run.op()` for fast primitives.
  An op records each call into a latency histogram (count, sum, min, max, p50/p95/p99) and sends one summary per second.
  It also emits a full span for slow calls (`slow_ms`, default 10 ms) and, optionally, for every Nth call (`span_sample_every`).
  An op can be shared across threads but is not re-entrant, so don't nest an op inside itself.
- **Sampling** runs a daemon thread that samples **the current process** with psutil. Supported metrics:

  | Metric | Unit | Meaning |
  |---|---|---|
  | `cpu_pct` | `%` | Percent of **one** logical CPU; can exceed 100 with multiple threads |
  | `memory_mb` | `MiB` | RSS |
  | `num_threads` | `count` | Thread count |

  Memory is sampled at start, each interval and at stop. CPU is sampled each interval and at stop (the start call only primes psutil).
  Samples go through the same background sender. Sampling errors never crash the application; `stop_sampling()` returns `{"samples_recorded", "errors"}`.
- **Dependencies.** HTTP uses only the standard library (`urllib`); the only third-party dependency is `psutil`.
- **Manual only.** There is no automatic instrumentation: no monkey-patching or bytecode hooks.

### SDK safety

The profiler must never break or slow down the application it measures. Every rule below has a test in `tests/test_sdk_safety.py` or `tests/test_sdk_privacy.py`.

| Rule | Behaviour |
|---|---|
| Never raise into host code | Internal failures are logged once (logger `vayunx_profiler`) and counted in `stats()["internal_errors"]`. Only API misuse raises, immediately: an invalid phase, category or metric, a nested run, or `start_sampling()` outside a run. |
| Never mask the host's exception | Run and span exit always return the host's own exception unchanged, even while the Service is down. |
| No network I/O on the caller's thread | Only the `vayunx-profiler-sender` thread talks to the Service. Starting a run never blocks, because run IDs are generated client-side. |
| Service unreachable | Offline mode: data stays queued and is retried with exponential backoff (0.5 s → 30 s). `stats()["offline"]` is True until the Service has accepted something, and whenever the last attempt failed. |
| Bounded memory | At most `max_queue` (default 10,000) data items are queued. Beyond that, new items are dropped and counted (`dropped_spans`, `dropped_samples`, `dropped_op_stats`). |
| Bounded exit delay | Run exit flushes for at most `flush_timeout_s` (default 2 s). Process exit flushes again only if that earlier flush did not already time out. |
| Permanent rejections | Other 4xx answers drop the batch and increment `send_errors`. |
| Thread-safe | Shared counters and queues are lock-protected; tested with 8 threads × 500 spans. |
| Privacy | Byte values, strings over 256 characters, and attribute or metadata keys that look secret (password, key, salt, token, hash, digest, plaintext, …) are dropped and counted (`dropped_attributes`). Size and parameter keys such as `crypto.key_bits` and `crypto.input_bytes` are allowed. |

**Measured overhead** (Intel Core i5-8400H, 4 cores / 8 threads, Windows 11, Python 3.13.9; one run each, so treat these as indicative):

| Path | Extra time per call | Notes |
|---|---|---|
| `run.op()` | ~1.8 µs | MD5 itself took 0.76 µs. The SDK also calibrates this once per client and reports it (`stats()["op_overhead_ns"]`, `sdk_overhead_ns` in each summary). |
| `run.span()`, caller thread only | ~9.0 µs | Was 13.9 µs before Phase 1. |
| `run.span()`, including the sender thread's work | ~16.4 µs | The sender serialises spans on another thread but shares Python's GIL, so a tight span loop pays for both. |

For primitives that take about a microsecond, use `run.op()`: a span costs many times the work it measures.

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
- **Integration** with `vayunx-perf-demo`, real VAYUNX, CryptoSPM or Abhed code.
- **Production hardening:** auth, multi-tenancy, deployment.

Phase 3 delivered what used to be listed here as "Non-Python SDKs" and "automatic / zero-code
instrumentation" (see [Phase 3](#profile-your-own-app-sdks-on-opentelemetry-phase-3)). The v1
`vayunx_profiler_sdk` is still manual-only.

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
8. **Wire-format additions beyond the original field list.** Phase 1 added two more that need confirming alongside the three below:
   - `POST /v1/op-stats` / `GET /v1/runs/{run_id}/op-stats`, with the `log2x8-ns` histogram scheme
   - client-generated `run_id` as the recommended way to start a run

   The three original additions:
   - `span_id`, which `parent_span_id` needs something to refer to
   - optional span `attributes`, used to keep span names identical across algorithm swaps
   - optional `metadata` on runs

   There is also one decision to confirm: spans and samples whose `service` differs from their run's `service` are **rejected**, so one run cannot hold several services (distributed traces are out of scope).
9. **More new labels to reconcile with VAYUNX's taxonomy.** Phase 2 adds several labels that are not severities (Critical/High/Medium/Low) or verdicts (MATCH/PARTIAL/MISSING):
   - "Safe for passwords" / "Not for passwords"
   - "weak data" and its flag codes (`few_trials`, `noisy_machine`, `unstable`, `timer_overhead`, …)
   - "not significant"
   - "noisy" / "quiet"
   - "reference"

   Reconcile them before the compare output feeds VAYUNX findings.
10. **Noise-check residual.** Even measured from cumulative counters, "other cores busy" reads about 0.2 cores higher during heavy multi-threaded variants on Windows: Argon2id at p=4 read 0.30–0.52 against MD5's 0.10–0.23 in the same quiet session. This is likely CPU accounting granularity, but that is not proven, and it pushes borderline heavy trials over the 0.5-core threshold. The threshold and method need review, ideally on Linux and macOS too.
11. **Untested platforms.** The Lab, sampler and UI were built and verified on Windows 11 only. The macOS paths (`ru_maxrss` in bytes, `sysctl` CPU model) and the Linux paths (`ru_maxrss` in KiB, `/proc/cpuinfo`) are written and unit-tested for units, but have not been run on those systems.
12. **Security references need re-checking** against the OWASP cheat sheet before shipping (spec G, checked 2026-09-19). Security notes for app data are best-effort by algorithm name, and their parameters are not checked.
13. **More new labels to reconcile with VAYUNX's taxonomy (Phase 3).** These are finding *kinds* and view
    labels. None carries a severity (Critical/High/Medium/Low) or a verdict (MATCH/PARTIAL/MISSING):
    - findings `blocking_event_loop` and `threadpool_queue`
    - `operation_kind` (`span` / `operation`)
    - "not measured" (CPU or memory per call)
    - runtime labels `Python` / `Node.js`

    Decide whether findings map to severities before they feed VAYUNX findings.
14. **Package names (decision D3).** `vayunx` (Python) and `@vayunx/profiler` (npm) are provisional. The npm
    package is `private`, and nothing has been published. The Python SDK ships in the same distribution as
    the Service (`vayunx-profiler` 0.3.0).
15. **Own histograms instead of OTel metrics for the fast path.** Standard OTLP on the wire, but not
    recorded through the OTel metrics API (cost; see Phase 3). Confirm this is acceptable for teams that
    expect OTel instruments.
16. **The Node.js SDK registers the global TracerProvider** when none exists, so Next.js framework spans
    (several per request) go to the Service. At high request rates the BatchSpanProcessor drops spans beyond
    its 2,048-span queue: one MD5 run kept 4,980 `login` spans for 13,539 logins. Histograms are complete;
    span lists are not. Confirm, or add sampling.
17. **App capacity is often blank.** It needs per-call CPU, which exists only for slow synchronous or
    threadpool-free calls that became spans. Async Node.js crypto and fast hashes show "not measured". The
    Lab remains the source for capacity numbers.
18. **Wire-format / API additions (Phase 3), to confirm alongside item 8:**
    - OTLP receiver endpoints
    - resource attributes `vayunx.run_id`, `vayunx.variant`, `vayunx.phase`, `vayunx.runtime`
    - span attributes `crypto.sync`, `vayunx.cpu_time_ms`, `vayunx.process_cpu_ms`, `vayunx.scope`,
      `vayunx.finding`
    - `<preset>@node` variant ids, and the `node` field in `GET /v2/presets`
    - compare fields `operation_kind`, `operation_detail`, `measurement_notes`
19. **Argon2id across runtimes.** Node.js `crypto.argon2Sync` took 2.2× the time and CPU of argon2-cffi
    at the same parameters here. The cause (implementation, build flags) is not investigated, and this is one
    noisy experiment on one machine.
20. **Phase 3 is verified on Windows only**, like item 11. The Next.js Edge light mode is built and type-checked
    but was not run in an Edge runtime.
21. **Sign-in and access to confirm.**
    - which identity provider to register with (OIDC works with Entra ID, Okta, Google and Keycloak; SAML is
      not built)
    - whether 12-hour sessions suit the organisation's policy
    - the new wording: "Sign in required", "admin", "revoked"
22. **Access model choices to confirm.**
    - Teams are managed in VAYUNX rather than taken from the identity provider's groups (mapping groups to
      teams is not built).
    - The Default project gives every signed-in person the editor role, so existing use keeps working.
    - New role words (`viewer` / `editor` / `admin`) sit next to VAYUNX's severity and verdict scales.

