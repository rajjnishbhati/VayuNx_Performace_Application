// Share-link check in a real browser (Edge): a signed-in person creates a link from a result, and a browser
// that never signed in opens it - read-only, no sign-in prompt, downloads work. Same setup as auth-check.mjs.
//   node scripts/share-check.mjs <outDir> [uiBase]
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { chromium } from "playwright-core";

const [out = "share-screens", base = "http://127.0.0.1:3001"] = process.argv.slice(2);
mkdirSync(out, { recursive: true });
const results = { consoleErrors: [] };
const browser = await chromium.launch({ channel: "msedge", headless: true });
const author = await browser.newPage({ viewport: { width: 1360, height: 900 } });
author.on("pageerror", (e) => results.consoleErrors.push(String(e)));

try {
  // a small Lab experiment to share (1 trial x 0.3 s per algorithm)
  await author.goto(`${base}/settings`);
  await author.getByRole("button", { name: "Sign out" }).waitFor({ timeout: 60000 });
  const exp = await author.evaluate(async () => {
    const r = await fetch("/api/v2/lab/runs", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ presets: ["md5", "sha256"], trials: 1, duration_s: 0.3 }) });
    return (await r.json()).experiment_id;
  });
  await author.goto(`${base}/?experiment=${exp}`);
  await author.locator(".verdict").waitFor({ timeout: 120000 });
  await author.getByLabel("Share link lifetime").selectOption("30");
  await author.getByRole("button", { name: "Create share link" }).click();
  const shown = author.locator("pre.code-block").filter({ hasText: "/shared/vxs_" });
  await shown.waitFor({ timeout: 30000 });
  const link = (await shown.innerText()).trim();
  results.linkShape = link.replace(/vxs_[\w-]+/, "vxs_…");
  await author.screenshot({ path: join(out, "01-share-created.png"), fullPage: true });

  // a stranger: fresh browser context, never signed in
  const ctx = await browser.newContext({ viewport: { width: 1360, height: 900 } });
  const stranger = await ctx.newPage();
  stranger.on("pageerror", (e) => results.consoleErrors.push(String(e)));
  await stranger.goto(link);
  await stranger.locator(".verdict").waitFor({ timeout: 60000 });
  results.strangerStayedOnLink = new URL(stranger.url()).pathname.startsWith("/shared/");
  results.strangerVerdict = await stranger.locator(".verdict").innerText();
  results.banner = await stranger.getByRole("note").innerText();
  results.shareButtonHidden = (await stranger.getByRole("button", { name: "Create share link" }).count()) === 0;
  const pdf = await stranger.getByRole("link", { name: "PDF report" }).getAttribute("href");
  results.pdfStatus = await stranger.evaluate((h) => fetch(h).then((r) => `${r.status} ${r.headers.get("content-type")}`), pdf);
  results.strangerCannotListRuns = await stranger.evaluate(() => fetch("/api/v2/runs").then((r) => r.status));
  await stranger.screenshot({ path: join(out, "02-shared-view.png"), fullPage: true });
} finally {
  await browser.close();
  writeFileSync(join(out, "results.json"), JSON.stringify(results, null, 2));
  console.log(JSON.stringify(results, null, 2));
}
