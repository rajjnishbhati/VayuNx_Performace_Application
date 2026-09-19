/**
 * Automatic crypto hooks for Node.js.
 *
 *  - node:crypto: createHash/createHmac (timed across update()+digest()), hash(), pbkdf2[Sync],
 *    scrypt[Sync], argon2[Sync] (Node >= 24.7), sign/verify, and WebCrypto subtle.*. ESM named imports of
 *    node:crypto see the wrappers too (module.syncBuiltinESMExports).
 *  - npm packages loaded through CommonJS - including CommonJS packages imported from ESM, which Node
 *    also loads through Module.prototype.load: bcrypt, bcryptjs (its CommonJS build), argon2, jsonwebtoken.
 *
 * Describers read sizes and public cost parameters only (bcrypt cost digits, the m/t/p field of a PHC
 * string, iteration counts); passwords, keys, salts, hashes and tokens never leave this module.
 *
 * Not seen: packages bundled into the app (webpack/turbopack - list them in serverExternalPackages),
 * ESM-only packages (e.g. bcryptjs imported as ESM), the Hash stream API (write()/end()).
 */

// the real module object (an `import * as` namespace would be a frozen copy we cannot patch)
import nodeCrypto = require("node:crypto");
import * as fs from "node:fs";
import Module from "node:module";
import * as path from "node:path";

import { context } from "@opentelemetry/api";

import { current, registerHooks, type CallInfo, type State } from "./core";

const kOrig = Symbol.for("vayunx.original");
type Fn = (...args: any[]) => any; // eslint-disable-line @typescript-eslint/no-explicit-any
type Describe = (args: any[], self: any) => CallInfo; // eslint-disable-line @typescript-eslint/no-explicit-any

// ----------------------------------------------------------------------------- describers

export function byteLen(v: unknown): number | undefined {
  if (typeof v === "string") return Buffer.byteLength(v);
  if (v && typeof v === "object" && typeof (v as ArrayBufferView).byteLength === "number") return (v as ArrayBufferView).byteLength;
  return undefined;
}

const HASH_NAMES: Record<string, string> = {
  md5: "MD5", sha1: "SHA1", sha224: "SHA224", sha256: "SHA256", sha384: "SHA384", sha512: "SHA512",
  "sha512-224": "SHA512/224", "sha512-256": "SHA512/256", "sha3-224": "SHA3-224", "sha3-256": "SHA3-256",
  "sha3-384": "SHA3-384", "sha3-512": "SHA3-512", shake128: "SHAKE128", shake256: "SHAKE256",
  blake2b512: "BLAKE2b", blake2s256: "BLAKE2s", sm3: "SM3", "md5-sha1": "MD5-SHA1", ripemd160: "RIPEMD160",
};

/** 'sha256' / 'SHA-256' / 'RSA-SHA256' -> 'SHA256' (the same names the Python SDK reports). */
export function hashName(v: unknown): string {
  const s = String(v ?? "?").toLowerCase().replace(/^rsa-/, "");
  return HASH_NAMES[s] ?? HASH_NAMES[s.replace(/^sha-/, "sha")] ?? String(v).toUpperCase();
}

const ARGON2: Record<string, string> = { argon2id: "Argon2id", argon2i: "Argon2i", argon2d: "Argon2d" };

/** Algorithm and m/t/p from a PHC string ('$argon2id$v=19$m=65536,t=3,p=4$<salt>$<hash>'), normalised to
 *  'm=..,t=..,p=..' so Node and Python series match. Salt and hash fields are never read. */
export function phc(encoded: unknown): { algorithm?: string; params?: string } {
  try {
    const parts = String(encoded).split("$");
    if (parts.length < 4 || parts[0] !== "") return {};
    const field = parts.slice(2, 4).find((p) => p.startsWith("m=") || p.includes(",m=") || /(^|,)t=/.test(p));
    let params: string | undefined;
    if (field) {
      const kv = Object.fromEntries(field.split(",").map((x) => x.split("=") as [string, string]));
      params = ["m", "t", "p"].every((k) => kv[k] !== undefined) ? `m=${kv.m},t=${kv.t},p=${kv.p}` : field.slice(0, 64);
    }
    return { algorithm: ARGON2[parts[1]] ?? parts[1].slice(0, 24), params };
  } catch {
    return {};
  }
}

