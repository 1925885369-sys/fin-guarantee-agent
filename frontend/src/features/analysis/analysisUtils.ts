import type { CheckStatus, FinancialMetric, RunNode } from "../../types";

/** 口径标签：与 `App.tsx` 的 `FactTable` 保持同一份 5 值映射。
 *  金融指标、勾稽结果、核验结果三处必须共用它，不能再写 `scope === "consolidated" ? … : "单体"` 的二值判断
 *  ——实测 `unknown` 占多数，二值判断会把它们错标成"单体口径"。 */
export const SCOPE_LABELS: Record<string, string> = {
  standalone: "单体",
  consolidated: "合并",
  entity: "主体",
  transaction: "本次交易",
  unknown: "口径待确认",
};

export function scopeLabel(scope: string | null | undefined): string {
  const key = (scope || "").trim();
  if (!key) return SCOPE_LABELS.unknown;
  return SCOPE_LABELS[key] || key;
}

/** 期间是纯年度字符串或空串（实测去重 = ['', '2003', '2023'…]）。
 *  排序键 = Number(period)；**空串与非数值恒定排到最小**，否则空串会被当成 0 排到最前，
 *  重现"选了最旧 / 无效期间"的老 bug。非数值同为空串处理。 */
function periodRank(period: string | null | undefined): number {
  const trimmed = (period ?? "").trim();
  if (!trimmed) return Number.NEGATIVE_INFINITY;
  const value = Number(trimmed);
  return Number.isFinite(value) ? value : Number.NEGATIVE_INFINITY;
}

/** 每个 (subject_id, scope, metric_key) 取期间最大的一条（最新期间）。
 *  不再 `[...metrics].reverse()`、不再 `.slice(0, 12)`——抽屉要保留后端返回的全部数据。 */
export function latestMetricRecords(metrics: FinancialMetric[]): FinancialMetric[] {
  const groups = new Map<string, FinancialMetric>();
  metrics.forEach((item) => {
    const key = `${item.subject_id ?? ""}\u0000${item.scope ?? ""}\u0000${item.metric_key}`;
    const current = groups.get(key);
    if (!current || periodRank(item.period) > periodRank(current.period)) groups.set(key, item);
  });
  return Array.from(groups.values());
}

/** 指标种类数（distinct metric_key），按全部记录去重。 */
export function metricKindCount(metrics: FinancialMetric[]): number {
  return new Set(metrics.map((item) => item.metric_key)).size;
}

/** 期间范围：全部记录里非空、可解析期间的 min–max；单期只显示一期；无有效期间显示 —。 */
export function metricPeriodRange(metrics: FinancialMetric[]): string {
  const values = metrics
    .map((item) => (item.period ?? "").trim())
    .filter((period) => period && Number.isFinite(Number(period)))
    .map(Number);
  if (!values.length) return "—";
  const min = Math.min(...values);
  const max = Math.max(...values);
  return min === max ? String(min) : `${min}–${max}`;
}

/** 指标抽屉「最新」态回显用的最大期间；全部记录均无有效期间时返回空串。 */
export function latestMetricPeriod(metrics: FinancialMetric[]): string {
  const values = metrics
    .map((item) => (item.period ?? "").trim())
    .filter((period) => period && Number.isFinite(Number(period)))
    .map(Number);
  return values.length ? String(Math.max(...values)) : "";
}

/** 分区筛选键：异常 / 材料不足 / 通过 / 全部。 */
export type AnalysisFilter = "warning" | "insufficient" | "passed" | "all";

export const ANALYSIS_FILTERS: AnalysisFilter[] = ["warning", "insufficient", "passed", "all"];

export const FILTER_LABELS: Record<AnalysisFilter, string> = {
  warning: "异常",
  insufficient: "材料不足",
  passed: "通过",
  all: "全部",
};

export type StatusCounts = { total: number; warning: number; insufficient: number; passed: number };

export function countStatuses(items: { status: CheckStatus }[]): StatusCounts {
  return {
    total: items.length,
    warning: items.filter((item) => item.status === "warning").length,
    insufficient: items.filter((item) => item.status === "insufficient").length,
    passed: items.filter((item) => item.status === "passed").length,
  };
}

export function filterByStatus<T extends { status: CheckStatus }>(items: T[], filter: AnalysisFilter): T[] {
  if (filter === "all") return items;
  return items.filter((item) => item.status === filter);
}

/** 分区结果行 / 计数 chip 的三态元数据（通过 / 异常 / 材料不足）。 */
export const STATUS_META: Record<CheckStatus, { label: string; className: string }> = {
  passed: { label: "通过", className: "is-passed" },
  warning: { label: "异常", className: "is-warning" },
  insufficient: { label: "材料不足", className: "is-insufficient" },
};

/** 分区结果行的展示模型（与 tieOuts / checks 的具体字段解耦）。 */
export type AnalysisResultRow = {
  id: string;
  status: CheckStatus;
  title: string;
  summary: string;
  meta: string;
};

/** 11 个运行节点的固定顺序与中文名（顺序不可变）。 */
export const NODE_LABELS: Record<string, string> = {
  inventory: "材料盘点", extract: "事实抽取", conflict: "冲突识别", metrics: "指标计算",
  cross_check: "交叉核验", candidate: "定性风险", tasks: "复核任务", risk: "正式风险",
  draft: "报告组装", verify: "引用校验", review: "人工复核",
};

export const NODE_KEYS: string[] = Object.keys(NODE_LABELS);

export type NodeVisualState = "completed" | "running" | "warning" | "degraded" | "failed" | "pending";

