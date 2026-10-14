import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import {
  AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, Filter, ListChecks, PackageSearch, X,
} from "lucide-react";
import { DEFAULT_PAGE_SIZE, Pagination } from "../../components/common";
import type { PageSize } from "../../components/common";
import { ANALYSIS_FILTERS, FILTER_LABELS, STATUS_META, filterByStatus } from "./analysisUtils";
import type { AnalysisFilter, AnalysisResultRow, StatusCounts } from "./analysisUtils";

function RowIcon({ status }: { status: AnalysisResultRow["status"] }) {
  if (status === "warning") return <AlertTriangle size={13} />;
  if (status === "passed") return <CheckCircle2 size={13} />;
  return <PackageSearch size={13} />;
}

/**
 * 核验详情弹窗：完整结果列表 + 状态筛选 + 分页 + 行内单项详情。
 * - 层级 z 36 / 遮罩 35，**低于证据抽屉 z 40**：弹窗内点证据时证据层正确覆盖。
 * - 证据层打开期间只由最上层处理键盘与焦点（`evidenceOpen` 为真时本弹窗的 Escape 放行）。
 * - 关闭时记录触发按钮并把焦点还回去；打开期间锁背景滚动。
 * - 筛选 / 页码 / 展开项在证据查看往返中保持不变（弹窗不卸载）。
 */
export function AnalysisDetailModal({
  open, onClose, id, title, description, icon: Icon, rows, counts, emptyText, emptyIcon: EmptyIcon,
  renderDetail, resetKey, evidenceOpen = false,
}: {
  open: boolean;
  onClose: () => void;
  id: string;
  title: string;
  description: string;
  icon: LucideIcon;
  rows: AnalysisResultRow[];
  counts: StatusCounts;
  emptyText: string;
  emptyIcon: LucideIcon;
  renderDetail: (row: AnalysisResultRow) => ReactNode;
  resetKey: string;
  evidenceOpen?: boolean;
}) {
  const [filter, setFilter] = useState<AnalysisFilter>("all");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState<PageSize>(DEFAULT_PAGE_SIZE);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const dialogRef = useRef<HTMLElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const lastTrigger = useRef<HTMLElement | null>(null);
  // 默认筛选只在"每次打开"时算一次：counts 会在数据刷新时变化，
  // 若把它放进依赖数组，刷新会把用户手动选的筛选悄悄冲掉。
  const countsRef = useRef(counts);
  countsRef.current = counts;

  useEffect(() => {
    if (!open) { setFilter("all"); setPage(1); setExpandedId(null); return; }
    setFilter(countsRef.current.warning > 0 ? "warning" : "all");
    setPage(1);
    setExpandedId(null);
  }, [open, resetKey]);

  // 打开：记录触发元素、锁背景滚动、把焦点移入弹窗；关闭：复位并归还焦点。
  useEffect(() => {
    if (!open) return;
    lastTrigger.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const frame = window.requestAnimationFrame(() => closeRef.current?.focus());
    return () => {
      window.cancelAnimationFrame(frame);
      document.body.style.overflow = previousOverflow;
      const trigger = lastTrigger.current;
      if (trigger && document.contains(trigger)) window.requestAnimationFrame(() => trigger.focus());
    };
  }, [open]);

  // 键盘：Escape 关弹窗（证据层在上时放行）；Tab 在弹窗内循环。
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        if (evidenceOpen) return; // 最上层是证据抽屉，交给它关自己
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const box = dialogRef.current;
      if (!box) return;
      const focusable = Array.from(box.querySelectorAll<HTMLElement>('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'))
        .filter((element) => !element.hasAttribute("disabled") && element.offsetParent !== null);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [open, onClose, evidenceOpen]);

  const filtered = useMemo(() => filterByStatus(rows, filter), [rows, filter]);
  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
  const currentPage = Math.min(page, pageCount);
  const visible = filtered.slice((currentPage - 1) * pageSize, currentPage * pageSize);
  const countFor = (key: AnalysisFilter) => (key === "all" ? counts.total : counts[key]);

  if (!open) return null;

  return <>
    <div className="analysis-modal-mask" role="presentation" onClick={onClose} />
    <section ref={dialogRef} className="analysis-modal" role="dialog" aria-modal="true" aria-labelledby={`analysis-modal-title-${id}`}>
      <header className="analysis-modal-head">
        <Icon size={18} className="analysis-modal-icon" aria-hidden="true" />
        <div className="analysis-modal-label">
          <h2 id={`analysis-modal-title-${id}`}>{title}</h2>
          <p>{description}</p>
        </div>
        <span className="analysis-card-total"><ListChecks size={13} />共 {counts.total}</span>
        <button ref={closeRef} type="button" className="icon-button" title="关闭详情" aria-label="关闭详情" onClick={onClose}><X size={18} /></button>
      </header>

      <div className="analysis-modal-toolbar">
        <div className="analysis-modal-filter segmented" role="group" aria-label={`${title}筛选`}>
          {ANALYSIS_FILTERS.map((key) => <button key={key} type="button" className={filter === key ? "active" : ""} aria-pressed={filter === key} onClick={() => { setFilter(key); setPage(1); }}>{FILTER_LABELS[key]} {countFor(key)}</button>)}
        </div>
        <label className="analysis-filter-select">
          <select aria-label={`${title}筛选`} value={filter} onChange={(event) => { setFilter(event.target.value as AnalysisFilter); setPage(1); }}>
            {ANALYSIS_FILTERS.map((key) => <option key={key} value={key}>{FILTER_LABELS[key]} {countFor(key)}</option>)}
          </select>
        </label>
      </div>

      <div className="analysis-modal-body">
        {rows.length === 0
          ? <div className="analysis-empty"><EmptyIcon size={20} /><span>{emptyText}</span></div>
          : filtered.length === 0
            ? <div className="analysis-empty"><Filter size={18} /><div><strong>当前筛选下没有{FILTER_LABELS[filter]}的结果</strong></div><button type="button" className="text-button" onClick={() => { setFilter("all"); setPage(1); }}>查看全部</button></div>
            : visible.map((row) => {
                const meta = STATUS_META[row.status];
                const expanded = expandedId === row.id;
                return <div className={`analysis-modal-row ${expanded ? "is-expanded" : ""}`} key={row.id} data-row-status={row.status}>
                  <button type="button" className="analysis-modal-row-head" aria-expanded={expanded} onClick={() => setExpandedId(expanded ? null : row.id)}>
                    <span className={`arr-status ${meta.className}`}><RowIcon status={row.status} />{meta.label}</span>
                    <span className="arr-main"><strong>{row.title}</strong><small>{row.summary || "—"}</small></span>
                    <span className="arr-meta">{row.meta}{expanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}</span>
                  </button>
                  {expanded && <div className="arr-detail">{renderDetail(row)}</div>}
                </div>;
              })}
      </div>

      {filtered.length > 0 && <footer className="analysis-modal-foot">
        <Pagination page={currentPage} pageCount={pageCount} pageSize={pageSize} total={filtered.length}
          onPage={setPage} onPageSize={(next) => { setPage(1); setPageSize(next); }} />
      </footer>}
    </section>
  </>;
}
