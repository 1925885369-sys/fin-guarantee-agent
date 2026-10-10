import type {
  CommitProfileImportInput,
  EntityProfileDetail,
  EntityProfileReadiness,
  EntityProfileVersionDetail,
  EntityProfileVersionSummary,
  ProfileImportJob,
  ProfileListQuery,
  ProfileRepository,
  ProjectProfileEvidence,
  ProjectProfileSnapshot,
  ProjectProfileSnapshotStatus,
  StartProfileImportInput,
} from "../contracts";
import {
  profileFixtures,
  readinessFixtures,
  snapshotFixture,
  snapshotStatusFixture,
  versionDetailFixtures,
  versionFixtures,
} from "./profileFixtures";

function clone<T>(value: T): T {
  return structuredClone(value);
}

export function createMockProfileRepository(): ProfileRepository {
  const profiles = clone(profileFixtures);
  const readiness = clone(readinessFixtures);
  const versions = clone(versionFixtures);
  const versionDetails = clone(versionDetailFixtures);
  const imports = new Map<string, ProfileImportJob>();
  const pollCounts = new Map<string, number>();
  let snapshot = clone(snapshotFixture);
  let snapshotStatus = clone(snapshotStatusFixture);

  const requireProfile = (profileId: string): EntityProfileDetail => {
    const profile = profiles.find((item) => item.id === profileId);
    if (!profile) throw new Error("模拟主体档案不存在");
    return profile;
  };

  return {
    async listProfiles(query?: ProfileListQuery) {
      const search = query?.search?.trim().toLocaleLowerCase() || "";
      return clone(profiles.filter((profile) => {
        const searchMatched = !search || `${profile.name} ${profile.unified_code}`.toLocaleLowerCase().includes(search);
        const statusMatched = !query?.status || query.status === "all" || profile.status === query.status;
        return searchMatched && statusMatched;
      }));
    },
    async getProfile(profileId: string) {
      return clone(requireProfile(profileId));
    },
    async getReadiness(profileId: string): Promise<EntityProfileReadiness> {
      const item = readiness[profileId];
      if (!item) throw new Error("模拟就绪度不存在");
      return clone(item);
    },
    async listVersions(profileId: string): Promise<EntityProfileVersionSummary[]> {
      requireProfile(profileId);
      return clone(versions[profileId] || []);
    },
    async getVersion(profileId: string, version: number): Promise<EntityProfileVersionDetail> {
      const item = versionDetails[`${profileId}:${version}`];
      if (!item) throw new Error("模拟历史版本不存在");
      return clone(item);
    },
    async startImport(input: StartProfileImportInput): Promise<ProfileImportJob> {
      if (!input.structured_file.name.match(/\.(xlsx|json)$/i)) {
        throw new Error("结构化文件仅支持 XLSX 或 JSON");
      }
      const id = `mock-import-${Date.now()}`;
      const evidenceSourceId = `mock-source-${Date.now()}`;
      const job: ProfileImportJob = {
        id,
        target_profile_id: input.target_profile_id,
        status: "parsing",
        package_kind: input.package_kind,
        baseline_date: input.baseline_date,
        period: input.period,
        scope: input.scope,
        progress: 35,
        files: [input.structured_file, ...input.evidence_files].map((file, index) => ({
          id: `${id}-file-${index}`,
          filename: file.name,
          size: file.size,
          status: index === 0 ? "parsed" : "parsing",
          page_count: index === 0 ? null : 12,
          parse_quality: index === 0 ? 1 : 0.96,
          error: "",
        })),
        candidates: [{
          id: `${id}-candidate-assets`,
          fact_key: "total_assets",
          name: "资产总额",
          category: "financial",
          value: 346000,
          text_value: "",
          unit: "万元",
          period: input.period || "2026-06-30",
          scope: input.scope,
          proposed_status: "single_source",
          source_ids: [evidenceSourceId],
          source_label: input.evidence_files[0]?.name || "证据文件待补充",
          locator: "第8页资产负债表",
          quote: "资产总额为346,000万元。",
          confidence: 0.97,
          issue_codes: input.evidence_files.length ? [] : ["missing_evidence"],
        }],
        issues: input.evidence_files.length ? [] : [{ code: "missing_evidence", severity: "error", message: "候选事实没有证据文件和可定位原文" }],
        error: "",
        created_at: new Date().toISOString(),
        committed_version: null,
      };
      imports.set(id, job);
      pollCounts.set(id, 0);
      return clone(job);
    },
    async getImport(importId: string): Promise<ProfileImportJob> {
      const job = imports.get(importId);
      if (!job) throw new Error("模拟导入任务不存在");
      const count = (pollCounts.get(importId) || 0) + 1;
      pollCounts.set(importId, count);
      if (job.status === "parsing" && count >= 1) {
        job.progress = 100;
        job.status = job.issues.some((issue) => issue.severity === "error") ? "needs_mapping" : "ready_to_commit";
        job.files = job.files.map((file) => ({ ...file, status: "parsed" }));
      }
      return clone(job);
    },
    async commitImport(importId: string, input: CommitProfileImportInput): Promise<EntityProfileDetail> {
      const job = imports.get(importId);
      if (!job || !job.target_profile_id) throw new Error("模拟导入任务未绑定主体");
      if (!input.selected_candidate_ids.length) throw new Error("至少选择一项候选事实");
      const profile = requireProfile(job.target_profile_id);
      const accepted = job.candidates.filter((candidate) => input.selected_candidate_ids.includes(candidate.id));
      if (accepted.some((candidate) => !candidate.source_ids.length || candidate.issue_codes.includes("missing_evidence"))) {
        throw new Error("无可靠来源的候选事实不能提交");
      }
      profile.version += 1;
      profile.baseline_date = job.baseline_date || profile.baseline_date;
      profile.updated_at = new Date().toISOString();
      profile.facts = [...profile.facts, ...accepted.map((candidate) => ({
        id: `mock-fact-${candidate.id}`,
        fact_key: candidate.fact_key,
        name: candidate.name,
        category: candidate.category,
        value: candidate.value,
        text_value: candidate.text_value,
        unit: candidate.unit,
        period: candidate.period,
        scope: candidate.scope,
        status: candidate.proposed_status,
        valid_until: null,
        source_ids: candidate.source_ids,
        source_label: candidate.source_label,
      }))];
      job.status = "committed";
      job.committed_version = profile.version;
      const summary: EntityProfileVersionSummary = {
        version: profile.version,
        baseline_date: profile.baseline_date,
        latest_financial_period: profile.latest_financial_period,
        status: profile.status,
        completeness: profile.completeness,
        created_at: profile.updated_at,
        created_by: "本地模拟用户",
        change_summary: input.change_summary,
        source_count: profile.sources.length,
        fact_count: profile.facts.length,
        current: true,
      };
      versions[profile.id] = (versions[profile.id] || []).map((item) => ({ ...item, current: false }));
      versions[profile.id].unshift(summary);
      return clone(profile);
    },
    async getProjectSnapshot(): Promise<ProjectProfileSnapshot> {
      return clone(snapshot);
    },
    async getProjectEvidence(): Promise<ProjectProfileEvidence> {
      return {
        project_id: "project-fixture-profile",
        snapshot_id: snapshot.id,
        snapshot_version: snapshot.version,
        evidence_status: "ready",
        evidence_count: snapshot.sources.length,
        evidence_error: "",
        documents: [],
        sources: snapshot.sources.map((item) => ({ ...item, origin_source_id: item.id, profile_version: snapshot.version, entity_role: item.role })),
      };
    },
    async getProjectSnapshotStatus(): Promise<ProjectProfileSnapshotStatus> {
      return clone(snapshotStatus);
    },
    async refreshProjectSnapshot(): Promise<ProjectProfileSnapshot> {
      snapshot = {
        ...snapshot,
        id: "snapshot-fixture-v2",
        version: snapshot.version + 1,
        baseline_date: profiles[0].baseline_date,
        created_at: new Date().toISOString(),
        roles: snapshot.roles.map((role) => ({ ...role, profile_version: profiles[0].version, status: profiles[0].status, completeness: profiles[0].completeness, baseline_date: profiles[0].baseline_date })),
        profile_versions: { [profiles[0].id]: profiles[0].version },
      };
      snapshotStatus = {
        ...snapshotStatus,
        snapshot_id: snapshot.id,
        snapshot_version: snapshot.version,
        snapshot_profile_version: profiles[0].version,
        current_profile_version: profiles[0].version,
        has_newer_version: false,
        refresh_required: true,
        analysis_stale: true,
        reason: "主体快照已刷新，需要重新运行 Agent",
      };
      return clone(snapshot);
    },
  };
}
