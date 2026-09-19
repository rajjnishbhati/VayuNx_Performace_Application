// Projects & access check in a real browser (Edge), through the UI proxy, signed in as a global administrator.
//   node scripts/projects-check.mjs <outDir> [uiBase]
// Expects the setup of auth-check.mjs (a Service with VAYUNX_AUTH=oidc; the test provider signs in as an admin).
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { chromium } from "playwright-core";

const [out = "projects-screens", base = "http://127.0.0.1:3001"] = process.argv.slice(2);
mkdirSync(out, { recursive: true });
const results = { consoleErrors: [] };
const browser = await chromium.launch({ channel: "msedge", headless: true });
const page = await browser.newPage({ viewport: { width: 1360, height: 900 } });
page.on("console", (m) => m.type() === "error" && !/401/.test(m.text()) && results.consoleErrors.push(m.text()));
page.on("pageerror", (e) => results.consoleErrors.push(String(e)));
const api = (path, init = {}) => page.evaluate(async ([p, i]) => {
  const r = await fetch(`/api${p}`, { headers: { "Content-Type": "application/json" }, ...i });
  return { status: r.status, body: await r.json().catch(() => null) };
}, [path, init]);

const tag = Date.now().toString(36); // unique names, so the check can run again on the same database
const P = `Payments API ${tag}`, pid = `payments-api-${tag}`, T = `payments-team-${tag}`;

try {
  await page.goto(`${base}/settings`);
  await page.getByRole("heading", { name: /Projects & access/ }).waitFor({ timeout: 60000 });

  // create a project and a team from the page, then grant the team a role
  await page.getByLabel("New project").fill(P);
  await page.getByRole("button", { name: "Create project" }).click();
  await page.getByRole("rowheader", { name: new RegExp(P) }).waitFor({ timeout: 30000 });
  await page.getByLabel("New team").fill(T);
  await page.getByRole("button", { name: "Create team" }).click();
  await page.locator("strong", { hasText: T }).waitFor({ timeout: 30000 });
  await page.getByLabel(`Team to grant on ${P}`).selectOption({ label: T });
  await page.getByLabel(`Role to grant on ${P}`).selectOption("editor");
  await page.getByRole("row", { name: new RegExp(P) }).getByRole("button", { name: "Grant" }).click();
  await page.getByText(`${T}: editor`).waitFor({ timeout: 30000 });
  await page.getByLabel(`Default role on ${P}`).selectOption("viewer");
  await page.waitForTimeout(500);
  results.projects = (await api("/v2/projects")).body.map((p) => [p.project_id, p.default_role, (p.grants ?? []).map((g) => `${g.team}:${g.role}`)]);
  await page.screenshot({ path: join(out, "01-projects-admin.png"), fullPage: true });

  // one run in each project, then the header selector filters the Runs page
  const mk = (project) => api("/v1/runs", { method: "POST", body: JSON.stringify({ run_id: crypto.randomUUID().replaceAll("-", ""), service: `svc-${project}`, label: "x", phase: "baseline", project }) });
  results.createRuns = [(await mk(pid)).status, (await mk("default")).status];
  await page.goto(`${base}/runs`);
  await page.getByLabel("Project").selectOption(pid);
  await page.waitForLoadState("load");
  await page.getByText(`svc-${pid}`).first().waitFor({ timeout: 30000 });
  results.paymentsPageShowsDefaultRun = await page.getByText("svc-default").count();
  await page.screenshot({ path: join(out, "02-runs-filtered.png"), fullPage: true });
  await page.getByLabel("Project").selectOption("");
  await page.waitForLoadState("load");
  await page.getByText("svc-default").first().waitFor({ timeout: 30000 });
  results.allProjectsShowsBoth = (await page.getByText(`svc-${pid}`).count()) > 0;
} finally {
  await browser.close();
  writeFileSync(join(out, "results.json"), JSON.stringify(results, null, 2));
  console.log(JSON.stringify(results, null, 2));
}
