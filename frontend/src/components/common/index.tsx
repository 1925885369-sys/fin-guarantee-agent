import type { ReactNode } from "react";
import { AlertTriangle, Inbox, LoaderCircle, X } from "lucide-react";

export function StatusBadge({ tone = "neutral", children }: { tone?: "success" | "info" | "warning" | "danger" | "neutral"; children: ReactNode }) {
  return <span className={`status-badge tone-${tone}`}>{children}</span>;
}

export function ReadinessBar({ label, value, detail, tone = "info" }: { label: string; value: number; detail: string; tone?: "success" | "info" | "warning" | "danger" }) {
  const normalized = Math.max(0, Math.min(100, Math.round(value <= 1 ? value * 100 : value)));
  return <div className="readiness-bar"><div><strong>{label}</strong><span>{detail}</span><b>{normalized}%</b></div><div><i className={`tone-${tone}`} style={{ width: `${normalized}%` }} /></div></div>;
}

export function SourceBadge({ source, tone = "info" }: { source: string; tone?: "success" | "info" | "warning" | "danger" | "neutral" }) {
  return <StatusBadge tone={tone}>{source}</StatusBadge>;
}

export function EmptyState({ title, description }: { title: string; description: string }) {
  return <div className="inline-empty"><Inbox size={22} /><strong>{title}</strong><span>{description}</span></div>;
}

export function LoadingState({ label = "正在载入" }: { label?: string }) {
  return <div className="inline-loading"><LoaderCircle className="spin" size={20} />{label}</div>;
}

export function ErrorState({ message, retry }: { message: string; retry?: () => void }) {
  return <div className="inline-error"><AlertTriangle size={15} /><span>{message}</span>{retry && <button className="text-button" onClick={retry}>重试</button>}</div>;
}

export function ConfirmDialog({ title, description, confirmLabel, onConfirm, onClose }: { title: string; description: string; confirmLabel: string; onConfirm: () => void; onClose: () => void }) {
  return <div className="modal-backdrop"><section className="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="confirm-title"><div className="modal-head"><div><span>请确认</span><h2 id="confirm-title">{title}</h2></div><button className="icon-button" onClick={onClose} aria-label="关闭"><X size={18} /></button></div><p>{description}</p><div className="modal-actions"><button className="secondary-button" onClick={onClose}>取消</button><button className="primary-button" onClick={onConfirm}>{confirmLabel}</button></div></section></div>;
}
