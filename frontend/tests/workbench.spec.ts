import { expect, test } from "@playwright/test";

test("demo project can be inspected from evidence to report", async ({ page }, testInfo) => {
  await page.goto("/");
  await expect(page.getByText("华辰精密制造有限公司").first()).toBeVisible();
  if (testInfo.project.name === "mobile") {
    await page.locator(".menu-button").click();
  }
  await page.getByRole("button", { name: /当前项目/ }).click();
  await page.getByRole("button", { name: /启航供应链贸易有限公司/ }).click();
  await expect(page.getByRole("heading", { name: "启航供应链贸易有限公司" })).toBeVisible();

  if (testInfo.project.name === "mobile") {
    await page.locator(".menu-button").click();
  }
  await page.getByRole("button", { name: /材料与事实/ }).click();
  await expect(page.getByRole("heading", { name: "材料与事实" })).toBeVisible();
  await expect(page.getByText("标准化事实")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();

  if (testInfo.project.name === "mobile") {
    await page.locator(".menu-button").click();
  }
  await page.getByRole("button", { name: /尽调报告/ }).click();
  await expect(page.getByText("融资担保尽调报告")).toBeVisible();
  await expect(page.getByRole("heading", { name: "启航供应链贸易有限公司" })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("workbench.png"), fullPage: true });
});

test("new project form requires data consent", async ({ page }, testInfo) => {
  await page.goto("/");
  if (testInfo.project.name === "mobile") {
    await page.locator(".menu-button").click();
  }
  await page.getByRole("button", { name: /当前项目/ }).click();
  await page.getByRole("button", { name: /新建项目/ }).click();
  await expect(page.getByRole("heading", { name: "录入担保申请" })).toBeVisible();
  await expect(page.getByRole("button", { name: "创建项目" })).toBeDisabled();
});
