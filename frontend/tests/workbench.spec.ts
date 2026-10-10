import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type APIRequestContext } from "@playwright/test";

const materialsDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../", process.env.APP_DATA_DIR || ".data-e2e", "demo_materials", "demo-bond-huachen");
const prospectus = path.join(materialsDir, "募集说明书（申报稿）.pdf");
const ratingReport = path.join(materialsDir, "信用评级报告（拟发行版）.pdf");

async function analyzeDemo(request: APIRequestContext, projectId: string) {
  const existingReport = await request.get(`/api/projects/${projectId}/report`);
  if (existingReport.ok() && (await existingReport.json()).length > 0) return;
  const started = await request.post(`/api/projects/${projectId}/runs`, { data: { focus: "全面尽调" } });
  expect(started.ok()).toBeTruthy();
  const runId = (await started.json()).id as string;
  for (let attempt = 0; attempt < 600; attempt += 1) {
    const response = await request.get(`/api/runs/${runId}`);
    const run = await response.json();
    if (["completed", "failed", "cancelled", "model_required"].includes(run.status)) {
      expect(run.status, run.error || run.message).toBe("completed");
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("P0 demo analysis timed out");
}

test("bond project can be inspected from evidence to report", async ({ page }, testInfo) => {
  await page.route("**/api/projects/demo-bond-huachen/material-qa", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      status: "answered",
      answer: "募集资金拟用于补充流动资金。",
      claims: [{ text: "募集资金拟用于补充流动资金。", source_ids: ["qa-source"] }],
      citations: [{
        id: "c-qa-source", chunk_id: "qa-chunk", source_id: "qa-source", document_id: "qa-document",
        filename: "募集说明书.pdf", page: 3, locator: "第 3 页", quote: "本期募集资金用于补充流动资金。",
        chunk_type: "text", table_key: "", score: 0.02,
      }],
      retrieval_mode: "bm25_only", conflict_detected: false,
    }),
  }));
  await page.route("**/api/citations/c-qa-source", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      id: "c-qa-source", label: "募集说明书.pdf · 第3页",
      quote: "本期募集资金用于补充流动资金。", locator_type: "pdf_page", locator: "第 3 页",
      page: 3, bbox: [], extraction_method: "native",
    }),
  }));
  await page.goto("/");
  await expect(page.getByText("华辰精密制造有限公司").first()).toBeVisible();
  await expect(page.getByRole("heading", { name: "华辰精密制造有限公司" })).toBeVisible();
  await expect(page.getByRole("button", { name: /债券材料准备/ })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("workflow-cockpit.png"), fullPage: true });
  await analyzeDemo(page.request, "demo-bond-huachen");
  await page.reload();

  if (testInfo.project.name === "mobile") {
    await page.locator(".menu-button").click();
  }
  await page.getByRole("button", { name: /债券材料准备/ }).click();
  await expect(page.getByRole("heading", { name: "材料与证据" })).toBeVisible();
  await page.getByRole("button", { name: "标准化事实" }).click();
  await expect(page.getByRole("heading", { name: "标准化事实" })).toBeVisible();
  await expect(page.getByText(/单体/).first()).toBeVisible();
  await page.getByRole("button", { name: "材料清单" }).click();
  await page.getByRole("button", { name: "材料问答" }).click();
  await page.getByTestId("material-qa").locator("textarea").fill("募集资金的用途是什么？");
  await page.getByRole("button", { name: "提问" }).click();
  await expect(page.getByText("募集资金拟用于补充流动资金。").first()).toBeVisible();
  await page.getByRole("button", { name: /募集说明书.pdf.*第 3 页/ }).first().click();
  await expect(page.locator(".evidence-drawer")).toContainText("本期募集资金用于补充流动资金。");
  await page.getByRole("button", { name: "关闭" }).last().click();
  await page.getByRole("button", { name: "材料清单" }).click();
  await page.getByRole("button", { name: "证据链概览" }).click();
  await expect(page.getByText("条证据关系")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();

  if (testInfo.project.name === "mobile") {
    await page.locator(".menu-button").click();
  }
  await page.getByRole("button", { name: /债券 Agent 分析/ }).click();
  await expect(page.getByText("最近一次运行")).toBeVisible();
  await expect(page.getByText("报表勾稽校验")).toBeVisible();
  await page.getByRole("button", { name: "查看计算链路" }).first().click();
  await expect(page.getByText("Deterministic calculation trace")).toBeVisible();
  await page.getByRole("button", { name: "关闭计算链路" }).click();

  if (testInfo.project.name === "mobile") {
    await page.locator(".menu-button").click();
  }
  await page.getByRole("button", { name: /尽调报告/ }).click();
  await expect(page.getByText("债券发行融资担保尽调报告")).toBeVisible();
  await expect(page.getByRole("heading", { name: /华辰精密制造有限公司2026年面向专业投资者公开发行公司债券/ })).toBeVisible();
  await expect(page.getByText(/事实支持率 \d+%/)).toBeVisible();
  await expect(page.getByText(/无证据主张/)).toBeVisible();
  // 篇幅徽标：实际分析正文字符数 + 该章 Prompt 设置的目标区间（不再有系统硬上限）。
  await expect(page.locator(".section-length").first()).toContainText("正文");
  await expect(page.locator(".section-length").first()).toContainText(/目标 \d+～\d+/);
  await expect(page.locator(".section-length").first()).not.toContainText("硬上限");
  await expect(page.getByText(/建议有条件提供担保|建议暂缓担保|建议在落实授信条件后提供担保/)).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("workbench.png"), fullPage: true });
});

