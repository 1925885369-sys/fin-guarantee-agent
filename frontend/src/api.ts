import type { Citation, CrossCheck, Fact, Health, Inventory, Project, ProjectSummary, ReportSection, Risk, Run, RunNode } from "./types";

function headers(token?: string, json = false): HeadersInit {
  return { ...(json ? { "Content-Type": "application/json" } : {}), ...(token ? { "X-Project-Token": token } : {}) };
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: "请求失败" }));
    throw new Error(body.detail || "请求失败");
  }
  return response.json() as Promise<T>;
}

export const api = {
  health: () => request<Health>("/api/health"),
  projects: () => request<ProjectSummary[]>("/api/projects"),
  createProject: (payload: { company_name: string; amount: number; term_months: number; purpose: string; data_consent: boolean }) =>
    request<{ id: string; accessToken: string }>("/api/projects", { method: "POST", headers: headers(undefined, true), body: JSON.stringify(payload) }),
  project: (id: string, token?: string) => request<Project>(`/api/projects/${id}`, { headers: headers(token) }),
  inventory: (id: string, token?: string) => request<Inventory>(`/api/projects/${id}/document-inventory`, { headers: headers(token) }),
  facts: (id: string, token?: string) => request<Fact[]>(`/api/projects/${id}/facts`, { headers: headers(token) }),
  checks: (id: string, token?: string) => request<CrossCheck[]>(`/api/projects/${id}/cross-checks`, { headers: headers(token) }),
  risks: (id: string, token?: string) => request<Risk[]>(`/api/projects/${id}/risks`, { headers: headers(token) }),
  report: (id: string, token?: string) => request<ReportSection[]>(`/api/projects/${id}/report`, { headers: headers(token) }),
  citation: (id: string, token?: string) => request<Citation>(`/api/citations/${id}`, { headers: headers(token) }),
  run: (id: string, token?: string) => request<Run>(`/api/runs/${id}`, { headers: headers(token) }),
  nodes: (id: string, token?: string) => request<RunNode[]>(`/api/runs/${id}/nodes`, { headers: headers(token) }),
  startRun: (id: string, token?: string) => request<{ id: string }>(`/api/projects/${id}/runs`, {
    method: "POST", headers: headers(token, true), body: JSON.stringify({ focus: "全面尽调" }),
  }),
  retryRun: (id: string, token?: string) => request<{ id: string }>(`/api/runs/${id}/retry`, { method: "POST", headers: headers(token) }),
  cancelRun: (id: string, token?: string) => request<{ status: string }>(`/api/runs/${id}/cancel`, { method: "POST", headers: headers(token) }),
  saveFact: (projectId: string, fact: Fact, token?: string) => request(`/api/projects/${projectId}/facts/${fact.id}`, {
    method: "PATCH", headers: headers(token, true), body: JSON.stringify({ period: fact.period, value: fact.value, unit: fact.unit, text_value: fact.text_value }),
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
  exportReport: async (projectId: string, format: "md" | "docx", company: string, token?: string) => {
    const response = await fetch(`/api/projects/${projectId}/export?format=${format}`, { headers: headers(token) });
    if (!response.ok) throw new Error("报告导出失败");
    const url = URL.createObjectURL(await response.blob());
    const anchor = document.createElement("a"); anchor.href = url; anchor.download = `${company}_融资担保尽调报告.${format}`; anchor.click();
    URL.revokeObjectURL(url);
  },
};
