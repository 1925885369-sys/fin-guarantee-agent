/**
 * 分析页 v2（Agent 分析 · 契约 `docs/design/analysis-page-v2.md`）验收测试。
 *
 * 设计取舍：本文件用 page.route 通配拦截全部 `/api` 请求，完整接管后端，
 * 因此每个用例都是**确定性的**，不依赖 `.data-e2e` 播种出的真实数据，
 * 也不受 `workbench.spec.ts` 那个坏掉的入口用例影响。
 *
 * 进入分析页的方式：`addInitScript` 预置 `yandan-current` / `yandan-active-view`，
 * 让 App 以「项目详情」启动（`yandan-active-view` 只接受 project-detail/due-diligence/solution，
 * 直接写 analysis 会落到项目列表），再从侧栏工作流点「债券 Agent 分析」。
 *
 * 只读验证：本文件不修改 `src/`。
 */
import { expect, test, type Page, type Route } from "@playwright/test";
import { writeFileSync } from "node:fs";

const PROJECT_ID = "demo-bond-huachen";
const COMPANY_NAME = "华辰精密制造有限公司";

// 只在 desktop 项目跑全部用例；移动端视口由 D 组在用例内用 setViewportSize 显式覆盖，
// 避免同一批断言在 mobile 项目里重复执行一遍（翻倍耗时）。
test.beforeEach(({}, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "分析页断言由 desktop 项目统一执行，移动端视口在 D 组显式覆盖");
});

// ---------------------------------------------------------------------------
// 基础 fixture
// ---------------------------------------------------------------------------

const health = {
  status: "ok", modelConfigured: true, model: "deepseek-v4-flash", version: "test",
  policy: "bond-guarantee", policyVersion: "v1",
  uploadLimits: { maxFileMb: 50, maxFiles: 10, formats: ["pdf"] },
  retrieval: { mode: "hybrid", model: "bge", modelRevision: "r1", modelReady: true, indexVersion: "i1", errorType: "" },
};

const projectSummary = {
  id: PROJECT_ID, company_name: COMPANY_NAME, amount: 20000, term_months: 60,
  purpose: "补充流动资金", status: "active", project_type: "bond_guarantee",
  bond_name: `${COMPANY_NAME}2026年面向专业投资者公开发行公司债券`,
  data_class: "fictional_demo", evaluation_eligible: 1, issuer_profile_id: null,
  created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z",
};

const projectDetail = {
  ...projectSummary,
  stats: { documents: 2, facts: 10, checks: 3, risks: 2, highRisks: 0 },
  analysis_target: "full_due_diligence", effective_analysis_mode: "full_due_diligence",
  report_state: "draft_current",
};

const inventory = {
  documents: [{
    id: "doc-1", filename: "募集说明书（申报稿）.pdf", kind: "募集说明书", media_type: "application/pdf",
    size: 1000, parse_status: "parsed", quality: 1, locator_count: 12, warning: "", parser: "native",
    document_role: "prospectus", ocr_status: "not_needed", page_count: 30,
  }],
  gaps: [], readiness: "ready", core_materials_ready: true, complete_due_diligence_ready: true,
  analysis_mode: "full_due_diligence",
};

const bondSummary = {
  projectId: PROJECT_ID, bondName: projectSummary.bond_name, issuer: COMPANY_NAME,
  issueAmount: 20000, currentIssueAmount: 20000, bondNameSource: "project", issueAmountSource: "project",
  registeredAmount: null, termYears: 5, couponRate: null, repayment: null,
  issuerRating: "AA-", bondRating: "AA+", outlook: null, guarantor: "东辰担保",
  guaranteeType: null, guaranteeApplicable: true, guaranteeScope: null, guaranteePeriod: null,
  guaranteeCap: null, reportLevel: "完整尽调", dataClass: "fictional_demo", evaluationEligible: true,
  conflictedFields: [], coreMaterialsReady: true, completeDueDiligenceReady: true,
  analysisMode: "full_due_diligence", materialGaps: [], tranches: [],
};

const policy = {
  active: { profile: "bond-guarantee", version: "v1", name: "担保风险政策", description: "", sha256: "x", rules: [] },
  lastRun: { profile: "bond-guarantee", version: "v1", sha256: "x", created_at: "2026-01-01T00:00:00Z" },
};

const reviewSummary = {
  total_unique_issues: 0, conflict_count: 0, candidate_count: 0, action_package_count: 0,
  information_notice_count: 0, review_revision: 1, report_revision: 1, report_reviewed_revision: 0,
  report_reviewed_at: null, report_stale: false, report_stale_reasons: [], report_state: "draft_current",
};

const NODE_KEYS = [
  "inventory", "extract", "conflict", "metrics", "cross_check",
  "candidate", "tasks", "risk", "draft", "verify", "review",
] as const;
const NODE_LABELS = ["材料盘点", "事实抽取", "冲突识别", "指标计算", "交叉核验", "定性风险", "复核任务", "正式风险", "报告组装", "引用校验", "人工复核"];

type AnyNode = {
  id: string; node_key: string; status: string; progress: number; attempt: number;
  output_summary: string; error: string; started_at: string | null; finished_at: string | null;
};
type AnyRun = {
  id: string; project_id: string; status: string; current_node: string; progress: number;
  message: string; latency_ms: number; input_tokens: number; output_tokens: number; cost: number;
  error: string; started_at?: string; finished_at?: string | null;
};

function makeNodes(map: Record<string, string>, summaries: Record<string, string> = {}): AnyNode[] {
  return NODE_KEYS.map((key) => {
    const status = map[key] ?? "pending";
    return {
      id: `n-${key}`, node_key: key, status, progress: status === "completed" ? 100 : 0, attempt: 1,
      output_summary: summaries[key] ?? (status === "completed" ? `${key} 已产出摘要` : ""),
      error: status === "warning" ? "该节点产出但需人工关注" : status === "failed" ? "节点执行失败" : "",
      started_at: status === "pending" ? null : "2026-01-01T10:00:00Z",
      finished_at: status === "pending" ? null : "2026-01-01T10:00:05Z",
    };
  });
}

const allCompleted = Object.fromEntries(NODE_KEYS.map((k) => [k, "completed"]));

function makeRun(over: Partial<AnyRun> & { id: string; status: string }): AnyRun {
  return {
    id: over.id, project_id: PROJECT_ID, status: over.status,
    current_node: over.current_node ?? "review", progress: over.progress ?? 100,
    message: over.message ?? "", latency_ms: 12400, input_tokens: 1000, output_tokens: 234, cost: 0.21,
    error: over.error ?? "", started_at: "2026-01-01T10:00:00Z", finished_at: "2026-01-01T10:00:12Z",
    ...over,
  };
}

// --- 勾稽 / 核验结果 --------------------------------------------------------

function tieOut(i: number, status: string) {
  const bad = status === "warning";
  return {
    id: `tie-${i}`, subject_id: "issuer", scope: i % 2 ? "consolidated" : "standalone", period: "2024",
    check_key: `tie_key_${i}`, title: `报表勾稽项目 ${i}`, status, formula: "资产 = 负债 + 所有者权益",
    input_fact_ids: [], source_ids: ["src-1"], left_value: 100, right_value: bad ? 97 : 100,
    difference: bad ? 3 : 0, tolerance: 1, rationale: "左值与右值应在容差内相等", policy_version: "v1",
  };
}

function crossCheck(i: number, status: string) {
  return {
    id: `check-${i}`, rule_key: `rule_key_${i}`, title: `核验规则 ${i}`, severity: "medium", status,
    summary: "规则结论摘要", formula: "覆盖率 = 经营现金流 / 到期债务", values_json: [{ label: "分子", value: 1.2 }, { label: "分母", value: 1 }],
    source_ids: ["src-1"], fact_ids: [], policy_version: "v1", thresholds: { min: 1 }, result_value: 1.2,
    rationale: "", subject_id: "issuer", scope: "consolidated", trace_json: {},
  };
}

