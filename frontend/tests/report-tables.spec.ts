import { expect, test, type Page } from "@playwright/test";

/**
 * 报告页受控表格（Track B）
 *
 * 后端报告的 section.blocks 有两种形态，前端必须都能渲染：
 *
 * 1. 受控数据表块：`TableBlock.to_dict()` 的原样投影，逐格带 status / source_ids /
 *    adoption_basis，冲突值并列保留、缺失值标 not_applicable；
 * 2. 旧版 Markdown 分块（含 `| a | b |` 管道表）——后端未升级或历史报告走这条路。
 *
 * 受控表格块的后端由另一条轨道同时开发，因此本用例用 `page.route` 拦截报告接口
 * （`/api/projects/{id}/report`）喂构造好的 blocks，不依赖后端进度；
 * 最后再用**未拦截**的真实接口数据断言一次兼容性。
 */

const PROJECT = "demo-bond-huachen";

const PROFILE_NOTE = "注：本表为系统依据已核验事实生成的口径汇总，不是从原始报表逐格提取。";
const CONFLICT_NOTE = "注：同一事项的多个披露值并列保留，未自动择一；差异需取得原始材料后人工确认。";

/** 单元格：与 backend/app/table_block.py TableCell.to_dict() 同构。 */
function textCell(raw: string, sourceIds: string[] = []) {
  return {
    raw, normalized: null, unit: "", status: "text", origin_type: "derived", fact_key: null,
    metric_key: null, source_ids: sourceIds, locator: {}, adoption_basis: "", manual_note: "",
  };
}

function valueCell(raw: string, normalized: string, sourceIds: string[]) {
  return {
    raw, normalized, unit: "万元", status: "ok", origin_type: "profile_fact", fact_key: null,
    metric_key: null, source_ids: sourceIds, locator: { page: 12, bbox: [0.1, 0.2, 0.3, 0.4] },
    adoption_basis: "", manual_note: "",
  };
}

/** 2024 年未取得数据——必须显示 "—"，不能显示 0，也不能空白。 */
function missingCell() {
  return {
    raw: "—", normalized: null, unit: "万元", status: "not_applicable", origin_type: "derived",
    fact_key: null, metric_key: null, source_ids: [], locator: {}, adoption_basis: "", manual_note: "",
  };
}

/** 两来源取值不同：披露值并列保留，采用值为空——不得自动择一。 */
function conflictCell() {
  return {
    raw: "12,000.00 ／ 13,500.00", normalized: null, unit: "万元", status: "conflict",
    origin_type: "derived", fact_key: null, metric_key: null, source_ids: ["c-conflict01"],
    locator: {}, adoption_basis: "两来源取值不同，并列保留待人工确认", manual_note: "",
  };
}

const BALANCE_TABLE = {
  kind: "table",
  table_id: "report-financial-balance",
  table_key: "financial-balance",
  table_type: "annual_series",
  title: "三年主要资产负债项目表",
  subject: "华辰精密制造有限公司",
  scope: "合并口径",
  unit: "万元",
  currency: "CNY",
  display_mode: "report",
  origin_mode: "profile_facts",
  validation_status: "passed",
  columns: [
    { key: "subject", label: "科目", role: "subject", unit: "" },
    { key: "period_0", label: "2023年度", role: "period_value", unit: "万元" },
    { key: "period_1", label: "2024年度", role: "period_value", unit: "万元" },
    { key: "period_2", label: "2025年度", role: "period_value", unit: "万元" },
  ],
  rows: [
    {
      row_key: "货币资金", kind: "normal",
      cells: [
        textCell("货币资金"),
        valueCell("12,000.00", "12000.00", ["c-cash2023"]),
        missingCell(),
        valueCell("13,500.00", "13500.00", ["c-cash2025"]),
      ],
    },
    {
      row_key: "应收账款", kind: "normal",
      cells: [
        textCell("应收账款"),
        valueCell("8,600.00", "8600.00", ["c-ar2023"]),
        valueCell("9,150.00", "9150.00", ["c-ar2024"]),
        conflictCell(),
      ],
    },
  ],
  notes: [PROFILE_NOTE],
  source_refs: ["s-profile"],
};

