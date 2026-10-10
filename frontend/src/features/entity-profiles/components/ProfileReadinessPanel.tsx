import { AlertTriangle, CheckCircle2, CircleHelp, ShieldCheck } from "lucide-react";
import { EmptyState, ReadinessBar, StatusBadge } from "../../../components/common";
import type { EntityProfileReadiness } from "../contracts";
import { ProfilePanelHeading, RequirementStatusBadge } from "./profilePresentation";

export function ProfileReadinessPanel({ readiness }: { readiness: EntityProfileReadiness | null }) {
  if (!readiness) return <EmptyState title="尚无就绪度结果" description="后端完成主体资料检查后，会在这里展示建项、Agent 与完整尽调门禁。" />;
  const blockers = readiness.hard_vetoes || [];
  return <div className="ep-readiness-stack">
    <section className="ep-panel">
      <ProfilePanelHeading title="使用门禁" description="建项允许保留信息缺口；Agent 仅在关键主体资料可用时启动。" />
      <div className="ep-gate-grid">
        <div><span>创建项目</span><strong>{readiness.ready_for_project ? "允许" : "存在阻塞"}</strong><StatusBadge tone={readiness.ready_for_project ? "success" : "danger"}>{readiness.ready_for_project ? "可建项" : "需处理"}</StatusBadge></div>
        <div><span>启动 Agent</span><strong>{readiness.ready_for_agent ? "允许" : "暂不可用"}</strong><StatusBadge tone={readiness.ready_for_agent ? "success" : "danger"}>{readiness.ready_for_agent ? "已就绪" : "硬门禁"}</StatusBadge></div>
        <div><span>完整尽调</span><strong>{readiness.complete_due_diligence_ready ? "材料满足" : "尚未满足"}</strong><StatusBadge tone={readiness.complete_due_diligence_ready ? "success" : "warning"}>{readiness.complete_due_diligence_ready ? "已就绪" : "待完善"}</StatusBadge></div>
      </div>
      <ReadinessBar label="主体档案完整度" value={readiness.completeness} detail={`${readiness.module_scores?.length || 0} 个资料模块已计分`} tone={readiness.ready_for_agent ? "success" : "warning"} />
    </section>

    {!!blockers.length && <section className="ep-panel ep-blocker-panel">
      <ProfilePanelHeading title="Agent 硬门禁" description="以下项目缺失、过期、冲突或材料不足时，不允许将数值用于确定性计算。" />
      {blockers.map((item) => <div className="ep-blocker-row" key={item.key}><AlertTriangle size={17} /><div><strong>{item.label}</strong><p>{item.reason}</p></div></div>)}
    </section>}

    <section className="ep-panel">
      <ProfilePanelHeading title="资料模块" description="模块权重用于衡量主体档案是否支持本次债券担保分析。" />
      <div className="ep-module-grid">{(readiness.module_scores || []).map((item) => <div key={item.key}>
        <span>{item.label}<small>权重 {Math.round(item.weight * 100)}%</small></span>
        <strong>{Math.round((item.score <= 1 ? item.score : item.score / 100) * 100)}%</strong>
        <i><b style={{ width: `${Math.max(0, Math.min(100, item.score <= 1 ? item.score * 100 : item.score))}%` }} /></i>
      </div>)}</div>
    </section>

    <section className="ep-panel">
      <ProfilePanelHeading title="待补资料包" description="按根因聚合展示，不把同一缺口拆成大量人工待办。" />
      {!readiness.requirements?.length ? <div className="ep-ready-empty"><CheckCircle2 size={18} />当前没有资料包级缺口</div> : readiness.requirements.map((item) => <details className="ep-requirement" key={item.key} open={item.status === "blocking"}>
        <summary><div>{item.status === "blocking" ? <AlertTriangle size={17} /> : <CircleHelp size={17} />}<span><strong>{item.label}</strong><small>{item.impact}</small></span></div><RequirementStatusBadge status={item.status} /></summary>
        <div className="ep-requirement-detail">
          {!!item.missing_fact_keys.length && <p><b>缺失字段</b>{item.missing_fact_keys.join("、")}</p>}
          {!!item.stale_fact_keys.length && <p><b>过期字段</b>{item.stale_fact_keys.join("、")}</p>}
          {!!item.conflicted_fact_keys.length && <p><b>冲突字段</b>{item.conflicted_fact_keys.join("、")}</p>}
          <p><b>阻塞级别</b>{item.blocking_level === "blocking" ? "阻塞" : "非阻塞"}</p>
        </div>
      </details>)}
    </section>

    {!!readiness.notices?.length && <section className="ep-panel ep-notice-panel"><ProfilePanelHeading title="信息提示" description="提示不计入项目人工待办。" />{readiness.notices.map((notice) => <p key={notice}><ShieldCheck size={16} />{notice}</p>)}</section>}
  </div>;
}