/** 12 条勾稽：9 异常 + 2 材料不足 + 1 通过（默认筛「异常」时 9 条，切「全部」12 条触发分页）。 */
const TIE_WARN = [
  ...Array.from({ length: 9 }, (_, i) => tieOut(i + 1, "warning")),
  ...Array.from({ length: 2 }, (_, i) => tieOut(i + 10, "insufficient")),
  tieOut(12, "passed"),
];
/** 12 条核验：全部通过（→ 分区默认折叠，用于折叠/无异常用例）。 */
const CHECK_PASS_12 = Array.from({ length: 12 }, (_, i) => crossCheck(i + 1, "passed"));

const TIE_PASS = [tieOut(1, "passed"), tieOut(2, "passed")];
const CHECK_PASS_2 = [crossCheck(1, "passed"), crossCheck(2, "passed")];
const TIE_INSUFFICIENT_ONLY = [tieOut(1, "insufficient"), tieOut(2, "insufficient")];

// --- 指标 fixture（B 组边界） ----------------------------------------------

let metricSeq = 0;
function metric(over: {
  metric_key: string; name: string; period: string; value: number | null; unit: string; scope: string; subject_id?: string;
}) {
  metricSeq += 1;
  return {
    id: `m-${metricSeq}`, metric_key: over.metric_key, name: over.name, period: over.period, value: over.value,
    unit: over.unit, formula: `${over.metric_key} = f(inputs)`, input_fact_ids: [], source_ids: ["src-1"],
    status: "computed", policy_version: "v1", subject_id: over.subject_id ?? "issuer", scope: over.scope, trace_json: {},
  };
}

/** 15 个 (subject_id, scope, metric_key) 分组 → 最新态 15 行（> 12，证明取消 12 项截断）。
 *  debt_ratio 同组有 2023=1 / 2025=2 → 最新必须取 2025=2（缺陷 1 回归）。
 *  current_ratio 同组有 空串=99 / 2024=88 → 空串不得当选（缺陷 1 回归）。 */
const METRICS = [
  metric({ metric_key: "debt_ratio", name: "资产负债率", period: "2023", value: 1, unit: "%", scope: "consolidated" }),
  metric({ metric_key: "debt_ratio", name: "资产负债率", period: "2025", value: 2, unit: "%", scope: "consolidated" }),
  metric({ metric_key: "current_ratio", name: "流动比率", period: "", value: 99, unit: "%", scope: "consolidated" }),
  metric({ metric_key: "current_ratio", name: "流动比率", period: "2024", value: 88, unit: "%", scope: "consolidated" }),
  metric({ metric_key: "net_margin", name: "销售净利率", period: "2024", value: 5, unit: "%", scope: "unknown" }),
  metric({ metric_key: "total_assets", name: "资产总额", period: "2024", value: null, unit: "万元", scope: "consolidated" }),
  metric({ metric_key: "equity", name: "所有者权益", period: "2024", value: 100, unit: "万元", scope: "standalone" }),
  metric({ metric_key: "revenue", name: "营业收入", period: "2024", value: 200, unit: "万元", scope: "entity" }),
  metric({ metric_key: "consideration", name: "本次交易对价", period: "2024", value: 300, unit: "万元", scope: "transaction" }),
  metric({ metric_key: "roe", name: "净资产收益率", period: "2024", value: 10, unit: "%", scope: "consolidated" }),
  metric({ metric_key: "gross_margin", name: "毛利率", period: "2024", value: 20, unit: "%", scope: "consolidated" }),
  metric({ metric_key: "ebitda", name: "EBITDA", period: "2024", value: 400, unit: "万元", scope: "consolidated" }),
  metric({ metric_key: "interest_coverage", name: "利息保障倍数", period: "2024", value: 3, unit: "%", scope: "consolidated" }),
  metric({ metric_key: "quick_ratio", name: "速动比率", period: "2024", value: 1, unit: "%", scope: "consolidated" }),
  metric({ metric_key: "inventory_turnover", name: "存货周转率", period: "2024", value: 5, unit: "%", scope: "consolidated" }),
  metric({ metric_key: "receivable_turnover", name: "应收账款周转率", period: "2024", value: 6, unit: "%", scope: "consolidated" }),
  metric({ metric_key: "ocf", name: "经营现金流", period: "2024", value: 500, unit: "万元", scope: "consolidated" }),
];

function traceFixture(metricId: string) {
  const m = METRICS.find((item) => item.id === metricId) ?? METRICS[0];
  return {
    metric: m,
    inputs: [{
      canonical_fact: {
        id: "fact-1", fact_key: "total_assets", subject: "华辰精密制造有限公司", period: "2024", category: "financial",
        name: "资产总额", status: "confirmed", selected_assertion_id: "assert-1", value: 100, unit: "万元",
        text_value: "", assertion_ids: ["assert-1"], assertions: [], resolution_note: "", resolved_by: "",
        subject_id: "issuer", scope: "consolidated", period_type: "annual",
      },
      assertion: {
        id: "assert-1", fact_key: "total_assets", subject: "华辰精密制造有限公司", period: "2024", category: "financial",
        name: "资产总额", value: 100, unit: "万元", text_value: "", raw_value: "100", source_ids: ["src-1"],
        confidence: 0.98, extraction_method: "原生表格", subject_id: "issuer", scope: "consolidated", period_type: "annual",
      },
      sources: [{
        id: "src-1", document_id: "doc-1", locator_type: "pdf_page", locator: "第 12 页", text: "资产总额 100 万元",
        page: 12, bbox_json: [0.1, 0.2, 0.3, 0.4], extraction_method: "native",
      }],
    }],
  };
}

// ---------------------------------------------------------------------------
// 场景
// ---------------------------------------------------------------------------

type Scenario = {
  run: AnyRun | null;
  nodes: AnyNode[];
  tieOuts: ReturnType<typeof tieOut>[];
  checks: ReturnType<typeof crossCheck>[];
};

const SCENARIOS: Record<string, Scenario> = {
  notStarted: {
    run: null, nodes: [],
    tieOuts: TIE_PASS, checks: CHECK_PASS_2,
  },
  running: {
    run: makeRun({ id: "run-running", status: "running", current_node: "cross_check", progress: 45, message: "正在比对左右值", finished_at: null }),
    nodes: makeNodes({ inventory: "completed", extract: "completed", conflict: "completed", metrics: "completed", cross_check: "running" }),
    tieOuts: TIE_PASS, checks: CHECK_PASS_2,
  },
  completed: {
    run: makeRun({ id: "run-completed", status: "completed" }),
    nodes: makeNodes(allCompleted),
    tieOuts: TIE_PASS, checks: CHECK_PASS_2,
  },
  completedInsufficientOnly: {
    run: makeRun({ id: "run-warn-insufficient", status: "completed" }),
    nodes: makeNodes(allCompleted),
    tieOuts: TIE_INSUFFICIENT_ONLY, checks: CHECK_PASS_2,
  },
  warning: {
    run: makeRun({ id: "run-warning", status: "completed" }),
    nodes: makeNodes({ ...allCompleted, cross_check: "warning" }),
    tieOuts: TIE_WARN, checks: CHECK_PASS_12,
  },
  degraded: {
    run: makeRun({ id: "run-degraded", status: "completed" }),
    nodes: makeNodes({ ...allCompleted, conflict: "degraded" }),
    tieOuts: TIE_PASS, checks: CHECK_PASS_2,
  },
  failed: {
    run: makeRun({ id: "run-1", status: "failed", current_node: "metrics", progress: 30, error: "指标计算节点缺少输入事实", message: "运行失败", finished_at: null }),
    nodes: makeNodes({ inventory: "completed", extract: "completed", conflict: "completed", metrics: "failed" }),
    tieOuts: TIE_WARN, checks: CHECK_PASS_12,
  },
  cancelled: {
    run: makeRun({ id: "run-cancelled", status: "cancelled", current_node: "extract", progress: 10, message: "用户取消", finished_at: null }),
    nodes: makeNodes({ inventory: "completed", extract: "cancelled" }),
    tieOuts: TIE_PASS, checks: CHECK_PASS_2,
  },
  modelRequired: {
    run: makeRun({ id: "run-model", status: "model_required", current_node: "", progress: 0, message: "需要配置模型服务", finished_at: null }),
    nodes: makeNodes({}),
    tieOuts: TIE_PASS, checks: CHECK_PASS_2,
  },
  // D 组首屏预算用：少量异常（自动展开但行数少），保证两个分区标题都在首屏内。
  compactWarning: {
    run: makeRun({ id: "run-compact", status: "completed" }),
    nodes: makeNodes(allCompleted),
    tieOuts: [tieOut(1, "warning"), tieOut(2, "warning"), tieOut(3, "insufficient"), tieOut(4, "passed")],
    checks: [crossCheck(1, "passed"), crossCheck(2, "passed"), crossCheck(3, "passed")],
  },
};