const CONFLICT_TABLE = {
  kind: "table",
  table_id: "report-financial-conflicts",
  table_key: "financial-conflicts",
  table_type: "conflict_comparison",
  title: "冲突对照表",
  subject: "华辰精密制造有限公司",
  scope: "冲突对照",
  unit: "",
  currency: "CNY",
  display_mode: "report",
  origin_mode: "merged",
  validation_status: "blocked",
  columns: [
    { key: "item", label: "事项", role: "subject", unit: "" },
    { key: "period", label: "期间与口径", role: "period", unit: "" },
    { key: "reference", label: "资料及定位", role: "reference", unit: "" },
    { key: "disclosure", label: "披露值", role: "disclosure", unit: "" },
    { key: "status", label: "状态", role: "status", unit: "" },
  ],
  rows: [
    {
      row_key: "应收账款:2025年度", kind: "normal",
      cells: [
        textCell("应收账款"),
        textCell("2025年度／合并口径"),
        textCell("资料01 第12页", ["c-ref00001"]),
        conflictCell(),
        textCell("冲突"),
      ],
    },
  ],
  notes: [CONFLICT_NOTE],
  source_refs: ["s-upload"],
};

const FINANCIAL_SECTION = {
  id: "s-financial",
  section_key: "financial_analysis",
  title: "财务分析",
  content: "财务分析正文原文。",
  part_label: "第二部分 主体与财务分析",
  part_number: "二",
  chapter_number: "（一）",
  chapter_label: "财务分析",
  content_origin: "model",
  blocks: [
    { kind: "heading", level: 3, number: "1.", text: "资产负债结构" },
    BALANCE_TABLE,
    { kind: "paragraph", text: "**表后分析**：货币资金较上年增长，应收账款规模基本稳定。" },
    CONFLICT_TABLE,
    { kind: "paragraph", text: "上述冲突项需取得原始材料后人工确认。" },
  ],
  citation_ids: ["c-cash2023", "c-ar2025"],
  review_status: "draft",
  updated_at: "2026-09-24T00:00:00",
  support_rate: 0.82,
  validation_status: "passed",
  length_status: "passed",
  visible_content_chars: 120,
  length_policy: { effective_min_chars: 0, effective_max_chars: 0 },
};

const DETERMINISTIC_SECTION = {
  id: "s-disclosure",
  section_key: "source_disclosure",
  title: "来源与口径说明",
  content: "本报告数据来源说明。",
  content_origin: "deterministic",
  blocks: [{ kind: "paragraph", text: "本报告结论基于已核验事实与上传材料，不构成自动审批意见。" }],
  citation_ids: [],
  review_status: "draft",
  updated_at: "2026-09-24T00:00:00",
  support_rate: 1,
  validation_status: "passed",
  length_status: "passed",
  visible_content_chars: 40,
  length_policy: { effective_min_chars: 0, effective_max_chars: 0 },
};

/** 旧数据 / 后端未升级：section 没有 blocks 字段，只能退回正文原文。 */
const LEGACY_SECTION = {
  id: "s-legacy",
  section_key: "legacy_chapter",
  title: "历史章节",
  content: "历史报告正文：未分块，只有一段纯文本。",
  citation_ids: [],
  review_status: "draft",
  updated_at: "2026-09-24T00:00:00",
  support_rate: 0.5,
  validation_status: "warning",
  length_status: "not_checked",
};

