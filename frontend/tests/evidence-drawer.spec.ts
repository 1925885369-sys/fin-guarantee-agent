import { expect, test, type Locator, type Page } from "@playwright/test";

/**
 * 证据抽屉的原文重排。
 *
 * 整页抽取文本本身是一行一条记录（`货币资金 472,231.65 343,013.30 312,899.01`），
 * 只是浏览器把换行折叠掉才显得混乱。这里锁定重排后的四条契约：
 *
 * 1. 只搬运不改写：blockquote 的 textContent 与 citation.quote 逐字节一致；
 * 2. 表格页才对齐：整页判定不过时（正文页）一行都不进栅格；
 * 3. 列数精确相等：值个数不等于列数的行退回普通文本，绝不错列；
 * 4. 命中行定位：从复核卡片带过来的原值要能对上页面文本（无千分位 ↔ 有千分位）。
 *
 * 用 page.route 拦截 fact-groups 与 citations 喂构造数据，不依赖后端抽取结果。
 */

const PROJECT_ISSUER = "华辰精密制造有限公司";

const CITATION_BASE = {
  id: "c-page96",
  label: "中邮03募集说明书.pdf · 第96页",
  locator_type: "pdf_page",
  locator: "第 96 页",
  page: 96,
  bbox: [0, 0, 595.35, 842],
  extraction_method: "native",
};

/** 页眉、页码、正文残留和表头——这些都不该进栅格。 */
const PAGE_HEAD_LINES = [
  "中邮证券有限责任公司2026年面向专业投资者公开发行公司债券（第二期）募集说明书",
  "95",
  "况”之二、（二）报告期内发生的重大会计政策变更、会计估计变更及会计差错更正。",
  "二、发行人财务会计信息及主要财务指标",
  "表/图合并资产负债表",
  "单位：万元",
  "项目 2025 年末 2024 年末 2023 年末",
  "资产：",
];

const ASSET_LABELS = ["货币资金", "结算备付金", "融出资金", "交易性金融资产", "买入返售金融资产", "应收账款"];

/** 18 行资产科目：每行一个标签 + 三个年度数值。 */
const PAGE_ASSET_ROWS = Array.from({ length: 18 }, (_, index) => {
  const label = ASSET_LABELS[index % ASSET_LABELS.length] + (index < ASSET_LABELS.length ? "" : String(Math.floor(index / ASSET_LABELS.length) + 1));
  return `${label} ${472231 + index}.65 ${343013 + index}.30 ${312899 + index}.01`;
});

const PAGE_TOTAL_LINE = "资产总计 2,741,290.13 2,526,880.06 2,114,692.49";

const PAGE_LIABILITY_ROWS = Array.from({ length: 6 }, (_, index) => `负债科目${index + 1} ${286767 - index * 11}.32 ${208437 - index * 7}.96 ${127121 - index * 3}.24`);

/** 命中行刻意放在页面末尾，这样「滚入视野」才真的需要滚动。 */
const PAGE_TAIL_ROWS = [
  "其他权益工具投资 137,902.04 10,900.61 7,500.00",
  "长期股权投资 70,665.97 69,141.22 68,982.62",
  "截至 2025 年末，公司资产总计 2,741,290.13",
];

const PAGE_QUOTE = [...PAGE_HEAD_LINES, ...PAGE_ASSET_ROWS, PAGE_TOTAL_LINE, ...PAGE_LIABILITY_ROWS, ...PAGE_TAIL_ROWS].join("\n");
const PAGE_ROW_COUNT = PAGE_ASSET_ROWS.length + 1 + PAGE_LIABILITY_ROWS.length + 2;

/** 正文页：满篇数字，但一行都不该被排成表格。 */
const PROSE_QUOTE = [
  "二、发行人财务会计信息及主要财务指标",
  "最近三年，公司资产规模保持增长，货币资金余额分别为 472,231.65 万元、343,013.30 万元和 312,899.01 万元。",
  "公司资产总计 2,741,290.13 万元，较上年末增长 8.48%，负债总额 2,204,387.89 万元。",
  "上述数据经审计，具体科目详见本节财务报表。",
].join("\n");