// ---------------------------------------------------------------------------
// API mock（catch-all，避免任何真实后端调用）
// ---------------------------------------------------------------------------

type MockState = {
  run: AnyRun | null;
  nodes: AnyNode[];
  tieOuts: ReturnType<typeof tieOut>[];
  checks: ReturnType<typeof crossCheck>[];
  /** 数据刷新后（retry 流终态触发 refresh）要换成的新数据；null 表示不变。 */
  tieOutsAfter: ReturnType<typeof tieOut>[] | null;
  checksAfter: ReturnType<typeof crossCheck>[] | null;
  /** retry 返回的 run id（同 id = 刷新不重置；新 id = 视为切换运行 → 重置）。 */
  retryRunId: string;
  /** retry 触发的事件流。 */
  retryEvents: AnyRun[];
  /** latestRun 覆盖（refresh 后 run 状态）。 */
  latestOverride: { run: AnyRun | null; nodes: AnyNode[] } | null;
  /** 计算链路接口是否返回 500（失败 → 可重试）。 */
  traceFail: boolean;
};

function initialState(scenario: Scenario): MockState {
  return {
    run: scenario.run, nodes: scenario.nodes, tieOuts: scenario.tieOuts, checks: scenario.checks,
    tieOutsAfter: null, checksAfter: null, retryRunId: scenario.run?.id ?? "", retryEvents: [],
    latestOverride: null, traceFail: false,
  };
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

async function installMocks(page: Page, state: MockState) {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace(/^\/api\//, "");
    const seg = path.split("/");
    const method = request.method();

    if (path === "health") return json(route, health);
    if (path === "entity-profiles") return json(route, []);

    if (seg[0] === "projects" && method === "GET" && seg.length === 1) {
      const dataClass = url.searchParams.get("data_class");
      return json(route, dataClass === "fictional_demo" ? [projectSummary] : []);
    }

    if (seg[0] === "runs") {
      if (seg[2] === "events") {
        return route.fulfill({
          status: 200,
          headers: { "content-type": "text/event-stream", "cache-control": "no-cache" },
          body: state.retryEvents.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(""),
        });
      }
      if (seg[2] === "nodes") return json(route, state.latestOverride?.nodes ?? state.nodes);
      if (seg[1] && seg.length === 2 && method === "GET") return json(route, state.latestOverride?.run ?? state.run);
      if (seg[2] === "retry" && method === "POST") return json(route, { id: state.retryRunId });
      if (seg[2] === "cancel") return json(route, { status: "cancelled" });
    }

    if (seg[0] === "citations") {
      return json(route, {
        id: seg[1], label: "募集说明书（申报稿）.pdf · 第12页",
        quote: "资产总额 100 万元\n负债总额 60 万元", locator_type: "pdf_page", locator: "第 12 页",
        page: 12, bbox: [0.1, 0.2, 0.3, 0.4], extraction_method: "native",
      });
    }

    if (seg[0] === "projects") {
      const sub = seg[2];
      if (seg.length === 2 && method === "GET") return json(route, projectDetail);
      if (sub === "document-inventory") return json(route, inventory);
      if (sub === "bond-summary") return json(route, bondSummary);
      if (sub === "facts") return json(route, []);
      if (sub === "fact-groups") return json(route, []);
      if (sub === "metrics") {
        if (seg[4] === "trace") {
          if (state.traceFail) return json(route, { detail: "计算链路读取失败" }, 500);
          return json(route, traceFixture(seg[3]));
        }
        return json(route, METRICS);
      }
      if (sub === "policy") return json(route, policy);
      if (sub === "cross-checks") return json(route, state.checksAfter ?? state.checks);
      if (sub === "financial-tie-outs") return json(route, state.tieOutsAfter ?? state.tieOuts);
      if (sub === "risks") return json(route, []);
      if (sub === "review-tasks") return json(route, []);
      if (sub === "candidate-findings") return json(route, []);
      if (sub === "audit-events") return json(route, []);
      if (sub === "report") return json(route, []);
      if (sub === "entities") return json(route, []);
      if (sub === "report-quality") return json(route, null);
      if (sub === "profile-snapshot") return json(route, null);
      if (sub === "review-summary") return json(route, reviewSummary);
      if (sub === "runs" && seg[3] === "latest") return json(route, state.latestOverride ?? { run: state.run, nodes: state.nodes });
    }

    return json(route, {});
  });
}

async function openAnalysis(page: Page, scenario: Scenario): Promise<MockState> {
  const state = initialState(scenario);
  await installMocks(page, state);
  await page.addInitScript((payload: string) => {
    localStorage.setItem("yandan-current", payload);
    localStorage.setItem("yandan-active-view", "project-detail");
  }, JSON.stringify({ id: PROJECT_ID }));
  await page.goto("/");
  // 等 refresh() 完成（当前项目名落到侧栏）再进分析页，避免点击时 Checks 尚未拿到数据。
  await expect(page.locator(".project-switch strong")).toHaveText(COMPANY_NAME, { timeout: 30_000 });
  await page.getByRole("button", { name: /债券 Agent 分析/ }).click();
  await expect(page.getByRole("heading", { name: "财务与交叉核验" })).toBeVisible();
  return state;
}

/** 摘要卡片（按标题定位）。 */
function card(page: Page, title: string) {
  return page.locator(".analysis-card").filter({ has: page.getByRole("heading", { name: title, exact: true }) });
}

/** 四阶段工作流里的某个阶段。 */
function stage(page: Page, label: string) {
  return page.locator(".analysis-stage").filter({ has: page.locator(".analysis-stage-name", { hasText: label }) });
}

/** 弹窗筛选器按钮（限定在 .analysis-modal-filter 内，避免与结果行无障碍名混淆）。 */
function modalFilter(page: Page, label: "异常" | "材料不足" | "通过" | "全部") {
  return page.locator(".analysis-modal-filter").getByRole("button", { name: new RegExp(`^${label} `) });
}

/** 打开某张卡片的详情弹窗。 */
async function openDetail(page: Page, which: "tie-outs" | "checks") {
  await page.locator(`#analysis-${which}-detail`).click();
  await expect(page.locator(".analysis-modal")).toBeVisible();
}

/** 阶段图标底色（按阶段状态类名取）。 */
async function stageIconBg(page: Page, cls: string): Promise<string> {
  return page.locator(`.analysis-stage.${cls} .analysis-stage-icon`).first().evaluate((el) => getComputedStyle(el).backgroundColor);
}

// 设计规格 §3.1 的状态色（:root 变量与既有字面值的计算结果）
const COLOR = {
  low: "rgb(22, 130, 71)",          // --low #168247
  primary: "rgb(47, 84, 212)",      // --primary #2f54d4
  mediumBg: "rgb(255, 247, 230)",   // --medium-bg #fff7e6
  high: "rgb(229, 57, 53)",         // --high #e53935
  panel: "rgb(255, 255, 255)",      // --panel #ffffff
  neutral: "rgb(247, 249, 252)",    // #f7f9fc
};

// ===========================================================================
// A 组 · 运行状态八态
// ===========================================================================