test("project switcher stays outside the workflow rail and supports search and dismissal", async ({ page }, testInfo) => {
  await page.goto("/");
  if (testInfo.project.name === "mobile") await page.locator(".menu-button").click();

  await expect(page.locator(".project-switch strong")).not.toHaveText("正在载入");
  const workflowBefore = await page.locator(".workflow-nav-wrap").boundingBox();
  const trigger = page.getByRole("button", { name: /当前项目/ });
  await trigger.click();
  const dialog = page.getByRole("dialog", { name: "切换债券项目" });
  await expect(dialog).toBeVisible();
  await expect(page.getByRole("heading", { name: "演示评测案例" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "真实参考项目" })).toBeVisible();

  const search = page.getByRole("textbox", { name: "搜索债券项目" });
  await expect(search).toBeFocused();
  await search.fill("中邮");
  await expect(dialog.getByRole("button", { name: /中邮证券有限责任公司/ })).toBeVisible();
  await expect(dialog.getByRole("button", { name: /华辰精密制造有限公司/ })).toHaveCount(0);

  const workflowAfter = await page.locator(".workflow-nav-wrap").boundingBox();
  expect(workflowAfter?.y).toBe(workflowBefore?.y);
  const dialogBox = await dialog.boundingBox();
  const viewport = page.viewportSize();
  if (testInfo.project.name === "mobile") {
    expect(dialogBox?.x).toBe(0);
    expect(dialogBox?.width).toBe(viewport?.width);
    // 建项入口在移动端保留：材料上传与建项都是移动端的一等能力。
    await expect(page.getByRole("button", { name: "新建债券担保项目" })).toBeVisible();
  } else {
    expect(dialogBox?.width).toBeGreaterThanOrEqual(400);
    expect(dialogBox?.x).toBeGreaterThanOrEqual(248);
  }

  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(trigger).toBeFocused();
  await trigger.click();
  if (testInfo.project.name === "mobile") {
    await dialog.getByRole("button", { name: "关闭项目选择" }).click();
  } else {
    await page.locator(".project-switcher-backdrop").click({ position: { x: 2, y: 2 } });
  }
  await expect(dialog).toBeHidden();
});

