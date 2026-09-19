// Next.js calls register() once per server process, before any route runs.
// NEXT_RUNTIME is replaced at build time, so the Edge bundle never includes the Node.js SDK.
export async function register() {
  if (process.env.NEXT_RUNTIME === "nodejs") {
    const { registerVayunx } = await import("@vayunx/profiler/next");
    // endpoint, variant and run id come from VAYUNX_ENDPOINT / VAYUNX_VARIANT / VAYUNX_RUN_ID
    registerVayunx({ service: process.env.VAYUNX_SERVICE ?? "next-login-example" });
  }
}