test("A1 未运行：显示「尚未运行 Agent」引导且不渲染步骤条", async ({ page }) => {
  await openAnalysis(page, SCENARIOS.notStarted);
  await expect(page.locator(".analysis-runfocus.empty-run")).toContainText("尚未运行 Agent");
  await expect(page.locator(".analysis-runfocus.empty-run")).toContainText("系统将依次完成事实、规则、风险和报告节点");
  await expect(page.locator(".analysis-stage")).toHaveCount(0);
  await expect(page.locator(".analysis-run-result")).toHaveCount(0);
});

test("A2 执行中：四阶段顺序与状态、当前阶段蓝色强调、进度与阶段归属正确", async ({ page }) => {
  await openAnalysis(page, SCENARIOS.running);

  await expect(page.locator(".analysis-run-status.is-running")).toHaveText("执行中");
  await expect(page.locator(".analysis-progress-percent")).toHaveText("45%");
  // 进度行显示当前节点 + 所属阶段（交叉核验属"核验"阶段）
  await expect(page.locator(".analysis-run-progress")).toContainText("当前节点：交叉核验");
  await expect(page.locator(".analysis-run-progress")).toContainText("核验阶段");
  await expect(page.locator(".analysis-run-progress")).toContainText("（5/11）");

  // 四阶段，顺序固定
  const names = await page.locator(".analysis-stage-name").allInnerTexts();
  expect(names.map((t) => t.trim())).toEqual(["材料", "核验", "风险", "报告"]);

  // 材料阶段 3/3 完成；核验阶段进行中 → 蓝色强调
  await expect(stage(page, "材料").locator(".analysis-stage-count")).toHaveText("3/3");
  await expect(stage(page, "核验").locator(".analysis-stage-state")).toHaveText("执行中");
  await expect(stage(page, "核验")).toHaveClass(/stage-running/);
  expect(await stageIconBg(page, "stage-running")).toBe(COLOR.primary);
  // 完成阶段用浅绿图标（弱化），不是实心绿
  await expect(stage(page, "材料")).toHaveClass(/stage-completed/);
  expect(await stageIconBg(page, "stage-completed")).toBe("rgb(237, 249, 241)");
  // 未开始的阶段仍是待执行
  await expect(stage(page, "报告")).toHaveClass(/stage-pending/);
});

test("A3 完成且零异常零材料不足：出现绿色结论带「校验全部通过」", async ({ page }) => {
  await openAnalysis(page, SCENARIOS.completed);
  await expect(page.locator(".analysis-run-status.is-completed")).toHaveText("已完成");
  const band = page.locator(".analysis-run-result");
  await expect(band).toHaveCount(1);
  await expect(band).toHaveClass(/is-passed/);
  await expect(band).toContainText("运行已完成");
  await expect(band).toContainText("全部校验通过");
  await expect(page.locator(".analysis-run-verdict.is-passed")).toContainText("校验全部通过");
  await expect(page.locator(".analysis-run-result.is-warning")).toHaveCount(0);
});

test("A3b 完成但零异常却有材料不足：不允许绿色结论带（中性，不谎报通过）", async ({ page }) => {
  await openAnalysis(page, SCENARIOS.completedInsufficientOnly);
  await expect(page.locator(".analysis-run-result.is-passed")).toHaveCount(0);
  await expect(page.locator(".analysis-run-result")).toHaveCount(0);
  const chip = page.locator(".analysis-run-verdict.is-empty");
  await expect(chip).toContainText("校验无异常");
  await expect(chip).toContainText("2 项材料不足");
});

test("A4 「运行完成 ≠ 校验通过」：完成但有异常 → 琥珀结论带 + 明确文案", async ({ page }) => {
  await openAnalysis(page, SCENARIOS.warning);
  const band = page.locator(".analysis-run-result.is-warning");
  await expect(band).toHaveCount(1);
  await expect(band).toContainText("运行完成不等于校验通过");
  await expect(band).toContainText("运行已完成，但校验存在异常");
  // 合并重复提醒：具体条数只在结论 chip 里给一次，结论带不再重复计数
  await expect(band).not.toContainText("9 项异常");
  await expect(page.locator(".analysis-run-result.is-passed")).toHaveCount(0);
  const chip = page.locator(".analysis-run-verdict.is-warning");
  await expect(chip).toContainText("校验 9 项异常");
  await expect(chip).toContainText("2 项材料不足");
  // 汇入异常节点的阶段用琥珀（与"完成"的浅绿、与"失败"的红都不同）
  await expect(stage(page, "核验")).toHaveClass(/stage-warning/);
  expect(await stageIconBg(page, "stage-warning")).toBe(COLOR.mediumBg);
  await expect(stage(page, "核验").locator(".analysis-stage-state")).toHaveCSS("color", "rgb(199, 120, 0)");
});

test("A5 降级：阶段走降级态（中性灰 + 虚线下边），与 warning 琥珀、failed 红可区分", async ({ page }) => {
  await openAnalysis(page, SCENARIOS.degraded);
  // conflict 节点降级 → 材料阶段整体降级
  await expect(stage(page, "材料")).toHaveClass(/stage-degraded/);
  expect(await stageIconBg(page, "stage-degraded")).toBe(COLOR.neutral);
  const dashed = await stage(page, "材料").locator(".analysis-stage-icon")
    .evaluate((el) => getComputedStyle(el).borderBottomStyle);
  expect(dashed).toBe("dashed");
  // 展开该阶段可见降级节点用的是空心减号语义（图标存在即可，形状由 lucide 保证）
  await stage(page, "材料").click();
  await expect(page.locator(".analysis-stage-node.node-degraded")).toHaveCount(1);
});

test("A6 失败：优先展示失败原因 + 失败节点红环 + 可重新运行", async ({ page }) => {
  await openAnalysis(page, SCENARIOS.failed);
  await expect(page.locator(".analysis-run-status.is-failed")).toHaveText("未完成");
  const error = page.locator(".analysis-run-error");
  await expect(error).toContainText("运行失败：指标计算节点缺少输入事实");
  // 失败原因带位于四阶段之前（优先展示）
  const beforeStrip = await page.evaluate(() => {
    const err = document.querySelector(".analysis-run-error");
    const strip = document.querySelector(".analysis-stages");
    return Boolean(err && strip && (err.compareDocumentPosition(strip) & Node.DOCUMENT_POSITION_FOLLOWING));
  });
  expect(beforeStrip).toBe(true);
  // 失败节点所在阶段整体呈现失败态
  await expect(stage(page, "核验")).toHaveClass(/stage-failed/);
  expect(await stageIconBg(page, "stage-failed")).toBe(COLOR.high);
  await expect(error.getByRole("button", { name: "重新运行" })).toBeVisible();
});

test("A7 取消：运行状态显示「未完成」，被取消节点按失败态呈现", async ({ page }) => {
  await openAnalysis(page, SCENARIOS.cancelled);
  await expect(page.locator(".analysis-run-status.is-pending")).toHaveText("未完成");
  // 被取消的节点按失败态呈现 → 材料阶段失败
  await expect(stage(page, "材料")).toHaveClass(/stage-failed/);
  await expect(page.locator(".analysis-run-result")).toHaveCount(0);
});

test("A8 模型未配置：状态 chip「需要模型配置」用琥珀", async ({ page }) => {
  await openAnalysis(page, SCENARIOS.modelRequired);
  const chip = page.locator(".analysis-run-status.is-model");
  await expect(chip).toContainText("需要模型配置");
  await expect(chip).toHaveCSS("color", "rgb(199, 120, 0)");
  await expect(page.locator(".analysis-run-result")).toHaveCount(0);
});

// ===========================================================================
// B 组 · 指标边界
// ===========================================================================