test("professional review center keeps qualitative findings behind human confirmation", async ({ page }, testInfo) => {
  await page.goto("/");
  const reset = await page.request.patch("/api/projects/demo-bond-jinghe/candidate-findings/candidate-demo-bond-jinghe", {
    data: { status: "proposed", decision_note: "E2E 初始状态" },
  });
  expect(reset.ok()).toBeTruthy();
  if (testInfo.project.name === "mobile") await page.locator(".menu-button").click();
  await page.getByRole("button", { name: /当前项目/ }).click();
  await page.getByRole("button", { name: /景和城市服务有限公司/ }).click();
  await expect(page.locator(".project-switch strong")).toHaveText("景和城市服务有限公司");
  if (testInfo.project.name === "mobile") await page.locator(".menu-button").click();
  await page.getByRole("button", { name: /复核中心/ }).click();
  await expect(page.getByRole("heading", { name: "复核中心" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "关键审批权限集中于实控人" })).toBeVisible();
  if (testInfo.project.name === "mobile") {
    await expect(page.getByRole("button", { name: "确认风险" })).toBeHidden();
    return;
  }
  await page.getByRole("button", { name: "确认风险" }).click();
  await expect(page.getByText("已确认")).toBeVisible();
  await page.request.patch("/api/projects/demo-bond-jinghe/candidate-findings/candidate-demo-bond-jinghe", {
    data: { status: "proposed", decision_note: "E2E 状态恢复" },
  });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
});

test("new project form requires data consent", async ({ page }, testInfo) => {
  await page.route("**/api/entity-profiles", async (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([{
    id: "profile-issuer", name: "测试发行人有限公司", unified_code: "91110000TEST000001", baseline_date: "2025-12-31",
    latest_financial_period: "2025年度", completeness: 0.86, status: "ready", version: 3, project_count: 2,
    counts: { confirmed: 24, single_source: 8, stale: 0, conflicted: 0 },
  }]) }));
  await page.goto("/");
  if (testInfo.project.name === "mobile") {
    await page.locator(".menu-button").click();
  }
  await page.getByRole("button", { name: /当前项目/ }).click();
  // 建项入口在移动端同样保留：材料上传与建项都是移动端的一等能力。
  const newProject = page.getByRole("button", { name: /新建债券担保项目/ });
  await expect(newProject).toBeVisible();
  await newProject.click();
  await expect(page.getByRole("heading", { name: "建项方式" })).toBeVisible();
  await page.getByText("使用主体资料库", { exact: true }).click();
  await page.getByRole("button", { name: /测试发行人有限公司/ }).click();
  await page.getByRole("button", { name: /下一步/ }).click();
  await expect(page.getByRole("heading", { name: "配置交易主体" })).toBeVisible();
  await page.getByLabel("不适用").check();
  await page.getByRole("button", { name: /下一步/ }).click();
  await expect(page.getByRole("heading", { name: "填写债券信息" })).toBeVisible();
  await expect(page.getByRole("button", { name: /下一步/ })).toBeDisabled();
  await page.getByLabel("预计本期规模（万元）").fill("20000");
  await page.getByLabel("募集资金用途").fill("补充营运资金");
  await expect(page.getByRole("button", { name: /下一步/ })).toBeDisabled();
  await page.getByText(/我确认本项目材料符合内部数据处理要求/).click();
  await page.getByRole("button", { name: /下一步/ }).click();
  await expect(page.getByRole("heading", { name: "上传核心材料" })).toBeVisible();
  await expect(page.getByRole("button", { name: /下一步/ })).toBeDisabled();
});

