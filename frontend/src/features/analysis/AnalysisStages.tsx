import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle, Check, ChevronDown, CircleDashed, FileText, LoaderCircle, MinusCircle, PackageSearch,
  ShieldCheck, Table2, X,
} from "lucide-react";
import type { RunNode } from "../../types";
import {
  NODE_LABELS, NODE_STATE_LABEL, REVIEW_NODE_NOTE, buildStageViews, nodeVisualState,
} from "./analysisUtils";
import type { NodeVisualState, StageKey } from "./analysisUtils";

const STAGE_ICONS: Record<StageKey, typeof FileText> = {
  materials: PackageSearch,
  verify: Table2,
  risk: AlertTriangle,
  report: FileText,
};

/** 阶段图标：形状已区分状态，颜色只是第二重表达（不靠颜色单独传达）。 */
function StageStateIcon({ state }: { state: NodeVisualState }) {
  if (state === "completed") return <Check size={16} />;
  if (state === "running") return <LoaderCircle className="spin" size={16} />;
  if (state === "warning") return <AlertTriangle size={16} />;
  if (state === "degraded") return <MinusCircle size={16} />;
  if (state === "failed") return <X size={16} />;
  return <CircleDashed size={16} />;
}

/**
 * 四阶段工作流：材料 / 核验 / 风险 / 报告。
 * - 桌面四列均分，窄屏两列；每阶段显示图标、名称、状态与「已执行/总数」。
 * - 完成阶段**只给浅绿图标 + 正常深色文字**（弱化已完成），当前阶段才用蓝色强调，
 *   异常/降级/失败各自单独配色——避免"运行完成"被读成"全部通过"。
 * - 点击阶段在其下方展开该阶段的节点列表与摘要；默认全部收起，
 *   且**进度刷新不覆盖用户选择**（用户点开/收起过就不再自动变）。
 */
export function AnalysisStages({ nodes, currentKey, running, resetKey }: {
  nodes: RunNode[];
  currentKey: string;
  running: boolean;
  /** `${projectId}:${runId}` —— 切换项目或运行时清空展开状态。
   *  Checks 在切换项目时不会重新挂载（同一组件实例），所以重置必须靠这个 key 显式触发。 */
  resetKey: string;
}) {
  const [openStage, setOpenStage] = useState<StageKey | null>(null);
  const [touched, setTouched] = useState(false);

  // 切换项目 / 切换运行：回到"默认全部收起"。
  useEffect(() => { setTouched(false); setOpenStage(null); }, [resetKey]);

  const nodeByKey = useMemo(() => {
    const map: Record<string, RunNode | undefined> = {};
    nodes.forEach((node) => { map[node.node_key] = node; });
    return map;
  }, [nodes]);

  const stages = useMemo(() => buildStageViews(nodeByKey, currentKey, running), [nodeByKey, currentKey, running]);

  // 默认收起；用户一旦手动展开过，运行进度的变化不再改他的选择。
  useEffect(() => { if (!touched) setOpenStage(null); }, [touched, currentKey]);

  const opened = stages.find((item) => item.stage.key === openStage);

  const toggle = (key: StageKey) => {
    setTouched(true);
    setOpenStage((current) => (current === key ? null : key));
  };

  return <div className="analysis-stages-wrap">
    <div className="analysis-stages" role="group" aria-label="四阶段工作流">
      {stages.map(({ stage, state, done, total }) => {
        const Icon = STAGE_ICONS[stage.key];
        const expanded = openStage === stage.key;
        return <button
          key={stage.key}
          type="button"
          className={`analysis-stage stage-${state} ${expanded ? "is-open" : ""}`}
          aria-expanded={expanded}
          onClick={() => toggle(stage.key)}
          title={stage.description}
        >
          <span className="analysis-stage-icon"><StageStateIcon state={state} /></span>
          <span className="analysis-stage-body">
            <span className="analysis-stage-name"><Icon size={14} aria-hidden="true" />{stage.label}</span>
            <span className="analysis-stage-meta">
              <span className={`analysis-stage-state state-${state}`}>{NODE_STATE_LABEL[state]}</span>
              <span className="analysis-stage-count">{done}/{total}</span>
            </span>
          </span>
          <ChevronDown size={15} className={`analysis-stage-chevron ${expanded ? "is-up" : ""}`} />
        </button>;
      })}
    </div>

    {opened && <div className="analysis-stage-panel">
      <div className="analysis-stage-panel-head">
        <strong>{opened.stage.label}</strong>
        <span>{opened.stage.description}</span>
      </div>
      <ul className="analysis-stage-nodes">
        {opened.stage.nodes.map((key, index) => {
          const node = nodeByKey[key];
          const state = nodeVisualState(node?.status, running && currentKey === key);
          const summary = node?.error || node?.output_summary || (state === "pending" ? "等待执行" : "该节点尚未产出摘要");
          return <li className={`analysis-stage-node node-${state}`} key={key}>
            <span className="analysis-stage-node-icon"><StageStateIcon state={state} /></span>
            <div className="analysis-stage-node-main">
              <div className="analysis-stage-node-head">
                <strong>{NODE_LABELS[key]}</strong>
                <span className={`analysis-node-chip node-${state}`}>{NODE_STATE_LABEL[state]}</span>
                <small>第 {index + 1} 步</small>
              </div>
              <p className={node?.error ? "is-error" : undefined}>{summary}</p>
              {key === "review" && <p className="analysis-stage-node-note"><ShieldCheck size={13} />{REVIEW_NODE_NOTE}</p>}
            </div>
          </li>;
        })}
      </ul>
    </div>}
  </div>;
}