test("B 财务汇总 + 指标抽屉：最新期间取最大、空串不当选、5 种口径、null→材料不足、无 12 项截断", async ({ page }) => {
  await openAnalysis(page, SCENARIOS.warning);

  // L2 汇总行
  const summary = page.locator(".analysis-finance-summary");
  await expect(summary).toContainText("指标种类");
  await expect(summary.locator(".afs-stat").nth(0)).toContainText("15 类");
  await expect(summary.locator(".afs-stat").nth(1)).toContainText("14 / 15");
  await expect(summary.locator(".afs-stat").nth(2)).toContainText("2023–2025");
  await expect(summary.locator(".afs-note")).toHaveText("异常数量见下方核验卡片");

  await page.getByRole("button", { name: "查看全部指标" }).click();
  await expect(page.locator(".analysis-metrics-drawer.open")).toBeVisible();
  await expect(page.locator(".analysis-drawer-period-note")).toHaveText("最新期间：2025");

  const row = (name: string) => page.locator(".amt-row").filter({ hasText: name });
  const valueOf = (name: string) => row(name).locator(".amt-value");

  // 缺陷 1 回归：同组 2023=1 / 2025=2 → 最新取 2025=2（不是最早）
  await expect(valueOf("资产负债率")).toHaveText("2%");
  await expect(row("资产负债率").locator(".amt-period")).toHaveText("2025");

  // 缺陷 1 回归：同组 空串=99 / 2024=88 → 空串不得当选
  await expect(valueOf("流动比率")).toHaveText("88%");
  await expect(row("流动比率").locator(".amt-period")).toHaveText("2024");

  // 5 种口径文案
  await expect(row("资产负债率").locator(".amt-scope")).toHaveText("合并");
  await expect(row("所有者权益").locator(".amt-scope")).toHaveText("单体");
  await expect(row("营业收入").locator(".amt-scope")).toHaveText("主体");
  await expect(row("本次交易对价").locator(".amt-scope")).toHaveText("本次交易");
  // 缺陷 2 回归：unknown 必须显示「口径待确认」，不能是「单体口径」
  await expect(row("销售净利率").locator(".amt-scope")).toHaveText("口径待确认");

  // null → 材料不足，而不是 0
  await expect(valueOf("资产总额")).toHaveText("材料不足");
  await expect(valueOf("资产总额")).toHaveClass(/is-empty/);

  // 无 12 项截断：15 行全量
  await expect(page.locator(".amt-row")).toHaveCount(15);
});

// ===========================================================================
// C 组 · 交互与键盘
// ===========================================================================

test("C1 卡片统计值 + 详情弹窗：有异常默认筛「异常」、四档筛选、分页默认 10 可切 50", async ({ page }) => {
  await openAnalysis(page, SCENARIOS.warning);

  const tie = card(page, "报表勾稽校验");
  // 卡片三个统计值
  await expect(tie.locator(".acs-chip.is-warning")).toHaveText("异常9");
  await expect(tie.locator(".acs-chip.is-insufficient")).toHaveText("材料不足2");
  await expect(tie.locator(".acs-chip.is-passed")).toHaveText("通过1");
  // 卡片内不再有结果区
  await expect(page.locator(".analysis-result-row")).toHaveCount(0);

  await openDetail(page, "tie-outs");
  const m = page.locator(".analysis-modal");
  // 有异常 → 默认筛「异常」
  await expect(modalFilter(page, "异常")).toHaveText("异常 9");
  await expect(m.locator(".analysis-modal-row")).toHaveCount(9);

  await modalFilter(page, "材料不足").click();
  await expect(m.locator(".analysis-modal-row")).toHaveCount(2);
  await expect(m.locator(".arr-status").first()).toHaveText("材料不足");

  await modalFilter(page, "通过").click();
  await expect(m.locator(".analysis-modal-row")).toHaveCount(1);
  await expect(m.locator(".arr-status").first()).toHaveText("通过");

  await modalFilter(page, "全部").click();
  await expect(m.locator(".analysis-modal-row")).toHaveCount(10); // 默认每页 10
  await expect(m.locator(".analysis-modal-foot")).toContainText("第 1 / 2 页 · 共 12 条");

  // 切每页 50 → 12 行
  await m.getByLabel("每页条数").selectOption("50");
  await expect(m.locator(".analysis-modal-row")).toHaveCount(12);

  // 换另一张卡片：独立筛选/分页，仍是默认 10 且互不影响
  await page.getByRole("button", { name: "关闭详情" }).click();
  await openDetail(page, "checks");
  await expect(page.locator(".analysis-modal-row")).toHaveCount(10);
  await expect(page.locator(".analysis-modal").getByLabel("每页条数")).toHaveValue("10");
});

test("C2 卡片恒定展示统计与重点结果，且默认不渲染弹窗", async ({ page }) => {
  await openAnalysis(page, SCENARIOS.completed);
  await expect(page.locator(".analysis-modal")).toHaveCount(0);
  const tie = card(page, "报表勾稽校验");
  // 全部通过时三个统计值都在（0 计数弱化但不隐藏）
  await expect(tie.locator(".acs-chip")).toHaveCount(3);
  await expect(tie.locator(".acs-chip.is-passed")).toHaveText("通过2");
  await expect(tie.locator(".acs-chip").filter({ hasText: "异常" })).toHaveClass(/is-muted/);
  // 无异常/材料不足 → 不展示重点结果
  await expect(tie.locator(".ach-row")).toHaveCount(0);
  await expect(tie.locator(".analysis-card-none")).toBeVisible();
});

test("C3 指标抽屉：打开移焦到关闭按钮；Escape / 遮罩 / 关闭按钮均可关闭且焦点回到触发按钮", async ({ page }) => {
  await openAnalysis(page, SCENARIOS.warning);
  const trigger = page.getByRole("button", { name: "查看全部指标" });

  await trigger.click();
  const drawer = page.locator(".analysis-metrics-drawer.open");
  await expect(drawer).toBeVisible();
  await expect(drawer.getByRole("button", { name: "关闭财务指标" })).toBeFocused();

  // Escape 关闭
  await page.keyboard.press("Escape");
  await expect(page.locator(".analysis-metrics-drawer.open")).toHaveCount(0);
  await expect(trigger).toBeFocused();

  // 遮罩关闭
  await trigger.click();
  await expect(drawer).toBeVisible();
  await page.locator(".analysis-drawer-mask").click({ position: { x: 400, y: 400 } });
  await expect(page.locator(".analysis-metrics-drawer.open")).toHaveCount(0);
  await expect(trigger).toBeFocused();

  // 关闭按钮关闭
  await trigger.click();
  await expect(drawer).toBeVisible();
  await page.getByRole("button", { name: "关闭财务指标" }).click();
  await expect(page.locator(".analysis-metrics-drawer.open")).toHaveCount(0);
  await expect(trigger).toBeFocused();
});

test("C4 抽屉内「计算链路」进入详情与「返回指标列表」", async ({ page }) => {
  await openAnalysis(page, SCENARIOS.warning);
  await page.getByRole("button", { name: "查看全部指标" }).click();
  await page.locator(".amt-row").filter({ hasText: "资产负债率" }).getByRole("button", { name: "查看计算链路" }).click();

  await expect(page.getByText("Deterministic calculation trace")).toBeVisible();
  await expect(page.locator(".analysis-trace-result")).toContainText("2%");
  await expect(page.locator(".analysis-trace-result span")).toContainText("debt_ratio = f(inputs)");
  await expect(page.locator(".ati-row").first()).toContainText("资产总额");
  await expect(page.locator(".analysis-trace-back")).toBeVisible();

  await page.getByRole("button", { name: "返回指标列表" }).click();
  await expect(page.locator(".amt-row")).toHaveCount(15);
  await expect(page.locator(".analysis-trace-view")).toHaveCount(0);
});

test("C5 抽屉计算链路加载失败 → 错误态带「重试」且可恢复", async ({ page }) => {
  const state = await openAnalysis(page, SCENARIOS.warning);
  await page.getByRole("button", { name: "查看全部指标" }).click();

  state.traceFail = true;
  await page.locator(".amt-row").first().getByRole("button", { name: "查看计算链路" }).click();
  const error = page.locator(".analysis-trace-view .analysis-error");
  await expect(error).toContainText("计算链路加载失败");
  await expect(error.getByRole("button", { name: "重试" })).toBeVisible();
  await expect(page.locator(".analysis-trace-back")).toBeVisible(); // 仍可返回列表

  state.traceFail = false;
  await error.getByRole("button", { name: "重试" }).click();
  await expect(page.locator(".analysis-trace-result")).toBeVisible();
  await expect(page.locator(".analysis-trace-view .analysis-error")).toHaveCount(0);
});

