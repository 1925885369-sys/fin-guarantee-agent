import { AlertTriangle, Building2, CheckCircle2, ChevronRight, Clock3 } from "lucide-react";
import type { EntityProfileSummary } from "../../types";
import { StatusBadge } from "../common";

const labels = { ready: "可用于建项", stale: "资料待更新", conflicted: "存在冲突", incomplete: "档案不完整" } as const;
const tones = { ready: "success", stale: "warning", conflicted: "danger", incomplete: "neutral" } as const;

export function ProfileSummaryCard({ profile, selected, onSelect }: { profile: EntityProfileSummary; selected?: boolean; onSelect: () => void }) {
  const Icon = profile.status === "ready" ? CheckCircle2 : profile.status === "stale" ? Clock3 : profile.status === "conflicted" ? AlertTriangle : Building2;
  const completeness = profile.completeness <= 1 ? profile.completeness * 100 : profile.completeness;
  return <button className={`profile-summary-card ${selected ? "selected" : ""}`} onClick={onSelect}><Icon size={18} /><div><strong>{profile.name}</strong><span>{profile.unified_code || "统一社会信用代码待补充"}</span><small>{profile.latest_financial_period || "财务期间待补充"} · 被 {profile.project_count || 0} 个项目引用</small></div><div><StatusBadge tone={tones[profile.status]}>{labels[profile.status]}</StatusBadge><b>{Math.round(completeness || 0)}%</b></div><ChevronRight size={16} /></button>;
}
