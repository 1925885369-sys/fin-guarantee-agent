import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page } from "@playwright/test";

// 核心材料上传走真实链路：真项目、真 PDF、真解析。上传后能否启动 Agent 由后端
// 的 complete_due_diligence_ready 决定，界面不得自行放宽。
const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const materialsRoot = path.join(repositoryRoot, process.env.APP_DATA_DIR || ".data-e2e", "demo_materials");
const materialsDir = path.join(materialsRoot, "demo-bond-huachen");
const PROSPECTUS = path.join(materialsDir, "募集说明书（申报稿）.pdf");
const RATING_REPORT = path.join(materialsDir, "信用评级报告（拟发行版）.pdf");
// 演示案例只播种两份核心材料，没有非核心原件可用；补充材料这一路借用另一个虚构
// 案例的 PDF 字节（内容不同，不会命中重复校验），并显式改成「其他材料」。
const SUPPLEMENTARY = path.join(materialsRoot, "demo-bond-ancheng", "募集说明书（申报稿）.pdf");
const ISSUER_PROFILE_ID = "profile-demo-huachen-manufacturing";

async function openMaterialPage(page: Page, projectId: string, token: string, mobile: boolean) {
  await page.addInitScript((reference) => {
    localStorage.setItem("yandan-current", JSON.stringify(reference));
    localStorage.setItem("yandan-projects", JSON.stringify([reference]));
  }, { id: projectId, token });
  await page.goto("/");
  if (mobile) await page.locator(".menu-button").click();
  await page.getByRole("button", { name: /债券材料准备/ }).click();
  await expect(page.getByRole("heading", { name: "材料与证据" })).toBeVisible();
}

async function uploadFromModal(page: Page, filePath: string, expectedKind: string, timeout = 90_000) {
  await expect(page.getByLabel("材料类型")).toHaveValue(expectedKind);
  await page.getByLabel("选择材料文件").setInputFiles(filePath);
  await page.getByRole("button", { name: "上传", exact: true }).click();
  await expect(page.locator(".upload-status.busy")).toBeHidden({ timeout });
  await expect(page.locator(".upload-status.error, .upload-status.duplicate")).toHaveCount(0);
  await expect(page.locator(".upload-status.done")).toBeVisible({ timeout });
  await page.getByRole("button", { name: "关闭", exact: true }).click();
}

