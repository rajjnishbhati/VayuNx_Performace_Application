// Phase 3 check in a real browser (installed Microsoft Edge via playwright-core). Read-only: starts nothing.
//   node scripts/phase3-check.mjs <outDir> <experimentId> <appRunA,appRunB> [<appRunC,appRunD> ...]
// Expects the UI on http://127.0.0.1:3000 (VAYUNX_UI to override) and those runs/experiment in its Service.
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { chromium } from "playwright-core";

const [out = "phase3-screens", experimentId, ...pairs] = process.argv.slice(2);
const base = process.env.VAYUNX_UI ?? "http://127.0.0.1:3000";
mkdirSync(out, { recursive: true });
const results = { pairs: [], consoleErrors: [] };

const browser = await chromium.launch({ channel: "msedge", headless: true });

async function open(url, { width = 1360, dark = false } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: dark ? "dark" : "light" });
  const page = await ctx.newPage();
  page.on("console", (m) => m.type() === "error" && results.consoleErrors.push(`${url}: ${m.text()}`));
  page.on("pageerror", (e) => results.consoleErrors.push(`${url}: ${e}`));
  await page.goto(`${base}${url}`);
  return page;
}

const noHorizontalScroll = (page) => page.evaluate(() => document.scrollingElement.scrollWidth <= window.innerWidth);

try {
  // 1. "My app": each pair compared, what was compared is stated
  for (const [i, pair] of pairs.entries()) {
    const page = await open(`/?runs=${pair}`);
    await page.locator(".verdict").waitFor({ timeout: 60000 });
    const r = {
      pair,
      verdict: await page.locator(".verdict").innerText(),
      compared: await page.getByText(/Compared: the crypto calls inside/).first().innerText().catch(() => null),
      notes: await page.getByText(/CPU per call comes from per-call thread CPU/).count(),
    };
    await page.screenshot({ path: join(out, `app-${i + 1}.png`), fullPage: true });
    await page.getByRole("tab", { name: /Security/ }).click();
    r.securityTab = await page.getByText(/Not for passwords/).count() > 0 && await page.getByText(/Safe for passwords/).count() > 0;
    await page.getByRole("tab", { name: /Machine view/ }).click();
    await page.waitForTimeout(800);
    await page.screenshot({ path: join(out, `app-${i + 1}-machine.png`), fullPage: true });
    results.pairs.push(r);
    await page.context().close();
  }

  // 2. The Python vs Node.js Lab experiment
  if (experimentId) {
    const page = await open(`/?experiment=${experimentId}`);
    await page.locator(".verdict").waitFor({ timeout: 60000 });
    results.experimentVerdict = await page.locator(".verdict").innerText();
    await page.screenshot({ path: join(out, "lab-python-vs-node.png"), fullPage: true });
    await page.context().close();
    const dark = await open(`/?experiment=${experimentId}`, { dark: true });
    await dark.locator(".verdict").waitFor({ timeout: 60000 });
    await dark.screenshot({ path: join(out, "lab-python-vs-node-dark.png"), fullPage: true });
    await dark.context().close();
    const phone = await open(`/?experiment=${experimentId}`, { width: 390 });
    await phone.locator(".verdict").waitFor({ timeout: 60000 });
    results.phoneNoHorizontalScroll = await noHorizontalScroll(phone);
    await phone.screenshot({ path: join(out, "lab-python-vs-node-phone.png"), fullPage: true });
    await phone.context().close();
  }

  // 3. Picker: Node.js row and the cross-runtime quick pick (not clicked: that would start an experiment)
  {
    const page = await open("/");
    await page.getByRole("group", { name: /Node\.js algorithms/ }).waitFor({ timeout: 30000 });
    results.nodeChips = await page.getByRole("group", { name: /Node\.js algorithms/ }).getByRole("button").count();
    results.crossRuntimeQuickPick = await page.getByRole("button", { name: /Argon2id: Python vs Node\.js/ }).count();
    await page.screenshot({ path: join(out, "picker.png"), fullPage: true });
    await page.getByRole("button", { name: "My app" }).click();
    await page.getByRole("heading", { name: /Pick app runs to compare/ }).waitFor({ timeout: 30000 });
    const text = await page.locator("section", { has: page.locator("#apps-title") }).innerText();
    results.myAppListsExamples = ["fastapi-login-example", "next-login-example"].map((s) => text.includes(s));
    await page.screenshot({ path: join(out, "my-app-list.png"), fullPage: true });
    await page.context().close();
  }

  // 4. Algorithms page: Node.js column
  {
    const page = await open("/algorithms");
    await page.getByRole("columnheader", { name: "Node.js" }).waitFor({ timeout: 30000 });
    await page.getByText("md5@node").waitFor({ timeout: 30000 });
    results.algorithmsNodeCells = await page.getByText(/^node:crypto/).count();
    await page.screenshot({ path: join(out, "algorithms.png"), fullPage: true });
    await page.context().close();
  }
} finally {
  await browser.close();
  writeFileSync(join(out, "results.json"), JSON.stringify(results, null, 2));
  console.log(JSON.stringify(results, null, 2));
}