/** 'cost=12' from a bcrypt salt/hash ('$2b$12$...') or a rounds number - public, not secret. */
export function bcryptCost(v: unknown): string | undefined {
  if (typeof v === "number") return `cost=${v}`;
  const m = /^\$2[abxy]?\$(\d\d)\$/.exec(typeof v === "string" ? v : Buffer.isBuffer(v) ? v.toString("latin1", 0, 7) : "");
  return m ? `cost=${Number(m[1])}` : undefined;
}

const LIB_NODE = `node:crypto (node ${process.versions.node})`;

// fast-path CallInfo objects are cached per algorithm so the hot path allocates nothing
const infoCache = new Map<string, CallInfo>();
function cached(op: string, algorithm: string, library: string, sync: boolean): CallInfo {
  const k = `${op}|${algorithm}|${library}|${sync}`;
  let c = infoCache.get(k);
  if (!c) infoCache.set(k, (c = { operation: op, algorithm, library, sync }));
  return c;
}

// ----------------------------------------------------------------------------- wrapper kinds

function mark<F extends Fn>(wrapper: F, orig: Fn): F {
  Object.defineProperty(wrapper, kOrig, { value: orig });
  try {
    Object.defineProperty(wrapper, "name", { value: orig.name });
    Object.defineProperty(wrapper, "length", { value: orig.length });
  } catch { /* cosmetic */ }
  return wrapper;
}

export function original<F extends Fn>(fn: F): F {
  return ((fn as unknown as Record<symbol, Fn>)[kOrig] as F) ?? fn;
}

/** A synchronous call. Nested hooked calls (depth > 0) belong to this one and are not recorded. */
function wrapSync(orig: Fn, describe: Describe, slow: boolean): Fn {
  return mark(function (this: unknown, ...args: unknown[]) {
    const st = current();
    if (st === undefined || st.depth > 0) return orig.apply(this, args);
    st.depth++;
    const cpu0 = slow ? process.cpuUsage() : undefined;
    const t0 = performance.now();
    let err: unknown;
    try {
      return orig.apply(this, args);
    } catch (e) {
      err = e ?? new Error("thrown");
      throw e;
    } finally {
      const t1 = performance.now();
      st.depth--;
      try {
        let cpuMs: number | undefined;
        if (cpu0 && st.inflightThreadpool === 0) { // process-wide CPU: only meaningful when nothing else is hashing
          const d = process.cpuUsage(cpu0);
          cpuMs = (d.user + d.system) / 1000;
        }
        st.record(describe(args, this), t0, t1, undefined, err, cpuMs);
      } catch {
        st.counters.internal_errors++;
      }
    }
  }, orig);
}

/** Starts async work; `finish(err, value)` must be called exactly once when it completes. */
function begin(st: State, info: CallInfo) {
  const parent = context.active();
  const queued = info.threadpool === true && st.inflightThreadpool >= st.threadpoolSize;
  if (info.threadpool) st.inflightThreadpool++;
  const t0 = performance.now();
  let done = false;
  return (err: unknown, fixInfo?: (i: CallInfo) => CallInfo) => {
    if (done) return;
    done = true;
    const t1 = performance.now();
    if (info.threadpool) st.inflightThreadpool--;
    try {
      st.record(fixInfo ? fixInfo(info) : info, t0, t1, parent, err ?? undefined, undefined, queued);
    } catch {
      st.counters.internal_errors++;
    }
  };
}

/** Node-style: callback as the last argument when present, otherwise a returned Promise (or a sync value). */
function wrapAsync(orig: Fn, describe: Describe, syncDescribe: Describe | undefined,
                   onValue?: (info: CallInfo, value: unknown) => CallInfo): Fn {
  return mark(function (this: unknown, ...args: unknown[]) {
    const st = current();
    if (st === undefined || st.depth > 0) return orig.apply(this, args);
    const last = args[args.length - 1];
    let info: CallInfo;
    try {
      info = describe(args, this);
    } catch {
      return orig.apply(this, args);
    }
    const finish = begin(st, info);
    if (typeof last === "function") {
      args[args.length - 1] = function (this: unknown, err: unknown, ...rest: unknown[]) {
        finish(err, onValue ? (i) => onValue(i, rest[0]) : undefined);
        return (last as Fn).call(this, err, ...rest);
      };
      st.depth++; // the synchronous part: hooked calls it makes belong to this call
      try {
        return orig.apply(this, args);
      } catch (e) {
        finish(e);
        throw e;
      } finally {
        st.depth--;
      }
    }
    let result: unknown;
    st.depth++;
    try {
      result = orig.apply(this, args);
    } catch (e) {
      finish(e);
      throw e;
    } finally {
      st.depth--;
    }
    if (result && typeof (result as Promise<unknown>).then === "function") {
      // a new promise that settles the same way: a rejection the app does not handle stays unhandled
      return (result as Promise<unknown>).then(
        (v) => { finish(undefined, onValue ? (i) => onValue(i, v) : undefined); return v; },
        (e) => { finish(e); throw e; });
    }
    finish(undefined, syncDescribe ? () => syncDescribe(args, this) : undefined);
    return result;
  }, orig);
}

