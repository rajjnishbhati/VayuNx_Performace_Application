import type { NextConfig } from "next";

// The UI talks to the Python Profiler Service through /api/* (same origin, so no CORS).
// Override the service location with VAYUNX_API_URL, e.g. VAYUNX_API_URL=http://127.0.0.1:8011
const API_URL = process.env.VAYUNX_API_URL ?? "http://127.0.0.1:8010";

const nextConfig: NextConfig = {
  // Dev server only: allow opening the UI as http://127.0.0.1:3000 (Next 16 blocks non-localhost dev origins,
  // which otherwise stops the page from hydrating). Has no effect on `next build` / `next start`.
  allowedDevOrigins: ["127.0.0.1"],
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_URL}/:path*` }];
  },
};

export default nextConfig;
