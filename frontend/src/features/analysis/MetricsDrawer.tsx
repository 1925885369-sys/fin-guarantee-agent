import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle, ArrowLeft, Calculator, FileText, ListFilter, PanelRightClose, RotateCcw,
} from "lucide-react";
import { api } from "../../api";
import type { FinancialMetric, MetricTrace } from "../../types";
import { latestMetricPeriod, latestMetricRecords, metricKindCount, scopeLabel } from "./analysisUtils";

const VALUE_FORMAT: Intl.NumberFormatOptions = { maximumFractionDigits: 2 };

function metricValue(metric: FinancialMetric): string {
  if (metric.value === null || metric.value === undefined) return "材料不足";
  return `${metric.value.toLocaleString(undefined, VALUE_FORMAT)}${metric.unit || ""}`;
}

function Skeleton({ rows }: { rows: number }) {
  return <div className="analysis-skeleton" aria-hidden="true">{Array.from({ length: rows }, (_, index) => <i key={index} />)}</div>;
}

/**
 * 财务指标抽屉：列表态（最新 / 全部期间 + 紧凑表格）与计算链路详情态互斥渲染。
 * - 最新态按 (subject_id, scope, metric_key) 取期间最大的一条；空串期间恒排最后。
 * - 不做任何截断，完整保留后端返回的全部记录。
 * - 层级：指标抽屉 z 38 / 遮罩 37，低于证据抽屉 z 40，抽屉内看证据时可正确覆盖。
 */