test("C6 抽屉内「证据」：证据层覆盖指标抽屉（z 更高）", async ({ page }) => {
  await openAnalysis(page, SCENARIOS.warning);
  await page.getByRole("button", { name: "查看全部指标" }).click();
  await page.locator(".amt-row").filter({ hasText: "资产负债率" }).getByRole("button", { name: "查看计算链路" }).click();
  await page.locator(".ati-row .evidence-links button").first().click();

  const evidence = page.locator(".evidence-drawer.open");
  await expect(evidence).toBeVisible();
  await expect(evidence).toContainText("资产总额 100 万元");
  const z = await page.evaluate(() => {
    const ev = document.querySelector(".evidence-drawer") as HTMLElement;
    const md = document.querySelector(".analysis-metrics-drawer") as HTMLElement;
    return { evidence: Number(getComputedStyle(ev).zIndex), metrics: Number(getComputedStyle(md).zIndex) };
  });
  expect(z.evidence).toBeGreaterThan(z.metrics);
});

test("C6b 证据打开焦点进入证据层，关闭后焦点回到抽屉内触发按钮（design §4.2）", async ({ page }) => {
  await openAnalysis(page, SCENARIOS.warning);
  await page.getByRole("button", { name: "查看全部指标" }).click();
  await page.locator(".amt-row").filter({ hasText: "资产负债率" }).getByRole("button", { name: "查看计算链路" }).click();
  const evidenceTrigger = page.locator(".ati-row .evidence-links button").first();
  await evidenceTrigger.click();
  await expect(page.locator(".evidence-drawer.open")).toBeVisible();

  // 打开：焦点必须进入证据层（关闭按钮），不能仍留在被它覆盖的指标抽屉里
  const onOpen = await page.evaluate(() => {
    const el = document.activeElement as HTMLElement | null;
    return { cls: el?.className ?? "", inEvidence: Boolean(el?.closest(".evidence-drawer")) };
  });
  console.log(`[C6b] 打开证据后 activeElement = ${JSON.stringify(onOpen)}`);
  expect(onOpen, `打开证据后 activeElement = ${JSON.stringify(onOpen)}`).toMatchObject({ inEvidence: true });

  const drawerClose = page.locator(".evidence-drawer .icon-button");
  await drawerClose.click();
  await expect(page.locator(".evidence-drawer.open")).toHaveCount(0);

  const active = await page.evaluate(() => {
    const el = document.activeElement as HTMLElement | null;
    return { cls: el?.className ?? "", inEvidence: Boolean(el?.closest(".evidence-drawer")), inMetrics: Boolean(el?.closest(".analysis-metrics-drawer")) };
  });
  console.log(`[C6b] 关闭证据后 activeElement = ${JSON.stringify(active)}`);
  // 规格要求焦点回到「指标抽屉内的证据触发按钮」
  expect(active, `关闭证据后 activeElement = ${JSON.stringify(active)}`).toMatchObject({ inMetrics: true, inEvidence: false });
});

test("C6c Escape 层级：证据层只关自己（trace 保留），再按依次「返回列表」「关抽屉」", async ({ page }) => {
  await openAnalysis(page, SCENARIOS.warning);
  await page.getByRole("button", { name: "查看全部指标" }).click();
  await page.locator(".amt-row").filter({ hasText: "资产负债率" }).getByRole("button", { name: "查看计算链路" }).click();
  await expect(page.getByText("Deterministic calculation trace")).toBeVisible();

  const evidenceTrigger = page.locator(".ati-row .evidence-links button").first();
  await evidenceTrigger.click();
  await expect(page.locator(".evidence-drawer.open")).toBeVisible();

  // 第 1 次 Escape：只关证据层——指标抽屉的 trace 视图必须原样保留、抽屉不关
  await page.keyboard.press("Escape");
  await expect(page.locator(".evidence-drawer.open")).toHaveCount(0);
  await expect(page.locator(".analysis-metrics-drawer.open")).toHaveCount(1);
  await expect(page.getByText("Deterministic calculation trace")).toBeVisible();
  await expect(page.locator(".analysis-trace-view")).toBeVisible();
  // 焦点回到被证据层覆盖的抽屉内触发按钮
  await expect(evidenceTrigger).toBeFocused();
  console.log("[C6c] 第1次 Escape 后：证据层已关，trace 视图仍在，抽屉仍开");

  // 第 2 次 Escape：trace 详情 → 返回指标列表（抽屉仍在，回到 15 行列表态）
  await page.keyboard.press("Escape");
  await expect(page.locator(".analysis-trace-view")).toHaveCount(0);
  await expect(page.locator(".analysis-metrics-drawer.open")).toHaveCount(1);
  await expect(page.locator(".amt-row")).toHaveCount(15);
  console.log("[C6c] 第2次 Escape 后：回到指标列表（15 行），抽屉仍开");

  // 第 3 次 Escape：列表 → 关闭指标抽屉，焦点回到「查看全部指标」
  await page.keyboard.press("Escape");
  await expect(page.locator(".analysis-metrics-drawer.open")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "查看全部指标" })).toBeFocused();
  console.log("[C6c] 第3次 Escape 后：指标抽屉已关，焦点回到「查看全部指标」");
});

test("C7 用户手动展开阶段/筛选后，运行数据推进不抢回选择（同一 run.id 刷新）", async ({ page }) => {
  const state = await openAnalysis(page, SCENARIOS.failed);

  // 手动展开一个阶段（touched=true）
  await stage(page, "材料").click();
  await expect(page.locator(".analysis-stage-panel")).toHaveCount(1);
  await expect(page.locator(".analysis-stage-panel")).toContainText("材料盘点");

  // 手动把弹窗筛选切到「全部」（用户选择，刷新不得覆盖）
  await openDetail(page, "tie-outs");
  await modalFilter(page, "全部").click();
  await expect(modalFilter(page, "全部")).toHaveText("全部 12");

  // 准备：同一 run.id 的数据推进；刷新后异常归 0（用户已手动选择，不得被抢回）
  state.retryRunId = "run-1";
  state.retryEvents = [
    makeRun({ id: "run-1", status: "running", current_node: "extract", progress: 20, finished_at: null }),
    makeRun({ id: "run-1", status: "completed", current_node: "review", progress: 100 }),
  ];
  state.tieOutsAfter = TIE_PASS;
  state.checksAfter = CHECK_PASS_2;

  // 弹窗遮罩按设计拦截指针（背景不可点击），用原生 click() 直接触发（与 C8 同一手法）
  await page.locator(".analysis-run-error").getByRole("button", { name: "重新运行" })
    .evaluate((el) => (el as HTMLButtonElement).click());

  // 数据推进后：阶段展开保持、弹窗筛选保持用户选择的「全部」
  await expect(page.locator(".analysis-stage-panel")).toContainText("材料盘点");
  await expect(page.locator(".analysis-modal")).toBeVisible();
  await expect(modalFilter(page, "全部")).toHaveText("全部 2");
  await expect(page.locator(".analysis-modal-row")).toHaveCount(2);
});

