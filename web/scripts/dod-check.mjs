// Phase 2 definition-of-done check, driven in a real browser (installed Microsoft Edge via playwright-core).
//   node scripts/dod-check.mjs <outDir> [baseUrl]
// Expects the UI on baseUrl (default http://127.0.0.1:3000) talking to a Profiler Service with an EMPTY database.
import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { chromium } from "playwright-core";

const out = process.argv[2] ?? "dod-screens";
const base = process.argv[3] ?? "http://127.0.0.1:3000";
mkdirSync(out, { recursive: true });
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);
const shot = (page, name) => page.screenshot({ path: join(out, `${name}.png`), fullPage: true });

const browser = await chromium.launch({ channel: "msedge", headless: true });
const page = await browser.newPage({ viewport: { width: 1360, height: 900 } });
const consoleErrors = [];
page.on("console", (m) => m.type() === "error" && consoleErrors.push(m.text()));
page.on("pageerror", (e) => consoleErrors.push(String(e)));
let clicks = 0;
const click = async (locator) => { clicks += 1; await locator.click(); };
const results = {};

try {
  await page.goto(base);
  await page.getByText("Compare your first two algorithms").waitFor({ timeout: 30000 });
  await shot(page, "01-empty-state");
  log("empty state shown");

  // Canonical flow: "Run now: MD5 → Argon2id" starts the comparison in one click.
  await click(page.getByRole("group", { name: "Run a common comparison now" }).getByRole("button", { name: /MD5 → Argon2id/ }));
  await page.getByRole("progressbar").waitFor({ timeout: 30000 });
  log("running; clicks so far:", clicks);
  await page.waitForTimeout(8000);
  await shot(page, "02-progress");

  const started = Date.now();
  await page.locator(".verdict").waitFor({ timeout: 8 * 60 * 1000 });
  results.runSeconds = Math.round((Date.now() - started) / 1000) + 8;
  results.verdict = await page.locator(".verdict").innerText();
  log("verdict:", results.verdict);
  results.clicksToResult = clicks;
  results.scorecardRows = await page.locator("table.data tbody tr").first().locator("xpath=ancestor::table").locator("tbody tr").count();
  results.safeBadges = await page.getByText("Safe for passwords").count();
  results.notForPasswords = await page.getByText("Not for passwords").count();
  results.weakDataFlags = await page.getByRole("button", { name: /Weak data/ }).count();
  results.capacityVisible = await page.getByRole("heading", { name: "What if…" }).isVisible();
  results.appViewSelected = await page.getByRole("tab", { name: /App view/ }).getAttribute("aria-selected");
  await shot(page, "03-result-app-view");

  await click(page.getByRole("tab", { name: /Machine view/ }));
  await page.locator("svg[aria-label^='Cores busy']").waitFor({ timeout: 30000 });
  results.clicksToMachineView = clicks;
  await shot(page, "04-machine-view");

  await click(page.getByRole("tab", { name: "Security" }));
  results.clicksToSecurity = clicks;
  await shot(page, "05-security");

  // Keyboard: arrow keys move between tabs.
  await page.getByRole("tab", { name: "Security" }).focus();
  await page.keyboard.press("ArrowLeft");
  results.keyboardTabs = await page.getByRole("tab", { name: /Machine view/ }).getAttribute("aria-selected");

  // Table twin of the App view.
  await page.getByRole("tab", { name: /App view/ }).click();
  await page.getByRole("button", { name: "Table", exact: true }).click();
  await shot(page, "06-app-table");

  // Dark theme.
  await page.getByLabel("Theme").selectOption("dark");
  await page.getByRole("button", { name: "Chart", exact: true }).click();
  await shot(page, "07-result-dark");
  await page.getByLabel("Theme").selectOption("system");

  // Friendly error state.
  await page.goto(`${base}/?experiment=does-not-exist`);
  await page.getByText("How to fix it").waitFor({ timeout: 15000 });
  await shot(page, "08-error-state");

  await page.goto(`${base}/runs`);
  await page.getByText(/\d+ runs/).waitFor({ timeout: 15000 });
  await shot(page, "09-runs");
  await page.goto(`${base}/algorithms`);
  await page.getByText("OWASP Password Storage Cheat Sheet").waitFor({ timeout: 15000 });
  await shot(page, "10-algorithms");
} catch (e) {
  results.failure = String(e);
  await shot(page, "99-failure").catch(() => undefined);
} finally {
  results.consoleErrors = consoleErrors;
  console.log(JSON.stringify(results, null, 2));
  await browser.close();
}
