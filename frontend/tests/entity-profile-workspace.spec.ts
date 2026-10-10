import { expect, test, type Page, type Route } from "@playwright/test";

const source = {
  id: "profile-source-e2e",
  filename: "测试发行人2025年度审计报告.pdf",
  kind: "审计报告",
  locator: "第8页资产负债表",
  page: 8,
  quote: "截至2025年末，资产总额320,000万元。",
};

const identitySource = {
  id: "profile-source-e2e-identity",
  filename: "测试发行人工商资料.pdf",
  kind: "工商资料",
  locator: "第2页工商登记",
  page: 2,
  quote: "法定代表人为张明，住所为北京市朝阳区小营北路6号。",
};

const baseProfile = {
  id: "profile-e2e-issuer",
  name: "测试发行人有限公司",
  unified_code: "91110000PROFILEE2E01",
  baseline_date: "2025-12-31",
  latest_financial_period: "2025年度",
  completeness: 0.86,
  status: "ready",
  version: 3,
  project_count: 2,
  counts: { confirmed: 8, single_source: 2, stale: 1, conflicted: 0, insufficient: 0 },
};

const facts = [{
  id: "profile-fact-e2e-representative",
  fact_key: "legal_representative",
  name: "法定代表人",
  category: "identity",
  value: null,
  text_value: "张明",
  unit: "",
  period: null,
  scope: "entity",
  status: "confirmed",
  valid_until: "2027-12-31",
  source_ids: [identitySource.id],
  source_id: identitySource.id,
  source_label: identitySource.filename,
  page: 2,
}, {
  id: "profile-fact-e2e-address",
  fact_key: "registered_address",
  name: "注册地址",
  category: "identity",
  value: null,
  text_value: "北京市朝阳区小营北路6号京投大厦",
  unit: "",
  period: null,
  scope: "entity",
  status: "confirmed",
  valid_until: "2027-12-31",
  source_ids: [identitySource.id],
  source_id: identitySource.id,
  source_label: identitySource.filename,
  page: 2,
}, {
  id: "profile-fact-e2e-assets",
  fact_key: "total_assets",
  name: "资产总额",
  category: "financial",
  value: 320000,
  text_value: "",
  unit: "万元",
  period: "2023",
  scope: "consolidated",
  status: "single_source",
  valid_until: "2026-12-31",
  source_ids: [source.id],
  source_id: source.id,
  source_label: source.filename,
  page: 8,
}, ...[2024, 2025].map((year) => ({
  id: `profile-fact-e2e-assets-${year}`,
  fact_key: "total_assets",
  name: "资产总额",
  category: "financial",
  value: year === 2024 ? 305000 : 320000,
  text_value: "",
  unit: "万元",
  period: String(year),
  scope: "consolidated",
  status: "confirmed",
  valid_until: "2026-12-31",
  source_ids: [source.id],
  source_id: source.id,
  source_label: source.filename,
  page: 8,
}))];

function detail(version = 3) {
  return {
    ...baseProfile,
    version,
    facts,
    shareholders: [],
    financial_periods: [],
    debt_instruments: [],
    credit_facilities: [],
    concentrations: [],
    related_transactions: [],
    legal_events: [],
    material_packages: [],
    sources: [identitySource, source],
    data: {},
    version_created_at: "2026-04-15T09:30:00+08:00",
  };
}

const readiness = {
  profile_id: baseProfile.id,
  ready_for_project: true,
  ready_for_agent: false,
  complete_due_diligence_ready: false,
  completeness: 0.86,
  status: "ready",
  module_scores: [{ key: "financial", label: "三个完整年度财务", score: 0.9, weight: 0.3, status: "ready" }],
  hard_vetoes: [{ key: "debt", label: "当前债务结构", reason: "债务到期结构已超过有效期" }],
  blocking_reasons: ["当前债务结构需要更新"],
  notices: ["2项事实仅有单一来源，尚未独立验证"],
  counts: baseProfile.counts,
  requirements: [{
    key: "debt_maturity",
    label: "债务与到期结构",
    category: "debt",
    status: "blocking",
    blocking_level: "blocking",
    required_fact_keys: ["interest_bearing_debt", "debt_due_12m"],
    missing_fact_keys: ["debt_due_12m"],
    stale_fact_keys: ["interest_bearing_debt"],
    conflicted_fact_keys: [],
    source_ids: [source.id],
    impact: "阻塞 Agent 对短期偿债压力的确定性计算",
  }],
};

function versions(currentVersion = 3) {
  return [
    { version: currentVersion, baseline_date: "2025-12-31", latest_financial_period: "2025年度", status: "ready", completeness: 0.86, created_at: "2026-04-15T09:30:00+08:00", created_by: "项目经理", change_summary: "更新审计数据", source_count: 1, fact_count: 1, current: true },
    { version: 2, baseline_date: "2024-12-31", latest_financial_period: "2024年度", status: "stale", completeness: 0.72, created_at: "2025-11-01T09:30:00+08:00", created_by: "项目经理", change_summary: "补充财务数据", source_count: 1, fact_count: 1, current: false },
  ];
}

