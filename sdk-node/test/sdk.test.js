"use strict";
// Node SDK on OpenTelemetry (spec F + spec A): hooks, OTLP export, safety, privacy, findings.

const { after, before, test } = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const { promisify } = require("node:util");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const vayunx = require("..");
const { bucketIndex } = require("../dist/histogram");
const { startService, get, counts, pythonBuckets, newRunId } = require("./helpers");

const SECRET = "correct horse battery staple #42";
let svc;

before(async () => { svc = await startService(); });
after(async () => { await vayunx.shutdown(1000); svc?.stop(); });

function start(extra = {}) {
  const runId = newRunId();
  const st = vayunx.init({ endpoint: svc.url, service: "node-sdk-test", variant: "md5", runId, exportIntervalMs: 200, ...extra });
  return { runId, st };
}

test("histogram buckets match the Python implementation exactly", () => {
  const values = [0, 1, 7, 8, 9, 15, 16, 17, 100, 999, 1000, 1024, 12345, 991000, 55600000, 2 ** 31 - 1, 2 ** 31,
    2 ** 32 - 1, 2 ** 32, 2 ** 32 + 1, 5e9, 123456789012];
  assert.deepEqual(values.map(bucketIndex), pythonBuckets(values));
});

test("hooks capture fast and slow crypto without code changes", async () => {
  const { runId, st } = start({ slowMs: 0.5 });
  assert.equal(st.initialized, true);
  assert.equal(st.hooks["crypto.createHash"], "installed");
  for (let i = 0; i < 1000; i++) crypto.createHash("md5").update(SECRET).digest("hex");
  crypto.createHmac("sha256", "k".repeat(32)).update("message").digest();
  crypto.hash("sha256", "x");
  crypto.pbkdf2Sync(SECRET, "s".repeat(16), 1000, 32, "sha256");
  await promisify(crypto.pbkdf2)(SECRET, "s".repeat(16), 1000, 32, "sha256");
  const bcrypt = require("bcrypt");
  const h = await bcrypt.hash(SECRET, 4);
  assert.equal(await bcrypt.compare(SECRET, h), true);
  const argon2 = require("argon2");
  const stored = await argon2.hash(SECRET, { memoryCost: 8192, timeCost: 1, parallelism: 1 });
  assert.equal(await argon2.verify(stored, SECRET), true);
  const jwt = require("jsonwebtoken");
  const token = jwt.sign({ sub: "u1" }, "k".repeat(32), { algorithm: "HS256" });
  assert.equal(jwt.verify(token, "k".repeat(32)).sub, "u1");
  assert.equal(await vayunx.shutdown(5000), true);

  const stats = await get(`${svc.url}/v1/runs/${runId}/op-stats`);
  const c = counts(stats);
  assert.equal(c["hash|MD5"], 1000);
  assert.equal(c["mac|HMAC-SHA256"], 1, "JWT's internal HMACs must not be counted again");
  assert.equal(c["hash|SHA256"], 1);
  assert.equal(c["kdf|PBKDF2-HMAC-SHA256"], 2);
  assert.equal(c["hash|bcrypt"], 1);
  assert.equal(c["verify|bcrypt"], 1);
  assert.equal(c["hash|Argon2id"], 1);
  assert.equal(c["verify|Argon2id"], 1);
  assert.equal(c["sign|HS256"], 1);
  assert.equal(c["verify|HS256"], 1);

  const spans = await get(`${svc.url}/v2/runs/${runId}/spans`);
  const argon = spans.find((s) => s.attributes["crypto.algorithm"] === "Argon2id" && s.attributes["crypto.operation"] === "hash");
  assert.ok(argon, "slow Argon2id call should also be a span");
  assert.equal(argon.attributes["crypto.params"], "m=8192,t=1,p=1");
  assert.match(argon.attributes["crypto.library"], /^argon2 /);
  assert.equal(argon.attributes["crypto.sync"], false);
  assert.equal(argon.attributes["crypto.input_bytes"], Buffer.byteLength(SECRET));
  const kdfSync = spans.find((s) => s.attributes["crypto.operation"] === "kdf" && s.attributes["crypto.sync"] === true);
  assert.ok(kdfSync.attributes["vayunx.cpu_time_ms"] >= 0);
  const run = await get(`${svc.url}/v1/runs/${runId}`);
  assert.equal(run.service, "node-sdk-test");
  const wire = JSON.stringify(spans) + JSON.stringify(stats);
  for (const secret of [SECRET, h, stored, token]) assert.ok(!wire.includes(secret), "no secrets, hashes or tokens");
});

