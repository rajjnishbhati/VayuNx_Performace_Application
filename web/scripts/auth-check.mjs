// Sign-in check in a real browser (installed Microsoft Edge via playwright-core), through the Next.js proxy.
//   node scripts/auth-check.mjs <outDir> [uiBase]
// Expects a UI (default http://127.0.0.1:3001) whose Service runs with VAYUNX_AUTH=oidc against an identity
// provider that signs the user in without a form (tests/fake_oidc.py does).
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { chromium } from "playwright-core";

const [out = "auth-screens", base = "http://127.0.0.1:3001"] = process.argv.slice(2);
mkdirSync(out, { recursive: true });
const results = { consoleErrors: [] };
const browser = await chromium.launch({ channel: "msedge", headless: true });
const page = await browser.newPage({ viewport: { width: 1360, height: 900 } });
page.on("console", (m) => m.type() === "error" && !/401/.test(m.text()) && results.consoleErrors.push(m.text()));
page.on("pageerror", (e) => results.consoleErrors.push(String(e)));

try {
  // 1. Opening the app signs in through the provider and lands back on the page that was asked for
  await page.goto(`${base}/runs`);
  await page.getByRole("button", { name: "Sign out" }).waitFor({ timeout: 60000 });
  results.landedOn = new URL(page.url()).pathname;
  results.header = await page.locator("header").innerText();
  await page.screenshot({ path: join(out, "01-signed-in.png") });

  // 2. Settings: create a token (shown once), see it listed, revoke it
  await page.goto(`${base}/settings`);
  await page.getByRole("heading", { name: "API tokens" }).waitFor({ timeout: 30000 });
  await page.getByLabel(/Name \(what is it for\?\)/).fill("ci-login-gate");
  await page.getByRole("button", { name: "Create token" }).click();
  const shown = page.locator("pre.code-block").filter({ hasText: /^vx_/ });
  await shown.waitFor({ timeout: 30000 });
  const token = (await shown.innerText()).trim();
  results.tokenShownOnce = token.startsWith("vx_");
  await page.screenshot({ path: join(out, "02-token-created.png"), fullPage: true });
  const api = (path, t = token) => fetch(`${base}/api${path}`, { headers: { Authorization: `Bearer ${t}` } }).then((r) => r.status);
  results.tokenWorks = await api("/v2/runs");
  await page.getByRole("button", { name: "Done" }).click();
  results.listedAfterCreate = await page.getByRole("row", { name: /ci-login-gate/ }).count();
  await page.getByRole("row", { name: /ci-login-gate/ }).getByRole("button", { name: "Revoke" }).click();
  await page.getByRole("row", { name: /ci-login-gate/ }).getByText("revoked").waitFor({ timeout: 30000 });
  results.tokenAfterRevoke = await api("/v2/runs");
  await page.screenshot({ path: join(out, "03-token-revoked.png"), fullPage: true });

  // 3. Sign out: the session is gone on the server, not just in the browser
  const cookiesBefore = await page.context().cookies();
  const session = cookiesBefore.find((c) => c.name === "vayunx_session");
  results.sessionCookieFlags = session && { httpOnly: session.httpOnly, sameSite: session.sameSite, path: session.path };
  await page.getByRole("button", { name: "Sign out" }).click();
  await page.waitForLoadState("load");
  results.oldSessionAfterSignOut = await fetch(`${base}/api/v2/runs`, { headers: { Cookie: `vayunx_session=${session.value}` } }).then((r) => r.status);
} finally {
  await browser.close();
  writeFileSync(join(out, "results.json"), JSON.stringify(results, null, 2));
  console.log(JSON.stringify(results, null, 2));
}
