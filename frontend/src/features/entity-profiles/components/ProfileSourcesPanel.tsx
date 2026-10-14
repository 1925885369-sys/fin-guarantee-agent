import { useEffect, useRef } from "react";
import { FileCheck2, FileText } from "lucide-react";
import { EmptyState } from "../../../components/common";
import type { EntityProfileDetail, EntityProfileModuleRecord } from "../contracts";
import { FactStatusBadge, MISSING_VALUE, ProfilePanelHeading } from "./profilePresentation";

function moduleRecordTitle(record: EntityProfileModuleRecord) {
  const candidate = record.name || record.label || record.package_name || record.kind || record.type;
  return typeof candidate === "string" && candidate ? candidate : "主体资料包";
}

/**
 * 材料与证据：材料包与证据文件都用表格呈现，原文摘录默认收起。
 * `focusSourceId` 由「结构化事实 / 财务摘要」的来源引用跳转过来，用于定位到具体一行。
 */
export function ProfileSourcesPanel({ profile, focusSourceId }: { profile: EntityProfileDetail; focusSourceId?: string }) {
  const packages = profile.material_packages || [];
  const focusRef = useRef<HTMLTableRowElement | null>(null);

  useEffect(() => {
    if (focusSourceId && focusRef.current) {
      focusRef.current.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }, [focusSourceId, profile.id]);

  return <div className="ep-source-stack">
    <section className="ep-panel">
      <ProfilePanelHeading title="材料包" description="主体材料独立存储，不占用项目募集说明书和评级报告的上传限额。" action={<FileCheck2 size={18} />} />
      {!packages.length ? <EmptyState title="尚无材料包记录" description="导入主体主数据时一并上传审计、工商、股权、债务或法律证据。" /> : <div className="ep-table-scroll">
        <table className="ep-table">
          <thead><tr><th>材料包</th><th>期间 / 基准日</th><th>口径</th><th>状态</th></tr></thead>
          <tbody>{packages.map((item) => <tr key={item.id}>
            <td><strong>{moduleRecordTitle(item)}</strong></td>
            <td>{item.period || item.as_of_date || MISSING_VALUE}</td>
            <td>{item.scope || MISSING_VALUE}</td>
            <td><FactStatusBadge status={item.status} />{item.status === "confirmed" && <span className="ep-muted-cell">已确认</span>}</td>
          </tr>)}</tbody>
        </table>
      </div>}
    </section>
    <section className="ep-panel">
      <ProfilePanelHeading title="证据文件" help="只有包含可靠原文摘录和定位信息（文件 + 页码/定位符）的来源，才能投影为项目报告引用。" action={<FileText size={18} />} />
      {!profile.sources.length ? <EmptyState title="尚无证据文件" description="无可靠引用的事实仍可保留为材料不足，但不能进入报告事实性主张。" /> : <div className="ep-table-scroll">
        <table className="ep-table">
          <thead><tr><th>文件</th><th>类型</th><th>定位</th><th>原文摘录</th></tr></thead>
          <tbody>{profile.sources.map((source) => <tr key={source.id} ref={source.id === focusSourceId ? focusRef : undefined} className={source.id === focusSourceId ? "ep-row-focus" : ""}>
            <td><strong>{source.filename}</strong></td>
            <td>{source.kind || MISSING_VALUE}</td>
            <td>{source.locator || (source.page ? `第${source.page}页` : MISSING_VALUE)}</td>
            <td><details className="ep-quote"><summary>{source.quote ? "查看摘录" : "暂无摘录"}</summary><p>{source.quote || "原文摘录待补充"}</p></details></td>
          </tr>)}</tbody>
        </table>
      </div>}
    </section>
  </div>;
}
