import { AlertTriangle, CheckCircle2, ShieldCheck } from "lucide-react";
import { EmptyState, HelpTip, StatusBadge } from "../../../components/common";
import type { EntityProfileReadiness, ProfileRequirementStatus } from "../contracts";
import { RequirementStatusBadge } from "./profilePresentation";

const GATES: Array<{ key: "ready_for_project" | "ready_for_agent" | "complete_due_diligence_ready"; label: string; pass: string; fail: string }> = [
  { key: "ready_for_project", label: "创建项目", pass: "允许", fail: "存在阻塞" },
  { key: "ready_for_agent", label: "启动分析", pass: "允许", fail: "暂不可用" },
  { key: "complete_due_diligence_ready", label: "完整尽调", pass: "材料满足", fail: "尚未满足" },
];

/**
 * 资料状态：集中展示建项、启动分析、完整尽调三个条件及各自的阻塞原因。
 * 整体完整度只在标题区摘要行出现一次，这里不重复；模块计分明细默认折叠。
 */
export function ProfileReadinessPanel({ readiness }: { readiness: EntityProfileReadiness | null }) {
  if (!readiness) return <EmptyState title="尚无资料检查结果" description="后端完成主体资料检查后，会在这里展示建项、启动分析与完整尽调的门禁条件。" />;
  const vetoes = readiness.hard_vetoes || [];
  const reasons = readiness.blocking_reasons || [];
  return <div className="ep-readiness-stack">
    <section className="ep-panel">
      <header className="ep-panel-heading"><div><h3>使用门禁<HelpTip label="使用门禁说明">建项允许保留信息缺口；启动分析只在关键主体资料可用时放行；完整尽调要求全部核心资料满足。</HelpTip></h3></div></header>
      <div className="ep-gate-grid">
        {GATES.map((gate) => {
          const passed = Boolean(readiness[gate.key]);
          return <div key={gate.key}>
            <span>{gate.label}</span>
            <strong>{passed ? gate.pass : gate.fail}</strong>
            <StatusBadge tone={passed ? "success" : gate.key === "complete_due_diligence_ready" ? "warning" : "danger"}>
              {passed ? "已就绪" : "未就绪"}
            </StatusBadge>
          </div>;
        })}
      </div>
      {(!!vetoes.length || !!reasons.length) && <div className="ep-gate-reasons">
        {!!reasons.length && <p className="ep-gate-reason-lead">阻塞原因：{reasons.join("；")}</p>}
        {vetoes.map((item) => <div className="ep-blocker-row" key={item.key}><AlertTriangle size={17} /><div><strong>{item.label}</strong><p>{item.reason}</p></div></div>)}
      </div>}
      {!vetoes.length && !reasons.length && <div className="ep-gap-empty ep-gap-clear"><CheckCircle2 size={18} /><span>三个条件全部满足，当前没有阻塞原因。</span></div>}
    </section>

    <section className="ep-panel">
      <header className="ep-panel-heading"><div><h3>待补资料包<HelpTip label="待补资料包说明">按同一材料根因聚合展示，不把同一个缺口拆成大量人工待办。</HelpTip></h3></div></header>
      {!readiness.requirements?.length ? <div className="ep-gap-empty ep-gap-clear"><CheckCircle2 size={18} /><span>当前没有资料包级缺口</span></div>
        : readiness.requirements.map((item) => <details className="ep-requirement" key={item.key} open={item.status === "blocking"}>
          <summary>
            <div>{item.status === "blocking" ? <AlertTriangle size={17} /> : <CheckCircle2 size={17} />}<span><strong>{item.label}</strong><small>{item.impact}</small></span></div>
            <RequirementStatusBadge status={item.status} />
          </summary>
          <div className="ep-requirement-detail">
            {!!item.missing_fact_keys.length && <p><b>缺失字段</b>{item.missing_fact_keys.join("、")}</p>}
            {!!item.stale_fact_keys.length && <p><b>过期字段</b>{item.stale_fact_keys.join("、")}</p>}
            {!!item.conflicted_fact_keys.length && <p><b>冲突字段</b>{item.conflicted_fact_keys.join("、")}</p>}
            <p><b>阻塞级别</b>{item.blocking_level === "blocking" ? "阻塞" : "非阻塞"}</p>
          </div>
        </details>)}
    </section>

    <details className="ep-panel ep-collapsible">
      <summary><span><strong>模块计分明细</strong><small>{readiness.module_scores?.length || 0} 个资料模块，权重用于衡量主体档案是否支持本次债券担保分析</small></span><RequirementStatusBadge status={(readiness.module_scores?.some((item) => item.status === "blocking") ? "blocking" : readiness.module_scores?.some((item) => item.status === "warning") ? "warning" : "ready") as ProfileRequirementStatus} /></summary>
      <div className="ep-module-grid">{(readiness.module_scores || []).map((item) => <div key={item.key}>
        <span>{item.label}<small>权重 {Math.round(item.weight * 100)}%</small></span>
        <strong>{Math.round((item.score <= 1 ? item.score : item.score / 100) * 100)}%</strong>
        <i><b style={{ width: `${Math.max(0, Math.min(100, item.score <= 1 ? item.score * 100 : item.score))}%` }} /></i>
      </div>)}</div>
    </details>

    {!!readiness.notices?.length && <section className="ep-panel ep-notice-panel"><header className="ep-panel-heading"><div><h3>信息提示</h3><p>提示不计入项目人工待办。</p></div><ShieldCheck size={18} /></header>{readiness.notices.map((notice) => <p key={notice}><ShieldCheck size={16} />{notice}</p>)}</section>}
  </div>;
}