// ----------------------------------------------------------------------------- patch bookkeeping

const patched: { owner: Record<string, unknown>; key: string; orig: unknown; name: string }[] = [];

function patch(st: State, name: string, owner: Record<string, unknown> | undefined, key: string, make: (orig: Fn) => Fn): void {
  try {
    const orig = owner?.[key];
    if (typeof orig !== "function") {
      st.hookStatus[name] = "not available in this version";
      return;
    }
    if ((orig as unknown as Record<symbol, unknown>)[kOrig]) {
      st.hookStatus[name] = "installed";
      return;
    }
    owner![key] = make(orig as Fn);
    if (owner![key] === orig) throw new TypeError("read-only");
    patched.push({ owner: owner!, key, orig, name });
    st.hookStatus[name] = "installed";
  } catch (e) {
    st.hookStatus[name] = `unhookable (${e instanceof Error ? e.name : "error"})`;
  }
}

function unpatchAll(): void {
  for (const p of patched.splice(0).reverse()) {
    try { p.owner[p.key] = p.orig; } catch { /* keep going */ }
  }
  try { Module.syncBuiltinESMExports(); } catch { /* ignore */ }
}

// ----------------------------------------------------------------------------- node:crypto

const kInfo = Symbol("vayunx.info");
const kMs = Symbol("vayunx.ms");
const kBytes = Symbol("vayunx.bytes");
type Timed = { [kInfo]?: CallInfo; [kMs]: number; [kBytes]: number };

/** createHash()/createHmac(): tag the object, then time update() and digest() on the prototype. */
function hookIncremental(st: State, factoryName: "createHash" | "createHmac"): void {
  const c = nodeCrypto as unknown as Record<string, Fn>;
  const isHmac = factoryName === "createHmac";
  const proto = Object.getPrototypeOf(isHmac ? c.createHmac("sha256", "k") : c.createHash("sha256")) as Record<string, unknown>;
  const infos = new Map<unknown, CallInfo>(); // raw algorithm argument -> cached CallInfo
  const infoFor = (alg: unknown): CallInfo => {
    let i = infos.get(alg);
    if (i === undefined) {
      i = cached(isHmac ? "mac" : "hash", isHmac ? `HMAC-${hashName(alg)}` : hashName(alg), LIB_NODE, true);
      if (infos.size < 256 && (typeof alg === "string")) infos.set(alg, i);
    }
    return i;
  };
  patch(st, `crypto.${factoryName}`, c as unknown as Record<string, unknown>, factoryName, (orig) => mark(function (this: unknown, alg: unknown, a2?: unknown, a3?: unknown) {
    const obj = (arguments.length <= 1 ? orig.call(this, alg) : orig.call(this, alg, a2, a3)) as Timed;
    const s = current();
    if (s !== undefined && s.depth === 0) {
      obj[kInfo] = infoFor(alg);
      obj[kMs] = 0;
      obj[kBytes] = 0;
    }
    return obj;
  }, orig));
  const label = isHmac ? "Hmac" : "Hash";
  patch(st, `crypto.${label}.update`, proto, "update", (orig) => mark(function (this: Timed, data: unknown, enc?: unknown) {
    if (this[kInfo] === undefined || current() === undefined) return orig.call(this, data, enc);
    const t0 = performance.now();
    try {
      return orig.call(this, data, enc);
    } finally {
      this[kMs] += performance.now() - t0;
      this[kBytes] += typeof data === "string" ? Buffer.byteLength(data, enc as BufferEncoding) : byteLen(data) ?? 0;
    }
  }, orig));
  patch(st, `crypto.${label}.digest`, proto, "digest", (orig) => mark(function (this: Timed, enc?: unknown) {
    const info = this[kInfo];
    const s = current();
    if (info === undefined || s === undefined) return orig.call(this, enc);
    const t0 = performance.now();
    let err: unknown;
    try {
      return orig.call(this, enc);
    } catch (e) {
      err = e ?? new Error("thrown");
      throw e;
    } finally {
      const t1 = performance.now();
      const total = this[kMs] + (t1 - t0);
      this[kInfo] = undefined;
      // start = digest end - time spent inside update()+digest(): the time the app waited on hashing
      s.record(total >= s.cfg.slowMs ? { ...info, inputBytes: this[kBytes] } : info, t1 - total, t1, undefined, err);
    }
  }, orig));
}

