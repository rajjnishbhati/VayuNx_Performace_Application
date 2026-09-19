"use strict";
// Starts the real VAYUNX Profiler Service (Python) on a free port with a throwaway database.

const { spawn, execFileSync } = require("node:child_process");
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..", "..");
const PYTHON = process.platform === "win32" ? path.join(ROOT, ".venv", "Scripts", "python.exe") : path.join(ROOT, ".venv", "bin", "python");

function freePort() {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.listen(0, "127.0.0.1", () => { const { port } = s.address(); s.close(() => resolve(port)); });
    s.on("error", reject);
  });
}

async function startService() {
  const port = await freePort();
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "vayunx-node-test-"));
  const db = path.join(dir, "test.db").replace(/\\/g, "/");
  const proc = spawn(PYTHON, ["-m", "profiler_service"], {
    cwd: ROOT, stdio: ["ignore", "ignore", "pipe"],
    env: { ...process.env, VAYUNX_PROFILER_PORT: String(port), VAYUNX_PROFILER_DB_URL: `sqlite:///${db}` },
  });
  let stderr = "";
  proc.stderr.on("data", (d) => { stderr += d; });
  const url = `http://127.0.0.1:${port}`;
  for (let i = 0; i < 200; i++) {
    try {
      const r = await fetch(`${url}/v2/presets`);
      if (r.ok) return { url, stop: () => { proc.kill(); } };
    } catch { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 100));
  }
  proc.kill();
  throw new Error(`service did not start: ${stderr}`);
}

async function get(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: ${r.status} ${await r.text()}`);
  return r.json();
}

function counts(stats) {
  const out = {};
  for (const s of stats) {
    const k = `${s.op_name}|${s.attributes["crypto.algorithm"]}`;
    out[k] = (out[k] ?? 0) + s.count;
  }
  return out;
}

/** bucket_index / bucket_bounds computed by the Python implementation, for parity checks. */
function pythonBuckets(values) {
  const code = "import json,sys\nfrom vayunx_profiler_sdk.histogram import bucket_index\n"
    + "print(json.dumps([bucket_index(v) for v in json.loads(sys.argv[1])]))";
  return JSON.parse(execFileSync(PYTHON, ["-c", code, JSON.stringify(values)], { cwd: ROOT, encoding: "utf8" }));
}

const newRunId = () => require("node:crypto").randomUUID().replace(/-/g, "");

module.exports = { startService, get, counts, pythonBuckets, newRunId, ROOT, PYTHON };