test("host errors pass through unchanged", async () => {
  start();
  assert.throws(() => crypto.createHash("no-such-hash"), (e) => e.message === "Digest method not supported");
  assert.throws(() => crypto.pbkdf2Sync(SECRET, "s", -1, 32, "sha256"), (e) => e.code === "ERR_OUT_OF_RANGE");
  const argon2 = require("argon2");
  await assert.rejects(argon2.verify("not-a-phc-string", "x"), (e) => e instanceof TypeError || e instanceof Error);
  await new Promise((resolve) => crypto.pbkdf2(SECRET, "s", 1, 32, "no-such-digest", (err) => {
    assert.equal(err?.code ?? "thrown-sync", "ERR_CRYPTO_INVALID_DIGEST");
    resolve();
  })).catch((e) => assert.equal(e.code, "ERR_CRYPTO_INVALID_DIGEST"));
  await vayunx.shutdown(1000);
});

test("service down never breaks the app and shutdown is bounded", async () => {
  vayunx.init({ endpoint: "http://127.0.0.1:9", service: "offline-app", variant: "x", exportIntervalMs: 200 });
  for (let i = 0; i < 200; i++) crypto.createHash("sha256").update("data").digest();
  vayunx.span("checkout", () => crypto.createHash("md5").update("x").digest());
  const t = performance.now();
  await vayunx.shutdown(2000);
  assert.ok(performance.now() - t < 4000);
});

test("a slow synchronous hash is a blocking_event_loop finding; the async one is not", async () => {
  const { runId } = start({ slowMs: 0.5 });
  crypto.pbkdf2Sync("pw", "s".repeat(16), 20000, 32, "sha256");
  await promisify(crypto.pbkdf2)("pw", "s".repeat(16), 20000, 32, "sha256");
  assert.equal(vayunx.status().findings.blocking_event_loop, 1);
  await vayunx.shutdown(5000);
  const spans = await get(`${svc.url}/v2/runs/${runId}/spans`);
  assert.equal(spans.filter((s) => s.attributes["vayunx.finding"] === "blocking_event_loop").length, 1);
});

test("async KDFs beyond the threadpool size are flagged as queued", async () => {
  const { runId } = start({ slowMs: 0.5 });
  const size = vayunx.status().threadpoolSize;
  await Promise.all(Array.from({ length: size * 2 }, () => promisify(crypto.pbkdf2)("pw", "s".repeat(16), 20000, 32, "sha256")));
  assert.equal(vayunx.status().findings.threadpool_queue, size);
  await vayunx.shutdown(5000);
  const spans = await get(`${svc.url}/v2/runs/${runId}/spans`);
  assert.equal(spans.filter((s) => s.attributes["vayunx.finding"] === "threadpool_queue").length, size);
});

test("span() and measure() nest across await", async () => {
  const { runId } = start({ slowMs: 0 });
  const login = vayunx.measure("login", async () => {
    vayunx.span("load_user", () => undefined);
    await promisify(crypto.pbkdf2)("pw", "s".repeat(16), 5000, 32, "sha256");
  });
  await login();
  await vayunx.shutdown(5000);
  const spans = Object.fromEntries((await get(`${svc.url}/v2/runs/${runId}/spans`)).map((s) => [s.span_name, s]));
  assert.equal(spans.load_user.parent_span_id, spans.login.span_id);
  const kdf = Object.values(spans).find((s) => s.attributes["crypto.operation"] === "kdf");
  assert.equal(kdf.parent_span_id, spans.login.span_id);
  assert.ok(spans.login.attributes["vayunx.process_cpu_ms"] >= 0);
});