function kdfInfo(algorithm: string, params: string | undefined, input: unknown, sync: boolean): CallInfo {
  return { operation: "kdf", algorithm, params, library: LIB_NODE, sync, inputBytes: byteLen(input), threadpool: !sync };
}

function scryptParams(opts: unknown): string {
  const o = (opts ?? {}) as Record<string, number>;
  // Node's documented defaults: N=16384, r=8, p=1
  return `n=${o.N ?? o.cost ?? 16384},r=${o.r ?? o.blockSize ?? 8},p=${o.p ?? o.parallelization ?? 1}`;
}

function keyType(key: unknown): string {
  const k = key as { asymmetricKeyType?: string; key?: { asymmetricKeyType?: string } } | undefined;
  const t = k?.asymmetricKeyType ?? k?.key?.asymmetricKeyType;
  return ({ ed25519: "Ed25519", ed448: "Ed448", rsa: "RSA", "rsa-pss": "RSA-PSS", ec: "ECDSA", dsa: "DSA" } as Record<string, string>)[t ?? ""]
    ?? "asymmetric";
}

function subtleAlg(alg: unknown, key?: unknown): { algorithm: string; params?: string } {
  const a = (typeof alg === "string" ? { name: alg } : alg ?? {}) as { name?: string; hash?: unknown; iterations?: number };
  const name = String(a.name ?? "?");
  const hashOf = (h: unknown) => hashName(typeof h === "string" ? h : (h as { name?: string })?.name);
  const upper = name.toUpperCase();
  if (upper === "PBKDF2") return { algorithm: `PBKDF2-HMAC-${hashOf(a.hash)}`, params: a.iterations ? `i=${a.iterations}` : undefined };
  if (upper === "HMAC") {
    const kh = (key as { algorithm?: { hash?: unknown } } | undefined)?.algorithm?.hash;
    return { algorithm: `HMAC-${hashOf(a.hash ?? kh)}` };
  }
  if (upper.startsWith("SHA-")) return { algorithm: hashName(name) };
  return { algorithm: name };
}

