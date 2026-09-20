# VAYUNX Crypto Profiler: project log

What was asked, what was decided and what was built, phase by phase, from the build brief (19 September 2026)
to the end of Phase 4 (20 September 2026).

- **Repository:** `vayunx-profiler-service` (git, branch `main`, 28 commits). Nothing was pushed or published.
- **Machine used for all measurements:** Intel Core i5-8400H (4 cores / 8 threads), 7.8 GiB RAM,
  Windows 11 Pro 10.0.26200, Python 3.13.9 (OpenSSL 3.5.6), Node.js 24.15.0 (OpenSSL 3.5.5).

---

## 1. The brief

The build brief asked to turn a Python/FastAPI proof of concept into a **cryptography migration profiler**.
The user picks two or more algorithms (for example MD5 and Argon2id) and sees what the switch costs, in about three clicks:

- **App view (instrumentation):** time per call, measured in the code.
- **Machine view (sampling):** CPU, memory and noise on the machine while the code runs.
- **Security context:** is this algorithm fit for password storage?
- **Capacity:** cores and memory needed at a given rate.
- **Statistical rigour:** repeated trials, spreads, significance and weak-data flags.

### Working rules (kept throughout)

1. **Phase by phase:** one phase at a time, a todo list, small commits, tests after each change, and tests first for logic.
2. **Real numbers only:** every figure comes from a run on this machine, and the machine is named.
3. **Code beats brief:** where the brief and the code conflicted, the code won, the user was told, and a fix was proposed.
4. **Questions:** at most two at a time, each with two options and a recommendation.
5. **Explicit yes needed:** nothing is published, pushed or deleted without the user's explicit yes.
6. **Phase reports:** every phase ends with a Done / Verified / Open report.
7. **SDK privacy:** the SDKs never record passwords, keys, salts, plaintexts, hashes or tokens. Only sizes, parameters and timings.
8. **No arbitrary code:** the service may launch only built-in presets with validated parameters.
9. **Review list:** the README's "REVIEW NEEDED" list is kept, and nothing on it is resolved silently.
10. **Label check:** any new label is flagged against VAYUNX's severity scale (Critical/High/Medium/Low) and its
    verdict language (MATCH/PARTIAL/MISSING). This is a standing user preference.

### Decisions made with the user

| Decision | Choice | User's words |
|---|---|---|
| Phase 0: git setup and op-stats | `git init` with a `.gitignore`; `/v1/op-stats` in Phase 1 | "Option A" |
| D2: UI technology | Next.js + TypeScript | "go ahead" |
| D1: SDK foundation | OpenTelemetry | "Option A" |
| Phase 4 scope | OIDC sign-in; own inventory page first (link to VAYUNX later) | "go ahead" |
| D3: package names | `vayunx` / `@vayunx/profiler`, provisional | still open, only needed before publishing |

---

## 2. Phase 0: orient (read-only)

- Read the repository, ran the tests and the demo, and checked the brief's facts against the code.
- Created the git repository with a `.gitignore` (databases and logs are never committed).
- **Commit:** `c9cb528` (baseline).

## 3. Phase 1: make the base safe and honest

- **SDK safety contract (spec A):**
  - never raise into the host application and never mask its exceptions;
  - network I/O only on a background thread, through a bounded queue (overflow is dropped and counted);
  - offline mode with backoff when the service is down;
  - flush with a hard timeout at exit;
  - thread-safe counters;
  - privacy scrubbing of secret-looking attributes.
- **Fast path for microsecond calls:** an in-process latency histogram (`log2x8-ns`, ±6.25 %) per operation,
  with one summary sent per interval.
- **Readable comparison output:** automatic units, "×" ratios for large changes and % for small ones, and CPU shown as "cores busy".
- **Run list:** no more N+1 queries, pagination, friendly HTML error pages, and an honest offline state.
- **Fixes found on the way:**
  - A flaky sampler test was polluted by offline sender threads from earlier tests.
  - On Windows, `num_threads()` and `num_ctx_switches()` cost about 1.5 ms each, so they are now read on the slow cadence only.
