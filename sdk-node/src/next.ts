/**
 * Next.js (Node.js runtime). In the app's instrumentation.ts:
 *
 *     export async function register() {
 *       if (process.env.NEXT_RUNTIME === "nodejs") {
 *         const { registerVayunx } = await import("@vayunx/profiler/next");
 *         registerVayunx({ service: "my-next-app" });
 *       }
 *     }
 *
 * and in next.config.ts:  serverExternalPackages: ["@vayunx/profiler"]  (so Node loads it natively and
 * its hooks patch the real node:crypto / module loader instead of a bundled copy). Password libraries you
 * want measured (bcrypt, argon2) must also be external - Next.js already treats both as external.
 * Next.js replaces process.env.NEXT_RUNTIME at build time, so the Edge bundle never includes this file.
 * For the Edge runtime use "@vayunx/profiler/edge".
 */

import { init } from "./index";
import type { InitOptions } from "./config";

export function registerVayunx(opts: InitOptions = {}): Record<string, unknown> {
  if (process.env.NEXT_RUNTIME && process.env.NEXT_RUNTIME !== "nodejs") {
    return { initialized: false, reason: `NEXT_RUNTIME=${process.env.NEXT_RUNTIME}: use @vayunx/profiler/edge` };
  }
  return init(opts);
}

export { init, shutdown, span, measure, status } from "./index";