test("bond intake accepts 200000万元 and retries only the failed upload", async ({ page }, testInfo) => {
  let createAttempts = 0;
  let uploadAttempts = 0;
  let createdAmount = 0;
  await page.route("**/api/entity-profiles", async (route) => route.fulfill({ status: 200, contentType: "application/json", body: "[]" }));
  await page.route("**/api/projects", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    createAttempts += 1;
    const payload = route.request().postDataJSON() as { amount: number };
    createdAmount = payload.amount;
    if (createAttempts === 1) {
      return route.fulfill({ status: 422, contentType: "application/json", body: JSON.stringify({
        detail: [{ loc: ["body", "amount"], msg: "Input should be greater than 0", type: "greater_than" }],
      }) });
    }
    return route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify({ id: "project-intake-retry", accessToken: "test-project-token" }) });
  });
  await page.route("**/api/projects/project-intake-retry/documents", async (route) => {
    uploadAttempts += 1;
    if (uploadAttempts === 2) {
      return route.fulfill({ status: 413, contentType: "application/json", body: JSON.stringify({ detail: "单个文件超过限制" }) });
    }
    const kind = uploadAttempts === 1 ? "募集说明书" : "信用评级报告";
    return route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify({
      id: `uploaded-${uploadAttempts}`, kind, documentRole: uploadAttempts === 1 ? "prospectus" : "rating_report",
      parseStatus: "parsed", quality: 1, warning: "",
    }) });
  });
  await page.goto("/");
  if (testInfo.project.name === "mobile") await page.locator(".menu-button").click();
  await page.getByRole("button", { name: /当前项目/ }).click();
  await page.getByRole("button", { name: /新建债券担保项目/ }).click();
  await page.getByText("直接使用发行材料", { exact: true }).click();
  await page.getByLabel("发行人名称").fill("中邮证券有限责任公司");
  await page.getByRole("button", { name: /下一步/ }).click();
  await page.getByLabel("不适用").check();
  await page.getByRole("button", { name: /下一步/ }).click();
  await page.getByLabel("预计本期规模（万元）").fill("200000");
  await page.getByLabel("募集资金用途").fill("补充流动资金");
  await page.getByText(/我确认本项目材料符合内部数据处理要求/).click();
  await page.getByRole("button", { name: /下一步/ }).click();
  await page.locator('.project-intake input[type="file"]').nth(0).setInputFiles(prospectus);
  await page.locator('.project-intake input[type="file"]').nth(1).setInputFiles(ratingReport);
  await page.getByRole("button", { name: /下一步/ }).click();

  await page.getByRole("button", { name: "确认并创建" }).click();
  await expect(page.locator(".inline-error").filter({ hasText: "本期规模：必须大于 0" })).toBeVisible();
  await page.getByRole("button", { name: "确认并创建" }).click();
  await expect(page.locator(".inline-error").filter({ hasText: "评级报告上传失败：单个文件超过限制" })).toBeVisible();
  expect(createAttempts).toBe(2);
  expect(createdAmount).toBe(200000);
  expect(uploadAttempts).toBe(2);

  await page.getByRole("button", { name: "重试未完成上传" }).click();
  await expect(page.locator(".project-intake")).toHaveCount(0);
  expect(createAttempts).toBe(2);
  expect(uploadAttempts).toBe(3);
});