function jsonBody(body: unknown) {
  return { status: 200, contentType: "application/json", body: JSON.stringify(body) };
}

/** 一个冲突事实组；rawValue 是复核卡片上「原值」那一栏，会作为定位提示带进抽屉。 */
function conflictGroup(index: number, rawValue: string) {
  return {
    id: `g-${index}`,
    fact_key: `fact_${index}`,
    subject: "发行人",
    period: "2025 年末",
    category: "财务数据",
    name: `冲突科目${index}`,
    status: "conflicted",
    selected_assertion_id: null,
    value: null,
    unit: "万元",
    text_value: "",
    assertion_ids: [`a-${index}`],
    assertions: [{
      id: `a-${index}`,
      extraction_method: "deterministic",
      value: null,
      unit: "万元",
      text_value: "",
      raw_value: rawValue,
      source_ids: [`c-page96-${index}`],
      confidence: 0.86,
      review_status: "pending",
      subject: "发行人",
      subject_id: "subject-1",
      scope: "consolidated",
      period_type: "point_in_time",
    }],
  };
}

async function mockEvidence(page: Page, quote: string, focusValues: string[]) {
  await page.route("**/api/projects/*/fact-groups", (route) => route.fulfill(jsonBody(focusValues.map((focusValue, index) => conflictGroup(index, focusValue)))));
  await page.route("**/api/citations/*", (route) => route.fulfill(jsonBody({ ...CITATION_BASE, quote })));
}

async function openReviewCenter(page: Page, testInfo: { project: { name: string } }) {
  await page.goto("/");
  await expect(page.getByText(PROJECT_ISSUER).first()).toBeVisible();
  if (testInfo.project.name === "mobile") await page.locator(".menu-button").click();
  await page.getByRole("button", { name: /复核中心/ }).click();
  await expect(page.getByRole("heading", { name: "复核中心" })).toBeVisible();
}

/** 点第 index 张冲突卡片上的「证据」，返回打开的抽屉。 */
async function openDrawerFrom(page: Page, index: number) {
  const card = page.locator(".conflict-item").nth(index);
  await expect(card).toBeVisible();
  await card.locator(".assertion-option").first().locator("i").click();
  const drawer = page.locator(".evidence-drawer.open");
  await expect(drawer).toBeVisible();
  return drawer;
}

async function closeDrawer(page: Page) {
  await page.locator(".evidence-drawer.open").getByTitle("关闭").click();
  await expect(page.locator(".evidence-drawer.open")).toHaveCount(0);
}

/** 抽屉可视区内是否完整看得到第一条命中行。 */
function hitInView(drawer: Locator) {
  return drawer.evaluate((box) => {
    const target = box.querySelector(".evidence-line.hit");
    if (!target) return false;
    const visible = box.getBoundingClientRect();
    const rect = target.getBoundingClientRect();
    return rect.top >= visible.top - 1 && rect.bottom <= visible.bottom + 1;
  });
}

