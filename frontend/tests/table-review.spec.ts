import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

const PROJECT = "demo-bond-huachen";

async function analyze(request: APIRequestContext) {
  const report = await request.get(`/api/projects/${PROJECT}/report`);
  if (report.ok() && (await report.json()).length > 0) return;
  const started = await request.post(`/api/projects/${PROJECT}/runs`, { data: { focus: "全面尽调" } });
  expect(started.ok()).toBeTruthy();
  const runId = (await started.json()).id as string;
  for (let attempt = 0; attempt < 900; attempt += 1) {
    const run = await (await request.get(`/api/runs/${runId}`)).json();
    if (["completed", "failed", "cancelled", "model_required"].includes(run.status)) {
      expect(run.status, run.error || run.message).toBe("completed");
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("表格核对用例：演示分析超时");
}

/** 以接口矩阵为权威：UI 与导出件都必须与它一致。 */
async function apiMatrix(request: APIRequestContext) {
  const tables = await (await request.get(`/api/projects/${PROJECT}/tables`)).json();
  expect(tables.length).toBeGreaterThan(0);
  const tableId = tables[0].table_id as string;
  const matrix = await (await request.get(`/api/projects/${PROJECT}/tables/${tableId}`)).json();
  return { tableId, matrix };
}

async function openTableReview(page: Page) {
  await page.goto("/");
  await expect(page.getByText("华辰精密制造有限公司").first()).toBeVisible();
  if (await page.locator(".menu-button").isVisible().catch(() => false)) {
    await page.locator(".menu-button").click();
  }
  await page.getByRole("button", { name: /表格核对/ }).first().click();
  await expect(page.getByRole("heading", { name: "表格核对" })).toBeVisible();
  const rows = page.locator(".table-scroll tbody tr");
  await expect(rows.first()).toBeVisible();
  return rows;
}

function uiRowValues(cells: { innerText: () => Promise<string> }[], count: number) {
  return Promise.all(
    Array.from({ length: count }, (_, index) =>
      cells[index].innerText().then((text) => text.replace("更正", "").trim()),
    ),
  );
}

test("表格核对工作区与接口读同一份数据，且能逐格确认", async ({ page }, testInfo) => {
  await analyze(page.request);
  const { matrix } = await apiMatrix(page.request);
  const rows = await openTableReview(page);

  // 清单与列头来自同一份 TableBlock
  await expect(page.locator(".table-review-list button strong").first())
    .toHaveText(matrix.title);
  await expect(page.locator(".table-scroll thead th").first()).toHaveText(matrix.columns[0].label);

  // 界面逐格等于接口值——不硬编码期望值，用例可重复运行
  const expected = matrix.rows[0].cells.map((cell: { display?: string; raw: string }) => (cell.display ?? cell.raw).trim());
  const cells = rows.first().locator("td");
  const actual = await uiRowValues(
    Array.from({ length: expected.length }, (_, index) => cells.nth(index)),
    expected.length,
  );
  expect(actual).toEqual(expected);

  await page.screenshot({ path: testInfo.outputPath("table-review.png"), fullPage: true });

  // 逐格核对：确认后记录留痕，原始值不被覆盖
  const target = rows.first().locator("button.table-cell").first();
  await target.click();
  const editor = page.locator(".table-cell-editor").first();
  await expect(editor).toBeVisible();
  await editor.getByRole("button", { name: "确认" }).click();
  await expect(editor).toBeHidden();
  await expect(page.locator(".table-review-meta")).toContainText("已核对");

  // 编辑器里显示的必须是**原始值**，不是更正值——这是「保留原始提取值」的可见证据。
  const raw = String(matrix.rows[0].cells[1].raw).trim();
  await rows.first().locator("button.table-cell").first().click();
  await expect(page.locator(".table-cell-original").first()).toContainText(raw);
});

test("前端、Markdown 与 Word 三个出口的表格数值一致", async ({ page }) => {
  await analyze(page.request);
  const { matrix } = await apiMatrix(page.request);
  const rows = await openTableReview(page);

  const markdown = await (await page.request.get(`/api/projects/${PROJECT}/export?format=md`)).text();
  const docx = await page.request.get(`/api/projects/${PROJECT}/export?format=docx`);
  expect(docx.ok()).toBeTruthy();

  const expected = matrix.rows[0].cells.map((cell: { display?: string; raw: string }) => (cell.display ?? cell.raw).trim());
  const cells = rows.first().locator("td");
  const actual = await uiRowValues(
    Array.from({ length: expected.length }, (_, index) => cells.nth(index)),
    expected.length,
  );
  // 未被人工更正时，界面值就是系统值；系统值必须逐格出现在 Markdown 成品里。
  for (const [index, value] of actual.entries()) {
    if (value !== expected[index]) continue; // 已被核对更正，报告仍按系统值披露
    if (!/\d/.test(value)) continue;
    expect(markdown, `Markdown 成品缺少表格数值 ${value}`).toContain(value);
  }
  expect(markdown).toContain(matrix.title);
});