/** 旧版 Markdown 分块：表格是 `| 科目 | 2025 |` 管道行，不是受控数据表。 */
const MARKUP_BLOCKS_SECTION = {
  ...LEGACY_SECTION,
  id: "s-markup",
  section_key: "markup_chapter",
  title: "分块章节",
  content: "分块章节正文。",
  content_origin: "deterministic",
  blocks: [
    { kind: "heading", level: 3, number: "1.", text: "旧版分块" },
    { kind: "table", rows: ["| 科目 | 2025年度 |", "| --- | --- |", "| 货币资金 | 13,500.00 |"] },
    { kind: "paragraph", text: "旧版分块正文。" },
  ],
};

const CITATION = {
  id: "c-cash2023",
  label: "资料01 · 第12页",
  quote: "货币资金 12,000.00 万元",
  locator_type: "pdf_page",
  locator: "资料01 第12页",
  page: 12,
  bbox: [0.1, 0.2, 0.3, 0.4],
  extraction_method: "native",
};

function jsonBody(body: unknown) {
  return { status: 200, contentType: "application/json", body: JSON.stringify(body) };
}

/** 拦截报告接口喂构造数据；证据接口一并拦截，这样点击单元格能真的打开证据抽屉。 */
async function mockReport(page: Page, sections: unknown[]) {
  await page.route("**/api/projects/*/report", (route) => route.fulfill(jsonBody(sections)));
  await page.route("**/api/citations/*", (route) => route.fulfill(jsonBody(CITATION)));
}

/**
 * 打开报告页。有章节时是报告页，未生成报告时是空状态——两种都不允许白屏，
 * 因此只等到「页面出来了」，具体形态由各用例自己断言。
 */
async function openReport(page: Page) {
  await page.goto("/");
  await expect(page.getByText("华辰精密制造有限公司").first()).toBeVisible();
  if (await page.locator(".menu-button").isVisible().catch(() => false)) {
    await page.locator(".menu-button").click();
  }
  await page.getByRole("button", { name: /尽调报告/ }).first().click();
  await expect(page.locator(".report-view, .empty-state").first()).toBeVisible();
  // 移动端章节默认折叠，先把折叠的章节展开，避免用例只在桌面成立。
  // 桌面端该按钮不显示（display: none），循环一次都进不去。
  for (let attempt = 0; attempt < 20; attempt += 1) {
    const toggle = page.locator(".report-section.mobile-collapsed .mobile-section-toggle").first();
    if (!(await toggle.isVisible().catch(() => false))) break;
    await toggle.click();
  }
}

/** 有章节时必须渲染报告页（而不是空状态）。 */
async function expectReportView(page: Page) {
  await expect(page.getByRole("heading", { name: "尽调报告" })).toBeVisible();
  await expect(page.locator(".report-view")).toBeVisible();
}

test("报告页把受控表格块渲染成 HTML 表格，并显示表名、单位、口径、来源与校验状态", async ({ page }, testInfo) => {
  await mockReport(page, [FINANCIAL_SECTION]);
  await openReport(page);
  await expectReportView(page);

  const figure = page.locator('.report-data-table[data-table-id="report-financial-balance"]');
  await expect(figure).toBeVisible();
  await expect(figure.locator(".report-block-chip.system")).toContainText("系统整理数据表");
  await expect(figure.locator(".report-data-table-title")).toHaveText("三年主要资产负债项目表");

  const meta = figure.locator(".report-data-table-meta");
  await expect(meta).toContainText("单位：万元");
  await expect(meta).toContainText("口径：合并口径");
  await expect(meta).toContainText("来源：已核验事实汇总");
  await expect(meta.locator(".report-data-table-status.passed")).toHaveText("校验通过");

  // 表注
  await expect(figure.locator(".report-data-table-notes")).toContainText(PROFILE_NOTE);

  // 真的是 HTML 表格：4 列表头 + 2 行数据，不是 Markdown 文本
  await expect(figure.locator("table thead th")).toHaveCount(4);
  await expect(figure.locator("table thead th").nth(1)).toHaveText("2023年度");
  await expect(figure.locator("table tbody tr")).toHaveCount(2);

  // 逐格值取规范化值，科目列原样展示
  const cashRow = figure.locator("table tbody tr").first();
  await expect(cashRow.locator("td").nth(0)).toHaveText("货币资金");
  await expect(cashRow.locator("td").nth(1)).toHaveText("12,000.00");

  // 表格块容器可横向滚动（窄屏不撑破报告纸）
  await expect(figure.locator(".report-table-scroll")).toHaveCSS("overflow-x", "auto");
  await page.screenshot({ path: testInfo.outputPath("report-data-table.png"), fullPage: true });
});