test("证据抽屉按行重排原文，表格页数值列对齐", async ({ page }, testInfo) => {
  await mockEvidence(page, PAGE_QUOTE, ["137902.04", "2741290.13"]);
  await openReviewCenter(page, testInfo);
  await page.emulateMedia({ reducedMotion: "reduce" });
  const drawer = await openDrawerFrom(page, 0);

  // 1. 只搬运不改写：逐字节等于原文，重排没有增删、改写、重排任何字符。
  expect(await drawer.locator("blockquote").evaluate((element) => element.textContent)).toBe(PAGE_QUOTE);

  // 2. 表格行与年份表头都进了栅格；页码 "95"、`单位：万元`、正文残留行没有。
  await expect(drawer.locator(".evidence-line.row")).toHaveCount(PAGE_ROW_COUNT);
  await expect(drawer.locator(".evidence-line.header")).toHaveCount(1);
  await expect(drawer.locator(".evidence-line").nth(1)).toHaveClass("evidence-line plain");   // "95"

  if (testInfo.project.name === "mobile") {
    // 手机放不下 1 + 3 列，退回逐行文本；类名保留，判据是计算样式。
    await expect(drawer.locator(".evidence-line.row").first()).toHaveCSS("display", "block");
  } else {
    await expect(drawer).toHaveCSS("width", "720px");
    await expect(drawer.locator(".evidence-line.row").first()).toHaveCSS("display", "grid");

    // 3. 列真的对齐：每行是各自独立的栅格容器，轨道宽度必须按整页算成固定像素值，
    //    否则各行独立解析、数值列会错开。年份表头也要落在同一列上。
    const valueXs = await drawer.locator(".evidence-line.row, .evidence-line.header").evaluateAll(
      (lines) => lines.map((line) => line.querySelector(".evidence-value")?.getBoundingClientRect().x ?? -1),
    );
    expect(valueXs.length).toBe(PAGE_ROW_COUNT + 1);
    expect(valueXs.every((x) => x >= 0)).toBe(true);
    for (const x of valueXs) expect(x).toBeCloseTo(valueXs[0], 0);
  }

  // 4. 命中行定位 + 滚入视野（原值无千分位，页面文本有）。
  await expect(drawer.locator(".evidence-line.hit")).toHaveCount(1);
  await expect(drawer.locator(".evidence-line.hit").first()).toContainText("137,902.04");
  await expect.poll(() => drawer.evaluate((box) => box.scrollTop)).toBeGreaterThan(0);
  await expect.poll(() => hitInView(drawer)).toBe(true);

  // 5. 同一个值出现在多行时全部高亮（资产总计 与 正文各一处）。
  await closeDrawer(page);
  const multi = await openDrawerFrom(page, 1);
  await expect(multi.locator(".evidence-line.hit")).toHaveCount(2);
  await expect(multi.locator(".evidence-page")).toHaveAttribute("data-hit-count", "2");
  await expect.poll(() => hitInView(multi)).toBe(true);
});

test("定位不到或目标过短时不高亮、不误报", async ({ page }, testInfo) => {
  await mockEvidence(page, PAGE_QUOTE, ["99999999.99", "95"]);
  await openReviewCenter(page, testInfo);
  await page.emulateMedia({ reducedMotion: "reduce" });

  // 值不在本页：不高亮，明确告诉复核人抽取结果与事实对不上。
  const missing = await openDrawerFrom(page, 0);
  await expect(missing.locator(".evidence-line.hit")).toHaveCount(0);
  await expect(missing.locator(".evidence-focus-miss")).toContainText("未在本页文本中定位到「99999999.99」");
  await expect.poll(() => missing.evaluate((box) => box.scrollTop)).toBe(0);
  expect(await missing.locator("blockquote").evaluate((element) => element.textContent)).toBe(PAGE_QUOTE);

  // 目标不足 4 位数字不定位：否则页码 "95" 会把复核人带到页眉。
  await closeDrawer(page);
  const short = await openDrawerFrom(page, 1);
  await expect(short.locator(".evidence-line.hit")).toHaveCount(0);
  await expect(short.locator(".evidence-focus-miss")).toContainText("未在本页文本中定位到「95」");
});

test("正文页不排成假表格", async ({ page }, testInfo) => {
  await mockEvidence(page, PROSE_QUOTE, ["2741290.13"]);
  await openReviewCenter(page, testInfo);
  const drawer = await openDrawerFrom(page, 0);

  await expect(drawer.locator(".evidence-line")).toHaveCount(PROSE_QUOTE.split("\n").length);
  await expect(drawer.locator(".evidence-line.row")).toHaveCount(0);
  await expect(drawer.locator(".evidence-line.header")).toHaveCount(0);
  expect(await drawer.locator("blockquote").evaluate((element) => element.textContent)).toBe(PROSE_QUOTE);
});
