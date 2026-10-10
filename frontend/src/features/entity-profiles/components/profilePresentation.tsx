import type { ReactNode } from "react";
import { StatusBadge } from "../../../components/common";
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

export function ProfilePanelHeading({ title, description, action }: { title: string; description?: string; action?: ReactNode }) {
  return <header className="ep-panel-heading"><div><h3>{title}</h3>{description && <p>{description}</p>}</div>{action}</header>;
}

export function displayFactValue(value: number | null, textValue: string, unit: string) {
  if (value != null) return `${value.toLocaleString()}${unit ? ` ${unit}` : ""}`;
  return textValue || "材料不足";
}

export function percent(value: number) {
  return Math.round((value <= 1 ? value : value / 100) * 100);
}
