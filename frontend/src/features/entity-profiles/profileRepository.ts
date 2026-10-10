import type {
  CommitProfileImportInput,
  EntityProfileDetail,
  EntityProfileReadiness,
  EntityProfileSummary,
  EntityProfileVersionDetail,
  EntityProfileVersionSummary,
  ProfileImportJob,
  ProfileListQuery,
  ProfileRepository,
  ProjectProfileEvidence,
  ProjectProfileSnapshot,
  ProjectProfileSnapshotStatus,
  StartProfileImportInput,
} from "./contracts";
import { createMockProfileRepository } from "./mocks/mockProfileRepository";

export class ProfileApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(message: string, status: number, code = "profile_api_error") {
    super(message);
    this.name = "ProfileApiError";
    this.status = status;
    this.code = code;
  }
}

function projectHeaders(token?: string): HeadersInit {
  return token ? { "X-Project-Token": token } : {};
}

async function profileRequest<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as { detail?: string | { message?: string; code?: string }; code?: string };
    const detail = typeof body.detail === "string" ? body.detail : body.detail?.message;
    const code = body.code || (typeof body.detail === "object" ? body.detail?.code : undefined);
    throw new ProfileApiError(detail || `主体档案请求失败（HTTP ${response.status}）`, response.status, code);
  }
  return response.json() as Promise<T>;
}

export class HttpProfileRepository implements ProfileRepository {
  listProfiles(query?: ProfileListQuery): Promise<EntityProfileSummary[]> {
    const params = new URLSearchParams();
    if (query?.search?.trim()) params.set("search", query.search.trim());
    if (query?.status && query.status !== "all") params.set("status", query.status);
    const suffix = params.size ? `?${params.toString()}` : "";
    return profileRequest(`/api/entity-profiles${suffix}`);
  }

  getProfile(profileId: string): Promise<EntityProfileDetail> {
    return profileRequest(`/api/entity-profiles/${encodeURIComponent(profileId)}`);
  }

  getReadiness(profileId: string): Promise<EntityProfileReadiness> {
    return profileRequest(`/api/entity-profiles/${encodeURIComponent(profileId)}/readiness`);
  }

  listVersions(profileId: string): Promise<EntityProfileVersionSummary[]> {
    return profileRequest(`/api/entity-profiles/${encodeURIComponent(profileId)}/versions`);
  }

  getVersion(profileId: string, version: number): Promise<EntityProfileVersionDetail> {
    return profileRequest(`/api/entity-profiles/${encodeURIComponent(profileId)}/versions/${version}`);
  }

  startImport(input: StartProfileImportInput): Promise<ProfileImportJob> {
    const body = new FormData();
    if (input.target_profile_id) body.append("target_profile_id", input.target_profile_id);
    body.append("package_kind", input.package_kind);
    if (input.baseline_date) body.append("baseline_date", input.baseline_date);
    if (input.period) body.append("period", input.period);
    body.append("scope", input.scope);
    body.append("structured_file", input.structured_file);
    input.evidence_files.forEach((file) => body.append("evidence_files", file));
    return profileRequest("/api/entity-profile-imports", { method: "POST", body });
  }

  getImport(importId: string): Promise<ProfileImportJob> {
    return profileRequest(`/api/entity-profile-imports/${encodeURIComponent(importId)}`);
  }

  commitImport(importId: string, input: CommitProfileImportInput): Promise<EntityProfileDetail> {
    return profileRequest(`/api/entity-profile-imports/${encodeURIComponent(importId)}/commit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    });
  }

  getProjectSnapshot(projectId: string, token?: string): Promise<ProjectProfileSnapshot> {
    return profileRequest(`/api/projects/${encodeURIComponent(projectId)}/profile-snapshot`, { headers: projectHeaders(token) });
  }

  getProjectEvidence(projectId: string, token?: string): Promise<ProjectProfileEvidence> {
    return profileRequest(`/api/projects/${encodeURIComponent(projectId)}/profile-evidence`, { headers: projectHeaders(token) });
  }

  getProjectSnapshotStatus(projectId: string, token?: string): Promise<ProjectProfileSnapshotStatus> {
    return profileRequest(`/api/projects/${encodeURIComponent(projectId)}/profile-snapshot/status`, { headers: projectHeaders(token) });
  }

  refreshProjectSnapshot(projectId: string, token?: string): Promise<ProjectProfileSnapshot> {
    return profileRequest(`/api/projects/${encodeURIComponent(projectId)}/profile-snapshot/refresh`, {
      method: "POST",
      headers: projectHeaders(token),
    });
  }
}

const mockRequested = import.meta.env.VITE_USE_PROFILE_MOCKS === "true";

if (mockRequested && import.meta.env.PROD) {
  throw new Error("VITE_USE_PROFILE_MOCKS 只能用于本地开发或测试，生产构建禁止启用主体档案 mock。");
}

export function createProfileRepository(options?: { useMocks?: boolean }): ProfileRepository {
  const useMocks = options?.useMocks ?? mockRequested;
  if (useMocks && import.meta.env.PROD) {
    throw new Error("生产环境禁止创建主体档案 mock Repository。");
  }
  return useMocks ? createMockProfileRepository() : new HttpProfileRepository();
}

export const profileDataMode: "mock" | "http" = mockRequested ? "mock" : "http";
export const profileRepository: ProfileRepository = createProfileRepository();
