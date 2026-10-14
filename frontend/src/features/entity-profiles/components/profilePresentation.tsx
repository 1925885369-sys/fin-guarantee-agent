import type { ReactNode } from "react";
import { HelpTip, StatusBadge } from "../../../components/common";
import type { EntityProfileStatus, ProfileFactStatus, ProfileRequirementStatus } from "../contracts";

const factLabels: Record<ProfileFactStatus, string> = {
  confirmed: "",
  single_source: "单一来源，尚未独立验证",
  stale: "资料已过期",
  conflicted: "存在冲突",
  insufficient: "材料不足",
};

const profileLabels: Record<EntityProfileStatus, string> = {
  ready: "可用于建项",
  stale: "资料待更新",
  conflicted: "存在冲突",
  incomplete: "档案不完整",
};

const requirementLabels: Record<ProfileRequirementStatus, string> = {
  ready: "已就绪",
  warning: "需要关注",
  blocking: "阻塞 Agent",
};

function tone(status: ProfileFactStatus | EntityProfileStatus | ProfileRequirementStatus) {
  if (status === "confirmed" || status === "ready") return "success" as const;
  if (status === "single_source") return "info" as const;
  if (status === "conflicted" || status === "blocking") return "danger" as const;
  if (status === "stale" || status === "warning") return "warning" as const;
  return "neutral" as const;
}

export function FactStatusBadge({ status }: { status: ProfileFactStatus }) {
  if (status === "confirmed") return null;
  return <StatusBadge tone={tone(status)}>{factLabels[status]}</StatusBadge>;
}

export function ProfileStatusBadge({ status }: { status: EntityProfileStatus }) {
  return <StatusBadge tone={tone(status)}>{profileLabels[status]}</StatusBadge>;
}

export function RequirementStatusBadge({ status }: { status: ProfileRequirementStatus }) {
  return <StatusBadge tone={tone(status)}>{requirementLabels[status]}</StatusBadge>;
}

/**
 * 区块标题。`description` 只放**关键状态信息**（时间、口径、数量），
 * 原理、计分方式、脱敏规则一类解释放 `help`，收进标题旁的说明按钮。
 */
export function ProfilePanelHeading({ title, description, help, action }: { title: string; description?: string; help?: ReactNode; action?: ReactNode }) {
  return <header className="ep-panel-heading"><div><h3>{title}{help && <HelpTip label={`${title}说明`}>{help}</HelpTip>}</h3>{description && <p>{description}</p>}</div>{action}</header>;
}

/**
 * 缺失值统一文案。缺失**不显示为零**，也不写成「材料不足」——后者是事实状态，
 * 值是值、状态是状态，两者混用会让读者以为已经取到材料只是内容不足。
 */
export const MISSING_VALUE = "待补充";

export function displayFactValue(value: number | null, textValue: string, unit: string) {
  if (value != null) return `${value.toLocaleString()}${unit ? ` ${unit}` : ""}`;
  return textValue || MISSING_VALUE;
}

/** 事实的来源一行说明：来源标签 + 页码；没有来源时明确写「来源待补充」。 */
export function factSourceLabel(fact: { source_label?: string | null; page?: number | null } | undefined) {
  if (!fact) return MISSING_VALUE;
  const label = fact.source_label || "来源待补充";
  return fact.page ? `${label} · 第${fact.page}页` : label;
}

export function percent(value: number) {
  return Math.round((value <= 1 ? value : value / 100) * 100);
}
