// One Lab trial in Node.js, in a fresh process:  node vayunx_lab/node/worker.mjs --preset md5 --duration 10
//
// The Node.js twin of vayunx_lab/worker.py: same presets (same parameters, same synthetic password, a fresh
// 16-byte salt per operation where the algorithm takes one), same protocol - one JSON object per line:
//   {"event":"ready"} {"event":"measure_start"} {"event":"measure_end"} {"event":"result"}
// and the same log2x8-ns histogram, so the Service stores and compares Node trials exactly like Python ones.
//
// Allow-list only: --preset must be one of PRESETS below; nothing else can be run. Concurrency 1 only.
// `--list` prints which presets this Node.js can run (bcrypt needs the npm package in this folder).

import { createHash, pbkdf2Sync, randomBytes, scryptSync } from "node:crypto";
import * as crypto from "node:crypto";
import { createRequire } from "node:module";
import * as os from "node:os";

const require = createRequire(import.meta.url);
const SYNTHETIC_PASSWORD = Buffer.from("vayunx-lab synthetic password 0001"); // same bytes as presets.py
const SALT_LEN = 16;
const MAX_DURATION_S = 120;
const EXACT_LIMIT = 200_000;

function bcryptLib() {
  try {
    return require("bcrypt");
  } catch {
    return null;
  }
}

function libVersion(name) {
  try {
    return require(`${name}/package.json`).version;
  } catch {
    return null;
  }
}

const NODE_CRYPTO = `node:crypto (node ${process.versions.node}, OpenSSL ${process.versions.openssl})`;

// id -> [algorithm, params (as in presets.py), library label, min_ops, build() -> op]
const PRESETS = {
  md5: ["MD5", "", () => NODE_CRYPTO, 0, () => () => createHash("md5").update(SYNTHETIC_PASSWORD).digest()],
  sha256: ["SHA-256", "", () => NODE_CRYPTO, 0, () => () => createHash("sha256").update(SYNTHETIC_PASSWORD).digest()],
  "pbkdf2-sha256-600k": ["PBKDF2-HMAC-SHA256", "iterations=600000", () => NODE_CRYPTO, 10,
    () => () => pbkdf2Sync(SYNTHETIC_PASSWORD, randomBytes(SALT_LEN), 600_000, 32, "sha256")],
  "bcrypt-10": ["bcrypt", "cost=10", () => `bcrypt ${libVersion("bcrypt")} (npm)`, 10, () => bcryptOp(10)],
  "bcrypt-12": ["bcrypt", "cost=12", () => `bcrypt ${libVersion("bcrypt")} (npm)`, 10, () => bcryptOp(12)],
  "scrypt-n17": ["scrypt", "N=131072,r=8,p=1", () => NODE_CRYPTO, 10,
    () => () => scryptSync(SYNTHETIC_PASSWORD, randomBytes(SALT_LEN), 32, { N: 2 ** 17, r: 8, p: 1, maxmem: 256 * 1024 * 1024 })],
  "argon2id-owasp": ["Argon2id", "m=19456,t=2,p=1", () => NODE_CRYPTO, 10, () => argon2Op(19456, 2, 1)],
  "argon2id-rfc9106-low": ["Argon2id", "m=65536,t=3,p=4", () => NODE_CRYPTO, 10, () => argon2Op(65536, 3, 4)],
};

function bcryptOp(cost) {
  const b = bcryptLib();
  if (!b) throw new Error("needs the bcrypt npm package: run `npm install` in vayunx_lab/node");
  return () => b.hashSync(SYNTHETIC_PASSWORD, b.genSaltSync(cost));
}

function argon2Op(memory, passes, parallelism) {
  if (typeof crypto.argon2Sync !== "function") throw new Error(`needs Node.js >= 24.7 for crypto.argon2Sync (this is ${process.version})`);
  return () => crypto.argon2Sync("argon2id", { message: SYNTHETIC_PASSWORD, nonce: randomBytes(SALT_LEN), parallelism,
    tagLength: 32, memory, passes });
}

// ----------------------------------------------------------------------------- histogram (log2x8-ns)