test("真实报告接口的嵌套结构化表格直接显示逐格内容", async ({ page }) => {
  const apiTable = {
    ...BALANCE_TABLE,
    rows: ["| 科目 | 2023年度 | 2024年度 | 2025年度 |", "| --- | ---: | ---: | ---: |"],
    table: BALANCE_TABLE,
  };
  await mockReport(page, [{ ...FINANCIAL_SECTION, blocks: [apiTable] }]);
  await openReport(page);

  const figure = page.locator('.report-data-table[data-table-id="report-financial-balance"]');
  await expect(figure.locator("table thead th")).toHaveCount(4);
  await expect(figure.locator("table tbody tr")).toHaveCount(2);
  await expect(figure.locator("table tbody tr").first()).toContainText("12,000.00");
  await expect(figure.locator("table tbody tr").nth(1)).toContainText("冲突");
});

test("表格过期时可手动更新并提交版本与正文哈希", async ({ page }) => {
  const section = {
    ...FINANCIAL_SECTION,
    section_key: "financial",
    report_revision: 7,
    content_sha256: "section-content-hash",
    system_tables: {
      snapshot_hash: "old-table-hash", current_hash: "current-table-hash",
      rule_version: "complete-standard-v1", updated_at: null, outdated: true,
    },
  };
  await mockReport(page, [section]);
  let submitted: Record<string, unknown> | null = null;
  await page.route("**/api/projects/*/report/tables/refresh", async (route) => {
    submitted = route.request().postDataJSON();
    await route.fulfill(jsonBody({
      status: "updated", chapters: [{ chapter_key: "financial", content_sha256: "new-hash", table_hash: "new-table-hash" }],
      report_revision: 8, report_state: "draft_current",
    }));
  });
  await openReport(page);
  await expect(page.getByText(/系统表格需要更新/)).toBeVisible();
  await page.getByRole("button", { name: "更新系统表格" }).click();
  await expect.poll(() => submitted).toEqual({
    chapters: ["financial"], expected_report_revision: 7,
    section_hashes: { financial: "section-content-hash" },
  });
  await expect(page.getByText("表后分析", { exact: false }).first()).toBeVisible();
});

test("报告目录靠左，正文铺满可用宽度", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name === "mobile", "桌面双栏布局");
  await mockReport(page, [FINANCIAL_SECTION]);
  await openReport(page);

  const view = await page.locator(".report-view").boundingBox();
  const toc = await page.locator(".report-toc").boundingBox();
  const paper = await page.locator(".report-paper").boundingBox();
  expect(view && toc && paper).toBeTruthy();
  const padding = await page.locator(".report-view").evaluate((element) => {
    const style = getComputedStyle(element);
    return { left: parseFloat(style.paddingLeft), right: parseFloat(style.paddingRight) };
  });
  expect(toc!.x - (view!.x + padding.left)).toBeLessThan(2);
  expect(view!.x + view!.width - padding.right - (paper!.x + paper!.width)).toBeLessThan(2);
  expect(paper!.width).toBeGreaterThan(850);
});

