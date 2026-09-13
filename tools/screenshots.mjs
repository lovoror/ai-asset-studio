// Capture portal screenshots for the README (dark + light). Usage: node tools/screenshots.mjs [baseUrl]
// Needs: npm i playwright && npx playwright install chromium   (inside tools/)
import { chromium } from "playwright";
import fs from "node:fs";
import path from "node:path";

const base = process.argv[2] || "http://127.0.0.1:8090";
const out = path.resolve(process.cwd(), "..", "docs", "screenshots");
fs.mkdirSync(out, { recursive: true });

const api = async (p) => (await fetch(base + p)).json();
const lib = await api("/v1/library?limit=60");
const session = lib.items.find((j) => j.kind === "image" && j.status === "completed");
const asset = lib.items.find((j) => j.kind === "asset" && j.status === "completed");

const pages = [
  ["create", "/"],
  ["queue", "/queue"],
  ["library", "/library"],
  session && ["session", `/sessions/${session.id}`],
  asset && ["asset", `/assets/${asset.id}`],
].filter(Boolean);

const browser = await chromium.launch();
for (const theme of ["dark", "light"]) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 960 }, colorScheme: theme, deviceScaleFactor: 1 });
  await ctx.addInitScript((t) => localStorage.setItem("as-theme", t), theme);
  const page = await ctx.newPage();
  for (const [name, url] of pages) {
    await page.goto(base + url, { waitUntil: "networkidle" });
    await page.waitForTimeout(name === "asset" ? 6000 : 1500); // let model-viewer load the GLB
    await page.screenshot({ path: path.join(out, `${name}-${theme}.png`), fullPage: name !== "asset" });
    console.log("saved", `${name}-${theme}.png`);
  }
  await ctx.close();
}
await browser.close();
