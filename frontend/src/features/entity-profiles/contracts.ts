export type ProfileFactStatus = "confirmed" | "single_source" | "stale" | "conflicted" | "insufficient";
export type EntityProfileStatus = "ready" | "stale" | "conflicted" | "incomplete";
export type ProfileRequirementStatus = "ready" | "warning" | "blocking";
export type ProfileImportStatus =
  | "queued"
  | "parsing"
  | "needs_mapping"
  | "ready_to_commit"
  | "failed"
  | "committed";
export type EntityRole = "issuer" | "guarantor" | "counter_guarantor";

export interface EntityProfileSummary {
  id: string;
  name: string;
  unified_code: string;
  baseline_date: string | null;
  latest_financial_period: string | null;
  completeness: number;
  status: EntityProfileStatus;
  version: number;
  project_count: number;
  counts: Partial<Record<ProfileFactStatus, number>>;
}

export interface EntityProfileSource {
  id: string;
  filename: string;
  kind: string;
  locator: string;
  page: number | null;
  quote: string;
  created_at?: string;
}

export interface EntityProfileFact {
  id: string;
  fact_key: string;
  name: string;
  category: string;
  value: number | null;
  text_value: string;
  unit: string;
  period: string | null;
  scope: string | null;
  status: ProfileFactStatus;
  valid_until: string | null;
  source_ids: string[];
  source_id?: string | null;
  source_label?: string | null;
  page?: number | null;
}

export interface EntityProfileDetail extends EntityProfileSummary {
  aliases?: string[];
  facts: EntityProfileFact[];
  shareholders: EntityProfileModuleRecord[];
  financial_periods: EntityProfileModuleRecord[];
  debt_instruments: EntityProfileModuleRecord[];
  credit_facilities: EntityProfileModuleRecord[];
  concentrations: EntityProfileModuleRecord[];
  related_transactions: EntityProfileModuleRecord[];
  legal_events: EntityProfileModuleRecord[];
  material_packages: EntityProfileModuleRecord[];
  sources: EntityProfileSource[];
  data: Record<string, unknown>;
  version_created_at?: string;
  updated_at?: string;
}

export interface EntityProfileModuleRecord {
  id: string;
  profile_id: string;
  version: number;
  period?: string | null;
  as_of_date?: string | null;
  scope: string;
  status: ProfileFactStatus;
  valid_until: string | null;
  source_ids: string[];
  reviewer: string | null;
  reviewed_at: string | null;
  unit?: string;
  currency?: string;
  [field: string]: unknown;
}

export interface ProfileReadinessRequirement {
  key: string;
  label: string;
  category: string;
  status: ProfileRequirementStatus;
  blocking_level: "blocking" | "non_blocking";
  required_fact_keys: string[];
  missing_fact_keys: string[];
  stale_fact_keys: string[];
  conflicted_fact_keys: string[];
  source_ids: string[];
  impact: string;
}

export interface EntityProfileReadiness {
  profile_id: string;
  ready_for_project: boolean;
  ready_for_agent: boolean;
  complete_due_diligence_ready: boolean;
  completeness: number;
  status?: EntityProfileStatus;
  module_scores: Array<{
    key: string;
    label: string;
    score: number;
    weight: number;
    status: ProfileRequirementStatus;
  }>;
  hard_vetoes: Array<{ key: string; label: string; reason: string }>;
  blocking_reasons?: string[];
  notices: string[];
  counts: Partial<Record<ProfileFactStatus, number>>;
  requirements: ProfileReadinessRequirement[];
}

export interface EntityProfileVersionSummary {
  version: number;
  baseline_date: string | null;
  latest_financial_period: string | null;
  status: EntityProfileStatus;
  completeness: number;
  created_at: string;
  created_by?: string;
  change_summary?: string;
  source_count: number;
  fact_count: number;
  current: boolean;
}

export interface EntityProfileVersionDetail {
  id?: string;
  profile_id?: string;
  version: number;
  name: string;
  unified_code: string;
  baseline_date: string | null;
  latest_financial_period: string | null;
  status: EntityProfileStatus;
  completeness: number;
  version_created_at?: string;
  content_hash?: string;
  change_note?: string;
  facts: EntityProfileFact[];
  sources: EntityProfileSource[];
  data: Record<string, unknown>;
  diff?: Record<string, unknown>;
}

export interface ProfileImportFile {
  id: string;
  filename: string;
  size: number;
  status: "queued" | "parsing" | "parsed" | "failed";
  page_count: number | null;
  parse_quality: number | null;
  error: string;
}

