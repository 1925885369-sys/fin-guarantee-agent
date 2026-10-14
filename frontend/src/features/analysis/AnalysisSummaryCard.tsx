import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { AlertTriangle, ArrowRight, CheckCircle2, ListChecks, PackageSearch } from "lucide-react";
import { HelpTip } from "../../components/common";
import { STATUS_META, pickHighlightRows } from "./analysisUtils";
import type { AnalysisResultRow, StatusCounts } from "./analysisUtils";

function RowIcon({ status }: { status: AnalysisResultRow["status"] }) {
  if (status === "warning") return <AlertTriangle size={13} />;
  if (status === "passed") return <CheckCircle2 size={13} />;
  return <PackageSearch size={13} />;
}

/**
 * 核验模块摘要卡片：模块名 + 总数 + 异常/材料不足/通过三个统计值 +
 * 最多两条重点结果（优先异常，其次材料不足）+ 明确的「详情」入口。
 * 统计值一律来自 `counts`（由 countStatuses 从后端结果算出），卡片自己不推断任何结论。
 */
export function AnalysisSummaryCard({ id, title, description, help, icon: Icon, counts, rows, onOpenDetail, detailLabelId }: {
  id: string;
  title: string;
  description: string;
  help: ReactNode;
  icon: LucideIcon;
  counts: StatusCounts;
  rows: AnalysisResultRow[];
  onOpenDetail: () => void;
  /** 「详情」按钮的可访问名 id，供弹窗 aria-labelledby 回指。 */
  detailLabelId: string;
}) {
  const highlights = pickHighlightRows(rows, 2);
  return <section className="analysis-card" aria-labelledby={`analysis-card-title-${id}`}>
    <header className="analysis-card-head">
      <Icon size={18} className="analysis-card-icon" aria-hidden="true" />
      <div className="analysis-card-label">
        <h2 id={`analysis-card-title-${id}`}>{title}</h2>
        <p>{description}</p>
        <HelpTip label={`${title}说明`}>{help}</HelpTip>
      </div>
      <span className="analysis-card-total"><ListChecks size={13} />共 {counts.total}</span>
    </header>

    <div className="analysis-card-stats">
      <span className={`acs-chip ${counts.warning ? "is-warning" : "is-muted"}`}><AlertTriangle size={14} /><span>异常</span><strong>{counts.warning}</strong></span>
      <span className={`acs-chip ${counts.insufficient ? "is-insufficient" : "is-muted"}`}><PackageSearch size={14} /><span>材料不足</span><strong>{counts.insufficient}</strong></span>
      <span className={`acs-chip ${counts.passed ? "is-passed" : "is-muted"}`}><CheckCircle2 size={14} /><span>通过</span><strong>{counts.passed}</strong></span>
    </div>

    <div className="analysis-card-highlights">
      {highlights.length === 0
        ? <p className="analysis-card-none">没有需要关注的异常或材料不足项。</p>
        : highlights.map((row) => {
            const meta = STATUS_META[row.status];
            return <div className={`ach-row ${meta.className}`} key={row.id}>
              <span className={`arr-status ${meta.className}`}><RowIcon status={row.status} />{meta.label}</span>
              <div className="ach-main"><strong>{row.title}</strong><small>{row.summary || "—"}</small></div>
              <small className="ach-meta">{row.meta}</small>
            </div>;
          })}
    </div>

    <footer className="analysis-card-foot">
      <button type="button" className="secondary-button analysis-card-action" id={detailLabelId} onClick={onOpenDetail}>
        详情<ArrowRight size={15} />
      </button>
    </footer>
  </section>;
}
