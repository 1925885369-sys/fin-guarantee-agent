import { isProjectVisible } from "./features/projects/projectVisibility";
import type {
  AuditEvent, CandidateFinding, Citation, CrossCheck, EvidenceGraphSummary, Fact, FactGroup, MaterialQAResponse,
  FinancialMetric, FinancialTieOut, Health, Inventory, LatestRun, MetricTrace, PolicyInfo, Project,
  BondSummary, EntityProfileDetail, EntityProfileReadiness, EntityProfileSummary, ProjectCreatePayload,
  ProjectEntity, ProjectProfileSnapshot, ProjectSummary, ReportQuality, ReportSection, ReviewSummary,
  ReviewTask, Risk, Run, RunNode, RunArtifact, ReportPrompt,
    TableMatrix, TableSummary, TableCellReviewRecord,
} from "./types";

function headers(token?: string, json = false): HeadersInit {
  return { ...(json ? { "Content-Type": "application/json" } : {}), ...(token ? { "X-Project-Token": token } : {}) };
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: "请求失败" }));
    const detail = body.detail;
    const formatValidationError = (item: unknown): string => {
      if (!item || typeof item !== "object") return "请求参数不合法";
      const value = item as { loc?: unknown[]; msg?: string };
      const field = Array.isArray(value.loc) ? value.loc.filter((part) => part !== "body").join(".") : "";
      const labels: Record<string, string> = {
        amount: "本期规模", term_months: "预计期限", purpose: "募集资金用途", company_name: "发行人",
        data_consent: "数据授权", issuer_profile_id: "发行人主体档案",
      };
      const label = labels[field] || field;
      const rawMessage = value.msg || "参数不合法";
      const message = rawMessage.includes("greater than 0") ? "必须大于 0"
        : rawMessage.includes("greater than or equal to") ? `不能小于 ${rawMessage.match(/[\d.]+$/)?.[0] || "允许下限"}`
        : rawMessage.includes("less than or equal to") ? `不能超过 ${rawMessage.match(/[\d.]+$/)?.[0] || "允许上限"}`
        : rawMessage.includes("finite") ? "必须是有限数值"
        : rawMessage;
      return label ? `${label}：${message}` : message;
    };
    const message = Array.isArray(detail)
      ? detail.map(formatValidationError).join("；")
      : typeof detail === "string" ? detail : detail?.message || `请求失败（HTTP ${response.status}）`;
    const error = new Error(message);
    (error as Error & { status?: number }).status = response.status;
    // 结构化 detail（例如重复材料的 document_id）随错误一起带出，供调用方分流，
    // 不必让调用方重新解析响应体。
    (error as Error & { detail?: unknown }).detail = detail;
    throw error;
  }
  return response.json() as Promise<T>;
}

/** 材料分类。``其他材料`` 即页面上的「补充核验材料」。 */
export type DocumentKind = "募集说明书" | "信用评级报告" | "其他材料";

export const DOCUMENT_ROLES: Record<DocumentKind, "prospectus" | "rating_report" | "other"> = {
  募集说明书: "prospectus", 信用评级报告: "rating_report", 其他材料: "other",
};

export interface UploadedDocument {
  id: string;
  kind: string;
  documentRole: string;
  parseStatus: string;
  quality: number;
  warning: string;
}

/** 重复材料：界面需要给出「查看已有材料」，不能当成上传成功。 */
export class DuplicateDocumentError extends Error {
  readonly documentId: string;

  constructor(message: string, documentId: string) {
    super(message);
    this.name = "DuplicateDocumentError";
    this.documentId = documentId;
  }
}

export function classifyDocument(projectId: string, documentId: string, kind: DocumentKind, token?: string) {
  return request<{ id: string; kind: string; document_role: string; changed: boolean }>(
    `/api/projects/${projectId}/documents/${documentId}`,
    { method: "PATCH", headers: headers(token, true), body: JSON.stringify({ kind, document_role: DOCUMENT_ROLES[kind] }) },
  );
}