test("冲突单元格有可见标识，缺失值显示 —", async ({ page }) => {
  await mockReport(page, [FINANCIAL_SECTION]);
  await openReport(page);
  await expectReportView(page);

  const balance = page.locator('.report-data-table[data-table-id="report-financial-balance"]');
  // not_applicable：显示 "—"，且不是可点击的证据按钮（没有来源可点）
  const missing = balance.locator('td[data-cell-status="not_applicable"]');
  await expect(missing).toHaveCount(1);
  await expect(missing).toHaveText("—");
  await expect(missing.locator("button")).toHaveCount(0);

  // conflict：红底 + 行内「冲突」标记，披露值并列保留不被择一
  const conflict = balance.locator('td[data-cell-status="conflict"]');
  await expect(conflict).toHaveCount(1);
  await expect(conflict).toHaveClass(/status-conflict/);
  await expect(conflict).toContainText("12,000.00");
  await expect(conflict).toContainText("13,500.00");
  await expect(conflict.locator(".report-cell-flag")).toHaveText("冲突");
  await expect(conflict).toHaveCSS("background-color", "rgb(255, 241, 240)");

  // 另一种来源模式与校验状态：档案与上传材料核对 / 校验未通过
  const reconciliation = page.locator('.report-data-table[data-table-id="report-financial-conflicts"]');
  await expect(reconciliation.locator(".report-data-table-meta")).toContainText("来源：档案与上传材料核对");
  await expect(reconciliation.locator(".report-data-table-status.blocked")).toHaveText("校验未通过");
  await expect(reconciliation.locator("td.status-conflict").first()).toHaveCSS("background-color", "rgb(255, 241, 240)");
  await expect(reconciliation.locator(".report-data-table-notes")).toContainText(CONFLICT_NOTE);
});

test("单元格可点击查看证据，段落标注正文来源", async ({ page }) => {
  await mockReport(page, [FINANCIAL_SECTION, DETERMINISTIC_SECTION]);
  await openReport(page);
  await expectReportView(page);

  // 段落按 section.content_origin 标注：model → 模型分析正文
  const financial = page.locator("#section-financial_analysis");
  await expect(financial.locator(".report-block-chip.prose").first()).toHaveText("模型分析正文");
  await expect(financial.locator(".report-block-chip.prose")).toHaveCount(2);
  await expect(financial.locator(".report-block-paragraph").first()).toContainText("表后分析");

  // 确定性章节标注：确定性模板
  const deterministic = page.locator("#section-source_disclosure");
  await expect(deterministic.locator(".report-block-chip.prose")).toHaveCount(1);
  await expect(deterministic.locator(".report-block-chip.prose")).toHaveText("确定性模板");

  // 点击带 source_ids 的单元格 → 打开证据抽屉（cell.source_ids[0]）
  const cell = financial.locator('.report-data-table[data-table-id="report-financial-balance"] button.report-cell-value').first();
  await expect(cell).toHaveText("12,000.00");
  await cell.click();
  const drawer = page.locator(".evidence-drawer.open");
  await expect(drawer).toBeVisible();
  await expect(drawer.locator("blockquote")).toContainText("货币资金 12,000.00 万元");
  await expect(drawer.locator(".drawer-head strong")).toContainText("资料01 · 第12页");
  await drawer.getByTitle("关闭").click();
  await expect(drawer).toHaveCount(0);
});

test("没有 blocks 字段时退回正文，不白屏", async ({ page }) => {
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await mockReport(page, [LEGACY_SECTION, MARKUP_BLOCKS_SECTION]);

  await openReport(page);
  await expectReportView(page);

  // 老章节：没有 blocks → 显示原始正文，不是空白
  const legacy = page.locator("#section-legacy_chapter .section-content");
  await expect(legacy).toContainText("历史报告正文：未分块，只有一段纯文本。");
  await expect(legacy.locator(".report-data-table")).toHaveCount(0);

  // 旧版 Markdown 分块仍然渲染：标题、管道表、段落
  const markup = page.locator("#section-markup_chapter .section-content");
  await expect(markup.locator(".report-block-heading")).toHaveText("1.旧版分块");
  await expect(markup.locator(".report-block-table tbody tr")).toHaveCount(2);
  await expect(markup.locator(".report-block-table td").last()).toHaveText("13,500.00");
  await expect(markup.locator(".report-block-paragraph")).toContainText("旧版分块正文。");

  // 报告纸整体没有塌：封面与章节都在
  await expect(page.locator(".report-cover h2")).toBeVisible();
  await expect(page.locator(".report-section")).toHaveCount(2);
  expect(pageErrors).toEqual([]);
});

