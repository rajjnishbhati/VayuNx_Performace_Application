# @vayunx/profiler (Node.js / Next.js SDK)

> Package name is provisional (decision D3) and the package is `private`: nothing has been published.

Measures the crypto your Node.js app already does - no code changes - and sends it to the VAYUNX
Profiler Service over OpenTelemetry (OTLP/HTTP).

```bash
npm install && npm run build          # in sdk-node/
node --require ./sdk-node/dist/register.js app.js          # or:
node sdk-node/dist/cli.js run --variant md5 -- node app.js  # (vayunx-node run ... once installed)
```

Configuration: `VAYUNX_ENDPOINT` (default `http://127.0.0.1:8010`), `VAYUNX_SERVICE`, `VAYUNX_VARIANT`,
`VAYUNX_RUN_ID` (32 hex), `VAYUNX_PHASE`, `VAYUNX_SLOW_MS` (default 1), `VAYUNX_EXPORT_INTERVAL_MS` (5000),
`VAYUNX_GAUGE_INTERVAL_MS` (1000), `VAYUNX_FLUSH_TIMEOUT_MS` (2000), `VAYUNX_GAUGES=0`, `VAYUNX_HOOKS=0`,
`VAYUNX_DISABLE=1`.

## Next.js

```ts
// instrumentation.ts
export async function register() {
  if (process.env.NEXT_RUNTIME === "nodejs") {
    const { registerVayunx } = await import("@vayunx/profiler/next");
    registerVayunx({ service: "my-app" });
  }
  if (process.env.NEXT_RUNTIME === "edge") {           // optional light mode
    const { initEdge } = await import("@vayunx/profiler/edge");
    initEdge({ service: "my-app" });
  }
}
// next.config.ts: serverExternalPackages: ["@vayunx/profiler"]
```

Our TracerProvider becomes the global one when none is set, so Next.js's own request spans are exported
too. Edge light mode wraps Web Crypto only (no spans, CPU, memory or event-loop data: the Edge runtime
exposes none) and sends with `fetch()` at most once per interval.

## What is hooked

| Source | Calls | Notes |
|---|---|---|
| `node:crypto` | `createHash`, `createHmac` (timed over `update()` + `digest()`), `hash`, `pbkdf2[Sync]`, `scrypt[Sync]`, `argon2[Sync]`, `sign`, `verify` | ESM named imports too (`syncBuiltinESMExports`) |
| WebCrypto | `crypto.subtle.digest/sign/verify/encrypt/decrypt/deriveBits/deriveKey` | async, threadpool |
| npm (CommonJS, also when imported from ESM) | `bcrypt`, `bcryptjs` (CJS build), `argon2`, `jsonwebtoken` | Argon2 parameters from the PHC string, normalised to `m=..,t=..,p=..` |

Not seen: packages bundled into the app (list them in `serverExternalPackages`), ESM-only packages
(e.g. `bcryptjs` imported as ESM), the Hash stream API (`write()`/`end()`).

## API

`init(opts)`, `shutdown(timeoutMs)` → `Promise<boolean>`, `span(name, fn)` (sync or async; crypto inside
gets `vayunx.scope = name`), `measure(name, fn)`, `status()`, `original(fn)`.

## Findings

- `blocking_event_loop`: a synchronous crypto call at least `slowMs` long (the event loop served nothing meanwhile).
- `threadpool_queue`: an async threadpool crypto call started while `UV_THREADPOOL_SIZE` (default 4) of ours
  were already running, so it waited. A lower bound: other threadpool users (fs, dns) are not counted.

## Measured overhead

`createHash('md5').update(pw).digest()` hooked vs unhooked: about 0.7-0.8 µs per call (Node 24.15.0,
i5-8400H, Windows 11), from `test/sdk.test.js`.

Tests (`npm test`) start the real Python service with a scratch database.
