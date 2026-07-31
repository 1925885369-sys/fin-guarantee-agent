import { mkdir, rename, rm } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { chromium } from "playwright";

const output = resolve("../docs/assets/due-diligence-demo.webm");
const videoDir = resolve("../docs/assets/.recording");
await mkdir(videoDir, { recursive: true });

const browser = await chromium.launch({ channel: "msedge" });
const context = await browser.newContext({
  viewport: { width: 1440, height: 900 },
  recordVideo: { dir: videoDir, size: { width: 1440, height: 900 } },
});
const page = await context.newPage();
const pause = (milliseconds = 3500) => page.waitForTimeout(milliseconds);

await page.goto(process.env.DEMO_BASE_URL || "http://127.0.0.1:8010");
await page.getByText("华辰精密制造有限公司").first().waitFor();
await pause();
await page.getByRole("button", { name: /当前项目/ }).click();
await page.getByRole("button", { name: /启航供应链贸易有限公司/ }).click();
await page.getByRole("heading", { name: "启航供应链贸易有限公司" }).waitFor();
await pause(4500);
await page.getByRole("button", { name: /材料与事实/ }).click();
await page.getByText("标准化事实").waitFor();
await pause(4500);
await page.getByText("标准化事实").scrollIntoViewIfNeeded();
await pause(4000);
await page.getByRole("button", { name: /交叉核验/ }).click();
await pause(5000);
const evidence = page.getByRole("button", { name: "查看证据" }).first();
if (await evidence.isVisible()) {
  await evidence.click();
  await pause(4500);
  await page.locator(".drawer-head .icon-button").click();
}
await page.getByRole("button", { name: /风险清单/ }).click();
await pause(5000);
await page.getByRole("button", { name: /尽调报告/ }).click();
await page.getByText("融资担保尽调报告").waitFor();
await pause(5000);
await page.locator("#section-financial").scrollIntoViewIfNeeded();
await pause(5000);
await page.locator("#section-risks").scrollIntoViewIfNeeded();
await pause(5000);
await page.locator("#section-evidence").scrollIntoViewIfNeeded();
await pause(4000);

const video = page.video();
await context.close();
await browser.close();
if (!video) throw new Error("Video recording was not created");
const temporary = await video.path();
await mkdir(dirname(output), { recursive: true });
await rm(output, { force: true });
await rename(temporary, output);
await rm(videoDir, { recursive: true, force: true });
console.log(output);