export function MetricsDrawer({ open, onClose, metrics, projectId, token, openCitation, evidenceOpen = false }: {
  open: boolean;
  onClose: () => void;
  metrics: FinancialMetric[];
  projectId: string;
  token?: string;
  openCitation: (id: string) => void;
  /** 证据层（z 40）是否正盖在本抽屉之上，由分析页从 citation 状态下传。 */
  evidenceOpen?: boolean;
}) {
  const [mode, setMode] = useState<"latest" | "all">("latest");
  const [view, setView] = useState<"list" | "trace">("list");
  const [activeMetric, setActiveMetric] = useState<FinancialMetric | null>(null);
  const [trace, setTrace] = useState<MetricTrace | null>(null);
  const [traceError, setTraceError] = useState("");
  const [traceLoading, setTraceLoading] = useState(false);
  const drawerRef = useRef<HTMLElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const backRef = useRef<HTMLButtonElement | null>(null);
  const lastTrigger = useRef<HTMLElement | null>(null);
  const listScroll = useRef(0);

  const display = useMemo(() => (mode === "latest" ? latestMetricRecords(metrics) : metrics), [mode, metrics]);
  const kindCount = useMemo(() => metricKindCount(metrics), [metrics]);
  const latestPeriod = useMemo(() => latestMetricPeriod(display), [display]);

  const close = useCallback(() => { onClose(); }, [onClose]);

  const backToList = useCallback(() => {
    const metricId = activeMetric?.id;
    setView("list");
    setTrace(null);
    setTraceError("");
    window.requestAnimationFrame(() => {
      const box = drawerRef.current;
      if (box) box.scrollTop = listScroll.current;
      if (metricId) box?.querySelector<HTMLElement>(`[data-trace-metric="${metricId}"]`)?.focus();
    });
  }, [activeMetric]);

  const loadTrace = useCallback(async (metric: FinancialMetric) => {
    listScroll.current = drawerRef.current?.scrollTop ?? 0;
    setActiveMetric(metric);
    setView("trace");
    setTrace(null);
    setTraceError("");
    setTraceLoading(true);
    window.requestAnimationFrame(() => backRef.current?.focus());
    try { setTrace(await api.metricTrace(projectId, metric.id, token)); }
    catch (reason) { setTraceError(reason instanceof Error ? reason.message : "计算链路读取失败"); }
    finally { setTraceLoading(false); }
  }, [projectId, token]);

  // 关闭时重置抽屉内部状态（最新/全部期间、视图层级、滚动、trace）。
  useEffect(() => {
    if (open) return;
    setMode("latest"); setView("list"); setActiveMetric(null); setTrace(null); setTraceError(""); setTraceLoading(false);
  }, [open]);

  // 打开：记录触发元素、锁背景滚动、焦点移到关闭按钮；关闭：复位并把焦点还给触发元素。
  useEffect(() => {
    if (!open) return;
    lastTrigger.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    document.body.style.overflow = "hidden";
    const frame = window.requestAnimationFrame(() => closeRef.current?.focus());
    return () => {
      window.cancelAnimationFrame(frame);
      document.body.style.overflow = "";
      const trigger = lastTrigger.current;
      if (trigger && document.contains(trigger)) window.requestAnimationFrame(() => trigger.focus());
    };
  }, [open]);

  // 键盘：Escape 两级返回（详情→列表→关闭）；Tab 在抽屉内循环。
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        // 证据层盖在上方时 Escape 归最上层：这里直接放行，交给 EvidenceDrawer 关闭自己。
        // 用显式 prop 而不是 document.querySelector(".evidence-drawer")：class 名会随样式
        // 重构漂移，而且从抽屉内部看不出层级关系，显式下传不依赖 DOM 结构。
        if (evidenceOpen) return;
        event.stopPropagation();
        if (view === "trace") backToList(); else close();
        return;
      }
      if (event.key !== "Tab") return;
      const box = drawerRef.current;
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
  }, [open, view, close, backToList, evidenceOpen]);

  const inTrace = view === "trace";

  return <>
    {open && <div className="analysis-drawer-mask" role="presentation" onClick={close} />}
    <aside ref={drawerRef} className={`analysis-metrics-drawer ${open ? "open" : ""}`} role="dialog" aria-modal="true" aria-label="财务指标">
      <div className="drawer-head analysis-drawer-head">
        <div>
          <span>{inTrace ? "Deterministic calculation trace" : "财务指标"}</span>
          <strong>{inTrace ? (activeMetric?.name || "计算链路") : `共 ${kindCount} 类 · ${display.length} 条记录`}</strong>
        </div>
        <button ref={closeRef} className="icon-button" type="button" title={inTrace ? "关闭计算链路" : "关闭财务指标"} aria-label={inTrace ? "关闭计算链路" : "关闭财务指标"} onClick={close}><PanelRightClose size={19} /></button>
      </div>

      {inTrace ? <div className="analysis-trace-view">
        <div className="analysis-trace-back">
          <button ref={backRef} className="text-button" type="button" onClick={backToList}><ArrowLeft size={15} />返回指标列表</button>
        </div>
        {traceLoading
          ? <Skeleton rows={3} />
          : traceError
            ? <div className="analysis-error"><AlertTriangle size={16} /><span>计算链路加载失败：{traceError}</span><button className="text-button" type="button" onClick={() => activeMetric && loadTrace(activeMetric)}><RotateCcw size={15} />重试</button></div>
            : trace && <>
                <div className="analysis-trace-result">
                  <strong>{metricValue(trace.metric)}</strong>
                  <span>{trace.metric.formula}</span>
                  <small>{trace.metric.period || "—"} · {scopeLabel(trace.metric.scope)} · v{trace.metric.policy_version}</small>
                </div>
                {trace.inputs.length === 0
                  ? <div className="analysis-empty"><Calculator size={20} /><span>该指标缺少可展示的输入事实</span></div>
                  : <div className="analysis-trace-inputs">
                      {trace.inputs.map((input) => <div className="ati-row" key={input.canonical_fact.id}>
                        <div>
                          <strong>{input.canonical_fact.name}</strong>
                          <span>{input.canonical_fact.value?.toLocaleString() ?? input.canonical_fact.text_value}{input.canonical_fact.unit}</span>
                        </div>
                        <small>{input.canonical_fact.subject || "主体待确认"} · {input.canonical_fact.period || "期间未注明"} · {input.assertion.extraction_method}</small>
                        {input.sources.length > 0 && <div className="evidence-links">
                          {input.sources.map((source) => <button key={source.id} type="button" onClick={() => openCitation(source.id)}><FileText size={13} />{source.locator}</button>)}
                        </div>}
                      </div>)}
                    </div>}
              </>}
      </div> : <>
        <div className="analysis-drawer-toolbar">
          <div className="segmented analysis-mode-switch" role="group" aria-label="期间范围">
            <button type="button" className={mode === "latest" ? "active" : ""} aria-pressed={mode === "latest"} onClick={() => setMode("latest")}>最新</button>
            <button type="button" className={mode === "all" ? "active" : ""} aria-pressed={mode === "all"} onClick={() => setMode("all")}>全部期间</button>
          </div>
          <small>{mode === "latest" ? "仅显示各主体、口径下每类指标的最新记录" : "显示全部期间记录"}</small>
        </div>
        {mode === "latest" && <p className="analysis-drawer-period-note">{latestPeriod ? `最新期间：${latestPeriod}` : "暂无有效期间"}</p>}
        {display.length === 0
          ? <div className="analysis-empty is-drawer"><ListFilter size={20} /><span>该运行尚未产出财务指标</span></div>
          : <>
              <div className="amt-head"><span>指标</span><span>数值</span><span>期间</span><span>口径</span><span>计算链路</span></div>
              <div className="analysis-metric-table">
                {display.map((metric) => <div className="amt-row" key={metric.id}>
                  <span className="amt-name"><strong>{metric.name}</strong></span>
                  <span className={`amt-value ${metric.value === null || metric.value === undefined ? "is-empty" : ""}`}>{metricValue(metric)}</span>
                  <span className="amt-period">{metric.period || "—"}</span>
                  <span><span className="amt-scope">{scopeLabel(metric.scope)}</span></span>
                  <button type="button" className="amt-link" data-trace-metric={metric.id} onClick={() => loadTrace(metric)}><Calculator size={14} />查看计算链路</button>
                </div>)}
              </div>
            </>}
      </>}
    </aside>
  </>;
}
