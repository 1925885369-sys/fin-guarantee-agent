import { FileCheck2, FileText, PackageCheck } from "lucide-react";
import { EmptyState } from "../../../components/common";
import type { EntityProfileDetail, EntityProfileModuleRecord } from "../contracts";
import { FactStatusBadge, ProfilePanelHeading } from "./profilePresentation";

function moduleRecordTitle(record: EntityProfileModuleRecord) {
  const candidate = record.name || record.label || record.package_name || record.kind || record.type;
  return typeof candidate === "string" && candidate ? candidate : "主体资料包";
}

export function ProfileSourcesPanel({ profile }: { profile: EntityProfileDetail }) {
  const packages = profile.material_packages || [];
  return <div className="ep-source-stack">
    <section className="ep-panel">
      <ProfilePanelHeading title="材料包" description="主体材料独立存储，不占用项目募集说明书和评级报告的上传限额。" action={<PackageCheck size={18} />} />
      {!packages.length ? <EmptyState title="尚无材料包记录" description="导入主体主数据时一并上传审计、工商、股权、债务或法律证据。" /> : <div className="ep-package-grid">{packages.map((item) => <article key={item.id}><FileCheck2 size={18} /><div><strong>{moduleRecordTitle(item)}</strong><span>{String(item.period || item.as_of_date || "基准日未提供")}</span></div><FactStatusBadge status={item.status} /></article>)}</div>}
    </section>
    <section className="ep-panel">
      <ProfilePanelHeading title="证据文件" description="只有包含可靠原文和定位信息的来源，才可投影为项目报告引用。" action={<FileText size={18} />} />
      {!profile.sources.length ? <EmptyState title="尚无证据文件" description="无可靠引用的事实仍可保留为材料不足，但不能进入报告事实性主张。" /> : <div className="ep-source-list">{profile.sources.map((source) => <article key={source.id}>
        <FileText size={17} />
        <div><strong>{source.filename}</strong><span>{source.kind} · {source.locator || (source.page ? `第${source.page}页` : "定位待补充")}</span><p>{source.quote || "原文摘录待补充"}</p></div>
      </article>)}</div>}
    </section>
  </div>;
}