export const api = {
  health: () => request<Health>("/api/health"),
  entityProfiles: (query?: { search?: string; status?: EntityProfileSummary["status"] | "all" }) => {
    const params = new URLSearchParams();
    if (query?.search) params.set("search", query.search);
    if (query?.status && query.status !== "all") params.set("status", query.status);
    const suffix = params.size ? `?${params.toString()}` : "";
    return request<EntityProfileSummary[]>(`/api/entity-profiles${suffix}`);
  },
  entityProfile: (id: string) => request<EntityProfileDetail>(`/api/entity-profiles/${id}`),
  entityProfileReadiness: (id: string) => request<EntityProfileReadiness>(`/api/entity-profiles/${id}/readiness`),
  createEntityProfile: (payload: Partial<EntityProfileDetail>) => request<EntityProfileDetail>("/api/entity-profiles", {
    method: "POST", headers: headers(undefined, true), body: JSON.stringify(payload),
  }),
  updateEntityProfile: (id: string, payload: Partial<EntityProfileDetail>) => request<EntityProfileDetail>(`/api/entity-profiles/${id}`, {
    method: "PATCH", headers: headers(undefined, true), body: JSON.stringify(payload),
  }),
  projects: (dataClass?: ProjectSummary["data_class"]) => request<ProjectSummary[]>(`/api/projects${dataClass ? `?data_class=${dataClass}` : ""}`).then((projects) => projects.filter(isProjectVisible)),
  createProject: (payload: ProjectCreatePayload) =>
    request<{ id: string; accessToken: string }>("/api/projects", { method: "POST", headers: headers(undefined, true), body: JSON.stringify(payload) }),
  project: (id: string, token?: string) => request<Project>(`/api/projects/${id}`, { headers: headers(token) }),
  archiveProject: (id: string, token?: string) => request<{ id: string; archived: boolean; archived_at: string | null; changed: boolean }>(`/api/projects/${id}/archive`, { method: "POST", headers: headers(token) }),
  restoreProject: (id: string, token?: string) => request<{ id: string; archived: boolean; archived_at: string | null; changed: boolean }>(`/api/projects/${id}/restore`, { method: "POST", headers: headers(token) }),
  profileSnapshot: (id: string, token?: string) => request<ProjectProfileSnapshot>(`/api/projects/${id}/profile-snapshot`, { headers: headers(token) }),
  refreshProfileSnapshot: (id: string, token?: string) => request<ProjectProfileSnapshot>(`/api/projects/${id}/profile-snapshot/refresh`, { method: "POST", headers: headers(token) }),
  reviewSummary: (id: string, token?: string) => request<ReviewSummary>(`/api/projects/${id}/review-summary`, { headers: headers(token) }),
  applyReview: (id: string, token?: string) => request<ReviewSummary & { affected_section_keys: string[]; applied: boolean; quality?: ReportQuality }>(`/api/projects/${id}/review/apply`, { method: "POST", headers: headers(token) }),
  markReportReviewed: (id: string, token?: string) => request<ReviewSummary>(`/api/projects/${id}/report/mark-reviewed`, { method: "POST", headers: headers(token) }),
  inventory: (id: string, token?: string) => request<Inventory>(`/api/projects/${id}/document-inventory`, { headers: headers(token) }),
  materialQA: (id: string, question: string, documentIds: string[] = [], token?: string) =>
    request<MaterialQAResponse>(`/api/projects/${id}/material-qa`, {
      method: "POST", headers: headers(token, true),
      body: JSON.stringify({ question, document_ids: documentIds }),
    }),
  bondSummary: (id: string, token?: string) => request<BondSummary>(`/api/projects/${id}/bond-summary`, { headers: headers(token) }),
  facts: (id: string, token?: string) => request<Fact[]>(`/api/projects/${id}/facts`, { headers: headers(token) }),
  factGroups: (id: string, token?: string) => request<FactGroup[]>(`/api/projects/${id}/fact-groups`, { headers: headers(token) }),
  tables: (id: string, token?: string) => request<TableSummary[]>(`/api/projects/${id}/tables`, { headers: headers(token) }),
  tableDetail: (id: string, tableId: string, token?: string) => request<TableMatrix>(`/api/projects/${id}/tables/${encodeURIComponent(tableId)}`, { headers: headers(token) }),
  reviewTableCell: (id: string, tableId: string, cellId: string, payload: { decision: string; corrected_raw?: string | null; note?: string; reviewer?: string }, token?: string) =>
    request<{ review: TableCellReviewRecord; cell_reviews: Record<string, TableCellReviewRecord> }>(
      `/api/projects/${id}/tables/${encodeURIComponent(tableId)}/cells/${encodeURIComponent(cellId)}/review`,
      { method: "POST", headers: headers(token, true), body: JSON.stringify(payload) },
    ),
  metrics: (id: string, token?: string) => request<FinancialMetric[]>(`/api/projects/${id}/metrics`, { headers: headers(token) }),
  metricTrace: (projectId: string, metricId: string, token?: string) => request<MetricTrace>(`/api/projects/${projectId}/metrics/${metricId}/trace`, { headers: headers(token) }),
  entities: (id: string, token?: string) => request<ProjectEntity[]>(`/api/projects/${id}/entities`, { headers: headers(token) }),
  tieOuts: (id: string, token?: string) => request<FinancialTieOut[]>(`/api/projects/${id}/financial-tie-outs`, { headers: headers(token) }),
  reportQuality: (id: string, token?: string) => request<ReportQuality>(`/api/projects/${id}/report-quality`, { headers: headers(token) }),
  evidenceGraph: (id: string, token?: string) => request<EvidenceGraphSummary>(`/api/projects/${id}/evidence-graph`, { headers: headers(token) }),
  policy: (id: string, token?: string) => request<PolicyInfo>(`/api/projects/${id}/policy`, { headers: headers(token) }),
  reviewTasks: (id: string, token?: string) => request<ReviewTask[]>(`/api/projects/${id}/review-tasks`, { headers: headers(token) }),
  candidates: (id: string, token?: string) => request<CandidateFinding[]>(`/api/projects/${id}/candidate-findings`, { headers: headers(token) }),
  auditEvents: (id: string, token?: string) => request<AuditEvent[]>(`/api/projects/${id}/audit-events`, { headers: headers(token) }),
  checks: (id: string, token?: string) => request<CrossCheck[]>(`/api/projects/${id}/cross-checks`, { headers: headers(token) }),
  risks: (id: string, token?: string) => request<Risk[]>(`/api/projects/${id}/risks`, { headers: headers(token) }),
  report: (id: string, token?: string) => request<ReportSection[]>(`/api/projects/${id}/report`, { headers: headers(token) }),
  refreshReportTables: (id: string, chapters: string[], expectedRevision: number, sectionHashes: Record<string, string>, token?: string) => request<{ status: string; chapters: { chapter_key: string; content_sha256: string; table_hash: string }[]; report_revision: number; report_state: string }>(`/api/projects/${id}/report/tables/refresh`, {
    method: "POST", headers: headers(token, true),
    body: JSON.stringify({ chapters, expected_report_revision: expectedRevision, section_hashes: sectionHashes }),
  }),
  reportPrompts: (id: string, token?: string) => request<ReportPrompt[]>(`/api/projects/${id}/report-prompts`, { headers: headers(token) }),
  saveReportPrompt: (id: string, chapterKey: string, config: ReportPrompt["editable_config"] | string, token?: string) => request<ReportPrompt>(`/api/projects/${id}/report-prompts/${chapterKey}`, {
    method: "PATCH", headers: headers(token, true), body: JSON.stringify(typeof config === "string" ? { prompt: config } : { config }),
  }),
  resetReportPrompt: (id: string, chapterKey: string, token?: string) => request<ReportPrompt>(`/api/projects/${id}/report-prompts/${chapterKey}`, {
    method: "DELETE", headers: headers(token),
  }),
  setGlobalReportPrompt: (id: string, chapterKey: string, config: ReportPrompt["editable_config"] | string, token?: string) => request<ReportPrompt>(`/api/projects/${id}/report-prompts/global/${chapterKey}`, {
    method: "PATCH", headers: headers(token, true), body: JSON.stringify(typeof config === "string" ? { prompt: config } : { config }),
  }),
  previewReportPrompt: (id: string, chapterKey: string, config: ReportPrompt["editable_config"], token?: string) => request<{ chapter_key: string; prompt: string }>(`/api/projects/${id}/report-prompts/${chapterKey}/preview`, {
    method: "POST", headers: headers(token, true), body: JSON.stringify({ config }),
  }),
  resetGlobalReportPrompt: (id: string, chapterKey: string, token?: string) => request<ReportPrompt>(`/api/projects/${id}/report-prompts/global/${chapterKey}`, {
    method: "DELETE", headers: headers(token),
  }),
  citation: (id: string, token?: string) => request<Citation>(`/api/citations/${id}`, { headers: headers(token) }),
  run: (id: string, token?: string) => request<Run>(`/api/runs/${id}`, { headers: headers(token) }),
  latestRun: (projectId: string, token?: string) => request<LatestRun>(`/api/projects/${projectId}/runs/latest`, { headers: headers(token) }),
  projectRuns: (projectId: string, token?: string) => request<Run[]>(`/api/projects/${projectId}/runs`, { headers: headers(token) }),
  nodes: (id: string, token?: string) => request<RunNode[]>(`/api/runs/${id}/nodes`, { headers: headers(token) }),
  artifacts: (id: string, token?: string) => request<RunArtifact[]>(`/api/runs/${id}/artifacts`, { headers: headers(token) }),
  startRun: (id: string, token?: string) => request<{ id: string }>(`/api/projects/${id}/runs`, {
    method: "POST", headers: headers(token, true), body: JSON.stringify({ focus: "债券发行融资担保尽调" }),
  }),
  retryRun: (id: string, token?: string) => request<{ id: string }>(`/api/runs/${id}/retry`, { method: "POST", headers: headers(token) }),
  cancelRun: (id: string, token?: string) => request<{ status: string }>(`/api/runs/${id}/cancel`, { method: "POST", headers: headers(token) }),
  saveFact: (projectId: string, fact: Fact, token?: string) => request(`/api/projects/${projectId}/facts/${fact.id}`, {
    method: "PATCH", headers: headers(token, true), body: JSON.stringify({ period: fact.period, value: fact.value, unit: fact.unit, text_value: fact.text_value }),
  }),
  resolveFactGroup: (projectId: string, groupId: string, assertionId: string, note: string, token?: string) =>
    request<FactGroup>(`/api/projects/${projectId}/fact-groups/${groupId}/resolve`, {
      method: "PATCH", headers: headers(token, true), body: JSON.stringify({ assertion_id: assertionId, note }),
    }),
  decideCandidate: (projectId: string, findingId: string, status: "proposed" | "confirmed" | "rejected", decisionNote: string, token?: string) =>
    request<CandidateFinding>(`/api/projects/${projectId}/candidate-findings/${findingId}`, {
      method: "PATCH", headers: headers(token, true), body: JSON.stringify({ status, decision_note: decisionNote }),
    }),
  updateReviewTask: (projectId: string, task: ReviewTask, status: "open" | "resolved" | "waived", resolutionNote: string, token?: string) =>
    request<ReviewTask>(`/api/projects/${projectId}/review-tasks/${task.id}`, {
      method: "PATCH", headers: headers(token, true), body: JSON.stringify({ status, resolution_note: resolutionNote, assignee: task.assignee, due_date: task.due_date }),
    }),
  saveSection: (projectId: string, key: string, content: string, reviewed: boolean, token?: string) =>
    request(`/api/projects/${projectId}/report/sections/${key}`, {
      method: "PATCH", headers: headers(token, true), body: JSON.stringify({ content, review_status: reviewed ? "reviewed" : "draft" }),
    }),
  regenerate: (projectId: string, key: string, token?: string) => request<{ content: string }>(`/api/projects/${projectId}/report/sections/${key}/regenerate`, { method: "POST", headers: headers(token) }),
  upload: async (projectId: string, files: File[], token?: string) => {
    for (const file of files) {
      const form = new FormData(); form.append("file", file);
      await request(`/api/projects/${projectId}/documents`, { method: "POST", headers: headers(token), body: form });
    }
  },
  /**
   * 单文件上传，并保证落库分类等于用户明确选择的类型。
   *
   * 后端按文件名与正文自动分类，因此用户选了「募集说明书」却分到「其他材料」时，
   * 必须在同一次操作里 PATCH 修正——否则核心材料就绪度会与用户看到的选择不一致。
   * 重复文件抛 ``DuplicateDocumentError``，由界面提示「该文件已上传」。
   */
  uploadOne: async (projectId: string, file: File, kind?: DocumentKind, token?: string): Promise<UploadedDocument> => {
    const form = new FormData(); form.append("file", file);
    let uploaded: UploadedDocument;
    try {
      uploaded = await request<UploadedDocument>(`/api/projects/${projectId}/documents`, { method: "POST", headers: headers(token), body: form });
    } catch (reason) {
      const detail = (reason as { detail?: { code?: string; document_id?: string } }).detail;
      if (detail?.code === "duplicate_document") {
        throw new DuplicateDocumentError(reason instanceof Error ? reason.message : "该文件已上传", detail.document_id || "");
      }
      throw reason;
    }
    if (!kind || uploaded.kind === kind) return uploaded;
    await classifyDocument(projectId, uploaded.id, kind, token);
    return { ...uploaded, kind, documentRole: DOCUMENT_ROLES[kind] };
  },
  classifyDocument,
  streamRun: async (id: string, token: string | undefined, onRun: (run: Run) => void) => {
    const response = await fetch(`/api/runs/${id}/events`, { headers: headers(token) });
    if (!response.ok || !response.body) throw new Error("无法连接任务进度流");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const events = buffer.split("\n\n");
      buffer = events.pop() || "";
      for (const event of events) {
        const data = event.split("\n").find((line) => line.startsWith("data: "));
        if (data) onRun(JSON.parse(data.slice(6)) as Run);
      }
    }
  },
  exportReport: async (projectId: string, format: "md" | "docx", filenameBase: string, token?: string, _bond = true, variant: "draft" | "final" = "draft") => {
    const response = await fetch(`/api/projects/${projectId}/export?format=${format}&variant=${variant}`, { headers: headers(token) });
    if (!response.ok) throw new Error("报告导出失败");
    const url = URL.createObjectURL(await response.blob());
    const safeBase = filenameBase.replace(/[\\/:*?"<>|]/g, "_").trim() || "债券发行融资担保项目";
    const suffix = "债券发行融资担保尽调报告";
    const label = variant === "draft" ? "草稿" : "最终";
    const anchor = document.createElement("a"); anchor.href = url; anchor.download = `${safeBase}_${suffix}_${label}.${format}`; anchor.click();
    URL.revokeObjectURL(url);
  },
};
