import { AlertTriangle, ArrowRight, CheckCircle2, CircleHelp, ShieldCheck } from "lucide-react";
import type { EntityProfileReadiness } from "../contracts";
import { ProfilePanelHeading, RequirementStatusBadge } from "./profilePresentation";

const MAX_GAPS = 3;

/**
 * 概览里的资料缺口：阻塞项在先、需关注项在后，最多三项；其余去「资料状态」看。
 * 没有缺口与「尚未取得检查结果」是两种不同的空状态，不混用同一句文案。
 */
export function ProfileGapsPanel({ readiness, onOpenAll }: { readiness: EntityProfileReadiness | null; onOpenAll: () => void }) {
  if (!readiness) {
    return <section className="ep-panel">
      <ProfilePanelHeading title="资料缺口" description="尚未取得资料检查结果" action={<ShieldCheck size={18} />} />
      <div className="ep-gap-empty"><CircleHelp size={18} /><span>后端完成主体资料检查后，这里会列出阻塞项与需关注项。</span></div>
    </section>;
  }
  const gaps = (readiness.requirements || []).filter((item) => item.status !== "ready");
  const ordered = [...gaps.filter((item) => item.status === "blocking"), ...gaps.filter((item) => item.status === "warning")];
  const shown = ordered.slice(0, MAX_GAPS);
  const blockingCount = ordered.filter((item) => item.status === "blocking").length;
  return <section className="ep-panel">
    <ProfilePanelHeading
      title="资料缺口"
      description={ordered.length ? `阻塞 ${blockingCount} 项 · 需关注 ${ordered.length - blockingCount} 项` : "当前没有资料缺口"}
      action={<ShieldCheck size={18} />}
    />
    {!ordered.length ? <div className="ep-gap-empty ep-gap-clear"><CheckCircle2 size={18} /><span>当前没有资料缺口。</span></div> : <>
      <div className="ep-gap-list">{shown.map((item) => <div className={`ep-gap-row ${item.status}`} key={item.key}>
        {item.status === "blocking" ? <AlertTriangle size={16} /> : <CircleHelp size={16} />}
        <div><strong>{item.label}</strong><small>{item.impact}</small></div>
        <RequirementStatusBadge status={item.status} />
      </div>)}</div>
      <footer className="ep-panel-foot">
        <span>{ordered.length > shown.length ? `另有 ${ordered.length - shown.length} 项未在此显示` : `共 ${ordered.length} 项资料缺口`}</span>
        <button className="text-button" onClick={onOpenAll}>查看全部<ArrowRight size={13} /></button>
      </footer>
    </>}
  </section>;
}