- **Commits:** `0029ec4`, `9b5eda0`, `19ed240`.

## 4. Phase 2: Crypto Lab, compare engine v2 and the compare screen

- **Crypto Lab (spec B)** has 8 allow-listed presets:
  - MD5 and SHA-256;
  - PBKDF2-HMAC-SHA256 with 600,000 iterations;
  - bcrypt at cost 10 and 12;
  - scrypt with N=2^17;
  - Argon2id at the OWASP minimum and at the RFC 9106 low-memory setting.

  Each trial runs in a fresh worker process. Trials alternate order (A B, B A, …), a quiet-machine check runs first,
  and percentiles are exact up to 200,000 operations per trial.
- **Sampler v2 (spec C):** process cost and machine noise, with a faster sampling interval while crypto runs.
- **Compare engine v2 and the `/v2` API (spec D):**
  - median of the trial medians, with p95 and p99;
  - spread as min–max plus a bootstrap confidence interval;
  - "not significant" when the ranges overlap;
  - weak-data flags that name the numbers they affect;
  - capacity estimates and security notes.
- **Compare screen (spec E, Next.js):**
  - result in one click, Machine view in two, Security in three;
  - dark mode and a table twin for every chart;
  - the colour palette checked with the validator.
- **Fixes:**
  - Histogram quantisation made trial medians identical; fixed with exact percentiles.
  - The benchmark's own load biased the noise signal; fixed with cumulative CPU counters.
  - React 19 lint issues were fixed.
  - The Next.js dev server blocked the `127.0.0.1` origin, so the page never came alive; fixed.
- **Measured** (5 trials × 10 s): MD5 took **991 ns** per hash and Argon2id (64 MiB, t=3, p=4) took **55.6 ms**,
  about 56,000× slower. At 100 logins/s that is about 19 cores and 359 MiB in flight.
- **Open:** macOS was not verified.
- **Commits:** `b437a11`, `6277c15`, `8084ece`, `0764ede`, `66727de`, `adee427`, `78bc478`, `0659982`.

## 5. Phase 3: app SDKs for Python and Next.js

- **OTLP receiver:** `/v1/traces` and `/v1/metrics` accept standard OpenTelemetry data (`44fdf7d`).
- **Python SDK (`vayunx`, `vayunx-run`)** needs no code changes (`ca777ae`):
  - It hooks hashlib, hmac, bcrypt, argon2-cffi, passlib, PyNaCl, PyJWT and cryptography.
  - Slow calls become spans with CPU time.
  - It raises a finding when a slow synchronous call blocks the event loop.
  - On Windows, CPU time comes from `QueryThreadCycleTime`, because the normal thread clock only ticks every 15.6 ms.
  - Measured overhead: about 1.0–1.2 µs per MD5 call.
- **Node.js / Next.js SDK (`sdk-node`)** (`9ec620f`):
  - It hooks `node:crypto`, WebCrypto, bcrypt, bcryptjs, argon2 and jsonwebtoken.
  - Next.js support comes through `instrumentation.ts`, with an Edge light mode.
  - It reports event-loop and thread-pool metrics, and flags calls that had to wait for the thread pool.
  - Measured overhead: about 0.7–0.8 µs per MD5 call.
- **Scope:** a span name such as `login` now labels the crypto calls inside it, so "My app" compares the right work (`cb896b1`).
- **Example apps:** FastAPI and Next.js login services that switch MD5 → Argon2id through `VAYUNX_VARIANT` (`c991291`):

  | App | MD5 (login) | Argon2id verify (login) | Logins/s (MD5 → Argon2id) |
  |---|---|---|---|
  | FastAPI | 5.89 µs | 65.0 ms | 677 → 55 |
  | Next.js | 10.8 µs | 60.8 ms | 541 → 60.5 |

