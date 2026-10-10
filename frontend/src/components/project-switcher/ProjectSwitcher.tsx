import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Check, ChevronDown, FolderPlus, Search, ShieldCheck, X } from "lucide-react";
import type { Project, ProjectSummary } from "../../types";
import { isProjectVisible } from "../../features/projects/projectVisibility";

type ProjectRef = { id: string; token?: string };

interface ProjectSwitcherProps {
  open: boolean;
  current: ProjectRef;
  project: Project | null;
  demoProjects: ProjectSummary[];
  realProjects: ProjectSummary[];
  savedProjects: ProjectRef[];
  onOpenChange: (open: boolean) => void;
  onSelect: (project: ProjectRef) => void;
  onCreate: () => void;
}

type ProjectOption = {
  reference: ProjectRef;
  companyName: string;
  bondName: string;
  meta: string;
  realReference?: boolean;
};

function projectMeta(project: ProjectSummary) {
  if (project.data_class === "real_reference") return "真实公开材料 · 非评测案例";
  if (project.data_class === "fictional_demo") return "虚构演示材料 · 评测案例";
  return project.status || "用户项目";
}

function summaryOption(project: ProjectSummary): ProjectOption {
  return {
    reference: { id: project.id },
    companyName: project.company_name,
    bondName: project.bond_name || `${project.amount.toLocaleString()}万元 · 债券发行担保项目`,
    meta: projectMeta(project),
    realReference: project.data_class === "real_reference",
  };
}

export function ProjectSwitcher({
  open,
  current,
  project,
  demoProjects,
  realProjects,
  savedProjects,
  onOpenChange,
  onSelect,
  onCreate,
}: ProjectSwitcherProps) {
  const triggerRef = useRef<HTMLButtonElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");

  const knownIds = useMemo(
    () => new Set([...demoProjects, ...realProjects].map((item) => item.id)),
    [demoProjects, realProjects],
  );
  const userOptions = useMemo(() => savedProjects
    .filter((item) => !knownIds.has(item.id) && isProjectVisible(item))
    .map((item): ProjectOption => item.id === current.id && project ? {
      reference: item,
      companyName: project.company_name,
      bondName: project.bond_name || `${project.amount.toLocaleString()}万元 · 债券发行担保项目`,
      meta: project.status || "用户项目",
    } : {
      reference: item,
      companyName: `债券项目 ${item.id.slice(0, 8)}`,
      bondName: "项目详情将在切换后载入",
      meta: item.id,
    }), [current.id, knownIds, project, savedProjects]);

  const groups = useMemo(() => [
    { key: "demo", label: "演示评测案例", options: demoProjects.map(summaryOption) },
    { key: "real", label: "真实参考项目", options: realProjects.map(summaryOption) },
    { key: "user", label: "我的项目", options: userOptions },
  ], [demoProjects, realProjects, userOptions]);

  const normalizedQuery = query.trim().toLocaleLowerCase();
  const visibleGroups = groups.map((group) => ({
    ...group,
    options: group.options.filter((option) => !normalizedQuery || [
      option.companyName,
      option.bondName,
      option.reference.id,
    ].some((value) => value.toLocaleLowerCase().includes(normalizedQuery))),
  })).filter((group) => group.options.length > 0);

  useEffect(() => {
    if (!open) return;
    setQuery("");
    const frame = window.requestAnimationFrame(() => searchRef.current?.focus());
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      onOpenChange(false);
      window.requestAnimationFrame(() => triggerRef.current?.focus());
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open, onOpenChange]);

  const close = (restoreFocus = false) => {
    onOpenChange(false);
    if (restoreFocus) window.requestAnimationFrame(() => triggerRef.current?.focus());
  };

  const select = (reference: ProjectRef) => {
    onSelect(reference);
    close();
  };

  return <div className="project-switcher">
    <button
      ref={triggerRef}
      className="project-switch"
      aria-expanded={open}
      aria-haspopup="dialog"
      aria-controls="project-switcher-panel"
      onClick={() => onOpenChange(!open)}
    >
      <span>当前项目</span>
      <strong>{project?.company_name || "正在载入"}</strong>
      <small>{project ? `${project.amount.toLocaleString()}万元 · ${project.term_months / 12}年债券` : ""}</small>
      <ChevronDown className={open ? "expanded" : ""} size={15} />
    </button>
    {open && createPortal(<>
      <button className="project-switcher-backdrop" aria-label="关闭项目选择" onClick={() => close(true)} />
      <section id="project-switcher-panel" className="project-switcher-panel" role="dialog" aria-modal="true" aria-labelledby="project-switcher-title">
        <header className="project-switcher-header">
          <div><span>Project workspace</span><h2 id="project-switcher-title">切换债券项目</h2></div>
          <button className="icon-button" aria-label="关闭项目选择" onClick={() => close(true)}><X size={18} /></button>
        </header>
        <label className="project-switcher-search">
          <Search size={16} />
          <input ref={searchRef} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索发行人、债券名称或项目 ID" aria-label="搜索债券项目" />
          {query && <button aria-label="清空项目搜索" onClick={() => { setQuery(""); searchRef.current?.focus(); }}><X size={14} /></button>}
        </label>
        <div className="project-switcher-list">
          {visibleGroups.length ? visibleGroups.map((group) => <section className="project-switcher-group" key={group.key} aria-labelledby={`project-group-${group.key}`}>
            <h3 id={`project-group-${group.key}`}>{group.label}<span>{group.options.length}</span></h3>
            {group.options.map((option) => {
              const selected = option.reference.id === current.id;
              return <button className={`project-switcher-option ${selected ? "selected" : ""} ${option.realReference ? "real-reference" : ""}`} key={option.reference.id} onClick={() => select(option.reference)} aria-current={selected ? "true" : undefined}>
                <span className="project-option-copy"><strong>{option.companyName}</strong><span>{option.bondName}</span><small>{option.meta} · {option.reference.id}</small></span>
                {option.realReference ? <ShieldCheck size={16} /> : selected ? <Check size={17} /> : null}
                {option.realReference && selected ? <Check className="project-selected-check" size={17} /> : null}
              </button>;
            })}
          </section>) : <div className="project-switcher-empty"><Search size={20} /><strong>没有匹配的项目</strong><span>请检查发行人、债券名称或项目 ID。</span></div>}
        </div>
        <footer className="project-switcher-footer"><button className="new-project" onClick={() => { onCreate(); close(); }}><FolderPlus size={16} />新建债券担保项目</button></footer>
      </section>
    </>, document.body)}
  </div>;
}