test("entity profile library drives the five-step project intake", async ({ page }, testInfo) => {
  const profile = {
    id: "profile-issuer", name: "华源基础设施有限公司", unified_code: "91110108PROFILE001", baseline_date: "2025-12-31",
    latest_financial_period: "2025年度", completeness: 0.9, status: "ready", version: 4, project_count: 3,
    counts: { confirmed: 31, single_source: 6, stale: 0, conflicted: 0 },
  };
  await page.route("**/api/entity-profiles", async (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([profile]) }));
  await page.route("**/api/entity-profiles/profile-issuer", async (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
    ...profile, aliases: ["华源基建"], facts: [
      { id: "f-basic", fact_key: "unified_code", name: "统一社会信用代码", category: "basic", value: profile.unified_code, period: null, scope: "主体", status: "confirmed", source_label: "工商档案", page: 1 },
      { id: "f-fin", fact_key: "total_assets", name: "资产总额", category: "financial", value: 320000, unit: "万元", period: "2025", scope: "consolidated", status: "single_source", source_label: "主体财务报表", page: 4 },
    ],
  }) }));
  await page.route("**/api/entity-profiles/profile-issuer/readiness", async (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
    ready_for_project: true, ready_for_agent: true, complete_due_diligence_ready: false, completeness: 0.9,
    module_scores: [], hard_vetoes: [], requirements: [], notices: ["单一来源事实尚未独立验证"],
  }) }));
  await page.route("**/api/entity-profiles/profile-issuer/versions", async (route) => route.fulfill({ status: 200, contentType: "application/json", body: "[]" }));
  await page.goto("/");
  if (testInfo.project.name === "mobile") await page.locator(".menu-button").click();
  await page.getByRole("button", { name: /主体档案库/ }).click();
  await expect(page.getByRole("heading", { name: "发行人主体档案" })).toBeVisible();
  await expect(page.getByText("91110108PROFILE001").first()).toBeVisible();
  await expect(page.getByText(/单一来源.*尚未独立验证/)).toBeVisible();
  await expect(page.getByText("90%").first()).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  const create = page.getByRole("button", { name: /基于主体建项/ });
  if (testInfo.project.name === "mobile") {
    await expect(create).toBeHidden();
    return;
  }
  await create.click();
  await page.locator(".project-intake .intake-profile-grid > button").filter({ hasText: "华源基础设施有限公司" }).click();
  await page.getByRole("button", { name: /下一步/ }).click();
  await page.getByLabel("不适用").check();
  await page.getByRole("button", { name: /下一步/ }).click();
  await page.getByLabel("预计本期规模（万元）").fill("20000");
  await page.getByLabel("募集资金用途").fill("偿还有息债务并补充营运资金");
  await page.getByText(/我确认本项目材料符合内部数据处理要求/).click();
  await page.getByRole("button", { name: /下一步/ }).click();
  await expect(page.getByRole("heading", { name: "上传核心材料" })).toBeVisible();
  await page.locator('.project-intake input[type="file"]').nth(0).setInputFiles(prospectus);
  await expect(page.getByRole("button", { name: /下一步/ })).toBeDisabled();
  await page.locator('.project-intake input[type="file"]').nth(1).setInputFiles(ratingReport);
  await page.getByRole("button", { name: /下一步/ }).click();
  await expect(page.getByRole("heading", { name: "确认并创建" })).toBeVisible();
  await expect(page.locator(".snapshot-confirm")).toContainText("主体档案 v4");
  await expect(page.locator(".snapshot-confirm")).toContainText("募集说明书（申报稿）.pdf");
  await expect(page.locator(".snapshot-confirm")).toContainText("信用评级报告（拟发行版）.pdf");
  await expect(page.locator(".snapshot-confirm")).toContainText("反担保：不适用");
});

test("user project document classification refreshes core material readiness", async ({ page }, testInfo) => {
  let classificationChanged = false;
  await page.route("**/api/projects/demo-bond-huachen", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({ response, json: { ...body, data_class: "user_project" } });
  });
  await page.route("**/api/projects/demo-bond-huachen/document-inventory", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    if (!classificationChanged) {
      await route.fulfill({ response, json: body });
      return;
    }
    const documents = body.documents.map((item: { kind: string; document_role: string }, index: number) =>
      index === 0 ? { ...item, kind: "其他材料", document_role: "other" } : item,
    );
    const gaps = body.gaps.some((item: { group: string }) => item.group === "募集说明书")
      ? body.gaps
      : [{ group: "募集说明书", expectedKinds: ["募集说明书"], status: "missing" }, ...body.gaps];
    await route.fulfill({ response, json: {
      ...body, documents, gaps, core_materials_ready: false, core_pre_review_ready: false,
      complete_due_diligence_ready: false, analysis_mode: "insufficient",
    } });
  });
  await page.route("**/api/projects/demo-bond-huachen/documents/*", async (route) => {
    if (route.request().method() !== "PATCH") {
      await route.continue();
      return;
    }
    classificationChanged = true;
    const payload = route.request().postDataJSON();
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ id: "mock-document", ...payload, changed: true }) });
  });

  await page.goto("/");
  if (testInfo.project.name === "mobile") await page.locator(".menu-button").click();
  await page.getByRole("button", { name: /债券材料准备/ }).click();
  await expect(page.getByText("完整尽调材料已齐备", { exact: true }).first()).toBeVisible();
  // 分类选择器在移动端同样必须可见可操作：材料上传是移动端的一等能力。
  const classification = page.getByRole("combobox", { name: /修改.*募集说明书.*分类/ }).first();
  await expect(classification).toBeVisible();
  await classification.selectOption("其他材料");
  await expect(page.getByText("材料不足", { exact: true }).first()).toBeVisible();
  await expect(page.locator(".inventory-summary")).toContainText("缺少：募集说明书");
});