test("C8 切换运行（resetKey 变化）→ 抽屉关闭、节点选择回自动、筛选分页复位", async ({ page }) => {
  const state = await openAnalysis(page, SCENARIOS.failed);

  // 制造非默认状态：展开阶段 + 开弹窗切筛选 + 开抽屉（抽屉会盖住「重新运行」，故最后开）
  await stage(page, "材料").click();
  await expect(page.locator(".analysis-stage-panel")).toHaveCount(1);
  await openDetail(page, "tie-outs");
  await modalFilter(page, "通过").click();
  await expect(modalFilter(page, "通过")).toHaveText("通过 1");
  await page.getByRole("button", { name: "关闭详情" }).click();
  await page.getByRole("button", { name: "查看全部指标" }).click();
  await expect(page.locator(".analysis-metrics-drawer.open")).toBeVisible();

  // 重跑返回**新的 run.id** → resetKey 变化
  const completed = makeRun({ id: "run-2", status: "completed", current_node: "review", progress: 100 });
  state.retryRunId = "run-2";
  state.retryEvents = [
    makeRun({ id: "run-2", status: "running", current_node: "extract", progress: 20, finished_at: null }),
    completed,
  ];
  state.latestOverride = { run: completed, nodes: makeNodes(allCompleted) };
  state.tieOutsAfter = TIE_WARN;
  state.checksAfter = CHECK_PASS_12;

  // 抽屉遮挡按钮，用原生 click() 直接触发（验证的是应用对 resetKey 的反应）
  await page.locator(".analysis-run-error").getByRole("button", { name: "重新运行" })
    .evaluate((el) => (el as HTMLButtonElement).click());

  await expect(page.locator(".analysis-metrics-drawer.open")).toHaveCount(0);      // 抽屉关闭
  await expect(page.locator(".analysis-stage-panel")).toHaveCount(0);              // 阶段展开复位
  await expect(page.locator(".analysis-modal")).toHaveCount(0);                    // 弹窗复位
  // 复位后重新打开弹窗：新运行有异常 → 默认筛「异常」
  await openDetail(page, "tie-outs");
  await expect(modalFilter(page, "异常")).toHaveText("异常 9");
});


// ===========================================================================
// A9-A10 · 四阶段工作流（本轮新增交互）
// ===========================================================================

test("A9 四阶段默认收起；点击展开该阶段节点列表与摘要；人工复核带语义说明", async ({ page }) => {
  await openAnalysis(page, SCENARIOS.warning);
  await expect(page.locator(".analysis-stage-panel")).toHaveCount(0); // 默认收起

  await stage(page, "报告").click();
  const panel = page.locator(".analysis-stage-panel");
  await expect(panel).toHaveCount(1);
  await expect(panel.locator(".analysis-stage-node")).toHaveCount(3);
  await expect(panel).toContainText("报告组装");
  await expect(panel).toContainText("引用校验");
  await expect(panel).toContainText("人工复核");
  // 人工复核：节点完成不代表人工事项已处理 → 必须带这句
  await expect(panel.locator(".analysis-stage-node-note")).toContainText("不代表人工事项已经处理");

  // 再点收起
  await stage(page, "报告").click();
  await expect(page.locator(".analysis-stage-panel")).toHaveCount(0);
  // 切到另一个阶段：一次只展开一个
  await stage(page, "核验").click();
  await expect(page.locator(".analysis-stage-panel")).toContainText("指标计算");
  await expect(page.locator(".analysis-stage-panel .analysis-stage-node")).toHaveCount(2);
  await stage(page, "风险").click();
  await expect(page.locator(".analysis-stage-panel")).toContainText("定性风险");
  await expect(page.locator(".analysis-stage-panel")).not.toContainText("指标计算");
});

test("A10 回归：阶段与节点的文字不再被通用 node-* 样式误染背景", async ({ page }) => {
  await openAnalysis(page, SCENARIOS.warning);
  // 旧 bug：.node-completed > span 命中了按钮里的两个直接 span，把「名称」也染成图标底色
  const bg = await page.locator(".analysis-stage-name").evaluateAll((els) => els.map((el) => getComputedStyle(el).backgroundColor));
  expect(bg.every((c) => c === "rgba(0, 0, 0, 0)"), `阶段名称不应有背景色：${JSON.stringify(bg)}`).toBe(true);
  const color = await page.locator(".analysis-stage-name").evaluateAll((els) => els.map((el) => getComputedStyle(el).color));
  expect(color.every((c) => c === "rgb(31, 41, 55)"), "完成阶段的名称应是正常深色文字").toBe(true);
  // 旧样式类应已不存在于页面
  await expect(page.locator(".node-step")).toHaveCount(0);
  await expect(page.locator(".node-timeline")).toHaveCount(0);

  await stage(page, "核验").click();
  const nodeBg = await page.locator(".analysis-stage-node strong").evaluateAll((els) => els.map((el) => getComputedStyle(el).backgroundColor));
  expect(nodeBg.every((c) => c === "rgba(0, 0, 0, 0)")).toBe(true);
});

// ===========================================================================
// C9-C12 · 摘要卡片与详情弹窗（本轮新增交互）
// ===========================================================================

test("C9 摘要卡片：统计值与后端一致、重点结果优先异常且最多两条、底部有明确「详情」按钮", async ({ page }) => {
  await openAnalysis(page, SCENARIOS.warning);
  const tie = card(page, "报表勾稽校验");
  await expect(tie.locator(".analysis-card-total")).toHaveText("共 12");
  await expect(tie.locator(".acs-chip.is-warning")).toHaveText("异常9");
  await expect(tie.locator(".acs-chip.is-insufficient")).toHaveText("材料不足2");
  await expect(tie.locator(".acs-chip.is-passed")).toHaveText("通过1");

  // 重点结果：最多两条，且优先异常
  const rows = tie.locator(".ach-row");
  await expect(rows).toHaveCount(2);
  await expect(rows.first()).toHaveClass(/is-warning/);
  await expect(tie.getByRole("button", { name: "详情" })).toBeVisible();

  // 只有材料不足时，重点结果落到材料不足（其次）
  const check = card(page, "核验规则");
  await expect(check.locator(".acs-chip").filter({ hasText: "异常" })).toHaveClass(/is-muted/);
  await expect(check.locator(".ach-row")).toHaveCount(0);
});

test("C10 详情弹窗：桌面尺寸上限、遮罩锁滚动、行内展开单项详情、关闭三路与焦点恢复", async ({ page }) => {
  await openAnalysis(page, SCENARIOS.warning);
  await openDetail(page, "tie-outs");
  const m = page.locator(".analysis-modal");

  const geo = await m.evaluate((el) => {
    const r = el.getBoundingClientRect();
    return { w: Math.round(r.width), h: Math.round(r.height), vw: window.innerWidth, vh: window.innerHeight };
  });
  expect(geo.w, "桌面宽度上限 880px").toBeLessThanOrEqual(880);
  expect(geo.h, "高度上限为视口 80%").toBeLessThanOrEqual(Math.round(geo.vh * 0.8) + 1);
  await expect(page.locator(".analysis-modal-mask")).toBeVisible();
  expect(await page.evaluate(() => document.body.style.overflow), "背景不可滚动").toBe("hidden");

  // 行内展开单项详情：勾稽要有左右值/差异/容差
  await m.locator(".analysis-modal-row-head").first().click();
  const detail = m.locator(".analysis-modal-row .arr-detail").first();
  await expect(detail).toBeVisible();
  await expect(detail).toContainText("左值");
  await expect(detail).toContainText("差异");
  await expect(detail).toContainText("容差");
  // 再点收起
  await m.locator(".analysis-modal-row-head").first().click();
  await expect(m.locator(".analysis-modal-row .arr-detail")).toHaveCount(0);

  // 遮罩点击关闭 + 焦点回触发按钮（遮罩中心被弹窗本体覆盖，改点弹窗外的遮罩区域）
  await page.locator(".analysis-modal-mask").click({ position: { x: 40, y: 40 } });
  await expect(page.locator(".analysis-modal")).toHaveCount(0);
  expect(await page.evaluate(() => document.body.style.overflow), "关闭后解锁滚动").toBe("");
  expect(await page.evaluate(() => document.activeElement?.id ?? "")).toBe("analysis-tie-outs-detail");

  // 关闭按钮 + Escape
  await openDetail(page, "tie-outs");
  await page.getByRole("button", { name: "关闭详情" }).click();
  await expect(page.locator(".analysis-modal")).toHaveCount(0);
  await openDetail(page, "tie-outs");
  await page.keyboard.press("Escape");
  await expect(page.locator(".analysis-modal")).toHaveCount(0);
});

