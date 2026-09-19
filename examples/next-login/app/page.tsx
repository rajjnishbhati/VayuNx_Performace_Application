import { VARIANT } from "../lib/passwords";

export const dynamic = "force-dynamic";

export default function Home() {
  return (
    <main>
      <h1>VAYUNX login example</h1>
      <p>Password hashing: <strong>{VARIANT}</strong>{VARIANT === "md5" ? " (insecure, demo only)" : " (OWASP minimum parameters)"}.</p>
      <p>POST /register and /login with {"{ username, password }"}; GET /health.</p>
    </main>
  );
}