export interface ProfileImportCandidate {
  id: string;
  fact_key: string;
  name: string;
  category: string;
  value: number | null;
  text_value: string;
  unit: string;
  period: string | null;
  scope: string;
  proposed_status: ProfileFactStatus;
  source_ids: string[];
  source_label: string;
  locator: string;
  quote: string;
  confidence: number;
  issue_codes: string[];
}

export interface ProfileImportIssue {
  code: string;
  severity: "warning" | "error";
  message: string;
  candidate_id?: string;
}

export interface ProfileImportJob {
  id: string;
  target_profile_id: string | null;
  status: ProfileImportStatus;
  package_kind: string;
  baseline_date: string | null;
  period: string | null;
  scope: string;
  progress: number;
  files: ProfileImportFile[];
  candidates: ProfileImportCandidate[];
  issues: ProfileImportIssue[];
  error: string;
  created_at: string;
  committed_version: number | null;
}

export interface StartProfileImportInput {
  target_profile_id: string | null;
  package_kind: string;
  baseline_date: string | null;
  period: string | null;
  scope: string;
  structured_file: File;
  evidence_files: File[];
}

export interface ProfileCandidateCorrection {
  candidate_id: string;
  value?: number | null;
  text_value?: string;
  unit?: string;
  period?: string | null;
  scope?: string;
  status?: ProfileFactStatus;
}

export interface CommitProfileImportInput {
  selected_candidate_ids: string[];
  corrections: ProfileCandidateCorrection[];
  change_summary: string;
}

export interface SnapshotRole {
  role: EntityRole;
  profile_id: string;
  name: string;
  profile_version: number;
  status: EntityProfileStatus;
  completeness: number;
  baseline_date: string | null;
  latest_financial_period: string | null;
}

export interface ProjectProfileSnapshot {
  id: string;
  version: number;
  baseline_date: string | null;
  created_at: string;
  roles: SnapshotRole[];
  counts: Record<ProfileFactStatus, number>;
  facts: Array<EntityProfileFact & { profile_id: string; role: EntityRole }>;
  records: Array<EntityProfileModuleRecord & { profile_id: string; role: EntityRole; record_type: string }>;
  sources: Array<EntityProfileSource & { profile_id: string; role: EntityRole }>;
  profile_versions: Record<string, number>;
  content_hash: string;
}

export interface ProjectProfileEvidence {
  project_id: string;
  snapshot_id: string;
  snapshot_version: number;
  evidence_status: string;
  evidence_count: number;
  evidence_error: string;
  documents: Array<{ id: string; filename: string; kind: string; profile_version: number; entity_role: EntityRole }>;
  sources: Array<EntityProfileSource & { origin_source_id: string; profile_version: number; entity_role: EntityRole }>;
}

export interface SnapshotRoleStatus {
  role: EntityRole;
  profile_id: string;
  name: string;
  snapshot_version: number;
  current_version: number;
  has_newer_version: boolean;
}

export interface ProjectProfileSnapshotStatus {
  project_id?: string;
  snapshot_id: string | null;
  snapshot_version: number | null;
  snapshot_profile_version: number | null;
  current_profile_version: number | null;
  has_newer_version: boolean;
  refresh_required: boolean;
  analysis_stale: boolean;
  evidence_status: string;
  evidence_count?: number;
  evidence_error?: string;
  roles?: SnapshotRoleStatus[];
  reason?: string;
}

export interface ProfileListQuery {
  search?: string;
  status?: EntityProfileStatus | "all";
}

export interface ProfileRepository {
  listProfiles(query?: ProfileListQuery): Promise<EntityProfileSummary[]>;
  getProfile(profileId: string): Promise<EntityProfileDetail>;
  getReadiness(profileId: string): Promise<EntityProfileReadiness>;
  listVersions(profileId: string): Promise<EntityProfileVersionSummary[]>;
  getVersion(profileId: string, version: number): Promise<EntityProfileVersionDetail>;
  startImport(input: StartProfileImportInput): Promise<ProfileImportJob>;
  getImport(importId: string): Promise<ProfileImportJob>;
  commitImport(importId: string, input: CommitProfileImportInput): Promise<EntityProfileDetail>;
  getProjectSnapshot(projectId: string, token?: string): Promise<ProjectProfileSnapshot>;
  getProjectEvidence(projectId: string, token?: string): Promise<ProjectProfileEvidence>;
  getProjectSnapshotStatus(projectId: string, token?: string): Promise<ProjectProfileSnapshotStatus>;
  refreshProjectSnapshot(projectId: string, token?: string): Promise<ProjectProfileSnapshot>;
}