/** 节点状态映射：degraded（降级）与 warning 形状 / 文案都不同，不依赖颜色单独传达。 */
export function nodeVisualState(status: string | undefined, isCurrent: boolean): NodeVisualState {
  if (status === "completed") return "completed";
  if (status === "running" || isCurrent) return "running";
  if (status === "warning") return "warning";
  if (status === "degraded") return "degraded";
  if (status === "failed" || status === "model_required" || status === "cancelled") return "failed";
  return "pending";
}

export const NODE_STATE_LABEL: Record<NodeVisualState, string> = {
  completed: "已完成", running: "执行中", warning: "警告",
  degraded: "降级", failed: "失败", pending: "待执行",
};

/** 运行状态 chip：只表达"节点执行到哪一步"，与校验结论 chip 语义分离。 */
export function runStatusVisual(status: string): { label: string; className: string } {
  if (status === "completed") return { label: "已完成", className: "is-completed" };
  if (status === "running" || status === "queued") return { label: "执行中", className: "is-running" };
  if (status === "model_required") return { label: "需要模型配置", className: "is-model" };
  if (status === "failed") return { label: "未完成", className: "is-failed" };
  return { label: "未完成", className: "is-pending" };
}

/** 校验结论（独立于运行状态）：
 *  - warning 数 > 0            → 异常
 *  - 有结果且异常=0、材料不足>0 → 中性（不谎报"全部通过"）
 *  - 有结果且异常=0、材料不足=0 → 全部通过
 *  - 无任何校验结果             → 暂无 */
export type AnalysisVerdict =
  | { kind: "warning"; warning: number; insufficient: number; total: number }
  | { kind: "neutral"; warning: number; insufficient: number; total: number }
  | { kind: "passed"; warning: number; insufficient: number; total: number }
  | { kind: "empty"; warning: number; insufficient: number; total: number };

export function deriveVerdict(counts: StatusCounts): AnalysisVerdict {
  if (counts.warning > 0) return { kind: "warning", ...counts };
  if (counts.total === 0) return { kind: "empty", ...counts };
  if (counts.insufficient > 0) return { kind: "neutral", ...counts };
  return { kind: "passed", ...counts };
}

/* ---------------------------------------------------------------------------
 * 四阶段工作流
 * 11 个节点按业务含义归入 4 个阶段；顺序固定，不随运行状态变化。
 * ------------------------------------------------------------------------- */

export type StageKey = "materials" | "verify" | "risk" | "report";

export type AnalysisStage = {
  key: StageKey;
  label: string;
  /** 阶段说明：用于展开区与 tooltip，不含任何结论性措辞。 */
  description: string;
  /** 该阶段的节点 key，顺序即执行顺序。 */
  nodes: string[];
};

export const ANALYSIS_STAGES: AnalysisStage[] = [
  { key: "materials", label: "材料", description: "材料盘点、事实抽取与冲突识别", nodes: ["inventory", "extract", "conflict"] },
  { key: "verify", label: "核验", description: "确定性指标计算与跨表交叉核验", nodes: ["metrics", "cross_check"] },
  { key: "risk", label: "风险", description: "定性风险、复核任务与正式风险", nodes: ["candidate", "tasks", "risk"] },
  { key: "report", label: "报告", description: "报告组装、引用校验与人工复核交付", nodes: ["draft", "verify", "review"] },
];

/**
 * 「人工复核」节点的语义：后端把它标成 completed 只代表**系统已把待复核事项交付出去**，
 * 不代表人工已经处理完。任何展示该节点完成态的地方都必须带上这句，否则会被读成"人工已处理"。
 */
export const REVIEW_NODE_NOTE = "该节点完成仅表示系统已交付待人工复核，不代表人工事项已经处理。";

/** 单个节点是否属于「已执行完」（供阶段完成数使用；警告/降级也算执行完，另行单独展示）。 */
export function nodeExecuted(state: NodeVisualState): boolean {
  return state === "completed" || state === "warning" || state === "degraded";
}

/**
 * 阶段状态由该阶段全部节点状态汇总，优先级：
 * failed > running > warning > degraded > completed > pending。
 * 关键点：**部分完成不冒充 completed**——只要还有 pending 就停在 pending，
 * 这样"未完成"信息不会被一个绿的阶段标题盖掉。
 */
export function aggregateStageState(states: NodeVisualState[]): NodeVisualState {
  if (states.some((state) => state === "failed")) return "failed";
  if (states.some((state) => state === "running")) return "running";
  if (states.some((state) => state === "warning")) return "warning";
  if (states.some((state) => state === "degraded")) return "degraded";
  if (states.length > 0 && states.every((state) => state === "completed")) return "completed";
  return "pending";
}

/** 阶段汇总视图模型。 */
export type StageView = {
  stage: AnalysisStage;
  state: NodeVisualState;
  /** 该阶段已完成节点数 / 总节点数。 */
  done: number;
  total: number;
};

export function buildStageViews(nodeByKey: Record<string, RunNode | undefined>, currentKey: string, running: boolean): StageView[] {
  return ANALYSIS_STAGES.map((stage) => {
    const states = stage.nodes.map((key) => nodeVisualState(nodeByKey[key]?.status, running && currentKey === key));
    return {
      stage,
      state: aggregateStageState(states),
      done: states.filter(nodeExecuted).length,
      total: stage.nodes.length,
    };
  });
}

/**
 * 摘要卡片的重点结果：优先异常，其次材料不足，最多 `limit` 条。
 * 只做"挑哪些行"的判断，不改任何统计值——卡片上的异常/材料不足/通过三个数
 * 一律来自 countStatuses（与后端结果逐条对应），不在这里二次推断。
 */
export function pickHighlightRows<T extends { status: CheckStatus }>(rows: T[], limit = 2): T[] {
  const warning = rows.filter((row) => row.status === "warning");
  const insufficient = rows.filter((row) => row.status === "insufficient");
  return [...warning, ...insufficient].slice(0, limit);
}
