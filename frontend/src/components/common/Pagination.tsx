import { ArrowDown, ArrowUp } from "lucide-react";

/** 每页条数：只允许 10 或 50，默认 10（组件卸载或刷新后回到默认值）。 */
export type PageSize = 10 | 50;
export const DEFAULT_PAGE_SIZE: PageSize = 10;
export const PAGE_SIZE_OPTIONS: PageSize[] = [10, 50];

/**
 * 表格分页工具条：左侧每页条数下拉，右侧「当前页 / 总页数」与上一页、下一页。
 * 主体档案库（前端已加载数据分页）与项目驾驶舱（筛选结果分页）共用同一组件。
 */
export function Pagination({ page, pageCount, pageSize, total, onPage, onPageSize }: {
  page: number;
  pageCount: number;
  pageSize: PageSize;
  total: number;
  onPage: (next: number) => void;
  onPageSize: (next: PageSize) => void;
}) {
  return <div className="pagination">
    <label className="pagination-size">
      <select aria-label="每页条数" value={pageSize} onChange={(event) => onPageSize(Number(event.target.value) as PageSize)}>
        {PAGE_SIZE_OPTIONS.map((option) => <option value={option} key={option}>每页 {option} 条</option>)}
      </select>
    </label>
    <div className="pagination-nav">
      <button className="icon-button" disabled={page <= 1} onClick={() => onPage(page - 1)} title="上一页" aria-label="上一页"><ArrowDown size={15} className="rotate-90" /></button>
      <span>第 {page} / {pageCount} 页 · 共 {total} 条</span>
      <button className="icon-button" disabled={page >= pageCount} onClick={() => onPage(page + 1)} title="下一页" aria-label="下一页"><ArrowUp size={15} className="rotate-90" /></button>
    </div>
  </div>;
}