function bitLength(n) {
  if (n < 4294967296) return 32 - Math.clz32(n);
  let b = 32;
  while (2 ** b <= n) b++;
  return b;
}

function bucketIndex(ns) {
  if (ns < 8) return ns > 0 ? ns : 0;
  const b = bitLength(ns);
  return 8 + 8 * (b - 4) + (Math.floor(ns / 2 ** (b - 4)) & 7);
}

class Histogram {
  count = 0; sum = 0; min = null; max = null; buckets = new Map();
  record(ns) {
    this.count++;
    this.sum += ns;
    if (this.min === null || ns < this.min) this.min = ns;
    if (this.max === null || ns > this.max) this.max = ns;
    const i = bucketIndex(ns);
    this.buckets.set(i, (this.buckets.get(i) ?? 0) + 1);
  }
  percentile(q) { // same rule as LatencyHistogram.percentile: bucket midpoint, clamped to [min, max]
    if (!this.count) return null;
    const rank = Math.max(1, Math.ceil((q / 100) * this.count));
    let seen = 0;
    for (const i of [...this.buckets.keys()].sort((a, b) => a - b)) {
      seen += this.buckets.get(i);
      if (seen >= rank) {
        const [lo, hi] = i < 8 ? [i, i + 1] : [(8 + ((i - 8) % 8)) * 2 ** Math.floor((i - 8) / 8), (9 + ((i - 8) % 8)) * 2 ** Math.floor((i - 8) / 8)];
        return Math.min(this.max, Math.max(this.min, Math.floor((lo + hi - 1) / 2)));
      }
    }
    return this.max;
  }
  toDict() {
    const buckets = {};
    for (const i of [...this.buckets.keys()].sort((a, b) => a - b)) buckets[String(i)] = this.buckets.get(i);
    return { scheme: "log2x8-ns", count: this.count, sum_ns: this.sum, min_ns: this.min, max_ns: this.max, buckets };
  }
}

// ----------------------------------------------------------------------------- measurement

const emit = (o) => process.stdout.write(`${JSON.stringify(o)}\n`);
const nowIso = () => new Date().toISOString().replace("Z", "+00:00");
const hr = process.hrtime.bigint;

function timerOverheadNs(iterations = 20_000) {
  const h = new Histogram();
  const noop = () => undefined;
  for (let i = 0; i < iterations; i++) {
    const t0 = hr();
    noop();
    h.record(Number(hr() - t0));
  }
  return h.percentile(50);
}

function exactPercentiles(raw) { // nearest rank, as worker.py
  if (!raw.length) return null;
  const s = Float64Array.from(raw).sort();
  const pick = (q) => s[Math.min(s.length - 1, Math.max(0, Math.ceil((s.length * q) / 100) - 1))];
  return { p25_ns: pick(25), p50_ns: pick(50), p75_ns: pick(75), p95_ns: pick(95), p99_ns: pick(99) };
}

function fingerprint() {
  const cpus = os.cpus();
  return {
    cpu_model: cpus[0]?.model?.trim() ?? "unknown", cpu_count_logical: cpus.length, ram_total_bytes: os.totalmem(),
    os: `${os.type()} ${os.release()}`, machine: os.arch(), node: process.version, openssl: process.versions.openssl,
    libraries: { bcrypt: libVersion("bcrypt") }, runtime: `node ${process.versions.node}`,
  };
}

function parseArgs(argv) {
  const out = { duration: 10, warmup: 1, concurrency: 1, minOps: null, preset: null, list: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const next = () => argv[++i];
    if (a === "--preset") out.preset = next();
    else if (a === "--duration") out.duration = Number(next());
    else if (a === "--warmup") out.warmup = Number(next());
    else if (a === "--concurrency") out.concurrency = Number(next());
    else if (a === "--min-ops") out.minOps = Number(next());
    else if (a === "--list") out.list = true;
    else throw new Error(`unknown argument ${JSON.stringify(a)}`);
  }
  return out;
}

function fail(message) {
  emit({ event: "error", error: message });
  process.exitCode = 2;
}

