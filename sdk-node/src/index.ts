/**
 * VAYUNX Crypto Profiler - Node.js SDK on OpenTelemetry (spec F).
 *
 * No code changes needed:
 *     node --require @vayunx/profiler/register app.js        (ESM apps too: --require runs before the ESM entry)
 *     vayunx-node run -- node app.js
 *
 * Or explicitly, as early as possible:
 *     const vayunx = require("@vayunx/profiler");
 *     vayunx.init({ service: "my-app", variant: "md5" });   // endpoint defaults to http://127.0.0.1:8010
 *
 * Safety contract (spec A): never throws into your code, never swallows your errors, never keeps the
 * process alive, bounds its memory, and never records passwords, keys, salts, hashes or tokens.
 */

import "./hooks"; // registers the hook installers with core

export { init, measure, shutdown, span, status, VERSION } from "./core";
export type { InitOptions } from "./config";
export { original } from "./hooks";