test("review task can be resolved and restored with an audit-visible state", async ({ page }, testInfo) => {
  const task = {
    id: "task-action-package-e2e", task_type: "material_gap", priority: "high", title: "补充债务与到期结构材料",
    description: "该根因影响债务集中度和偿债现金流核验。", status: "open", related_ids: [],
    required_evidence: "债务明细和到期债务表", resolution_note: "", assignee: "项目经理", due_date: "2026-09-15",
    issue_key: "material:debt-package", issue_category: "material_package", affected_fact_ids: [],
    affected_rule_ids: ["debt_concentration", "cashflow_coverage"], blocking_level: "blocking", source_requirement: "债务明细和到期债务表",
  };
  await page.route("**/api/projects/demo-bond-jinghe/review-tasks", async (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([task]) }));
  await page.route("**/api/projects/demo-bond-jinghe/review-tasks/task-action-package-e2e", async (route) => {
    const payload = route.request().postDataJSON() as { status: string; resolution_note: string };
    task.status = payload.status;
    task.resolution_note = payload.resolution_note;
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(task) });
  });
  await page.goto("/");
  if (testInfo.project.name === "mobile") await page.locator(".menu-button").click();
  await page.getByRole("button", { name: /当前项目/ }).click();
  await page.getByRole("button", { name: /景和城市服务有限公司/ }).click();
  if (testInfo.project.name === "mobile") await page.locator(".menu-button").click();
  await page.getByRole("button", { name: /复核中心/ }).click();
  await page.getByRole("button", { name: /尽调任务/ }).click();
  const taskRow = page.locator(".task-row").filter({ hasText: task.title });
  await expect(taskRow).toBeVisible();
  await expect(taskRow).toContainText("2 项规则");
  if (testInfo.project.name === "mobile") {
    await expect(taskRow.getByTitle("解决")).toBeHidden();
    return;
  }
  await taskRow.getByTitle("解决").click();
  await expect(taskRow.getByText("已解决")).toBeVisible();
  expect(task.status).toBe("resolved");
});

test("report section can be regenerated and Markdown can be downloaded", async ({ page }, testInfo) => {
  await page.goto("/");
  await analyzeDemo(page.request, "demo-bond-huachen");
  await page.reload();
  if (testInfo.project.name === "mobile") await page.locator(".menu-button").click();
  await page.getByRole("button", { name: /尽调报告/ }).click();

  const firstSection = page.locator(".report-section").first();
  if (testInfo.project.name === "mobile") {
    await expect(firstSection.getByTitle("重新生成")).toBeHidden();
    await expect(page.getByRole("button", { name: "Markdown" })).toBeHidden();
    return;
  }
  await firstSection.getByTitle("重新生成").click();
  await expect(firstSection.getByTitle("重新生成")).toBeEnabled();

  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "Markdown" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toContain("华辰精密制造有限公司2026年面向专业投资者公开发行公司债券");
  expect(download.suggestedFilename()).toMatch(/\.md$/);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
});