function main() {
  let args;
  try {
    args = parseArgs(process.argv.slice(2));
  } catch (e) {
    return fail(e.message);
  }
  if (args.list) {
    const available = {};
    for (const [id, p] of Object.entries(PRESETS)) {
      try { p[4](); available[id] = { available: true, library: p[2]() }; } catch (e) { available[id] = { available: false, reason: e.message }; }
    }
    return emit({ event: "presets", node: process.version, openssl: process.versions.openssl, presets: available });
  }
  const preset = Object.hasOwn(PRESETS, args.preset ?? "") ? PRESETS[args.preset] : null;
  if (!preset) return fail(`unknown preset ${JSON.stringify(args.preset)}; built-in presets: ${Object.keys(PRESETS).join(", ")}`);
  if (!(args.duration > 0 && args.duration <= MAX_DURATION_S)) return fail(`duration must be in (0, ${MAX_DURATION_S}] seconds`);
  if (args.concurrency !== 1) return fail("the Node.js runner supports concurrency 1 only");
  if (!(args.warmup >= 0)) return fail("warmup must be >= 0");
  const [algorithm, params, library, presetMinOps, build] = preset;
  const minOps = args.minOps === null ? presetMinOps : Math.max(0, Math.floor(args.minOps));
  let op;
  try {
    op = build();
    op(); // fail fast (missing library, unsupported Node.js) before announcing readiness
  } catch (e) {
    return fail(`${args.preset}: ${e.message}`);
  }

  const overhead = timerOverheadNs();
  const rssBefore = process.memoryUsage.rss();
  emit({ event: "ready", pid: process.pid, preset: args.preset, ts: nowIso() });
  const warmEnd = performance.now() + args.warmup * 1000;
  while (performance.now() < warmEnd) op();

  const ru0 = process.resourceUsage();
  const cpu0 = process.cpuUsage();
  const startIso = nowIso();
  const w0 = hr();
  emit({ event: "measure_start", ts: startIso });
  const deadline = w0 + BigInt(Math.round(args.duration * 1e9));
  const h = new Histogram();
  const raw = [];
  while (hr() < deadline || h.count < minOps) {
    const t0 = hr();
    op();
    const d = Number(hr() - t0);
    h.record(d);
    if (h.count <= EXACT_LIMIT) raw.push(d);
  }
  const wall = Number(hr() - w0) / 1e9;
  const cpu = process.cpuUsage(cpu0);
  const ru1 = process.resourceUsage();
  const endIso = nowIso();
  emit({ event: "measure_end", ts: endIso });

  const exact = h.count <= EXACT_LIMIT ? exactPercentiles(raw) : null;
  const cpuUser = cpu.user / 1e6;
  const cpuSys = cpu.system / 1e6;
  const ops = h.count;
  const ctx = process.platform === "win32" ? null // libuv does not report context switches on Windows
    : (ru1.voluntaryContextSwitches - ru0.voluntaryContextSwitches) + (ru1.involuntaryContextSwitches - ru0.involuntaryContextSwitches);
  emit({
    event: "result", preset: args.preset, variant: args.preset, algorithm, params, library: library(), concurrency: 1,
    duration_target_s: args.duration, min_ops: minOps, warmup_s: args.warmup,
    ops, wall_s: wall, ops_per_s: wall ? ops / wall : null,
    cpu_user_s: cpuUser, cpu_system_s: cpuSys, cpu_s_per_op: ops ? (cpuUser + cpuSys) / ops : null,
    cores_busy: wall ? (cpuUser + cpuSys) / wall : null,
    rss_before_bytes: rssBefore, peak_rss_bytes: ru1.maxRSS * 1024, peak_rss_method: "process.resourceUsage().maxRSS",
    threads_max: null, ctx_switches: ctx,
    histogram: h.toDict(), mean_ns: ops ? h.sum / ops : null,
    p50_ns: exact ? exact.p50_ns : h.percentile(50), p95_ns: exact ? exact.p95_ns : h.percentile(95),
    p99_ns: exact ? exact.p99_ns : h.percentile(99),
    exact_percentiles: exact, percentile_method: exact ? "exact" : "histogram (±6.25%)",
    timer_overhead_ns: overhead, measure_start: startIso, measure_end: endIso, env: fingerprint(),
  });
}

main();