test("core materials can be uploaded and gate the agent run", async ({ page }, testInfo) => {
  test.setTimeout(240_000);
  for (const file of [PROSPECTUS, RATING_REPORT, SUPPLEMENTARY]) {
    expect(existsSync(file), `缺少演示材料：${file}`).toBeTruthy();
  }
  const profiles = await (await page.request.get("/api/entity-profiles")).json();
  const issuer = profiles.find((item: { id: string }) => item.id === ISSUER_PROFILE_ID) || profiles[0];
  expect(issuer, "主体档案库为空，无法建项").toBeTruthy();
  const created = await page.request.post("/api/projects", {
    data: {
      company_name: issuer.name, amount: 20000, term_months: 36, purpose: "补充流动资金并优化债务结构",
      bond_name: "", data_consent: true, project_type: "bond_guarantee", issuer_profile_id: issuer.id,
      guarantor_name: "", counter_guarantor_names: [], counter_guarantee_status: "undetermined",
      analysis_target: "full_due_diligence",
    },
  });
  expect(created.status(), await created.text()).toBe(201);
  const { id, accessToken } = await created.json();
  const mobile = testInfo.project.name === "mobile";
  const agentButton = page.getByRole("button", { name: "启动 Agent" });
  // 移动端沿用既有的「只读」设计，不暴露启动按钮；那边改由就绪文案体现闸门状态。
  const expectGate = async (open: boolean) => {
    await expect(page.locator(".inventory-summary"))
      .toContainText(open ? "完整尽调材料已齐备" : /材料不足|尚未上传材料/);
    if (!mobile) await expect(agentButton).toBeEnabled({ enabled: open });
  };

  await openMaterialPage(page, id, accessToken, mobile);

  // 场景 1：新建项目进入材料页，看到两个核心材料位与首次进入提示。
  await expect(page.getByTestId("material-onboarding")).toContainText("请先上传募集说明书和信用评级报告");
  await expect(page.locator('[data-material-slot="募集说明书"]')).toContainText("尚未上传募集说明书");
  await expect(page.locator('[data-material-slot="信用评级报告"]')).toContainText("尚未上传信用评级报告");
  // 场景 1 的首屏：提示与两个快捷上传按钮必须在第一屏可见，不被快照面板挤到下面。
  await page.screenshot({ path: testInfo.outputPath("material-first-screen.png") });
  // 场景 5：缺少任意一份时启动按钮保持禁用。
  await expectGate(false);
  if (!mobile) await expect(page.getByText("启动条件未满足")).toBeVisible();

  // 场景 2 + 3：材料位的上传按钮预选对应类型，上传后显示解析状态与质量。
  await page.locator('[data-material-slot="募集说明书"]').getByRole("button", { name: /上传募集说明书/ }).click();
  await page.screenshot({ path: testInfo.outputPath("upload-modal.png") });
  await uploadFromModal(page, PROSPECTUS, "募集说明书");
  const prospectusSlot = page.locator('[data-material-slot="募集说明书"]');
  await expect(prospectusSlot).toContainText("募集说明书（申报稿）.pdf");
  await expect(prospectusSlot).toContainText("已就绪");
  await expect(prospectusSlot).toContainText("已解析");
  // 只有一份核心材料时，闸门仍然关闭。
  await expect(page.getByTestId("material-onboarding")).toBeVisible();
  await expectGate(false);

  // 场景 4：第二份核心材料上传后，就绪度到 100%，闸门打开。
  await page.getByTestId("material-onboarding").getByRole("button", { name: /上传信用评级报告/ }).click();
  await uploadFromModal(page, RATING_REPORT, "信用评级报告");
  await expect(page.locator('[data-material-slot="信用评级报告"]')).toContainText("信用评级报告（拟发行版）.pdf");
  await expect(page.locator(".material-readiness-grid")).toContainText("100%");
  await expect(page.getByTestId("material-onboarding")).toHaveCount(0);
  await expectGate(true);

  // 场景 8（本轮）：重复上传同一份文件必须提示「该文件已上传」，不能当成成功。
  await page.screenshot({ path: testInfo.outputPath("material-page.png"), fullPage: true });
  await page.getByRole("button", { name: "上传材料", exact: true }).click();
  await page.getByLabel("材料类型").selectOption("募集说明书");
  await page.getByLabel("选择材料文件").setInputFiles(PROSPECTUS);
  await page.getByRole("button", { name: "上传", exact: true }).click();
  await expect(page.locator(".upload-status.duplicate")).toContainText("该文件已上传");
  await expect(page.locator(".upload-status.duplicate").getByRole("button", { name: "查看材料" })).toBeVisible();
  await page.getByRole("button", { name: "关闭", exact: true }).click();

  // 场景 6 + 8：补充材料走批量上传（沿用后端的自动分类），分类不合适时在列表中修正，
  // 无论归入哪一类都不改变报告等级。
  await page.locator('input[type="file"][multiple]').setInputFiles({
    name: "法律意见书.pdf", mimeType: "application/pdf", buffer: readFileSync(SUPPLEMENTARY),
  });
  // 后端按文件名与正文自动分类：这份材料可能落进核心材料位下的同类折叠分组，
  // 也可能直接落进补充材料表。两种落点都允许，先展开折叠分组再修正分类。
  const inCoreExtra = page.locator(".material-slot-extra").filter({ hasText: "法律意见书.pdf" });
  const inSupplementary = page.locator(".material-table").filter({ hasText: "法律意见书.pdf" });
  await expect(inCoreExtra.or(inSupplementary)).toHaveCount(1, { timeout: 90_000 });
  if (await inCoreExtra.count()) await inCoreExtra.locator("summary").click();
  const supplementarySelect = page.getByRole("combobox", { name: /修改 法律意见书\.pdf 分类/ }).first();
  await expect(supplementarySelect).toBeVisible();
  await supplementarySelect.selectOption("其他材料");
  await expect(page.locator(".material-table")).toContainText("法律意见书.pdf");
  await expectGate(true);

  // 场景 9：刷新后上传结果与就绪状态保持。
  await page.reload();
  if (mobile) await page.locator(".menu-button").click();
  await page.getByRole("button", { name: /债券材料准备/ }).click();
  await expect(page.locator('[data-material-slot="募集说明书"]')).toContainText("募集说明书（申报稿）.pdf");
  await expect(page.locator('[data-material-slot="信用评级报告"]')).toContainText("信用评级报告（拟发行版）.pdf");
  await expectGate(true);
  // 场景 10：窄屏同样不允许横向滚动。
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
});