test("report prompt workspace supports structured chapter editing, preview, and reset", async ({ page }, testInfo) => {
  await page.goto("/");
  if (testInfo.project.name === "mobile") await page.locator(".menu-button").click();
  await page.locator(".prompt-manager-link").click();

  await expect(page.locator(".prompt-workspace")).toBeVisible();
  await expect(page.locator(".prompt-chapter-option")).toHaveCount(17);

  const selectChapter = async (index: number) => {
    if (testInfo.project.name === "mobile") await page.locator(".prompt-mobile-picker select").selectOption({ index });
    else await page.locator(".prompt-chapter-option").nth(index).click();
  };

  await selectChapter(0);
  await expect(page.locator(".deterministic-prompt-note")).toBeVisible();
  await expect(page.getByRole("button", { name: "保存项目配置" })).toHaveCount(0);

  await selectChapter(5);
  await expect(page.locator(".prompt-constraints-summary")).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.getByRole("button", { name: "查看详情" }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.getByRole("dialog")).toContainText("必须保留");
  await expect(page.getByRole("dialog")).toContainText("禁止事项");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);

  const editor = page.getByLabel("章节目标");
  await expect(editor).toBeVisible();
  const original = await editor.inputValue();
  const changed = `${original}
补充披露数据口径与待核验边界。`;
  await editor.fill(changed);
  await selectChapter(6);
  await selectChapter(5);
  await expect(editor).toHaveValue(changed);
  // 篇幅由逐章 Prompt 控制：编辑器展示目标区间与实际口径，且不再有系统字符硬上限。
  await page.locator("summary").filter({ hasText: "行文组织" }).first().click();
  await expect(page.getByText("目标最小字符")).toBeVisible();
  await expect(page.locator(".prompt-length-policy")).toContainText("目标篇幅");
  await expect(page.locator(".prompt-length-policy")).not.toContainText("硬上限");
  await expect(page.locator(".prompt-length-policy")).toContainText("只统计分析正文");
  await page.screenshot({ path: testInfo.outputPath("prompt-length-policy.png") });

  await page.getByRole("button", { name: "保存项目配置" }).click();
  await expect(page.locator(".prompt-detail-badges .custom")).toBeVisible();
  const globalButton = page.locator(".prompt-action-bar .secondary-button");
  await expect(globalButton).toBeEnabled();
  await globalButton.click();
  await expect(page.locator(".prompt-action-bar")).toContainText("当前配置已保存");
  await expect(page.locator(".prompt-action-bar")).toContainText("最近更新");
  await page.getByRole("tab", { name: "完整 Prompt" }).click();
  await expect(page.locator(".prompt-preview textarea")).toHaveValue(/不可修改的风控约束/);
  await page.getByRole("tab", { name: "结构化配置" }).click();
  await page.getByRole("button", { name: "撤销项目覆盖" }).click();
  if (await page.getByRole("button", { name: "撤销全局默认" }).count()) {
    await page.getByRole("button", { name: "撤销全局默认" }).click();
  }

  await page.screenshot({ path: testInfo.outputPath("report-prompts.png"), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
});

test("bond flagship exposes transaction materials and bond report structure", async ({ page }, testInfo) => {
  await page.goto("/");
  const summaryResponse = await page.request.get("/api/projects/demo-bond-huachen/bond-summary");
  expect(summaryResponse.ok()).toBeTruthy();
  const summary = await summaryResponse.json() as { bondName: string; issueAmount: number; issuerRating: string; bondRating: string; guarantor: string; reportLevel: string };
  expect(summary.bondName).toContain("面向专业投资者公开发行公司债券");
  expect(summary.issueAmount).toBe(20000);
  expect(summary.issuerRating).toBe("AA-");
  expect(summary.bondRating).toBe("AA+");
  expect(summary.guarantor).toContain("东辰");
  expect(summary.reportLevel).toBe("完整尽调");

  const inventoryResponse = await page.request.get("/api/projects/demo-bond-huachen/document-inventory");
  const inventory = await inventoryResponse.json() as {
    documents: Array<unknown>;
    complete_due_diligence_ready: boolean;
    analysis_mode: string;
  };
  expect(inventory.documents).toHaveLength(2);
  expect(inventory.complete_due_diligence_ready).toBeTruthy();
  expect(inventory.analysis_mode).toBe("full_due_diligence");
  await analyzeDemo(page.request, "demo-bond-huachen");
  await page.reload();
  await expect(page.locator(".project-switch strong")).toHaveText("华辰精密制造有限公司");
  await expect(page.getByText("20,000 万元").first()).toBeVisible();
  await expect(page.getByText("AA- / AA+")).toBeVisible();

  if (testInfo.project.name === "mobile") await page.locator(".menu-button").click();
  await page.getByRole("button", { name: /债券材料准备/ }).click();
  // 两份核心材料落在各自的固定材料位，不再走补充材料的表格行。
  await expect(page.locator(".material-slot")).toHaveCount(2);
  await expect(page.locator('[data-material-slot="募集说明书"]')).toContainText("募集说明书（申报稿）.pdf");
  await expect(page.locator('[data-material-slot="信用评级报告"]')).toContainText("信用评级报告（拟发行版）.pdf");
  if (testInfo.project.name === "mobile") await page.locator(".menu-button").click();
  await page.getByRole("button", { name: /报告交付/ }).click();
  await expect(page.getByText("债券发行融资担保尽调报告")).toBeVisible();
  await expect(page.getByRole("heading", { name: "证据索引和材料缺口" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
});

test("real public reference project is separated and shows bond tranches", async ({ page }, testInfo) => {
  await page.goto("/");
  const references = await page.request.get("/api/projects?data_class=real_reference");
  expect(references.ok()).toBeTruthy();
  expect((await references.json()) as Array<{ id: string }>).toContainEqual(expect.objectContaining({ id: "real-bond-zhongyou-2026-02" }));
  expect((await references.json()) as Array<{ id: string }>).toContainEqual(expect.objectContaining({ id: "real-bond-beitou-2026-01" }));

  if (testInfo.project.name === "mobile") await page.locator(".menu-button").click();
  await page.getByRole("button", { name: /当前项目/ }).click();
  await expect(page.getByText("真实参考项目")).toBeVisible();
  await page.getByRole("button", { name: /中邮证券有限责任公司/ }).click();

  await expect(page.getByRole("heading", { name: "中邮证券有限责任公司" })).toBeVisible();
  await expect(page.getByText("真实公开材料").first()).toBeVisible();
  await expect(page.getByText("非评测案例").first()).toBeVisible();
  await expect(page.getByText("完整尽调").first()).toBeVisible();
  await expect(page.getByText("品种一（3年期）")).toBeVisible();
  await expect(page.getByText("品种二（5年期）")).toBeVisible();
  await expect(page.getByText("材料未披露各品种分配金额")).toBeVisible();
  await expect(page.getByText("评级机构意见不等于担保机构判断")).toBeVisible();
  await expect(page.getByRole("button", { name: "Markdown" })).toHaveCount(0);

  if (testInfo.project.name === "mobile") await page.locator(".menu-button").click();
  await page.getByRole("button", { name: /当前项目/ }).click();
  await page.getByRole("button", { name: /北京市基础设施投资有限公司/ }).click();
  await expect(page.getByRole("heading", { name: "北京市基础设施投资有限公司" })).toBeVisible();
  await expect(page.getByText("品种一（3年期）")).toBeVisible();
  await expect(page.getByText("品种二（10年期）")).toBeVisible();
  await expect(page.getByText("无增信安排").first()).toBeVisible();
  await expect(page.getByText("募集说明书明确本期债券无增信安排，不适用于担保案例")).toBeVisible();
  await expect(page.getByText("保证人").locator("..").getByText("不适用")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
});