function installCrypto(st: State): void {
  const c = nodeCrypto as unknown as Record<string, unknown>;
  hookIncremental(st, "createHash");
  hookIncremental(st, "createHmac");
  patch(st, "crypto.hash", c, "hash", (o) => wrapSync(o, (a) => cached("hash", hashName(a[0]), LIB_NODE, true), false));
  patch(st, "crypto.pbkdf2Sync", c, "pbkdf2Sync", (o) => wrapSync(o,
    (a) => kdfInfo(`PBKDF2-HMAC-${hashName(a[4])}`, `i=${a[2]}`, a[0], true), true));
  patch(st, "crypto.pbkdf2", c, "pbkdf2", (o) => wrapAsync(o,
    (a) => kdfInfo(`PBKDF2-HMAC-${hashName(a[4])}`, `i=${a[2]}`, a[0], false), undefined));
  patch(st, "crypto.scryptSync", c, "scryptSync", (o) => wrapSync(o,
    (a) => kdfInfo("scrypt", scryptParams(a[3]), a[0], true), true));
  patch(st, "crypto.scrypt", c, "scrypt", (o) => wrapAsync(o,
    (a) => kdfInfo("scrypt", scryptParams(typeof a[3] === "function" ? undefined : a[3]), a[0], false), undefined));
  const argon = (a: unknown[], sync: boolean): CallInfo => {
    const p = (a[1] ?? {}) as { memory?: number; passes?: number; parallelism?: number; message?: unknown };
    return { operation: "hash", algorithm: ARGON2[String(a[0])] ?? String(a[0]), params: `m=${p.memory},t=${p.passes},p=${p.parallelism}`,
      library: LIB_NODE, sync, inputBytes: byteLen(p.message), threadpool: !sync };
  };
  patch(st, "crypto.argon2Sync", c, "argon2Sync", (o) => wrapSync(o, (a) => argon(a, true), true));
  patch(st, "crypto.argon2", c, "argon2", (o) => wrapAsync(o, (a) => argon(a, false), undefined));
  const signInfo = (op: string) => (a: unknown[]): CallInfo => {
    const sync = typeof a[a.length - 1] !== "function";
    const kt = keyType(a[2]);
    return { operation: op, algorithm: a[0] ? `${kt}-${hashName(a[0])}` : kt, library: LIB_NODE, sync,
      inputBytes: byteLen(a[1]), threadpool: !sync };
  };
  patch(st, "crypto.sign", c, "sign", (o) => wrapAsync(o, signInfo("sign"), signInfo("sign")));
  patch(st, "crypto.verify", c, "verify", (o) => wrapAsync(o, signInfo("verify"), signInfo("verify")));
  try {
    Module.syncBuiltinESMExports(); // `import { createHash } from "node:crypto"` now sees the wrappers
  } catch { /* older Node: CommonJS users are still covered */ }

  const subtle = (nodeCrypto.webcrypto as { subtle?: object }).subtle;
  const sp = subtle ? Object.getPrototypeOf(subtle) as Record<string, unknown> : undefined;
  const sub = (op: string, algIdx: number, keyIdx: number | undefined, dataIdx: number | undefined) =>
    (a: unknown[]): CallInfo => ({ operation: op, ...subtleAlg(a[algIdx], keyIdx === undefined ? undefined : a[keyIdx]),
      library: "webcrypto", sync: false, inputBytes: dataIdx === undefined ? undefined : byteLen(a[dataIdx]), threadpool: true });
  patch(st, "webcrypto.subtle.digest", sp, "digest", (o) => wrapAsync(o, sub("hash", 0, undefined, 1), undefined));
  patch(st, "webcrypto.subtle.sign", sp, "sign", (o) => wrapAsync(o, sub("sign", 0, 1, 2), undefined));
  patch(st, "webcrypto.subtle.verify", sp, "verify", (o) => wrapAsync(o, sub("verify", 0, 1, 3), undefined));
  patch(st, "webcrypto.subtle.encrypt", sp, "encrypt", (o) => wrapAsync(o, sub("encrypt", 0, 1, 2), undefined));
  patch(st, "webcrypto.subtle.decrypt", sp, "decrypt", (o) => wrapAsync(o, sub("decrypt", 0, 1, 2), undefined));
  patch(st, "webcrypto.subtle.deriveBits", sp, "deriveBits", (o) => wrapAsync(o, sub("kdf", 0, undefined, undefined), undefined));
  patch(st, "webcrypto.subtle.deriveKey", sp, "deriveKey", (o) => wrapAsync(o, sub("kdf", 0, undefined, undefined), undefined));
}

// ----------------------------------------------------------------------------- npm packages

interface Target { name: string; entry: RegExp; install(st: State, exports: Record<string, unknown>, library: string): void }

const pwInfo = (op: string, algorithm: string, params: string | undefined, input: unknown, library: string, sync: boolean,
                threadpool: boolean): CallInfo => ({ operation: op, algorithm, params, library, sync, inputBytes: byteLen(input), threadpool });

function bcryptLike(pkg: string, threadpool: boolean) {
  return (st: State, ex: Record<string, unknown>, lib: string) => {
    patch(st, `${pkg}.hashSync`, ex, "hashSync", (o) => wrapSync(o, (a) => pwInfo("hash", "bcrypt", bcryptCost(a[1]), a[0], lib, true, false), true));
    patch(st, `${pkg}.compareSync`, ex, "compareSync", (o) => wrapSync(o, (a) => pwInfo("verify", "bcrypt", bcryptCost(a[1]), a[0], lib, true, false), true));
    patch(st, `${pkg}.hash`, ex, "hash", (o) => wrapAsync(o, (a) => pwInfo("hash", "bcrypt", bcryptCost(a[1]), a[0], lib, false, threadpool), undefined));
    patch(st, `${pkg}.compare`, ex, "compare", (o) => wrapAsync(o, (a) => pwInfo("verify", "bcrypt", bcryptCost(a[1]), a[0], lib, false, threadpool), undefined));
  };
}

function jwtAlg(token: unknown): string {
  try {
    const header = String(token).split(".", 1)[0];
    return String(JSON.parse(Buffer.from(header, "base64url").toString("utf8")).alg ?? "JWT").slice(0, 16);
  } catch {
    return "JWT";
  }
}