test("真实报告接口的数据同样能渲染（未被拦截）", async ({ page }, testInfo) => {
  // 真实数据链路：隔离数据目录里演示项目可能还没有报告，先跑一次演示分析。
  const response = await page.request.get(`/api/projects/${PROJECT}/report`);
  expect(response.ok()).toBeTruthy();
  let sections = await response.json() as Record<string, unknown>[];
  if (!sections.length) {
    const started = await page.request.post(`/api/projects/${PROJECT}/runs`, { data: { focus: "全面尽调" } });
    expect(started.ok()).toBeTruthy();
    const runId = (await started.json()).id as string;
    for (let attempt = 0; attempt < 900; attempt += 1) {
      const run = await (await page.request.get(`/api/runs/${runId}`)).json();
      if (["completed", "failed", "cancelled", "model_required"].includes(run.status)) {
        testInfo.annotations.push({ type: "demo-run", description: `${run.status}${run.error ? `: ${run.error}` : ""}` });
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    sections = await (await page.request.get(`/api/projects/${PROJECT}/report`)).json() as Record<string, unknown>[];
  }

  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await openReport(page);

  if (!sections.length) {
    // 尚未生成报告：不允许白屏，页面必须给出可解释的空状态。
    await expect(page.locator(".empty-state")).toBeVisible();
    expect(pageErrors).toEqual([]);
    return;
  }

  await expectReportView(page);

  await expect(page.locator(".report-cover h2")).toBeVisible();
  await expect(page.locator(".report-section")).toHaveCount(sections.length);

  const withBlocks = sections.find((section) => Array.isArray(section.blocks) && (section.blocks as unknown[]).length > 0);
  if (withBlocks) {
    const content = page.locator(`#section-${withBlocks.section_key} .section-content`);
    await expect(content).not.toBeEmpty();
    expect(await content.locator("p, table, .report-data-table, .report-block-heading, .report-block-list, .report-block-quote").count()).toBeGreaterThan(0);

    const blocks = withBlocks.blocks as Record<string, unknown>[];

    // 段落来源标注：真实 section 的 content_origin 决定标签文案
    const proseLabel = withBlocks.content_origin === "model" ? "模型分析正文"
      : withBlocks.content_origin === "deterministic" ? "确定性模板" : "";
    if (proseLabel && blocks.some((block) => block.kind === "paragraph")) {
      await expect(content.locator(".report-block-chip.prose").first()).toHaveText(proseLabel);
    }

    // 后端一旦产出受控表格块（table_id + columns），报告页必须渲染成数据表而不是纯文本。
    const dataTable = blocks.find(
      (block) => block.kind === "table" && typeof block.table_id === "string" && Array.isArray(block.columns),
    );
    if (dataTable) {
      const figure = page.locator(`.report-data-table[data-table-id="${dataTable.table_id}"]`);
      await expect(figure).toBeVisible();
      await expect(figure.locator("table tbody tr").first()).toBeVisible();
      await expect(figure.locator(".report-block-chip.system")).toContainText("系统整理数据表");
    }
    // 旧版正文管道表（后端当前形态）同样要渲染成表格，不能退化成一行竖线文本
    const markupTable = blocks.find((block) => block.kind === "table" && Array.isArray(block.rows) && typeof (block.rows as unknown[])[0] === "string");
    if (markupTable) {
      await expect(content.locator(".report-block-table").first()).toBeVisible();
    }
  }
  expect(pageErrors).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath("report-real-data.png"), fullPage: true });
});
