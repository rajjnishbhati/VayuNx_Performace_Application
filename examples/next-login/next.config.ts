import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Load the profiler with Node's own require (not bundled), so its hooks patch the real node:crypto and
  // module loader. argon2 is already external by default in Next.js.
  serverExternalPackages: ["@vayunx/profiler"],
};

export default nextConfig;
