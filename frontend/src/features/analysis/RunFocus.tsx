import { useEffect, useState } from "react";
import {
  AlertOctagon, AlertTriangle, Check, CheckCircle2, ChevronDown, CircleDashed, LoaderCircle,
  MinusCircle, RotateCcw, SearchCheck, Settings2, ShieldCheck,
} from "lucide-react";
import type { Run, RunNode } from "../../types";
import { ANALYSIS_STAGES, NODE_KEYS, NODE_LABELS, runStatusVisual } from "./analysisUtils";
import type { AnalysisVerdict } from "./analysisUtils";
import { AnalysisStages } from "./AnalysisStages";

function RunStatusIcon({ status }: { status: string }) {
  if (status === "completed") return <Check size={14} />;
  if (status === "running" || status === "queued") return <LoaderCircle className="spin" size={14} />;
  if (status === "failed") return <AlertOctagon size={14} />;
  if (status === "model_required") return <Settings2 size={14} />;
  return <CircleDashed size={14} />;
}

/** 执行状态 chip：只表达"节点执行到哪一步"，与校验结论 chip 语义分离。 */
function VerdictChip({ verdict }: { verdict: AnalysisVerdict }) {
  if (verdict.kind === "warning") {
    return <span className="analysis-run-verdict is-warning"><AlertTriangle size={14} />校验 {verdict.warning} 项异常 · {verdict.insufficient} 项材料不足</span>;
  }
  if (verdict.kind === "passed") {
    return <span className="analysis-run-verdict is-passed"><ShieldCheck size={14} />校验全部通过</span>;
  }
  if (verdict.kind === "neutral") {
    return <span className="analysis-run-verdict is-empty"><MinusCircle size={14} />校验无异常 · {verdict.insufficient} 项材料不足</span>;
  }
  return <span className="analysis-run-verdict is-empty"><MinusCircle size={14} />暂无校验结果</span>;
}

/** 结论带：只承载"运行完成 ≠ 校验通过"这一句判断。
 *  具体条数已在上面的结论 chip 里给出，这里不再重复计数（合并重复提醒）。 */
function RunResultBand({ verdict }: { verdict: AnalysisVerdict }) {
  if (verdict.kind === "warning") {
    return <div className="analysis-run-result is-warning"><AlertTriangle size={16} /><span>运行已完成，但校验存在异常 —— 运行完成不等于校验通过，请查看下方两张核验卡片。</span></div>;
  }
  if (verdict.kind === "passed") {
    return <div className="analysis-run-result is-passed"><ShieldCheck size={16} /><span>运行已完成，全部校验通过。</span></div>;
  }
  return null;
}

/**
 * Layer 1 运行重点：
 * - 一行状态栏：运行状态、耗时、成本、Token（输入/输出 Token 与长运行消息收进"运行详情"）。
 * - 一行进度：当前节点（含所属阶段）+ 进度条 + 百分比。
 * - 四阶段工作流（材料 / 核验 / 风险 / 报告），点击阶段在下方展开该阶段节点与摘要。
 */
export function RunFocus({ run, nodes, projectId, formatDateTime, onRetry, verdict }: {
  run: Run | null;
  nodes: RunNode[];
  projectId: string;
  formatDateTime: (value?: string | null) => string;
  onRetry?: () => void;
  verdict: AnalysisVerdict;
}) {
  const [detailOpen, setDetailOpen] = useState(false);
  const resetKey = `${projectId}:${run?.id ?? ""}`;

  // 切换项目 / 切换运行：收起运行详情。
  useEffect(() => { setDetailOpen(false); }, [resetKey]);

  if (!run) {
    return <section className="analysis-runfocus empty-run"><SearchCheck size={22} /><div><strong>尚未运行 Agent</strong><span>核心材料齐备后启动分析，系统将依次完成事实、规则、风险和报告节点。</span></div></section>;
  }

  const status = runStatusVisual(run.status);
  const running = run.status === "running" || run.status === "queued";
  const currentLabel = NODE_LABELS[run.current_node] || "—";
  const currentIndex = NODE_KEYS.indexOf(run.current_node);
  const currentStage = ANALYSIS_STAGES.find((stage) => stage.nodes.includes(run.current_node));
  const tokenTotal = (run.input_tokens || 0) + (run.output_tokens || 0);
  const progress = Math.max(0, Math.min(100, Math.round(run.progress || 0)));

  return <section className={`analysis-runfocus run-${run.status}`}>
    <div className="analysis-run-bar">
      <span className="analysis-run-label">最近一次运行</span>
      <span className={`analysis-run-status ${status.className}`}><RunStatusIcon status={run.status} />{status.label}</span>
      <VerdictChip verdict={verdict} />
      <div className="analysis-run-facts">
        <div className="analysis-run-fact"><span>耗时</span><strong>{run.latency_ms ? `${(run.latency_ms / 1000).toFixed(1)} 秒` : "—"}</strong></div>
        <div className="analysis-run-fact"><span>成本</span><strong>¥{(run.cost || 0).toFixed(3)}</strong></div>
        <div className="analysis-run-fact"><span>Token</span><strong>{tokenTotal.toLocaleString()}</strong></div>
      </div>
      <button className="secondary-button analysis-run-toggle" type="button" aria-expanded={detailOpen} onClick={() => setDetailOpen((value) => !value)}>
        {detailOpen ? "收起详情" : "运行详情"}
        <ChevronDown size={15} className={detailOpen ? "analysis-chevron-up" : undefined} />
      </button>
    </div>

    {detailOpen && <div className="analysis-run-detail">
      <div className="analysis-run-detail-item"><span>输入 Token</span><strong>{(run.input_tokens || 0).toLocaleString()}</strong></div>
      <div className="analysis-run-detail-item"><span>输出 Token</span><strong>{(run.output_tokens || 0).toLocaleString()}</strong></div>
      <div className="analysis-run-detail-item"><span>模型成本</span><strong>¥{(run.cost || 0).toFixed(3)}</strong></div>
      <div className="analysis-run-detail-item"><span>开始时间</span><strong>{formatDateTime(run.started_at)}</strong></div>
      <div className="analysis-run-detail-item"><span>结束时间</span><strong>{formatDateTime(run.finished_at)}</strong></div>
      <div className="analysis-run-detail-item is-wide"><span>运行消息</span><strong>{run.message || "—"}</strong></div>
    </div>}

    {run.status === "failed"
      ? <div className="analysis-run-error"><AlertTriangle size={16} /><span>运行失败：{run.error || run.message || "未提供失败原因"}</span>{onRetry && <button className="text-button" type="button" onClick={onRetry}><RotateCcw size={15} />重新运行</button>}</div>
      : <div className="analysis-run-progress">
          <span className="analysis-run-now">
            {running ? <LoaderCircle className="spin" size={15} />
              : run.status === "completed" ? <CheckCircle2 size={15} />
              : <AlertTriangle size={15} />}
            <span>当前节点：<strong>{currentLabel}</strong>{currentStage ? ` · ${currentStage.label}阶段` : ""}（{currentIndex >= 0 ? currentIndex + 1 : "—"}/{NODE_KEYS.length}）</span>
          </span>
          <span className="analysis-progress-track"><span className="analysis-progress-fill" style={{ width: `${progress}%` }} /></span>
          <span className="analysis-progress-percent">{progress}%</span>
        </div>}

    {run.status === "completed" && <RunResultBand verdict={verdict} />}

    <AnalysisStages nodes={nodes} currentKey={run.current_node} running={running} resetKey={resetKey} />
  </section>;
}
