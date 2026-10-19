from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable

from .config import settings


logger = logging.getLogger(__name__)


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, access_token TEXT NOT NULL, company_name TEXT NOT NULL,
  amount REAL NOT NULL, term_months INTEGER NOT NULL, purpose TEXT NOT NULL,
  status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  project_type TEXT NOT NULL DEFAULT 'bond_guarantee', bond_name TEXT NOT NULL DEFAULT '',
  consent_at TEXT NOT NULL DEFAULT '',
  data_class TEXT NOT NULL DEFAULT 'user_project', evaluation_eligible INTEGER NOT NULL DEFAULT 0,
  issuer_profile_id TEXT, profile_snapshot_id TEXT,
  analysis_target TEXT NOT NULL DEFAULT 'full_due_diligence',
  effective_analysis_mode TEXT NOT NULL DEFAULT 'insufficient',
  analysis_stale INTEGER NOT NULL DEFAULT 0,
  review_revision INTEGER NOT NULL DEFAULT 0,
  report_revision INTEGER NOT NULL DEFAULT 0,
  report_reviewed_revision INTEGER NOT NULL DEFAULT 0,
  report_reviewed_at TEXT,
  report_stale INTEGER NOT NULL DEFAULT 0,
  report_stale_reasons TEXT NOT NULL DEFAULT '[]',
  counter_guarantee_status TEXT NOT NULL DEFAULT 'undetermined'
);
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, filename TEXT NOT NULL,
  kind TEXT NOT NULL, media_type TEXT NOT NULL, sha256 TEXT NOT NULL,
  size INTEGER NOT NULL, parse_status TEXT NOT NULL, quality REAL NOT NULL,
  locator_count INTEGER NOT NULL, warning TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, parser TEXT NOT NULL DEFAULT 'native', document_role TEXT NOT NULL DEFAULT '',
  ocr_status TEXT NOT NULL DEFAULT 'not_required', page_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, document_id TEXT NOT NULL,
  locator_type TEXT NOT NULL, locator TEXT NOT NULL, text TEXT NOT NULL,
  page INTEGER, bbox_json TEXT NOT NULL DEFAULT '[]', extraction_method TEXT NOT NULL DEFAULT 'native',
  content_hash TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS retrieval_indexes (
  project_id TEXT PRIMARY KEY,
  fingerprint TEXT NOT NULL,
  model_id TEXT NOT NULL DEFAULT '',
  model_revision TEXT NOT NULL DEFAULT '',
  vector_path TEXT NOT NULL DEFAULT '',
  dimension INTEGER NOT NULL DEFAULT 0,
  chunk_count INTEGER NOT NULL DEFAULT 0,
  build_duration_ms INTEGER NOT NULL DEFAULT 0,
  retrieval_mode TEXT NOT NULL DEFAULT 'bm25_only',
  error_type TEXT NOT NULL DEFAULT '',
  indexed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS retrieval_chunks (
  project_id TEXT NOT NULL,
  chunk_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  document_id TEXT NOT NULL,
  page INTEGER,
  kind TEXT NOT NULL DEFAULT '',
  document_role TEXT NOT NULL DEFAULT '',
  section_role TEXT NOT NULL DEFAULT '',
  filename TEXT NOT NULL DEFAULT '',
  heading TEXT NOT NULL DEFAULT '',
  chunk_type TEXT NOT NULL DEFAULT 'text',
  table_key TEXT NOT NULL DEFAULT '',
  vector_position INTEGER NOT NULL DEFAULT -1,
  start_offset INTEGER NOT NULL DEFAULT 0,
  end_offset INTEGER NOT NULL DEFAULT 0,
  content_hash TEXT NOT NULL,
  text TEXT NOT NULL,
  PRIMARY KEY(project_id,chunk_id)
);
CREATE TABLE IF NOT EXISTS document_sections (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, document_id TEXT NOT NULL,
  section_role TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
  start_source_id TEXT NOT NULL DEFAULT '', end_source_id TEXT NOT NULL DEFAULT '',
  start_page INTEGER, end_page INTEGER, locator_method TEXT NOT NULL DEFAULT '',
  confidence REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'missing',
  contract_version TEXT NOT NULL DEFAULT '', content_hash TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(project_id,document_id,section_role)
);
CREATE TABLE IF NOT EXISTS facts (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, category TEXT NOT NULL,
  name TEXT NOT NULL, period TEXT NOT NULL, value REAL, unit TEXT NOT NULL,
  text_value TEXT NOT NULL, source_ids TEXT NOT NULL, confidence REAL NOT NULL,
  fact_key TEXT NOT NULL DEFAULT '', raw_value TEXT NOT NULL DEFAULT '',
  extraction_method TEXT NOT NULL DEFAULT 'seed', review_status TEXT NOT NULL DEFAULT 'unreviewed',
  subject TEXT NOT NULL DEFAULT '', subject_id TEXT NOT NULL DEFAULT '',
  scope TEXT NOT NULL DEFAULT 'unknown', period_type TEXT NOT NULL DEFAULT 'unknown',
  origin_type TEXT NOT NULL DEFAULT 'document', origin_ref TEXT NOT NULL DEFAULT '',
  profile_snapshot_id TEXT NOT NULL DEFAULT '', assertion_status TEXT NOT NULL DEFAULT 'unreviewed'
);
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fact_assertions (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, fact_key TEXT NOT NULL,
  subject TEXT NOT NULL DEFAULT '', subject_id TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT 'unknown',
  period TEXT NOT NULL DEFAULT '', period_type TEXT NOT NULL DEFAULT 'unknown', category TEXT NOT NULL,
  name TEXT NOT NULL, value REAL, unit TEXT NOT NULL DEFAULT '', text_value TEXT NOT NULL DEFAULT '',
  raw_value TEXT NOT NULL DEFAULT '', source_ids TEXT NOT NULL DEFAULT '[]', confidence REAL NOT NULL DEFAULT 0.8,
  extraction_method TEXT NOT NULL, created_at TEXT NOT NULL,
  origin_type TEXT NOT NULL DEFAULT 'document', origin_ref TEXT NOT NULL DEFAULT '',
  profile_snapshot_id TEXT NOT NULL DEFAULT '', assertion_status TEXT NOT NULL DEFAULT 'unreviewed',
  UNIQUE(project_id, fact_key, subject_id, scope, period, raw_value, source_ids, extraction_method)
);
CREATE TABLE IF NOT EXISTS canonical_facts (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, fact_key TEXT NOT NULL,
  subject TEXT NOT NULL DEFAULT '', subject_id TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT 'unknown',
  period TEXT NOT NULL DEFAULT '', period_type TEXT NOT NULL DEFAULT 'unknown', category TEXT NOT NULL,
  name TEXT NOT NULL, status TEXT NOT NULL, selected_assertion_id TEXT,
  value REAL, unit TEXT NOT NULL DEFAULT '', text_value TEXT NOT NULL DEFAULT '',
  assertion_ids TEXT NOT NULL DEFAULT '[]', resolution_note TEXT NOT NULL DEFAULT '',
  resolved_by TEXT NOT NULL DEFAULT '', resolution_basis_hash TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
  UNIQUE(project_id, fact_key, subject_id, scope, period)
);
CREATE TABLE IF NOT EXISTS financial_metrics (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, metric_key TEXT NOT NULL,
  name TEXT NOT NULL, period TEXT NOT NULL DEFAULT '', value REAL, unit TEXT NOT NULL,
  formula TEXT NOT NULL, input_fact_ids TEXT NOT NULL DEFAULT '[]', source_ids TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'calculated', policy_version TEXT NOT NULL DEFAULT '',
  subject_id TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT 'unknown', trace_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(project_id, metric_key, subject_id, scope, period)
);
CREATE TABLE IF NOT EXISTS project_entities (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, entity_type TEXT NOT NULL,
  name TEXT NOT NULL, unified_code TEXT NOT NULL DEFAULT '', parent_id TEXT,
  is_borrower INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'unreviewed',
  attributes_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(project_id, entity_type, name)
);
CREATE TABLE IF NOT EXISTS evidence_edges (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, from_type TEXT NOT NULL, from_id TEXT NOT NULL,
  relation TEXT NOT NULL, to_type TEXT NOT NULL, to_id TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL, UNIQUE(project_id,from_type,from_id,relation,to_type,to_id)
);
CREATE TABLE IF NOT EXISTS financial_tie_outs (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, subject_id TEXT NOT NULL, scope TEXT NOT NULL,
  period TEXT NOT NULL, check_key TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL,
  formula TEXT NOT NULL, input_fact_ids TEXT NOT NULL DEFAULT '[]', source_ids TEXT NOT NULL DEFAULT '[]',
  left_value REAL, right_value REAL, difference REAL, tolerance REAL NOT NULL DEFAULT 0,
  rationale TEXT NOT NULL DEFAULT '', policy_version TEXT NOT NULL DEFAULT '',
  UNIQUE(project_id,subject_id,scope,period,check_key)
);
CREATE TABLE IF NOT EXISTS report_claims (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, section_id TEXT NOT NULL, claim_index INTEGER NOT NULL,
  claim_text TEXT NOT NULL, claim_type TEXT NOT NULL, support_status TEXT NOT NULL,
  source_ids TEXT NOT NULL DEFAULT '[]', supporting_ids TEXT NOT NULL DEFAULT '[]',
  unsupported_numbers TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL,
  UNIQUE(section_id,claim_index)
);
-- 解析出的表格。上传材料与演示材料共用同一条落库路径：材料里的表格不再只是
-- 解析过程中的临时产物，报告与核对都从这份数据读。
CREATE TABLE IF NOT EXISTS document_tables (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  document_id TEXT NOT NULL,
  table_key TEXT NOT NULL DEFAULT '',
  caption TEXT NOT NULL DEFAULT '',
  unit TEXT NOT NULL DEFAULT '',
  currency TEXT NOT NULL DEFAULT 'CNY',
  scope TEXT NOT NULL DEFAULT '',
  start_page INTEGER NOT NULL DEFAULT 0,
  end_page INTEGER NOT NULL DEFAULT 0,
  continued_from INTEGER,
  headers_json TEXT NOT NULL DEFAULT '[]',
  rows_json TEXT NOT NULL DEFAULT '[]',
  notes_json TEXT NOT NULL DEFAULT '[]',
  extraction_method TEXT NOT NULL DEFAULT '',
  confidence REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'extracted',
  parser_version TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_document_tables_project ON document_tables(project_id, document_id);
CREATE TABLE IF NOT EXISTS document_table_history (
  history_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, document_id TEXT NOT NULL,
  table_id TEXT NOT NULL, parser_version TEXT NOT NULL, snapshot_json TEXT NOT NULL,
  archived_at TEXT NOT NULL
);

-- 表格单元格的人工核对记录。**只追加，不覆盖**：每次确认或更正插入一条新记录，
-- 读取时取最新一条，原始值永远留在 original_raw 里（见 PLAN_material.md Phase 6）。
CREATE TABLE IF NOT EXISTS table_cell_reviews (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  table_id TEXT NOT NULL,
  cell_id TEXT NOT NULL,
  original_raw TEXT NOT NULL DEFAULT '',
  corrected_raw TEXT NOT NULL DEFAULT '',
  decision TEXT NOT NULL DEFAULT 'confirmed',
  note TEXT NOT NULL DEFAULT '',
  reviewer TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_table_cell_reviews_lookup ON table_cell_reviews(project_id, table_id, cell_id, created_at);

CREATE TABLE IF NOT EXISTS gold_annotations (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, annotation_type TEXT NOT NULL,
  annotation_key TEXT NOT NULL, expected_json TEXT NOT NULL, source_ids TEXT NOT NULL DEFAULT '[]',
  version TEXT NOT NULL DEFAULT 'p0-v1', created_at TEXT NOT NULL,
  UNIQUE(project_id,annotation_type,annotation_key,version)
);
CREATE TABLE IF NOT EXISTS policy_snapshots (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, run_id TEXT NOT NULL,
  profile TEXT NOT NULL, version TEXT NOT NULL, sha256 TEXT NOT NULL,
  content_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_tasks (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, task_type TEXT NOT NULL,
  priority TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open', related_ids TEXT NOT NULL DEFAULT '[]',
  required_evidence TEXT NOT NULL DEFAULT '', resolution_note TEXT NOT NULL DEFAULT '',
  assignee TEXT NOT NULL DEFAULT '', due_date TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  issue_key TEXT NOT NULL DEFAULT '', issue_category TEXT NOT NULL DEFAULT '',
  affected_fact_ids TEXT NOT NULL DEFAULT '[]', affected_rule_ids TEXT NOT NULL DEFAULT '[]',
  blocking_level TEXT NOT NULL DEFAULT 'non_blocking', source_requirement TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS candidate_findings (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, taxonomy TEXT NOT NULL,
  title TEXT NOT NULL, rationale TEXT NOT NULL, potential_impact TEXT NOT NULL,
  likelihood INTEGER NOT NULL, impact INTEGER NOT NULL, severity TEXT NOT NULL,
  support_source_ids TEXT NOT NULL DEFAULT '[]', counter_source_ids TEXT NOT NULL DEFAULT '[]',
  questions TEXT NOT NULL DEFAULT '[]', mitigation TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'proposed', confidence REAL NOT NULL DEFAULT 0.5,
  decision_note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, actor TEXT NOT NULL,
  action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
  before_json TEXT NOT NULL DEFAULT '{}', after_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cross_checks (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, rule_key TEXT NOT NULL,
  title TEXT NOT NULL, severity TEXT NOT NULL, status TEXT NOT NULL,
  summary TEXT NOT NULL, values_json TEXT NOT NULL, source_ids TEXT NOT NULL,
  formula TEXT NOT NULL DEFAULT '', fact_ids TEXT NOT NULL DEFAULT '[]',
  policy_version TEXT NOT NULL DEFAULT '', thresholds TEXT NOT NULL DEFAULT '{}',
  result_value REAL, rationale TEXT NOT NULL DEFAULT '', subject_id TEXT NOT NULL DEFAULT '',
  scope TEXT NOT NULL DEFAULT 'unknown', trace_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS risks (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, severity TEXT NOT NULL,
  category TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
  impact TEXT NOT NULL, action TEXT NOT NULL, source_ids TEXT NOT NULL,
  likelihood INTEGER NOT NULL DEFAULT 2, impact_score INTEGER NOT NULL DEFAULT 2,
  source_type TEXT NOT NULL DEFAULT 'rule', candidate_id TEXT NOT NULL DEFAULT '',
  condition_text TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS report_sections (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, section_key TEXT NOT NULL,
  title TEXT NOT NULL, content TEXT NOT NULL, citation_ids TEXT NOT NULL,
  review_status TEXT NOT NULL, updated_at TEXT NOT NULL,
  support_rate REAL NOT NULL DEFAULT 0, validation_status TEXT NOT NULL DEFAULT 'pending',
  UNIQUE(project_id, section_key)
);
-- 章节生成尝试台账（第三点细化方案 §四）：区分"现正文来自哪次成功"（report_sections.
-- accepted_attempt_id）与"最新尝试是否失败"（report_sections.latest_attempt_id）。原文
-- （首稿/修复稿）仅供授权项目诊断，不进入报告成品。字段齐备即可：尝试ID/项目/章节/运行ID、
-- 预期版本及哈希、冻结输入JSON、首稿与修复稿、分阶段诊断、是否采用、调用用量及时间。
CREATE TABLE IF NOT EXISTS chapter_generation_attempts (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, section_key TEXT NOT NULL,
  run_id TEXT NOT NULL DEFAULT '',
  expected_revision INTEGER NOT NULL DEFAULT 0,
  expected_content_sha256 TEXT NOT NULL DEFAULT '',
  expected_prompt_sha256 TEXT NOT NULL DEFAULT '',
  expected_materials_version TEXT NOT NULL DEFAULT '',
  frozen_input_json TEXT NOT NULL DEFAULT '{}',
  draft_json TEXT NOT NULL DEFAULT '{}',
  repair_json TEXT NOT NULL DEFAULT '{}',
  -- 组装后正文（补回来源边界块、拼上系统表格、真正落库的那一版）与代码指纹：
  -- 与 draft_json（原始首稿）、repair_json（真实修复响应）三段分列、互不冒充。
  assembled_json TEXT NOT NULL DEFAULT '{}',
  code_fingerprint TEXT NOT NULL DEFAULT '',
  diagnostics_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'skipped', stage TEXT NOT NULL DEFAULT '',
  repair_type TEXT NOT NULL DEFAULT '', adopted INTEGER NOT NULL DEFAULT 0,
  usage_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chapter_attempts_section
  ON chapter_generation_attempts(project_id, section_key, created_at);
CREATE TABLE IF NOT EXISTS report_table_snapshots (
  project_id TEXT NOT NULL, chapter_key TEXT NOT NULL, blocks_json TEXT NOT NULL,
  input_hash TEXT NOT NULL, rule_version TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(project_id,chapter_key)
);
-- 承保/拒保/额度/费率**建议草案**与逐条复核（风控约束分层 §四 line 52~56）。
-- 不加 (project_id,section_key,recommendation_id) 唯一约束：重新生成会产生同 ID 的新行，
-- 旧行靠 status='superseded' 留档——这正是"历史建议不可被静默覆盖"的实现方式。
CREATE TABLE IF NOT EXISTS report_recommendations (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL, section_key TEXT NOT NULL,
  recommendation_id TEXT NOT NULL,
  recommendation_type TEXT NOT NULL,
  content TEXT NOT NULL, basis TEXT NOT NULL DEFAULT '',
  basis_record_ids TEXT NOT NULL DEFAULT '[]', preconditions TEXT NOT NULL DEFAULT '[]',
  parameters_json TEXT NOT NULL DEFAULT '{}',
  amount_value TEXT NOT NULL DEFAULT '', amount_unit TEXT NOT NULL DEFAULT '',
  rate_value TEXT NOT NULL DEFAULT '', rate_unit TEXT NOT NULL DEFAULT '',
  draft_text TEXT NOT NULL,
  -- pending：待确认（阻断最终交付）｜confirmed：已确认采用｜rejected：已驳回
  -- stale：确认后正文/依据变化，需重新确认｜superseded：被新一轮生成取代
  status TEXT NOT NULL DEFAULT 'pending',
  -- 确认只表示"报告采用该建议"，不代表业务担保审批（方案 §四 line 54）
  decision_scope TEXT NOT NULL DEFAULT 'report_adoption',
  decision_note TEXT NOT NULL DEFAULT '', decided_by TEXT NOT NULL DEFAULT '', decided_at TEXT,
  basis_hash TEXT NOT NULL,
  section_content_sha256 TEXT NOT NULL DEFAULT '',
  attempt_id TEXT NOT NULL DEFAULT '', input_hash TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_report_recommendations_project
  ON report_recommendations(project_id, status);
CREATE INDEX IF NOT EXISTS idx_report_recommendations_section
  ON report_recommendations(project_id, section_key, status);
-- 完成任务时冻结的不可变报告快照：每个 run 最多一份（UNIQUE(run_id)）。
-- 与 report_sections（可变的"当前正文"）彻底解耦：snapshot 一旦写入不再更新，
-- 历史报告查看只回放 payload_json，不回查当前库——材料或复核决议后续怎么变，
-- 都不会改写既有快照。payload 内连 citation 原文一起冻结，避免历史阅读时
-- 还要去打 /api/citations（那些来源可能已随材料变化）。
CREATE TABLE IF NOT EXISTS report_snapshots (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, run_id TEXT NOT NULL,
  report_revision INTEGER NOT NULL DEFAULT 0, review_revision INTEGER NOT NULL DEFAULT 0,
  validation_status TEXT NOT NULL DEFAULT '', support_rate REAL NOT NULL DEFAULT 0,
  title TEXT NOT NULL DEFAULT '', report_level TEXT NOT NULL DEFAULT '',
  subject TEXT NOT NULL DEFAULT '', schema_version TEXT NOT NULL DEFAULT 'report-snapshot-v1',
  payload_json TEXT NOT NULL DEFAULT '{}', content_sha256 TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, UNIQUE(run_id)
);
CREATE INDEX IF NOT EXISTS idx_report_snapshots_project
  ON report_snapshots(project_id, created_at DESC);
CREATE TABLE IF NOT EXISTS report_prompt_overrides (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, chapter_key TEXT NOT NULL,
  prompt TEXT NOT NULL, updated_at TEXT NOT NULL,
  structured_json TEXT NOT NULL DEFAULT '', legacy_prompt TEXT NOT NULL DEFAULT '',
  migration_mode TEXT NOT NULL DEFAULT 'structured',
  UNIQUE(project_id, chapter_key)
);
CREATE TABLE IF NOT EXISTS global_report_prompts (
  chapter_key TEXT PRIMARY KEY, prompt TEXT NOT NULL, updated_at TEXT NOT NULL,
  structured_json TEXT NOT NULL DEFAULT '', legacy_prompt TEXT NOT NULL DEFAULT '',
  migration_mode TEXT NOT NULL DEFAULT 'structured'
);
-- 报告版本化目录（report-outline，docs/report-outline-spec.md §一）：每个 scope 只存**当前版本**一行，
-- 旧版本靠 audit_events（before/after 摘要）与报告快照留档。project scope 覆盖、global scope 覆盖
-- 各自一行；`global` 行的 project_id 固定为 ''。`payload_json` 是完整目录 JSON（顺序即顺序）。
CREATE TABLE IF NOT EXISTS report_outlines (
  scope TEXT NOT NULL,                 -- 'project' | 'global'
  project_id TEXT NOT NULL DEFAULT '', -- global 行固定 ''
  outline_version TEXT NOT NULL,       -- 每次保存生成新 uuid
  payload_json TEXT NOT NULL,          -- 完整目录 JSON
  updated_at TEXT NOT NULL,
  PRIMARY KEY (scope, project_id)
);
-- 报告采用的目录（P1-1）：区分"配置目录"（report_outlines 当前版本，随时可改）与
-- "报告采用目录"（整份生成开始时冻结、完成时随报告写入）。单章再生只刷新该章条目；
-- 二级结构（章节集合/顺序）变化必须整份重新生成（chapters_json 含每章标题与三级小节）。
CREATE TABLE IF NOT EXISTS report_outline_adoptions (
  project_id TEXT PRIMARY KEY,
  outline_version TEXT NOT NULL DEFAULT '',
  structure_sha256 TEXT NOT NULL DEFAULT '',
  chapters_json TEXT NOT NULL DEFAULT '[]',
  run_id TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS citations (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, source_id TEXT NOT NULL,
  document_id TEXT NOT NULL, label TEXT NOT NULL, quote TEXT NOT NULL,
  locator_type TEXT NOT NULL, locator TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, status TEXT NOT NULL,
  current_node TEXT NOT NULL, progress INTEGER NOT NULL, message TEXT NOT NULL,
  cancel_requested INTEGER NOT NULL DEFAULT 0, started_at TEXT NOT NULL,
  finished_at TEXT, latency_ms INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
  cost REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
  policy_profile TEXT NOT NULL DEFAULT '', policy_version TEXT NOT NULL DEFAULT '',
  policy_sha256 TEXT NOT NULL DEFAULT '', review_phase TEXT NOT NULL DEFAULT 'analysis'
);
CREATE TABLE IF NOT EXISTS run_nodes (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, project_id TEXT NOT NULL,
  node_key TEXT NOT NULL, status TEXT NOT NULL, progress INTEGER NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 0, started_at TEXT, finished_at TEXT,
  input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
  cost REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', output_summary TEXT NOT NULL DEFAULT '',
  UNIQUE(run_id, node_key)
);
CREATE TABLE IF NOT EXISTS llm_attempts (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, project_id TEXT NOT NULL, node_key TEXT NOT NULL,
  task_type TEXT NOT NULL, model TEXT NOT NULL, attempt INTEGER NOT NULL,
  status TEXT NOT NULL, request_hash TEXT NOT NULL, schema_name TEXT NOT NULL DEFAULT '',
  input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
  cost REAL NOT NULL DEFAULT 0, latency_ms INTEGER NOT NULL DEFAULT 0,
  error_type TEXT NOT NULL DEFAULT '', error_message TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_artifacts (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, project_id TEXT NOT NULL, node_key TEXT NOT NULL,
  input_hash TEXT NOT NULL, artifact_type TEXT NOT NULL, artifact_ids TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL, source_run_id TEXT NOT NULL DEFAULT '', details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
  UNIQUE(run_id,node_key)
);
CREATE TABLE IF NOT EXISTS rate_events (
  id TEXT PRIMARY KEY, client_ip TEXT NOT NULL, action TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sources_project ON sources(project_id);
CREATE INDEX IF NOT EXISTS idx_retrieval_chunks_project_source ON retrieval_chunks(project_id,source_id);
CREATE INDEX IF NOT EXISTS idx_document_sections_project ON document_sections(project_id,document_id,status);
CREATE INDEX IF NOT EXISTS idx_assertions_project_key ON fact_assertions(project_id, fact_key, period);
CREATE INDEX IF NOT EXISTS idx_canonical_project_key ON canonical_facts(project_id, fact_key, period);
CREATE INDEX IF NOT EXISTS idx_metrics_project ON financial_metrics(project_id, metric_key, period);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON review_tasks(project_id, status, priority);
CREATE INDEX IF NOT EXISTS idx_candidates_project ON candidate_findings(project_id, status, severity);
CREATE INDEX IF NOT EXISTS idx_audit_project ON audit_events(project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_rate_events_lookup ON rate_events(client_ip, action, created_at);
CREATE INDEX IF NOT EXISTS idx_llm_attempts_run ON llm_attempts(run_id,node_key,created_at);
CREATE INDEX IF NOT EXISTS idx_artifacts_project_node ON run_artifacts(project_id,node_key,created_at);
CREATE INDEX IF NOT EXISTS idx_entities_project ON project_entities(project_id,entity_type);
CREATE INDEX IF NOT EXISTS idx_edges_project_from ON evidence_edges(project_id,from_type,from_id);
CREATE INDEX IF NOT EXISTS idx_edges_project_to ON evidence_edges(project_id,to_type,to_id);
CREATE INDEX IF NOT EXISTS idx_tie_outs_project ON financial_tie_outs(project_id,status,period);
CREATE INDEX IF NOT EXISTS idx_claims_project ON report_claims(project_id,support_status);
CREATE INDEX IF NOT EXISTS idx_gold_project ON gold_annotations(project_id,annotation_type);
CREATE TABLE IF NOT EXISTS bond_tranches (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, tranche_key TEXT NOT NULL,
  tranche_name TEXT NOT NULL, term_years INTEGER NOT NULL, issue_amount REAL,
  repayment_method TEXT NOT NULL DEFAULT '', maturity_date TEXT NOT NULL DEFAULT '',
  coupon_rate REAL, amount_scope TEXT NOT NULL DEFAULT '', allocation_status TEXT NOT NULL DEFAULT 'not_disclosed',
  source_ids TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL,
  extraction_method TEXT NOT NULL DEFAULT 'deterministic', confidence REAL,
  UNIQUE(project_id, tranche_key)
);
CREATE INDEX IF NOT EXISTS idx_tranches_project ON bond_tranches(project_id,tranche_key);
CREATE TABLE IF NOT EXISTS entity_profiles (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, unified_code TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'incomplete', baseline_date TEXT,
  latest_financial_period TEXT, completeness REAL NOT NULL DEFAULT 0,
  current_version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entity_profile_versions (
  id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, version INTEGER NOT NULL,
  baseline_date TEXT, data_json TEXT NOT NULL DEFAULT '{}', content_hash TEXT NOT NULL DEFAULT '',
  profile_json TEXT NOT NULL DEFAULT '{}', change_note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
  UNIQUE(profile_id,version)
);
CREATE TABLE IF NOT EXISTS entity_profile_files (
  id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, version INTEGER NOT NULL,
  filename TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'other', media_type TEXT NOT NULL DEFAULT '',
  sha256 TEXT NOT NULL DEFAULT '', size INTEGER NOT NULL DEFAULT 0,
  parse_status TEXT NOT NULL DEFAULT 'parsed', created_at TEXT NOT NULL,
  UNIQUE(profile_id,version,sha256,filename)
);
CREATE TABLE IF NOT EXISTS entity_profile_sources (
  id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, version INTEGER NOT NULL,
  filename TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'other', locator TEXT NOT NULL DEFAULT '',
  page INTEGER, quote TEXT NOT NULL DEFAULT '', file_id TEXT NOT NULL DEFAULT '',
  locator_type TEXT NOT NULL DEFAULT 'profile_locator', content_hash TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entity_profile_facts (
  id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, version INTEGER NOT NULL,
  fact_key TEXT NOT NULL, category TEXT NOT NULL DEFAULT '', value REAL,
  unit TEXT NOT NULL DEFAULT '', text_value TEXT NOT NULL DEFAULT '', period TEXT NOT NULL DEFAULT '',
  scope TEXT NOT NULL DEFAULT 'entity', status TEXT NOT NULL DEFAULT 'single_source',
  valid_until TEXT, source_ids TEXT NOT NULL DEFAULT '[]', period_type TEXT NOT NULL DEFAULT 'unknown',
  as_of_date TEXT, currency TEXT NOT NULL DEFAULT 'CNY', reviewer TEXT NOT NULL DEFAULT '', reviewed_at TEXT,
  subject_role TEXT NOT NULL DEFAULT 'issuer', raw_value TEXT NOT NULL DEFAULT '',
  confidence REAL NOT NULL DEFAULT 0.8, record_hash TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entity_profile_records (
  id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, version INTEGER NOT NULL,
  record_type TEXT NOT NULL, period TEXT NOT NULL DEFAULT '', as_of_date TEXT,
  scope TEXT NOT NULL DEFAULT 'entity', status TEXT NOT NULL DEFAULT 'single_source', valid_until TEXT,
  source_ids TEXT NOT NULL DEFAULT '[]', reviewer TEXT NOT NULL DEFAULT '', reviewed_at TEXT,
  unit TEXT NOT NULL DEFAULT '', currency TEXT NOT NULL DEFAULT 'CNY', data_json TEXT NOT NULL DEFAULT '{}',
  record_hash TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_profile_roles (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, profile_id TEXT NOT NULL,
  role TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(project_id,profile_id,role)
);
CREATE TABLE IF NOT EXISTS project_profile_snapshots (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, version INTEGER NOT NULL,
  baseline_date TEXT, roles_json TEXT NOT NULL DEFAULT '[]', facts_json TEXT NOT NULL DEFAULT '[]',
  records_json TEXT NOT NULL DEFAULT '[]', sources_json TEXT NOT NULL DEFAULT '[]', counts_json TEXT NOT NULL DEFAULT '{}',
  profile_versions_json TEXT NOT NULL DEFAULT '{}', content_hash TEXT NOT NULL,
  evidence_status TEXT NOT NULL DEFAULT 'pending', evidence_count INTEGER NOT NULL DEFAULT 0,
  evidence_error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
  UNIQUE(project_id,version)
);
CREATE TABLE IF NOT EXISTS project_profile_documents (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, snapshot_id TEXT NOT NULL,
  profile_id TEXT NOT NULL, profile_version INTEGER NOT NULL, entity_role TEXT NOT NULL,
  origin_document_id TEXT NOT NULL DEFAULT '', filename TEXT NOT NULL, kind TEXT NOT NULL DEFAULT '主体档案',
  media_type TEXT NOT NULL DEFAULT 'application/x-entity-profile', sha256 TEXT NOT NULL,
  size INTEGER NOT NULL DEFAULT 0, parse_status TEXT NOT NULL DEFAULT 'parsed', quality REAL NOT NULL DEFAULT 1,
  locator_count INTEGER NOT NULL DEFAULT 0, warning TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
  parser TEXT NOT NULL DEFAULT 'profile_snapshot', document_role TEXT NOT NULL DEFAULT 'entity_profile',
  ocr_status TEXT NOT NULL DEFAULT 'not_required', page_count INTEGER NOT NULL DEFAULT 0,
  UNIQUE(project_id,snapshot_id,profile_id,origin_document_id)
);
CREATE TABLE IF NOT EXISTS project_profile_sources (
  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, snapshot_id TEXT NOT NULL,
  profile_id TEXT NOT NULL, profile_version INTEGER NOT NULL, entity_role TEXT NOT NULL,
  document_id TEXT NOT NULL, origin_source_id TEXT NOT NULL,
  locator_type TEXT NOT NULL DEFAULT 'profile_locator', locator TEXT NOT NULL DEFAULT '', text TEXT NOT NULL,
  page INTEGER, bbox_json TEXT NOT NULL DEFAULT '[]', extraction_method TEXT NOT NULL DEFAULT 'profile_snapshot',
  content_hash TEXT NOT NULL DEFAULT '',
  UNIQUE(project_id,snapshot_id,origin_source_id)
);
CREATE TABLE IF NOT EXISTS entity_profile_imports (
  id TEXT PRIMARY KEY, profile_id TEXT, filename TEXT NOT NULL, import_format TEXT NOT NULL,
  sha256 TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'preview', payload_json TEXT NOT NULL DEFAULT '{}',
  preview_json TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
  committed_version INTEGER, created_at TEXT NOT NULL, committed_at TEXT,
  UNIQUE(profile_id,sha256,status)
);
CREATE TABLE IF NOT EXISTS entity_profile_import_files (
  id TEXT PRIMARY KEY, import_id TEXT NOT NULL, filename TEXT NOT NULL, media_type TEXT NOT NULL DEFAULT '',
  sha256 TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0, storage_path TEXT NOT NULL DEFAULT '',
  file_role TEXT NOT NULL DEFAULT 'evidence', parse_status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
  UNIQUE(import_id,sha256,file_role)
);
CREATE INDEX IF NOT EXISTS idx_profile_facts_profile ON entity_profile_facts(profile_id,version,fact_key);
CREATE INDEX IF NOT EXISTS idx_profile_records_profile ON entity_profile_records(profile_id,version,record_type);
CREATE INDEX IF NOT EXISTS idx_profile_sources_profile ON entity_profile_sources(profile_id,version);
CREATE INDEX IF NOT EXISTS idx_profile_files_profile ON entity_profile_files(profile_id,version);
CREATE INDEX IF NOT EXISTS idx_project_profile_roles ON project_profile_roles(project_id,role);
CREATE INDEX IF NOT EXISTS idx_project_snapshots ON project_profile_snapshots(project_id,version);
CREATE INDEX IF NOT EXISTS idx_project_profile_documents ON project_profile_documents(project_id,snapshot_id);
CREATE INDEX IF NOT EXISTS idx_project_profile_sources ON project_profile_sources(project_id,snapshot_id,origin_source_id);
CREATE INDEX IF NOT EXISTS idx_profile_imports_profile ON entity_profile_imports(profile_id,status,created_at);
CREATE INDEX IF NOT EXISTS idx_profile_import_files ON entity_profile_import_files(import_id,file_role);
CREATE VIEW IF NOT EXISTS analysis_documents AS
SELECT d.id,d.project_id,d.filename,d.kind,d.media_type,d.sha256,d.size,d.parse_status,d.quality,
       d.locator_count,d.warning,d.created_at,d.parser,d.document_role,d.ocr_status,d.page_count,
       'document' AS origin_type,'' AS snapshot_id,'' AS profile_id,0 AS profile_version,
       '' AS entity_role,d.id AS origin_document_id
FROM documents d
UNION ALL
SELECT d.id,d.project_id,d.filename,d.kind,d.media_type,d.sha256,d.size,d.parse_status,d.quality,
       d.locator_count,d.warning,d.created_at,d.parser,d.document_role,d.ocr_status,d.page_count,
       'profile_snapshot' AS origin_type,d.snapshot_id,d.profile_id,d.profile_version,
       d.entity_role,d.origin_document_id
FROM project_profile_documents d
JOIN projects p ON p.id=d.project_id AND p.profile_snapshot_id=d.snapshot_id;
CREATE VIEW IF NOT EXISTS analysis_sources AS
SELECT s.id,s.project_id,s.document_id,s.locator_type,s.locator,s.text,s.page,s.bbox_json,
       s.extraction_method,s.content_hash,'document' AS origin_type,'' AS snapshot_id,
       '' AS profile_id,0 AS profile_version,'' AS entity_role,s.id AS origin_source_id
FROM sources s
UNION ALL
SELECT s.id,s.project_id,s.document_id,s.locator_type,s.locator,s.text,s.page,s.bbox_json,
       s.extraction_method,s.content_hash,'profile_snapshot' AS origin_type,s.snapshot_id,
       s.profile_id,s.profile_version,s.entity_role,s.origin_source_id
FROM project_profile_sources s
JOIN projects p ON p.id=s.project_id AND p.profile_snapshot_id=s.snapshot_id;
"""


MIGRATIONS = {
    "retrieval_indexes": {"build_duration_ms": "INTEGER NOT NULL DEFAULT 0"},
    # 表名恢复来源（unit_anchor | title_hint | none）。修订稿要求"新增 caption_origin 等
    # 元数据必须保存到数据库和版本历史"——不落库就无法区分表名是强锚点得来的还是猜的。
    "document_tables": {"caption_origin": "TEXT NOT NULL DEFAULT 'none'"},
    # 尝试台账的"组装后正文"（财务修复计划 §四）：与 draft_json（原始首稿）、
    # repair_json（**真实**修复响应）并列，三者互不冒充。组装后正文是补回来源边界块、
    # 拼上系统表格、真正落进 report_sections 的那一版。
    "chapter_generation_attempts": {
        # 输出侧摘要（风控约束分层 §三.3）：输入哈希在模型调用前算定，含不了模型输出；
        # 计算轨迹的摘要单独存一列，重放时才能区分"输入没变但输出计算变了"。
        "calculation_trace_hash": "TEXT NOT NULL DEFAULT ''",
        "assembled_json": "TEXT NOT NULL DEFAULT '{}'",
        "code_fingerprint": "TEXT NOT NULL DEFAULT ''",
    },
    "projects": {"consent_at": "TEXT NOT NULL DEFAULT ''", "project_type": "TEXT NOT NULL DEFAULT 'bond_guarantee'", "bond_name": "TEXT NOT NULL DEFAULT ''",
                 "data_class": "TEXT NOT NULL DEFAULT 'user_project'", "evaluation_eligible": "INTEGER NOT NULL DEFAULT 0",
                 "issuer_profile_id": "TEXT", "profile_snapshot_id": "TEXT",
                 "analysis_target": "TEXT NOT NULL DEFAULT 'full_due_diligence'",
                 "effective_analysis_mode": "TEXT NOT NULL DEFAULT 'insufficient'",
                 "analysis_stale": "INTEGER NOT NULL DEFAULT 0",
                 "review_revision": "INTEGER NOT NULL DEFAULT 0",
                 "report_revision": "INTEGER NOT NULL DEFAULT 0",
                 "report_reviewed_revision": "INTEGER NOT NULL DEFAULT 0",
                 "report_reviewed_at": "TEXT",
                 "report_stale": "INTEGER NOT NULL DEFAULT 0",
                 "report_stale_reasons": "TEXT NOT NULL DEFAULT '[]'",
                 "counter_guarantee_status": "TEXT NOT NULL DEFAULT 'undetermined'",
                 "archived_at": "TEXT"},
    "review_tasks": {
        "issue_key": "TEXT NOT NULL DEFAULT ''", "issue_category": "TEXT NOT NULL DEFAULT ''",
        "affected_fact_ids": "TEXT NOT NULL DEFAULT '[]'", "affected_rule_ids": "TEXT NOT NULL DEFAULT '[]'",
        "blocking_level": "TEXT NOT NULL DEFAULT 'non_blocking'", "source_requirement": "TEXT NOT NULL DEFAULT ''",
    },
    "facts": {
        "fact_key": "TEXT NOT NULL DEFAULT ''",
        "raw_value": "TEXT NOT NULL DEFAULT ''",
        "extraction_method": "TEXT NOT NULL DEFAULT 'seed'",
        "review_status": "TEXT NOT NULL DEFAULT 'unreviewed'",
        "subject": "TEXT NOT NULL DEFAULT ''",
        "subject_id": "TEXT NOT NULL DEFAULT ''",
        "scope": "TEXT NOT NULL DEFAULT 'unknown'",
        "period_type": "TEXT NOT NULL DEFAULT 'unknown'",
        "origin_type": "TEXT NOT NULL DEFAULT 'document'",
        "origin_ref": "TEXT NOT NULL DEFAULT ''",
        "profile_snapshot_id": "TEXT NOT NULL DEFAULT ''",
        "assertion_status": "TEXT NOT NULL DEFAULT 'unreviewed'",
    },
    "documents": {
        "parser": "TEXT NOT NULL DEFAULT 'native'",
        "document_role": "TEXT NOT NULL DEFAULT ''",
        "ocr_status": "TEXT NOT NULL DEFAULT 'not_required'",
        "page_count": "INTEGER NOT NULL DEFAULT 0",
    },
    "bond_tranches": {
        "amount_scope": "TEXT NOT NULL DEFAULT ''",
        "allocation_status": "TEXT NOT NULL DEFAULT 'not_disclosed'",
        "extraction_method": "TEXT NOT NULL DEFAULT 'deterministic'",
        "confidence": "REAL",
    },
    "sources": {
        "page": "INTEGER",
        "bbox_json": "TEXT NOT NULL DEFAULT '[]'",
        "extraction_method": "TEXT NOT NULL DEFAULT 'native'",
        "content_hash": "TEXT NOT NULL DEFAULT ''",
    },
    "fact_assertions": {
        "subject_id": "TEXT NOT NULL DEFAULT ''", "scope": "TEXT NOT NULL DEFAULT 'unknown'",
        "period_type": "TEXT NOT NULL DEFAULT 'unknown'",
        "origin_type": "TEXT NOT NULL DEFAULT 'document'", "origin_ref": "TEXT NOT NULL DEFAULT ''",
        "profile_snapshot_id": "TEXT NOT NULL DEFAULT ''", "assertion_status": "TEXT NOT NULL DEFAULT 'unreviewed'",
    },
    "canonical_facts": {
        "subject_id": "TEXT NOT NULL DEFAULT ''", "scope": "TEXT NOT NULL DEFAULT 'unknown'",
        "period_type": "TEXT NOT NULL DEFAULT 'unknown'",
        "resolution_basis_hash": "TEXT NOT NULL DEFAULT ''",
    },
    "financial_metrics": {
        "subject_id": "TEXT NOT NULL DEFAULT ''", "scope": "TEXT NOT NULL DEFAULT 'unknown'",
        "trace_json": "TEXT NOT NULL DEFAULT '{}'",
    },
    "cross_checks": {
        "formula": "TEXT NOT NULL DEFAULT ''",
        "fact_ids": "TEXT NOT NULL DEFAULT '[]'",
        "policy_version": "TEXT NOT NULL DEFAULT ''",
        "thresholds": "TEXT NOT NULL DEFAULT '{}'",
        "result_value": "REAL",
        "rationale": "TEXT NOT NULL DEFAULT ''",
        "subject_id": "TEXT NOT NULL DEFAULT ''",
        "scope": "TEXT NOT NULL DEFAULT 'unknown'",
        "trace_json": "TEXT NOT NULL DEFAULT '{}'",
    },
    "risks": {
        "likelihood": "INTEGER NOT NULL DEFAULT 2",
        "impact_score": "INTEGER NOT NULL DEFAULT 2",
        "source_type": "TEXT NOT NULL DEFAULT 'rule'",
        "candidate_id": "TEXT NOT NULL DEFAULT ''",
        "condition_text": "TEXT NOT NULL DEFAULT ''",
    },
    "runs": {
        "policy_profile": "TEXT NOT NULL DEFAULT ''",
        "policy_version": "TEXT NOT NULL DEFAULT ''",
        "policy_sha256": "TEXT NOT NULL DEFAULT ''",
        "review_phase": "TEXT NOT NULL DEFAULT 'analysis'",
        "resumed_from_run_id": "TEXT NOT NULL DEFAULT ''",
    },
    "run_artifacts": {"details_json": "TEXT NOT NULL DEFAULT '{}'"},
    "report_sections": {
        # 计算轨迹随正文一起冻结（风控约束分层 §三.1）：报告接口与导出要能回答
        # "这一章算了哪些指标、哪些通过复算"。输出侧摘要与 input_hash 分列。
        "calculations_json": "TEXT NOT NULL DEFAULT '[]'",
        "calculation_trace_hash": "TEXT NOT NULL DEFAULT ''",
        "support_rate": "REAL NOT NULL DEFAULT 0",
        "validation_status": "TEXT NOT NULL DEFAULT 'pending'",
        "prompt_version": "TEXT NOT NULL DEFAULT ''",
        "prompt_sha256": "TEXT NOT NULL DEFAULT ''",
        "input_hash": "TEXT NOT NULL DEFAULT ''",
        "fallback_reason": "TEXT NOT NULL DEFAULT ''",
        "allowed_source_ids": "TEXT NOT NULL DEFAULT '[]'",
        # 生成状态与证据支持状态分离：validation_status 是证据侧兼容字段，
        # 报告支持校验只允许更新它，不得覆盖 generation_status / fallback_reason。
        "generation_status": "TEXT NOT NULL DEFAULT ''",
        "support_validation_status": "TEXT NOT NULL DEFAULT ''",
        "validation_diagnostics": "TEXT NOT NULL DEFAULT '{}'",
        # 结构化允许记录（受控表格的业务格：带符号数值 + 单位 + 科目 + 期间 + 来源 + 页码）
        # 在**写入时冻结**。章节校验用它比对数字，后置支持校验也读它——这样"记录"才真正
        # 贯通两道校验（修订稿 §3.1："记录随章节上下文冻结并持久化…同时传入章节校验与
        # 后置支持校验"）。存 JSON 文本，供审计与校验复用，不参与其他查询。
        "table_records_json": "TEXT NOT NULL DEFAULT '[]'",
        # 统一冻结记录（三项遗留修复方案 §一）：fact/table/metric/rule 四类一次冻结，
        # 章节校验与后置支持校验读取同一份；旧行缺省为空，读取时兼容 table_records_json。
        "evidence_records_json": "TEXT NOT NULL DEFAULT '[]'",
        # 交付一致性：正文真实来源、正文哈希、最近一次完整交付校验对应的哈希、
        # 回退发生阶段、强制披露是否完整、是否可交付。后置回退必须同步这些字段，
        # 否则"正文已是模板、界面仍显示模型已采用"。
        "content_origin": "TEXT NOT NULL DEFAULT ''",
        "content_sha256": "TEXT NOT NULL DEFAULT ''",
        "validated_content_sha256": "TEXT NOT NULL DEFAULT ''",
        "fallback_stage": "TEXT NOT NULL DEFAULT 'none'",
        "disclosure_status": "TEXT NOT NULL DEFAULT 'not_checked'",
        "delivery_status": "TEXT NOT NULL DEFAULT 'not_checked'",
        # 生成时冻结的强制披露（冲突块、来源边界块）与确定性草稿：后置回退用它构建
        # 候选正文，避免"回退成裸模板"导致冲突披露丢失。
        "required_disclosures": "TEXT NOT NULL DEFAULT '[]'",
        "deterministic_draft": "TEXT NOT NULL DEFAULT ''",
        # 生成当时的篇幅诊断（历史报告无此值，读取时按 not_checked 处理）
        "length_status": "TEXT NOT NULL DEFAULT 'not_checked'",
        "raw_output_chars": "INTEGER NOT NULL DEFAULT 0",
        "visible_content_chars": "INTEGER NOT NULL DEFAULT 0",
        "effective_min_chars": "INTEGER NOT NULL DEFAULT 0",
        "effective_max_chars": "INTEGER NOT NULL DEFAULT 0",
        "length_repair_attempted": "INTEGER NOT NULL DEFAULT 0",
        # 尝试台账外键（第三点细化方案 §四）：accepted_attempt_id = 现正文来自哪次成功；
        # latest_attempt_id = 最新一次尝试（可能是失败/待核验/跳过）。默认空保持旧行兼容。
        "accepted_attempt_id": "TEXT NOT NULL DEFAULT ''",
        "latest_attempt_id": "TEXT NOT NULL DEFAULT ''",
    },
    "report_prompt_overrides": {
        "structured_json": "TEXT NOT NULL DEFAULT ''",
        "legacy_prompt": "TEXT NOT NULL DEFAULT ''",
        "migration_mode": "TEXT NOT NULL DEFAULT 'structured'",
    },
    "global_report_prompts": {
        "structured_json": "TEXT NOT NULL DEFAULT ''",
        "legacy_prompt": "TEXT NOT NULL DEFAULT ''",
        "migration_mode": "TEXT NOT NULL DEFAULT 'structured'",
    },
    "entity_profile_versions": {
        "content_hash": "TEXT NOT NULL DEFAULT ''", "profile_json": "TEXT NOT NULL DEFAULT '{}'",
        "change_note": "TEXT NOT NULL DEFAULT ''",
    },
    "entity_profile_sources": {
        "file_id": "TEXT NOT NULL DEFAULT ''", "locator_type": "TEXT NOT NULL DEFAULT 'profile_locator'",
        "content_hash": "TEXT NOT NULL DEFAULT ''",
    },
    "entity_profile_facts": {
        "period_type": "TEXT NOT NULL DEFAULT 'unknown'", "subject_role": "TEXT NOT NULL DEFAULT 'issuer'",
        "raw_value": "TEXT NOT NULL DEFAULT ''", "confidence": "REAL NOT NULL DEFAULT 0.8",
        "record_hash": "TEXT NOT NULL DEFAULT ''", "as_of_date": "TEXT", "currency": "TEXT NOT NULL DEFAULT 'CNY'",
        "reviewer": "TEXT NOT NULL DEFAULT ''", "reviewed_at": "TEXT",
    },
    "project_profile_snapshots": {
        "evidence_status": "TEXT NOT NULL DEFAULT 'pending'", "evidence_count": "INTEGER NOT NULL DEFAULT 0",
        "evidence_error": "TEXT NOT NULL DEFAULT ''", "records_json": "TEXT NOT NULL DEFAULT '[]'",
    },
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# WAL 模式的库，其 ``-wal``/``-shm`` 只在"至少有一个连接打开"期间存在：最后一个连接关闭时
# SQLite 会做 checkpoint 并把它们删掉。而 ``connect()`` 是**每条语句新建、用完即关**
# （实测一次工作流运行近 2000 个连接），并发线程下新连接会撞上这个清理瞬间、被 SQLite 以
# 只读方式打开，于是它的第一条写语句抛 ``SQLITE_READONLY: attempt to write a readonly
# database``——实测失败瞬间 ``query_only=0``、库文件与目录均可写、另开一条新连接写入成功，
# 只有那一条连接被判只读，所以不是权限问题。保留一个**进程常驻连接**即可消除该竞态。
# 对照实验：常驻 keeper / 按线程复用连接 / 关 WAL 三者任一都能通过；选改动最小、且保留
# WAL 并发能力的 keeper 方案。
_WAL_KEEPER: sqlite3.Connection | None = None


def keep_wal_connection_alive() -> sqlite3.Connection:
    """建立（或复用）进程常驻连接，避免 WAL 的 ``-wal``/``-shm`` 被反复清理。"""
    global _WAL_KEEPER
    if _WAL_KEEPER is None:
        _WAL_KEEPER = sqlite3.connect(str(settings.database_path), timeout=30, check_same_thread=False)
        _WAL_KEEPER.row_factory = sqlite3.Row
        _WAL_KEEPER.execute("PRAGMA journal_mode=WAL").fetchall()
    return _WAL_KEEPER


@contextmanager
def connect():
    database_path = settings.database_path
    conn = sqlite3.connect(str(database_path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    except sqlite3.Error as exc:
        logger.exception(
            "SQLite operation failed: database=%s code=%s name=%s",
            database_path,
            getattr(exc, "sqlite_errorcode", None),
            getattr(exc, "sqlite_errorname", "unknown"),
        )
        raise
    finally:
        conn.close()


def _apply_p0_migration(conn: sqlite3.Connection) -> None:
    point_in_time = (
        "accounts_receivable","inventory","prepayments","other_receivables","cash","fixed_assets",
        "total_assets","total_liabilities","equity","current_assets","current_liabilities",
        "interest_bearing_debt","short_term_debt","long_term_debt","debt_due_12m","credit_limit",
        "utilized_credit","overdue_debt","external_guarantee","contingent_liabilities",
        "collateral_appraised_value","collateral_discounted_value","prior_lien_amount",
    )
    placeholders = ",".join("?" for _ in point_in_time)
    for project in conn.execute("SELECT id,company_name FROM projects").fetchall():
        borrower_id = f"entity-{project['id']}-borrower"
        conn.execute(
            "INSERT OR IGNORE INTO project_entities (id,project_id,entity_type,name,is_borrower,status,attributes_json,created_at,updated_at) "
            "VALUES (?,?,?,?,1,'confirmed','{}',?,?)",
            (borrower_id, project["id"], "enterprise", project["company_name"], now_iso(), now_iso()),
        )
        for table in ("facts", "fact_assertions", "canonical_facts"):
            conn.execute(
                f"UPDATE {table} SET subject=CASE WHEN subject='' THEN ? ELSE subject END,"
                "subject_id=CASE WHEN subject_id='' THEN ? ELSE subject_id END,"
                "scope=CASE WHEN scope='unknown' THEN CASE WHEN category IN ('主体','主体治理') THEN 'entity' "
                "WHEN category IN ('融资申请','用途','担保方案') THEN 'transaction' ELSE 'standalone' END ELSE scope END,"
                f"period_type=CASE WHEN period_type='unknown' THEN CASE WHEN period='' THEN 'undated' WHEN fact_key IN ({placeholders}) "
                "THEN 'point_in_time' ELSE 'duration' END ELSE period_type END WHERE project_id=?",
                (project["company_name"], borrower_id, *point_in_time, project["id"]),
            )

    conn.executescript("""
    CREATE TABLE fact_assertions_p0 (
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL, fact_key TEXT NOT NULL,
      subject TEXT NOT NULL DEFAULT '', subject_id TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT 'unknown',
      period TEXT NOT NULL DEFAULT '', period_type TEXT NOT NULL DEFAULT 'unknown', category TEXT NOT NULL,
      name TEXT NOT NULL, value REAL, unit TEXT NOT NULL DEFAULT '', text_value TEXT NOT NULL DEFAULT '',
      raw_value TEXT NOT NULL DEFAULT '', source_ids TEXT NOT NULL DEFAULT '[]', confidence REAL NOT NULL DEFAULT 0.8,
      extraction_method TEXT NOT NULL, created_at TEXT NOT NULL,
      UNIQUE(project_id,fact_key,subject_id,scope,period,raw_value,source_ids,extraction_method)
    );
    INSERT OR IGNORE INTO fact_assertions_p0
      SELECT id,project_id,fact_key,subject,subject_id,scope,period,period_type,category,name,value,unit,text_value,
             raw_value,source_ids,confidence,extraction_method,created_at FROM fact_assertions;
    DROP TABLE fact_assertions;
    ALTER TABLE fact_assertions_p0 RENAME TO fact_assertions;

    CREATE TABLE canonical_facts_p0 (
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL, fact_key TEXT NOT NULL,
      subject TEXT NOT NULL DEFAULT '', subject_id TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT 'unknown',
      period TEXT NOT NULL DEFAULT '', period_type TEXT NOT NULL DEFAULT 'unknown', category TEXT NOT NULL,
      name TEXT NOT NULL, status TEXT NOT NULL, selected_assertion_id TEXT,
      value REAL, unit TEXT NOT NULL DEFAULT '', text_value TEXT NOT NULL DEFAULT '',
      assertion_ids TEXT NOT NULL DEFAULT '[]', resolution_note TEXT NOT NULL DEFAULT '',
      resolved_by TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
      UNIQUE(project_id,fact_key,subject_id,scope,period)
    );
    INSERT OR IGNORE INTO canonical_facts_p0
      SELECT id,project_id,fact_key,subject,subject_id,scope,period,period_type,category,name,status,
             selected_assertion_id,value,unit,text_value,assertion_ids,resolution_note,resolved_by,updated_at FROM canonical_facts;
    DROP TABLE canonical_facts;
    ALTER TABLE canonical_facts_p0 RENAME TO canonical_facts;

    CREATE TABLE financial_metrics_p0 (
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL, metric_key TEXT NOT NULL,
      name TEXT NOT NULL, period TEXT NOT NULL DEFAULT '', value REAL, unit TEXT NOT NULL,
      formula TEXT NOT NULL, input_fact_ids TEXT NOT NULL DEFAULT '[]', source_ids TEXT NOT NULL DEFAULT '[]',
      status TEXT NOT NULL DEFAULT 'calculated', policy_version TEXT NOT NULL DEFAULT '',
      subject_id TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT 'unknown', trace_json TEXT NOT NULL DEFAULT '{}',
      UNIQUE(project_id,metric_key,subject_id,scope,period)
    );
    INSERT OR IGNORE INTO financial_metrics_p0
      SELECT id,project_id,metric_key,name,period,value,unit,formula,input_fact_ids,source_ids,status,policy_version,
             subject_id,scope,trace_json FROM financial_metrics;
    DROP TABLE financial_metrics;
    ALTER TABLE financial_metrics_p0 RENAME TO financial_metrics;
    CREATE INDEX idx_assertions_project_key ON fact_assertions(project_id,fact_key,subject_id,scope,period);
    CREATE INDEX idx_canonical_project_key ON canonical_facts(project_id,fact_key,subject_id,scope,period);
    CREATE INDEX idx_metrics_project ON financial_metrics(project_id,metric_key,subject_id,scope,period);
    """)
    conn.execute("INSERT INTO schema_migrations (version,name,applied_at) VALUES (3,'p0_entity_scope_evidence_model',?)", (now_iso(),))


def _ensure_additive_columns(conn: sqlite3.Connection) -> None:
    for table, columns in MIGRATIONS.items():
        existing = {item[1] for item in conn.execute(f"PRAGMA table_info({table})")}
        for column, definition in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _apply_profile_contract_v1(conn: sqlite3.Connection) -> None:
    # Views deliberately expose uploaded evidence together with only the
    # currently selected immutable profile snapshot. Historical projection
    # rows remain available for audit but cannot leak into a new analysis.
    applied = conn.execute("SELECT 1 FROM schema_migrations WHERE version=5").fetchone()
    existing_views = {
        item["name"]
        for item in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view' AND name IN ('analysis_documents','analysis_sources')"
        ).fetchall()
    }
    # `init_db()` is called at every application lifespan. Dropping the views
    # on each call leaves concurrent workflow workers a brief interval where
    # their evidence query fails with "no such table". Once the migration has
    # been applied, retain valid views and only repair an unexpectedly missing
    # one.
    if applied and existing_views == {"analysis_documents", "analysis_sources"}:
        return
    conn.executescript("""
    DROP VIEW IF EXISTS analysis_sources;
    DROP VIEW IF EXISTS analysis_documents;
    CREATE VIEW analysis_documents AS
    SELECT d.id,d.project_id,d.filename,d.kind,d.media_type,d.sha256,d.size,d.parse_status,d.quality,
           d.locator_count,d.warning,d.created_at,d.parser,d.document_role,d.ocr_status,d.page_count,
           'document' AS origin_type,'' AS snapshot_id,'' AS profile_id,0 AS profile_version,
           '' AS entity_role,d.id AS origin_document_id
    FROM documents d
    UNION ALL
    SELECT d.id,d.project_id,d.filename,d.kind,d.media_type,d.sha256,d.size,d.parse_status,d.quality,
           d.locator_count,d.warning,d.created_at,d.parser,d.document_role,d.ocr_status,d.page_count,
           'profile_snapshot' AS origin_type,d.snapshot_id,d.profile_id,d.profile_version,
           d.entity_role,d.origin_document_id
    FROM project_profile_documents d
    JOIN projects p ON p.id=d.project_id AND p.profile_snapshot_id=d.snapshot_id;
    CREATE VIEW analysis_sources AS
    SELECT s.id,s.project_id,s.document_id,s.locator_type,s.locator,s.text,s.page,s.bbox_json,
           s.extraction_method,s.content_hash,'document' AS origin_type,'' AS snapshot_id,
           '' AS profile_id,0 AS profile_version,'' AS entity_role,s.id AS origin_source_id
    FROM sources s
    UNION ALL
    SELECT s.id,s.project_id,s.document_id,s.locator_type,s.locator,s.text,s.page,s.bbox_json,
           s.extraction_method,s.content_hash,'profile_snapshot' AS origin_type,s.snapshot_id,
           s.profile_id,s.profile_version,s.entity_role,s.origin_source_id
    FROM project_profile_sources s
    JOIN projects p ON p.id=s.project_id AND p.profile_snapshot_id=s.snapshot_id;
    """)
    if not applied:
        conn.execute(
            "INSERT INTO schema_migrations (version,name,applied_at) VALUES (5,'profile_contract_v1',?)",
            (now_iso(),),
        )


def init_db() -> None:
    # Runtime import avoids a module cycle while allowing legacy labels to be
    # upgraded through the same registry used by current extraction.
    from .facts import canonical_key

    with connect() as conn:
        conn.executescript(SCHEMA)
        _ensure_additive_columns(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_project_key ON facts(project_id, fact_key, period)")
        applied = conn.execute("SELECT 1 FROM schema_migrations WHERE version=1").fetchone()
        if not applied:
            legacy = conn.execute("SELECT * FROM facts").fetchall()
            for fact in legacy:
                assertion_id = str(fact["id"])
                fact_key = fact["fact_key"] or canonical_key(fact["name"])
                conn.execute("UPDATE facts SET fact_key=? WHERE id=?", (fact_key, assertion_id))
                conn.execute(
                    "INSERT OR IGNORE INTO fact_assertions (id,project_id,fact_key,subject,period,category,name,value,unit,text_value,raw_value,source_ids,confidence,extraction_method,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (assertion_id, fact["project_id"], fact_key, "", fact["period"], fact["category"], fact["name"],
                     fact["value"], fact["unit"], fact["text_value"], fact["raw_value"], fact["source_ids"], fact["confidence"],
                     fact["extraction_method"], now_iso()),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO canonical_facts (id,project_id,fact_key,subject,period,category,name,status,selected_assertion_id,value,unit,text_value,assertion_ids,resolution_note,resolved_by,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), fact["project_id"], fact_key, "", fact["period"], fact["category"], fact["name"],
                     "confirmed" if fact["review_status"] == "human_verified" else "unreviewed", assertion_id, fact["value"], fact["unit"],
                     fact["text_value"], json_dump([assertion_id]), "兼容迁移", "human" if fact["review_status"] == "human_verified" else "system", now_iso()),
                )
            conn.execute("INSERT INTO schema_migrations (version,name,applied_at) VALUES (1,'professional_fact_model',?)", (now_iso(),))
        repaired = conn.execute("SELECT 1 FROM schema_migrations WHERE version=2").fetchone()
        if not repaired:
            for fact in conn.execute("SELECT id,name,fact_key FROM facts WHERE fact_key='' OR fact_key IS NULL").fetchall():
                fact_key = canonical_key(fact["name"])
                conn.execute("UPDATE facts SET fact_key=? WHERE id=?", (fact_key, fact["id"]))
                conn.execute("UPDATE fact_assertions SET fact_key=? WHERE id=?", (fact_key, fact["id"]))
                conn.execute("UPDATE canonical_facts SET fact_key=? WHERE selected_assertion_id=?", (fact_key, fact["id"]))
            conn.execute("INSERT INTO schema_migrations (version,name,applied_at) VALUES (2,'repair_legacy_fact_keys',?)", (now_iso(),))
        p0_applied = conn.execute("SELECT 1 FROM schema_migrations WHERE version=3").fetchone()
        if not p0_applied:
            _apply_p0_migration(conn)
        hardening_applied = conn.execute("SELECT 1 FROM schema_migrations WHERE version=4").fetchone()
        if not hardening_applied:
            conn.execute(
                "INSERT INTO schema_migrations (version,name,applied_at) VALUES (4,'llm_gateway_and_run_artifacts',?)",
                (now_iso(),),
            )
        # The v3 table rebuild intentionally targets its historical schema.
        # Reapply additive columns afterwards before creating views that form
        # the frozen profile contract.
        _ensure_additive_columns(conn)
        _apply_profile_contract_v1(conn)
    # 建库之后立刻持有常驻连接：此后 -wal/-shm 不会被反复清理（见 connect 上方的说明）。
    keep_wal_connection_alive()


def recover_interrupted_runs() -> int:
    """Close active rows left behind when the API process stopped."""
    finished_at = now_iso()
    reason = "服务重启：上次运行被中断，请重新启动分析"
    with connect() as conn:
        interrupted = conn.execute(
            "SELECT id,project_id FROM runs WHERE status IN ('queued','running')"
        ).fetchall()
        if not interrupted:
            return 0
        run_ids = [item["id"] for item in interrupted]
        placeholders = ",".join("?" for _ in run_ids)
        conn.execute(
            f"UPDATE run_nodes SET status='failed',finished_at=?,error=? "
            f"WHERE run_id IN ({placeholders}) AND status IN ('queued','running')",
            (finished_at, reason, *run_ids),
        )
        conn.execute(
            f"UPDATE runs SET status='failed',message='运行因服务重启中断',error=?,finished_at=? "
            f"WHERE id IN ({placeholders})",
            (reason, finished_at, *run_ids),
        )
        project_ids = sorted({item["project_id"] for item in interrupted})
        project_placeholders = ",".join("?" for _ in project_ids)
        conn.execute(
            f"UPDATE projects SET status='failed',analysis_stale=1,updated_at=? "
            f"WHERE id IN ({project_placeholders}) AND status='running'",
            (finished_at, *project_ids),
        )
    return len(run_ids)


def rows(query: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(row) for row in conn.execute(query, tuple(params)).fetchall()]


def row(query: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    result = rows(query, params)
    return result[0] if result else None


def execute(query: str, params: Iterable[Any] = ()) -> None:
    with connect() as conn:
        conn.execute(query, tuple(params))


def executemany(query: str, values: Iterable[Iterable[Any]]) -> None:
    with connect() as conn:
        conn.executemany(query, values)


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def json_load(value: str) -> Any:
    return json.loads(value) if value else []