- **Node.js Lab runner (`<preset>@node`)** (`ea72520`), measured with Argon2id at m=19 MiB, t=2, p=1:
  Python took **31.8 ms** per hash and Node.js took **71.4 ms**, 2.2× slower and significant. All trials were flagged noisy.
- **Fixes:**
  - App CPU per call was process CPU divided by call count; it now comes from per-call measurements only.
  - The verdict wrongly called the difference between two password hashes "expected" (`3156a97`).
  - Windows command resolution in the Node launcher.
- **Docs and verification:** `452a10f`. The UI was checked in Edge in light, dark and phone layouts.

## 6. Phase 4: enterprise

| Step | What it gives you | Commit |
|---|---|---|
| 1. PostgreSQL | Managed schema migrations (Alembic), BIGINT ids, and a copy tool from SQLite into an empty Postgres. The whole test suite runs on both. Your real `profiler.db` (98,093 rows) copied in 6.1 s. | `d6e5e9b` |
| 2. Sign-in | Off by default. OIDC sign-in with PKCE; sessions and tokens stored only as hashes; a same-origin check on writes; API tokens for SDKs and CI. | `a688c7c` |
| 3. Projects and roles | Viewer, editor and admin roles per project, given to teams. Anything out of scope answers 404. Project-bound tokens. Existing data went to "Default project". | `4fbd8df` |
| 4. Data retention | Per project, off by default. Preview before deleting, then confirm. Hourly purge, with a global off switch. | `5b93edd` |
| 5. Exports | PDF report plus CSV (scorecard, per-trial data, runs), with the same numbers as the screen. Spreadsheet formula attacks are blocked. | `8b92930` |
| 6. Share links | Read-only, 7–90 days, revocable, no sign-in needed to open. | `05c52ff` |
| 7. CI gate | `vayunx-gate --rule "login p95 < 250ms"`. Exit codes: 0 pass, 1 fail, 3 no data, 2 error. JUnit XML and a GitHub summary. | `a3bd03a` |
| 8. Crypto inventory | Which algorithms each app used, with password-hash parameters checked against the OWASP minimums. | `f5102d5` |

- **Fixes found on the way:**
  - The Node SDK could lose its final export when an app exited.
  - Very old database files could not be upgraded.
  - The copy tool treated seeded rows as "not empty".
- **Tests:** 232 pass on SQLite and 232 on PostgreSQL 17, plus 11 Node SDK tests.
- **Browser checks in Edge:** sign-in, projects, retention, share links and inventory.

---

## 7. Where things stand

- **Service** (http://127.0.0.1:8010): runs the latest code on `profiler.db`, with sign-in off and every project keeping data forever.
- **UI:** http://127.0.0.1:3000.
- **Backups:** one taken before each database change, as `profiler-backup-*.db` in the project folder.

### Open items (see README "REVIEW NEEDED" 1–26)

- **Labels to reconcile** with VAYUNX's severity and verdict scales:
  - Phase 1: `matched` / `baseline_only` / `remediated_only`, the "slower / faster / no material change" wording, "indicative only"
  - Phase 2: "Safe for passwords" / "Not for passwords", "weak data" and its flag codes, "not significant",
    "noisy" / "quiet", "reference"
  - Phase 3: findings, "not measured", runtime labels
  - Phase 4: viewer / editor / admin, PASS / FAIL / NO DATA, the inventory statuses, "Sign in required"
- **Not built:**
  - SAML sign-in
  - teams taken from the identity provider's groups
  - the link into the VAYUNX / CryptoSPM inventory
  - an "organisation only" option for share links
  - the app-run time-series chart
- **Not yet tried:**
  - a real company identity provider
  - running the live service on a real Postgres server
  - macOS
  - the Next.js Edge runtime
- **Package names** (`vayunx`, `@vayunx/profiler`): still undecided; only needed before publishing.