async function openProfileWorkspace(page: Page) {
  await page.goto("/");
  if (((await page.viewportSize())?.width || 0) <= 600) await page.locator(".menu-button").click();
  await page.getByRole("button", { name: /主体档案库|发行人主体档案/ }).click();
  await expect(page.getByRole("heading", { name: "发行人主体档案" })).toBeVisible();
}

async function routeProfileContract(page: Page, options?: { committed?: () => boolean }) {
  await page.route("**/api/entity-profiles**", async (route: Route) => {
    const url = new URL(route.request().url());
    const currentVersion = options?.committed?.() ? 4 : 3;
    if (url.pathname.endsWith("/readiness")) return route.fulfill({ json: readiness });
    if (/\/versions\/\d+$/.test(url.pathname)) {
      const version = Number(url.pathname.split("/").at(-1));
      return route.fulfill({ json: {
        ...detail(version), content_hash: `version-${version}`, change_note: "版本测试",
        facts: version === 2 ? [{ ...facts[0], value: 305000, period: "2025-09-30" }] : facts,
        diff: { profile_id: baseProfile.id, version, compare_to: version - 1, added: [], removed: [], changed: [] },
      } });
    }
    if (url.pathname.endsWith("/versions")) return route.fulfill({ json: versions(currentVersion) });
    if (url.pathname === `/api/entity-profiles/${baseProfile.id}`) return route.fulfill({ json: detail(currentVersion) });
    if (url.pathname === "/api/entity-profiles") return route.fulfill({ json: [{ ...baseProfile, version: currentVersion }] });
    return route.continue();
  });
}

