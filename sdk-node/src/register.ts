/**
 * Preload entry: `node --require @vayunx/profiler/register app.js` (works for ESM apps too, because
 * --require runs before the ESM entry point). Configured entirely by VAYUNX_* environment variables.
 * Hooks go in before the app imports anything, so early-bound references are captured.
 */

import { init } from "./index";

try {
  const st = init();
  if (!st.initialized && process.env.VAYUNX_QUIET !== "1") {
    process.stderr.write(`vayunx: not profiling (${String(st.reason ?? "unknown reason")})\n`);
  }
} catch (e) {
  process.stderr.write(`vayunx: not profiling (${e instanceof Error ? e.message : String(e)})\n`);
}