test("C11 证据抽屉覆盖详情弹窗；关闭证据返回弹窗并保留筛选、每页与展开项", async ({ page }) => {
  await openAnalysis(page, SCENARIOS.warning);
  await openDetail(page, "tie-outs");
  const m = page.locator(".analysis-modal");

  // 制造非默认状态：筛「全部」+ 每页 50 + 展开第一行
  await modalFilter(page, "全部").click();
  await m.getByLabel("每页条数").selectOption("50");
  await m.locator(".analysis-modal-row-head").first().click();
  await expect(m.locator(".analysis-modal-row .arr-detail")).toHaveCount(1);

  await m.locator(".analysis-modal-row .evidence-links button").first().click();
  await expect(page.locator(".evidence-drawer.open")).toBeVisible();
  await expect(m, "弹窗仍在（证据层盖在其上）").toBeVisible();
  const z = await page.evaluate(() => ({
    ev: Number(getComputedStyle(document.querySelector(".evidence-drawer") as HTMLElement).zIndex),
    md: Number(getComputedStyle(document.querySelector(".analysis-modal") as HTMLElement).zIndex),
  }));
  expect(z.ev, `证据层 z(${z.ev}) 应高于弹窗 z(${z.md})`).toBeGreaterThan(z.md);

  // 只关最上层：Escape 先关证据
  await page.keyboard.press("Escape");
  await expect(page.locator(".evidence-drawer.open")).toHaveCount(0);
  await expect(m).toBeVisible();
  // 返回后保留：筛选、每页、展开项
  await expect(modalFilter(page, "全部")).toHaveText("全部 12");
  await expect(m.getByLabel("每页条数")).toHaveValue("50");
  await expect(m.locator(".analysis-modal-row .arr-detail")).toHaveCount(1);
  expect(await page.evaluate(() => document.activeElement?.closest(".analysis-modal") !== null), "焦点回到弹窗内").toBe(true);

  // 再按 Escape 才关弹窗
  await page.keyboard.press("Escape");
  await expect(page.locator(".analysis-modal")).toHaveCount(0);
});

test("C12 离开分析页再回来：阶段展开与详情弹窗不残留", async ({ page }) => {
  // 说明：resetKey = `${projectId}:${runId}`，切换"运行"的那条路径由 C8 覆盖（同一段复位逻辑）；
  // 切换"项目"的复位已在真实后端探针中验证。这里补的是"离开页面再回来"的用户路径。
  await openAnalysis(page, SCENARIOS.warning);
  await stage(page, "风险").click();
  await expect(page.locator(".analysis-stage-panel")).toHaveCount(1);
  await openDetail(page, "tie-outs");
  await expect(page.locator(".analysis-modal")).toHaveCount(1);

  // 弹窗遮罩拦截指针（属设计），用原生 click() 直接触发视图切换（与 C7/C8 同一手法）
  await page.getByRole("button", { name: /报告交付/ }).evaluate((el) => (el as HTMLButtonElement).click());
  await expect(page.getByRole("heading", { name: "财务与交叉核验" })).toHaveCount(0);
  await page.getByRole("button", { name: /债券 Agent 分析/ }).click();
  await expect(page.getByRole("heading", { name: "财务与交叉核验" })).toBeVisible();

  await expect(page.locator(".analysis-modal")).toHaveCount(0);
  await expect(page.locator(".analysis-stage-panel")).toHaveCount(0);
});

// ===========================================================================
// D 组 · 视觉与截图（1440×900 / 1920×1080 / 390×844）
// ===========================================================================

test("D 三档视口：首屏四要素（运行重点/财务汇总/两张卡片）可见、无横向溢出、无重叠", async ({ page }, testInfo) => {
  await openAnalysis(page, SCENARIOS.compactWarning);

  const shots = "C:/Users/Jamie/AppData/Local/Temp/analysis-qa";
  const record = async (label: string, width: number, height: number) => {
    await page.setViewportSize({ width, height });
    await page.waitForTimeout(150);
    const metrics = await page.evaluate(() => {
      const vy = () => window.innerHeight;
      const box = (sel: string) => {
        const el = document.querySelector(sel) as HTMLElement | null;
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return { top: Math.round(r.top), bottom: Math.round(r.bottom), left: Math.round(r.left), right: Math.round(r.right), h: Math.round(r.height) };
      };
      return {
        innerHeight: vy(),
        scrollHeight: document.documentElement.scrollHeight,
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
        runfocus: box(".analysis-runfocus"),
        summary: box(".analysis-finance-summary"),
        cards: Array.from(document.querySelectorAll(".analysis-card")).map((el) => {
          const r = el.getBoundingClientRect();
          const h2 = el.querySelector("h2");
          return { title: h2?.textContent ?? "", top: Math.round(r.top), bottom: Math.round(r.bottom), left: Math.round(r.left), width: Math.round(r.width) };
        }),
        stages: box(".analysis-stages"),
      };
    });
    await page.screenshot({ path: `${shots}/analysis-${label}.png`, fullPage: false });
    return metrics;
  };

  // 1440×900（设计硬预算视口）
  const m1440 = await record("1440x900", 1440, 900);
  expect(m1440.scrollWidth, "无横向溢出").toBeLessThanOrEqual(m1440.clientWidth);
  const titles1440 = m1440.cards.map((c) => c.title);
  expect(titles1440).toContain("报表勾稽校验");
  expect(titles1440).toContain("核验规则");
  // 两张卡片桌面等宽并排（同一行、宽度一致）
  expect(m1440.cards).toHaveLength(2);
  expect(m1440.cards[0].top).toBe(m1440.cards[1].top);
  expect(Math.abs(m1440.cards[0].width - m1440.cards[1].width)).toBeLessThanOrEqual(1);
  for (const c of m1440.cards) {
    expect(c.bottom, `卡片「${c.title}」应在首屏内 (bottom=${c.bottom} <= ${m1440.innerHeight})`).toBeLessThanOrEqual(m1440.innerHeight);
  }
  expect(m1440.runfocus && m1440.runfocus.bottom).toBeLessThan(m1440.innerHeight);
  expect(m1440.summary && m1440.summary.bottom).toBeLessThan(m1440.innerHeight);
  expect(m1440.scrollHeight, "1440×900 首屏不应出现整页纵向滚动").toBeLessThanOrEqual(m1440.innerHeight);

  // 1920×1080
  const m1920 = await record("1920x1080", 1920, 1080);
  expect(m1920.scrollWidth, "无横向溢出").toBeLessThanOrEqual(m1920.clientWidth);
  for (const c of m1920.cards) expect(c.bottom).toBeLessThanOrEqual(m1920.innerHeight);
  expect(m1920.scrollHeight).toBeLessThanOrEqual(m1920.innerHeight);

  // 390×844（移动端）
  const m390 = await record("390x844-mobile", 390, 844);
  expect(m390.scrollWidth, "移动端无横向溢出").toBeLessThanOrEqual(m390.clientWidth);

  // 重叠抽查：状态栏 / 步骤条 / 结果行 boundingBox 互不包含/交叠
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.waitForTimeout(150);
  const boxes = await page.evaluate(() => {
    const pick = (sel: string) => Array.from(document.querySelectorAll(sel)).slice(0, 4).map((el) => {
      const r = el.getBoundingClientRect();
      return { x: r.x, y: r.y, w: r.width, h: r.height };
    });
    return { status: pick(".analysis-run-status"), verdict: pick(".analysis-run-verdict"), nodes: pick(".analysis-stage"), rows: pick(".ach-row") };
  });
  const overlap = (a: { x: number; y: number; w: number; h: number }, b: typeof a) =>
    a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
  const pairs: Array<[typeof boxes.status[number], typeof boxes.status[number], string]> = [
    [boxes.status[0], boxes.verdict[0], "状态 chip × 校验结论 chip"],
    [boxes.nodes[0], boxes.nodes[1], "相邻节点"],
    [boxes.rows[0], boxes.rows[1], "相邻结果行"],
  ];
  const conflicts = pairs.filter(([a, b]) => a && b && overlap(a, b)).map(([, , name]) => name);
  expect(conflicts, `控件重叠：${conflicts.join(" / ")}`).toEqual([]);

  writeFileSync(`${shots}/measurements.json`, JSON.stringify({ m1440, m1920, m390, boxes }, null, 2));
  await testInfo.attach("首屏实测", { body: JSON.stringify({ m1440, m1920, m390 }, null, 2), contentType: "application/json" });
});