test("profile workspace exposes readiness, facts, evidence and immutable versions", async ({ page }, testInfo) => {
  await routeProfileContract(page);
  await openProfileWorkspace(page);
  await expect(page.getByText(baseProfile.unified_code).first()).toBeVisible();
  await expect(page.getByText("张明").first()).toBeVisible();
  await expect(page.getByText("北京市朝阳区").first()).toBeVisible();
  await expect(page.getByText("近三年主要财务报表")).toBeVisible();
  const financialLayout = testInfo.project.name === "mobile" ? page.locator(".ep-financial-mobile") : page.locator(".ep-financial-table");
  await expect(financialLayout.getByText("2023年", { exact: false }).first()).toBeVisible();
  await expect(financialLayout.getByText("2025年", { exact: false }).first()).toBeVisible();
  await expect(financialLayout.getByText(/人工确认|已人工确认|单一来源/)).toHaveCount(0);
  await expect(page.locator(".ep-workspace").getByText(/人工确认|已人工确认/)).toHaveCount(0);
  await expect(page.locator(".topbar .breadcrumb")).toHaveText("主体档案库");
  await expect(page.locator(".topbar .top-actions")).toHaveCount(0);
  await expect(page.getByText("Agent 硬门禁")).toBeVisible();
  await expect(page.getByText("债务到期结构已超过有效期")).toBeVisible();

  await page.getByRole("button", { name: "结构化事实" }).click();
  await expect(page.getByText("资产总额").first()).toBeVisible();
  await expect(page.getByText("单一来源，尚未独立验证")).toBeVisible();

  await page.getByRole("button", { name: "材料与证据", exact: true }).click();
  await expect(page.getByText(source.filename)).toBeVisible();
  await expect(page.getByText(source.quote)).toBeVisible();

  await page.getByRole("button", { name: "版本记录" }).click();
  await expect(page.getByText("v3 · 更新审计数据")).toBeVisible();
  await page.getByRole("button", { name: "比较" }).click();
  await expect(page.getByText("305,000 万元")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();

  if (testInfo.project.name === "mobile") {
    await expect(page.getByRole("button", { name: "导入主体资料" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "基于主体建项" })).toHaveCount(0);
  }
});

test("real profile API errors remain visible and never fall back to fixtures", async ({ page }) => {
  await page.route("**/api/entity-profiles**", async (route) => route.fulfill({ status: 503, json: { detail: "主体档案服务暂不可用" } }));
  await page.goto("/");
  if (((await page.viewportSize())?.width || 0) <= 600) await page.locator(".menu-button").click();
  await page.getByRole("button", { name: /主体档案库|发行人主体档案/ }).click();
  await expect(page.getByText("主体档案服务暂不可用")).toBeVisible();
  await expect(page.getByText(/模拟主体档案/)).toHaveCount(0);
});

test("profile import commits only selected source-backed candidates as a new version", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name === "mobile", "移动端主体档案保持只读");
  let committed = false;
  let commitPayload: { selected_candidate_ids: string[] } | null = null;
  await routeProfileContract(page, { committed: () => committed });
  const job = {
    id: "profile-import-e2e",
    target_profile_id: baseProfile.id,
    status: "parsing",
    package_kind: "financial",
    baseline_date: "2025-12-31",
    period: "2025",
    scope: "consolidated",
    progress: 40,
    files: [{ id: "file-1", filename: "主体数据.xlsx", size: 100, status: "parsed", page_count: null, parse_quality: 1, error: "" }, { id: "file-2", filename: "审计报告.pdf", size: 200, status: "parsing", page_count: 10, parse_quality: 0.96, error: "" }],
    candidates: [{ id: "candidate-assets", fact_key: "total_assets", name: "资产总额", category: "financial", value: 346000, text_value: "", unit: "万元", period: "2025", scope: "consolidated", proposed_status: "single_source", source_ids: ["source-import-e2e"], source_label: "审计报告.pdf", locator: "第8页", quote: "资产总额346,000万元。", confidence: 0.97, issue_codes: [] }],
    issues: [],
    error: "",
    created_at: "2026-09-01T10:00:00+08:00",
    committed_version: null,
  };
  await page.route("**/api/entity-profile-imports**", async (route) => {
    const request = route.request();
    if (request.url().endsWith("/commit")) {
      commitPayload = request.postDataJSON();
      committed = true;
      return route.fulfill({ json: detail(4) });
    }
    if (request.method() === "POST") return route.fulfill({ status: 201, json: job });
    return route.fulfill({ json: { ...job, status: "ready_to_commit", progress: 100, files: job.files.map((file) => ({ ...file, status: "parsed" })) } });
  });

  await openProfileWorkspace(page);
  await page.getByRole("button", { name: "导入主体资料" }).click();
  await page.getByLabel("结构化主数据").setInputFiles({ name: "主体数据.xlsx", mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", buffer: Buffer.from("fixture") });
  await page.getByLabel("主体证据文件").setInputFiles({ name: "审计报告.pdf", mimeType: "application/pdf", buffer: Buffer.from("fixture") });
  await page.getByRole("button", { name: "开始解析" }).click();
  await expect(page.getByText("候选事实审阅")).toBeVisible();
  await page.getByRole("button", { name: "提交为新版本" }).click();
  await expect.poll(() => commitPayload).not.toBeNull();
  expect(commitPayload?.selected_candidate_ids).toEqual(["candidate-assets"]);
  await expect(page.getByText(/主体档案 v4|v4 ·/).first()).toBeVisible();
});

test("project snapshot shows newer profile without changing the project until refresh", async ({ page }, testInfo) => {
  let refreshed = false;
  const projectResponse = await page.request.get("/api/projects/demo-bond-huachen");
  expect(projectResponse.ok()).toBeTruthy();
  const sourceProject = await projectResponse.json();
  const snapshot = {
    id: "snapshot-e2e-v1", version: 1, baseline_date: "2024-12-31", created_at: "2026-01-10T10:00:00+08:00",
    roles: [{ role: "issuer", profile_id: baseProfile.id, name: baseProfile.name, profile_version: refreshed ? 3 : 2, status: "ready", completeness: 0.8, baseline_date: refreshed ? "2025-12-31" : "2024-12-31", latest_financial_period: refreshed ? "2025年度" : "2024年度" }],
    counts: baseProfile.counts, facts: [], records: [], sources: [], profile_versions: { [baseProfile.id]: refreshed ? 3 : 2 }, content_hash: "1234567890abcdef",
  };
  await page.route("**/api/projects/demo-bond-huachen", async (route) => route.fulfill({ json: { ...sourceProject, data_class: "user_project" } }));
  await page.route("**/api/projects/demo-bond-huachen/profile-snapshot**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/status")) return route.fulfill({ json: { snapshot_id: refreshed ? "snapshot-e2e-v2" : snapshot.id, snapshot_version: refreshed ? 2 : 1, snapshot_profile_version: refreshed ? 3 : 2, current_profile_version: 3, has_newer_version: !refreshed, refresh_required: !refreshed, analysis_stale: refreshed, evidence_status: "available", evidence_count: 1, evidence_error: "" } });
    if (url.pathname.endsWith("/refresh")) { refreshed = true; return route.fulfill({ json: { ...snapshot, id: "snapshot-e2e-v2", version: 2, baseline_date: "2025-12-31" } }); }
    return route.fulfill({ json: { ...snapshot, id: refreshed ? "snapshot-e2e-v2" : snapshot.id, version: refreshed ? 2 : 1 } });
  });
  await page.goto("/");
  if (testInfo.project.name === "mobile") await page.locator(".menu-button").click();
  await page.getByRole("button", { name: /债券材料准备/ }).click();
  await expect(page.getByText("项目主体档案快照")).toBeVisible();
  await expect(page.getByText("主体库有新版本")).toBeVisible();
  await expect(page.getByText(/项目当前使用主体档案 v2/)).toBeVisible();
  const refreshButton = page.getByRole("button", { name: "刷新快照" });
  if (testInfo.project.name === "mobile") {
    await expect(refreshButton).toHaveCount(0);
    return;
  }
  await refreshButton.click();
  await expect(page.getByRole("heading", { name: "刷新项目主体快照" })).toBeVisible();
  await page.getByRole("button", { name: "确认刷新" }).click();
  await expect(page.getByText("分析已失效")).toBeVisible();
  await expect(page.getByText("快照已刷新，需要重新运行 Agent")).toBeVisible();
});