test("fast hook overhead is under two microseconds", () => {
  start();
  const orig = vayunx.original(crypto.createHash);
  const pw = "correct horse battery staple";
  const n = 50000;
  const runs = [];
  for (let r = 0; r < 5; r++) {
    let t = performance.now();
    for (let i = 0; i < n; i++) orig("md5").update(pw).digest();
    const plain = performance.now() - t;
    t = performance.now();
    for (let i = 0; i < n; i++) crypto.createHash("md5").update(pw).digest();
    runs.push(((performance.now() - t - plain) * 1e6) / n);
  }
  const overhead = runs.sort((a, b) => a - b)[2];
  console.log(`createHash('md5').update().digest() hook overhead: ${overhead.toFixed(0)} ns/call`);
  assert.ok(overhead < 2000);
});

test("shutdown restores every patched function", async () => {
  await vayunx.shutdown(1000);
  const before = [crypto.createHash, crypto.pbkdf2Sync, crypto.webcrypto.subtle.digest, require("bcrypt").hashSync];
  start();
  assert.notEqual(crypto.createHash, before[0]);
  await vayunx.shutdown(1000);
  assert.deepEqual([crypto.createHash, crypto.pbkdf2Sync, crypto.webcrypto.subtle.digest, require("bcrypt").hashSync], before);
  assert.equal(vayunx.status().initialized, false);
});

test("launcher profiles an ESM app with early-bound imports", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "vayunx-esm-"));
  const app = path.join(dir, "app.mjs");
  const jwtPath = require("node:url").pathToFileURL(require.resolve("jsonwebtoken")).href;
  fs.writeFileSync(app, `import { createHash } from "node:crypto";\nimport jwt from "${jwtPath}";\n`
    + "for (let i = 0; i < 50; i++) createHash('md5').update('early-bound').digest('hex');\n"
    + "jwt.sign({ a: 1 }, 'k'.repeat(32));\n");
  const runId = newRunId();
  const r = spawnSync(process.execPath, [path.join(__dirname, "..", "dist", "cli.js"), "run", "--endpoint", svc.url,
    "--service", "launched-node-app", "--variant", "md5", "--run-id", runId, "--", process.execPath, app],
  { encoding: "utf8", timeout: 60000 });
  assert.equal(r.status, 0, r.stderr);
  const c = counts(await get(`${svc.url}/v1/runs/${runId}/op-stats`));
  assert.equal(c["hash|MD5"], 50);
  assert.equal(c["sign|HS256"], 1);
  const run = await get(`${svc.url}/v1/runs/${runId}`);
  assert.equal(run.service, "launched-node-app");
});

test("crypto inside a span carries its scope", async () => {
  const { runId } = start();
  const login = vayunx.measure("login", async () => {
    crypto.createHash("md5").update("pw").digest();
    await promisify(crypto.pbkdf2)("pw", "s".repeat(16), 1000, 32, "sha256");
  });
  for (let i = 0; i < 3; i++) await login();
  crypto.createHash("md5").update("etag").digest(); // unrelated hashing outside any span
  await vayunx.shutdown(5000);
  const by = {};
  for (const s of await get(`${svc.url}/v1/runs/${runId}/op-stats`)) {
    const k = `${s.attributes["vayunx.scope"] ?? "-"}|${s.op_name}|${s.attributes["crypto.algorithm"]}`;
    by[k] = (by[k] ?? 0) + s.count;
  }
  assert.deepEqual(by, { "login|hash|MD5": 3, "login|kdf|PBKDF2-HMAC-SHA256": 3, "-|hash|MD5": 1 });
});