const TARGETS: Target[] = [
  { name: "bcrypt", entry: /[\\/]node_modules[\\/]bcrypt[\\/]bcrypt\.js$/, install: bcryptLike("bcrypt", true) },
  { name: "bcryptjs", entry: /[\\/]node_modules[\\/]bcryptjs[\\/](umd[\\/]index|dist[\\/]bcrypt)\.js$/, install: bcryptLike("bcryptjs", false) },
  {
    name: "argon2", entry: /[\\/]node_modules[\\/]argon2[\\/]argon2\.c?js$/,
    install(st, ex, lib) {
      // the parameters come from the PHC string the library returns / verifies against
      patch(st, "argon2.hash", ex, "hash", (o) => wrapAsync(o, (a) => pwInfo("hash", "Argon2", undefined, a[0], lib, false, true), undefined,
        (i, v) => ({ ...i, ...stripUndefined(phc(v)) })));
      patch(st, "argon2.verify", ex, "verify", (o) => wrapAsync(o, (a) => ({ ...pwInfo("verify", "Argon2", undefined, a[1], lib, false, true),
        ...stripUndefined(phc(a[0])) }), undefined));
    },
  },
  {
    name: "jsonwebtoken", entry: /[\\/]node_modules[\\/]jsonwebtoken[\\/]index\.js$/,
    install(st, ex, lib) {
      const alg = (o: unknown) => String((o as { algorithm?: string } | undefined)?.algorithm ?? "HS256");
      patch(st, "jsonwebtoken.sign", ex, "sign", (o) => wrapAsync(o, (a) => ({ operation: "sign", algorithm: alg(typeof a[2] === "function" ? undefined : a[2]),
        library: lib, sync: typeof a[a.length - 1] !== "function" }), (a) => ({ operation: "sign", algorithm: alg(a[2]), library: lib, sync: true })));
      patch(st, "jsonwebtoken.verify", ex, "verify", (o) => wrapAsync(o, (a) => ({ operation: "verify", algorithm: jwtAlg(a[0]),
        library: lib, sync: typeof a[a.length - 1] !== "function", inputBytes: byteLen(a[0]) }),
      (a) => ({ operation: "verify", algorithm: jwtAlg(a[0]), library: lib, sync: true, inputBytes: byteLen(a[0]) })));
    },
  },
];

function stripUndefined<T extends object>(o: T): Partial<T> {
  return Object.fromEntries(Object.entries(o).filter(([, v]) => v !== undefined)) as Partial<T>;
}

function libraryLabel(name: string, filename: string): string {
  try {
    let dir = path.dirname(filename);
    while (path.basename(dir) !== name && path.dirname(dir) !== dir) dir = path.dirname(dir);
    return `${name} ${JSON.parse(fs.readFileSync(path.join(dir, "package.json"), "utf8")).version}`;
  } catch {
    return name;
  }
}

let done = new WeakSet<object>();

function maybeInstall(st: State, filename: string, exports: unknown): void {
  if (!exports || typeof exports !== "object" && typeof exports !== "function" || done.has(exports as object)) return;
  for (const t of TARGETS) {
    if (t.entry.test(filename)) {
      done.add(exports as object);
      t.install(st, exports as Record<string, unknown>, libraryLabel(t.name, filename));
    }
  }
}

type ModuleProto = { load(this: { filename: string; exports: unknown }, filename: string): void };
let origLoad: ModuleProto["load"] | undefined;

function installModules(st: State): void {
  for (const t of TARGETS) st.hookStatus[t.name] = "not loaded yet";
  const proto = (Module as unknown as { prototype: ModuleProto }).prototype;
  if (!origLoad) {
    origLoad = proto.load;
    const saved = origLoad;
    proto.load = function (filename: string) {
      saved.call(this, filename);
      const s = current();
      if (s !== undefined) {
        try { maybeInstall(s, this.filename, this.exports); } catch { s.counters.internal_errors++; }
      }
    };
  }
  // packages loaded before init(): patch their exports now (ESM importers that already bound names keep the old ones)
  const cache = (Module as unknown as { _cache: Record<string, { exports: unknown }> })._cache;
  for (const [filename, m] of Object.entries(cache)) maybeInstall(st, filename, m?.exports);
}

function uninstallModules(): void {
  done = new WeakSet<object>();
  if (origLoad) {
    (Module as unknown as { prototype: ModuleProto }).prototype.load = origLoad;
    origLoad = undefined;
  }
}

registerHooks((st) => { installCrypto(st); installModules(st); }, () => { uninstallModules(); unpatchAll(); });
