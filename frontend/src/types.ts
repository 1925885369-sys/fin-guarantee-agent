export type Severity = "high" | "medium" | "low";
export type CheckStatus = "passed" | "warning" | "insufficient";
export type EntityRole = "issuer" | "guarantor" | "counter_guarantor";
export type AnalysisTarget = "full_due_diligence";
export type LegacyAnalysisTarget = AnalysisTarget | "core_pre_review";
export type EffectiveAnalysisMode = LegacyAnalysisTarget | "insufficient";
export type ProfileFactStatus = "confirmed" | "single_source" | "stale" | "conflicted" | "insufficient";
export type EntityProfileStatus = "ready" | "stale" | "conflicted" | "incomplete";

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
  counts?: Partial<Record<ProfileFactStatus, number>>;
}

export interface EntityProfileFact {
  id: string;
  fact_key: string;
  name: string;
  category: string;
  value: string | number | null;
  text_value?: string;
  unit?: string;
  period?: string | null;
  scope?: string | null;
  status: ProfileFactStatus;
  valid_until?: string | null;
  source_label?: string | null;
  source_id?: string | null;
  page?: number | null;
  source_ids?: string[];
}

export interface EntityProfileDetail extends EntityProfileSummary {
  facts: EntityProfileFact[];
  sources?: Array<{
    id: string;
    filename: string;
    kind: string;
    locator: string;
    page: number | null;
    quote: string;
  }>;
  data?: Record<string, unknown>;
  aliases?: string[];
  updated_at?: string;
}

export interface EntityProfileReadiness {
  profile_id: string;
  ready_for_project: boolean;
  complete_due_diligence_ready: boolean;
  completeness: number;
  blocking_reasons: string[];
  notices: string[];
  counts: Partial<Record<ProfileFactStatus, number>>;
}

export interface ProjectProfileSnapshot {
  id: string;
  version: number;
  baseline_date: string | null;
  created_at: string;
  roles: Array<{
    role: EntityRole; profile_id: string; name: string; profile_version: number;
    status: EntityProfileStatus; completeness: number; baseline_date: string | null;
    latest_financial_period: string | null;
  }>;
  counts: { confirmed: number; single_source: number; stale: number; conflicted: number; insufficient: number };
  facts: Array<EntityProfileFact & { profile_id: string; role: EntityRole }>;
  records: Array<Record<string, unknown> & { profile_id: string; role: EntityRole; record_type: string }>;
  sources: Array<{
    id: string; filename: string; kind: string; locator: string; page: number | null; quote: string;
    profile_id: string; role: EntityRole;
  }>;
  profile_versions: Record<string, number>;
  content_hash: string;
}

export interface ReviewSummary {
  total_unique_issues: number;
  conflict_count: number;
  candidate_count: number;
  action_package_count: number;
  information_notice_count: number;
  review_revision: number;
  report_revision: number;
  report_reviewed_revision: number;
  report_reviewed_at: string | null;
  report_stale: boolean;
  report_stale_reasons: string[];
  report_state: "missing" | "draft_stale" | "draft_current" | "final_ready";
}

/** 复核更新后的受影响章节（/review/apply 返回）：再生入口所需字段由后端计算下发。 */
export interface AffectedSection {
  section_key: string;
  title: string;
  /** 是否模型章节（确定性章节由系统重算，没有"手动再生"入口） */
  llm: boolean;
  /** 是否可手动再生（恒 ⊆ llm） */
  regenerable: boolean;
  regenerate_endpoint: string;
}

/** /review/apply 的返回：复核汇总 + 受影响章节与再生入口（复核更新后显示用）。 */
export interface ReviewApplyResult extends ReviewSummary {
  affected_section_keys: string[];
  affected_sections?: AffectedSection[];
  affected_model_section_keys?: string[];
  regenerable_section_keys?: string[];
  applied: boolean;
  quality?: ReportQuality;
}

export interface ProjectCreatePayload {
  company_name: string;
  amount: number;
  term_months: number;
  purpose: string;
  bond_name?: string;
  data_consent: boolean;
  project_type: "bond_guarantee";
  issuer_profile_id?: string | null;
  guarantor_name: string;
  counter_guarantor_names: string[];
  guarantor_profile_id?: string | null;
  counter_guarantor_profile_ids?: string[];
  counter_guarantee_status: "exists" | "not_applicable" | "undetermined";
  analysis_target: AnalysisTarget;
}

export interface ProjectSummary {
  id: string;
  company_name: string;
  amount: number;
  term_months: number;
  purpose: string;
  status: string;
  project_type: "bond_guarantee";
  bond_name: string;
  data_class: "fictional_demo" | "real_reference" | "user_project";
  evaluation_eligible: number | boolean;
  /** 列表接口本就返回该列：用于提示「已绑定主体档案的项目，改名不会更换发行主体」。 */
  issuer_profile_id?: string | null;
  created_at?: string;
  updated_at?: string;
  archived_at?: string | null;
}

/** 项目基础信息修改：三个字段都可选，只提交真正要改的那几个。 */
export interface ProjectUpdatePayload {
  company_name?: string;
  bond_name?: string;
  amount?: number;
}

/** PATCH 返回更新后的项目详情（不含访问令牌），changed 表示是否真的写入了变化。 */
export type ProjectUpdateResult = Project & { changed: boolean };

export interface Project extends ProjectSummary {
  created_at: string;
  updated_at: string;
  stats: { documents: number; facts: number; checks: number; risks: number; highRisks: number };
  issuer_profile_id?: string | null;
  guarantor_profile_id?: string | null;
  counter_guarantor_profile_ids?: string[];
  analysis_target?: LegacyAnalysisTarget;
  effective_analysis_mode?: EffectiveAnalysisMode;
  profile_snapshot_id?: string | null;
  analysis_stale?: boolean;
  review_revision?: number;
  report_revision?: number;
  report_reviewed_revision?: number;
  report_reviewed_at?: string | null;
  report_stale?: boolean;
  report_stale_reasons?: string[];
  report_state?: "missing" | "draft_stale" | "draft_current" | "final_ready";
  archived_at?: string | null;
}

export interface ReportPrompt {
  chapter_key: string;
  title: string;
  /** 章节所属的报告分部（中文，如“第二部分 财务、偿债与评级”），用于导航分组 */
  part?: string;
  prompt: string;
  default_prompt: string;
  editable_config: PromptEditableConfig;
  factory_config: PromptEditableConfig;
  constraints: PromptConstraints;
  source: "project" | "global" | "system" | string;
  migration_mode: "structured" | "parsed_legacy" | "preserved_legacy" | string;
  overridden: boolean;
  updated_at: string | null;
  llm: boolean;
  required_sections: string[];
  /** 篇幅的运行时口径（后端计算，前端只展示）：当前章节 Prompt 的目标范围 */
  length_policy?: LengthPolicy;
  /** 该章是否存在网页设置的“全局默认”(对所有未单独覆盖项目生效) */
  global_overridden?: boolean;
  global_updated_at?: string | null;
  /** 项目级编辑权限（后端按环境/本机直连判定；真实参考项目仅本机直连、非公开环境可编辑） */
  editable?: boolean;
  /** 全局默认是否可编辑：非公开环境 + 本机直连 + 无代理转发头时为 true
   *  （`PLAN.md` P0-A；旧口径"恒 false"已被替代） */
  global_editable?: boolean;
  /** 全局默认不可编辑的原因（对应 editable 的 edit_reason） */
  global_edit_reason?: string;
  /** 当前请求的范围：project（项目配置）/ global（全局默认），与 `?scope=` 一致 */
  scope?: "project" | "global" | string;
  /** **全局链**生效的 Prompt（全局覆盖 > 出厂 YAML，不掺项目覆盖），"全局默认"页据此回填 */
  global_prompt?: string;
  /** 全局链来源：global（有全局覆盖）/ system（出厂） */
  global_source?: "project" | "global" | "system" | string;
  /** 章节当前**生效**来源：项目覆盖 > 全局默认 > 出厂。有项目覆盖时项目仍优先 */
  effective_source?: "project" | "global" | "system" | string;
  /** **项目级**并发基准哈希（保存/重置项目配置时回传，不符则后端 409） */
  config_sha256?: string;
  /** **全局级**并发基准哈希（保存/重置全局默认时回传，不符则后端 409） */
  global_config_sha256?: string;
  /** 当前 Prompt 编译器版本：升级后同一份结构化配置会渲染出不同文本，
   *  客户端按旧版本编辑时保存会被 409（`PLAN.md` §四.2） */
  prompt_contract_version?: string;
  /** 项目级只读原因（editable=false 时展示给用户） */
  edit_reason?: string;
  /** 是否真实参考项目（只读提示与展示口径用） */
  real_reference?: boolean;
}

/** 篇幅目标：就是用户在章节 Prompt 里设置的区间，没有第二套系统上限。 */
export interface LengthPolicy {
  effective_min_chars?: number;
  effective_max_chars?: number;
  unit?: string;
  source?: string;
}

// ---------------------------------------------------------------------------
// 报告目录配置（report-outline）：契约见 `docs/report-outline-spec.md` §六
// ---------------------------------------------------------------------------

/** 三级小节：章内唯一且稳定（改名不换 ID）；`prompt` 空串＝沿用章级要求；`enabled=false`＝暂不参与生成。 */
export interface OutlineSubsection {
  subsection_id: string;
  title: string;
  order: number;
  prompt: string;
  enabled: boolean;
}

/**
 * 二级章节：`template_key` 内置＝yaml 键、自定义＝null（ID 为 `chapter-<uuid>`）。
 * `generation_mode` / `evidence_scope` 由服务端派生并重写，前端只读展示。
 */
export interface OutlineChapter {
  chapter_id: string;
  template_key: string | null;
  title: string;
  order: number;
  generation_mode: "generate" | "template";
  evidence_scope: "chapter_contract" | "prompt_retrieval";
  subsections: OutlineSubsection[];
}

/** 一级分部：内置固定 `part-1`~`part-6`，页面不编辑（仅原样透传）。 */
export interface OutlinePart {
  part_id: string;
  title: string;
  order: number;
  chapters: OutlineChapter[];
}

/** 完整目录：顺序即顺序，`order` 每次保存由服务端重排。 */
export interface ReportOutline {
  outline_version: string;
  parts: OutlinePart[];
}

/** `GET/PUT/DELETE /report-outline` 的响应（也含各 scope 的可编辑判据）。 */
export interface ReportOutlineState {
  scope: "project" | "global";
  outline: ReportOutline;
  source: string;
  outline_version: string;
  config_sha256: string;
  effective_source: string;
  has_project_override: boolean;
  editable: boolean;
  edit_reason: string;
  global_editable: boolean;
  global_edit_reason: string;
}

export interface PromptNarrativeConfig {
  tone: string;
  paragraph_order: string[];
  min_paragraphs: number;
  max_paragraphs: number;
  preferred_min_chars: number;
  preferred_max_chars: number;
  list_mode: "allowed" | "limited" | "prohibited" | string;
}

export interface PromptEditableConfig {
  role: string;
  goal: string;
  required_sections: string[];
  rules: string[];
  narrative_contract: PromptNarrativeConfig;
  additional_requirements: string;
}

export interface PromptConstraints {
  required_inputs: string[];
  input_contract: { fact_keys?: string[]; metric_keys?: string[]; rule_keys?: string[]; source_kinds?: string[]; max_evidence_items?: number; max_evidence_chars?: number };
  preserve_blocks: string[];
  forbidden: string[];
  output_contract: { required_phrases?: string[]; required_missing_markers?: string[]; forbidden_patterns?: string[]; max_citations?: number; attribution_mode?: string };
  global_rules: Record<string, string>;
}

export interface DocumentItem {
  id: string;
  filename: string;
  kind: string;
  media_type: string;
  size: number;
  parse_status: string;
  quality: number;
  locator_count: number;
  warning: string;
  parser: string;
  document_role?: "prospectus" | "rating_report" | string;
  ocr_status: "not_needed" | "required" | "completed" | "failed" | "disabled" | string;
  page_count: number;
}

export interface MaterialGap {
  group: string;
  expectedKinds: string[];
  status: "missing";
  issue_key?: string;
  affected_rule_ids?: string[];
  blocking_level?: "blocking" | "non_blocking";
}
export interface Inventory {
  documents: DocumentItem[];
  gaps: MaterialGap[];
  supplementary_gaps?: MaterialGap[];
  readiness: "empty" | "partial" | "ready";
  core_materials_ready?: boolean;
  core_pre_review_ready?: boolean;
  complete_due_diligence_ready?: boolean;
  analysis_mode?: "full_due_diligence" | "core_pre_review" | "insufficient" | "empty" | "partial" | "ready";
  // 就绪度百分比由后端按「已解析的覆盖度」给出，界面不再自行按文件个数推算——
  // 否则两个核心文件都解析失败时会显示 90%。
  core_pre_review_readiness?: number;
  full_due_diligence_readiness?: number;
  parse_quality?: { total: number; parsed: number; failed: number; average: number };
}

export interface BondTranche {
  tranche_key: string;
  tranche_name: string;
  term_years: number;
  issue_amount: number | null;
  repayment_method: string;
  maturity_date: string;
  coupon_rate: number | null;
  amount_scope: "aggregate_current_issue" | string;
  allocation_status: "not_disclosed" | "disclosed" | string;
  source_ids: string[];
}

export interface BondSummary {
  projectId: string;
  bondName: string;
  issuer: string;
  issueAmount: number | null;
  currentIssueAmount: number | null;
  /** 这两个值在材料未披露时会回落到项目录入，来源必须以后端标记为准。 */
  bondNameSource: "material" | "project" | "none";
  issueAmountSource: "material" | "project" | "conflict";
  registeredAmount: number | null;
  termYears: number | null;
  couponRate: number | null;
  repayment: string | null;
  issuerRating: string | null;
  bondRating: string | null;
  outlook: string | null;
  guarantor: string | null;
  guaranteeType: string | null;
  guaranteeApplicable: boolean;
  guaranteeScope: string | null;
  guaranteePeriod: string | null;
  guaranteeCap: number | null;
  reportLevel: "完整尽调" | "材料不足";
  dataClass: "fictional_demo" | "real_reference" | "user_project";
  evaluationEligible: boolean;
  conflictedFields: string[];
  coreMaterialsReady: boolean;
  completeDueDiligenceReady: boolean;
  analysisMode: LegacyAnalysisTarget | "insufficient";
  materialGaps: MaterialGap[];
  supplementaryGaps?: MaterialGap[];
  tranches: BondTranche[];
}

export interface Fact {
  id: string;
  category: string;
  fact_key: string;
  name: string;
  period: string;
  value: number | null;
  unit: string;
  text_value: string;
  raw_value: string;
  source_ids: string[];
  confidence: number;
  extraction_method: string;
  review_status: string;
  subject: string;
  subject_id: string;
  scope: string;
  period_type: string;
}

export interface CrossCheck {
  id: string;
  rule_key: string;
  title: string;
  severity: Severity;
  status: CheckStatus;
  summary: string;
  formula: string;
  values_json: Array<{ label: string; value: number }>;
  source_ids: string[];
  fact_ids: string[];
  policy_version: string;
  thresholds: Record<string, number>;
  result_value: number | null;
  rationale: string;
  subject_id: string;
  scope: string;
  trace_json: Record<string, unknown>;
}

export interface Risk {
  id: string;
  severity: Severity;
  category: string;
  title: string;
  description: string;
  impact: string;
  action: string;
  source_ids: string[];
  likelihood: 1 | 2 | 3;
  impact_score: 1 | 2 | 3;
  source_type: "rule" | "candidate";
  candidate_id: string;
  condition_text: string;
}

export interface FactAssertion {
  id: string;
  fact_key: string;
  subject: string;
  period: string;
  category: string;
  name: string;
  value: number | null;
  unit: string;
  text_value: string;
  raw_value: string;
  source_ids: string[];
  confidence: number;
  extraction_method: string;
  subject_id: string;
  scope: string;
  period_type: string;
}

export interface FactGroup {
  id: string;
  fact_key: string;
  subject: string;
  period: string;
  category: string;
  name: string;
  status: "unreviewed" | "confirmed" | "conflicted" | "insufficient";
  selected_assertion_id: string | null;
  value: number | null;
  unit: string;
  text_value: string;
  assertion_ids: string[];
  assertions: FactAssertion[];
  resolution_note: string;
  resolved_by: string;
  subject_id: string;
  scope: string;
  period_type: string;
}

export interface FinancialMetric {
  id: string;
  metric_key: string;
  name: string;
  period: string;
  value: number | null;
  unit: string;
  formula: string;
  input_fact_ids: string[];
  source_ids: string[];
  status: string;
  policy_version: string;
  subject_id: string;
  scope: string;
  trace_json: Record<string, unknown>;
}

export interface ProjectEntity {
  id: string;
  entity_type: string;
  name: string;
  is_borrower: boolean;
  status: string;
  attributes_json: Record<string, unknown>;
}

export interface FinancialTieOut {
  id: string;
  subject_id: string;
  scope: string;
  period: string;
  check_key: string;
  title: string;
  status: CheckStatus;
  formula: string;
  input_fact_ids: string[];
  source_ids: string[];
  left_value: number | null;
  right_value: number | null;
  difference: number | null;
  tolerance: number;
  rationale: string;
  policy_version: string;
}

export interface MetricTraceInput {
  canonical_fact: FactGroup;
  assertion: FactAssertion;
  sources: Array<{
    id: string;
    document_id: string;
    locator_type: string;
    locator: string;
    text: string;
    page: number | null;
    bbox_json: number[];
    extraction_method: string;
  }>;
}

export interface MetricTrace { metric: FinancialMetric; inputs: MetricTraceInput[] }

export interface ReportQuality {
  factual_claims: number;
  supported_claims: number;
  unsupported_claims: number;
  support_rate: number;
  invalid_citations: number;
  status: "passed" | "warning" | "not_generated";
  tie_outs: Record<CheckStatus, number>;
  entities: number;
  gold_annotations: number;
  /** P1-1：报告采用的目录与当前配置是否不一致。 */
  outline_outdated?: boolean;
  /** P1-1：再生要求——none/chapter（按章再生即可）/full_report（须整份重新生成）/unknown（旧报告缺凭据）。 */
  regeneration_requirement?: "none" | "chapter" | "full_report" | "unknown";
  /** P1-1：配置已变化的章节键（chapter 级再生用）。 */
  outline_changed_chapters?: string[];
}

export interface EvidenceGraphSummary {
  entities: Array<{ id: string; type: string; label: string }>;
  edge_summary: Array<{ from_type: string; relation: string; to_type: string; count: number }>;
  edge_count: number;
  truncated: boolean;
}

export interface PolicyInfo {
  active: {
    profile: string;
    version: string;
    name: string;
    description: string;
    sha256: string;
    rules: Array<{ key: string; title: string; severity: Severity; thresholds: Record<string, number> }>;
  };
  lastRun: { profile: string; version: string; sha256: string; created_at: string } | null;
}

export interface ReviewTask {
  id: string;
  task_type: "material_gap" | "fact_conflict" | "insufficient_rule" | "candidate_finding" | "financial_tie_out";
  priority: Severity;
  title: string;
  description: string;
  status: "open" | "resolved" | "waived";
  related_ids: string[];
  required_evidence: string;
  resolution_note: string;
  assignee: string;
  due_date: string;
  issue_key?: string;
  issue_category?: string;
  affected_fact_ids?: string[];
  affected_rule_ids?: string[];
  blocking_level?: "blocking" | "non_blocking";
  source_requirement?: string;
}

export interface CandidateFinding {
  id: string;
  taxonomy: string;
  title: string;
  rationale: string;
  potential_impact: string;
  likelihood: 1 | 2 | 3;
  impact: 1 | 2 | 3;
  severity: Severity;
  support_source_ids: string[];
  counter_source_ids: string[];
  questions: string[];
  mitigation: string;
  status: "proposed" | "confirmed" | "rejected";
  confidence: number;
  decision_note: string;
}

export interface AuditEvent {
  id: string;
  actor: string;
  action: string;
  entity_type: string;
  entity_id: string;
  before_json: Record<string, unknown>;
  after_json: Record<string, unknown>;
  created_at: string;
}

/** 章内正文的 Markdown 展示分块（由后端 report_outline.content_blocks 解析并编号）。 */
export interface ReportMarkupBlock {
  kind: "heading" | "paragraph" | "list" | "quote" | "table";
  /** 标题层级：3 = 三级标题（1. 2. 3.），4 = 更深层级（不编号） */
  level?: number;
  number?: string;
  label?: string;
  text?: string;
  ordered?: boolean;
  items?: string[];
  /** 旧版正文管道表：每个元素是一行 Markdown 表格行（`| 科目 | 2023 |`） */
  rows?: string[];
}

/** 受控数据表的单元格：与 `GET /api/projects/{id}/tables/{table_id}` 同源的 `TableCell.to_dict()`。 */
export interface ReportBlockCell {
  /** 原始提取字符串，人工核对以它为准 */
  raw?: string;
  /** 规范化后的数值字符串；无数据为 null */
  normalized?: string | null;
  unit?: string;
  /** ok | zero | not_applicable | blank | text | single_source | conflict | adopted | needs_review */
  status?: string;
  /** profile_fact | uploaded_cell | manual | derived */
  origin_type?: string;
  fact_key?: string | null;
  metric_key?: string | null;
  /** 该格的证据来源；点击单元格即用 source_ids[0] 打开证据 */
  source_ids?: string[];
  locator?: Record<string, unknown>;
  /** 采用依据（adopted 格必填，冲突格说明为何未择一） */
  adoption_basis?: string;
  manual_note?: string;
}

export interface ReportBlockRow {
  row_key?: string;
  /** normal | group | subtotal */
  kind?: string;
  cells?: ReportBlockCell[];
}

/**
 * 受控数据表块：后端 `TableBlock.to_dict()` 的原样投影。
 *
 * 与「正文管道表」的区别是**逐格带状态与证据**：冲突值并列保留、缺失值标 not_applicable、
 * 每个业务数据格可追到来源。前端只渲染，不改写数值，也不自行择一。
 *
 * 旧数据 / 后端未升级时，`blocks` 里仍是 `ReportMarkupBlock` 的管道表，两种形态必须都能渲染。
 */
export interface ReportTableBlock {
  kind: "table";
  /** 与 /tables 接口共用同一个 table_id；据此可跳转逐格核对 */
  table_id: string;
  table_key?: string;
  /** annual_series | dual_source_reconciliation | conflict_comparison */
  table_type?: string;
  title?: string;
  subject?: string;
  /** 报表口径（合并／母公司等） */
  scope?: string;
  unit?: string;
  currency?: string;
  display_mode?: string;
  /** profile_facts | uploaded_table | merged——不得互相冒充 */
  origin_mode?: string;
  /** passed | blocked | needs_review | pending */
  validation_status?: string;
  columns?: TableColumnInfo[];
  /** 新版报告接口把结构化行放在 table 内，顶层 rows 保留 Markdown 行。 */
  table?: { columns: TableColumnInfo[]; rows: ReportBlockRow[] };
  rows?: ReportBlockRow[] | string[];
  notes?: string[];
  source_refs?: string[];
}

/** 章内分块：Markdown 分块与受控数据表块共存，按 kind 与字段形态区分。 */
export type ReportBlock = ReportMarkupBlock | ReportTableBlock;

export interface ReportSection {
  id: string;
  section_key: string;
  title: string;
  content: string;
  /** 论文式标题层级编号（由后端按分部/章计算，见 report_outline） */
  part?: string;
  part_number?: string;
  part_label?: string;
  chapter_number?: string;
  chapter_label?: string;
  blocks?: ReportBlock[];
  citation_ids: string[];
  review_status: string;
  updated_at: string;
  support_rate: number;
  validation_status: "passed" | "warning" | string;
  prompt_version?: string;
  prompt_sha256?: string;
  input_hash?: string;
  fallback_reason?: string;
  allowed_source_ids?: string[];
  /** 生成状态：模型结果是否真正进入报告（与证据支持状态分开）。
   *  `model_needs_review`＝修复后**仅剩未决项**：正文是模型稿但**不计入合格采用率**。 */
  generation_status?: "model_passed" | "model_needs_review" | "fallback" | "deterministic" | "skipped" | "human_edit" | string;
  /** 正文生成时的 Prompt 哈希与**当前生效** Prompt 不同（报告用了旧 Prompt）。
   *  仅作提示，**保存 Prompt 不会自动再生**。 */
  prompt_outdated?: boolean;
  /** 最终正文的真实来源：模型 / 确定性模板 / 人工编辑 / 历史来源未核实 */
  content_origin?: "model" | "deterministic" | "human" | "unknown" | string;
  /** 回退发生阶段：章节校验 / 后置支持校验 / 无 */
  fallback_stage?: "chapter_validation" | "report_support" | "none" | string;
  /** 强制披露（冲突断言、来源边界块）是否完整 */
  disclosure_status?: "pending" | "passed" | "warning" | "not_checked" | string;
  /** 交付状态：可交付 / 待人工复核 / 待校验 */
  delivery_status?: "pending" | "eligible" | "needs_review" | "not_checked" | string;
  required_disclosures?: string[];
  content_sha256?: string;
  validated_content_sha256?: string;
  /** 证据支持状态（validation_status 为兼容字段，值相同） */
  support_validation_status?: string;
  validation_diagnostics?: {
    unexpected_numbers?: {
      raw?: string; normalized_value?: string; normalized_unit?: string;
      reason?: string; location_type?: string; context?: string;
    }[];
    safe_unit_conversions?: { raw?: string; normalized_value?: string; normalized_unit?: string; reason?: string }[];
    ignored_format_numbers?: string[];
    page_numbers_ignored?: string[];
    forbidden_matches?: { term?: string; context?: string; classification?: "forbidden" | "safe_negative" | string }[];
    source_attribution_restored?: boolean;
    /** 篇幅压缩稿未通过校验、已退回原正文时为 true */
    length_repair_failed?: boolean;
    length_repair_reasons?: string[];
    /** 文风写作提示（技术化起句/系统术语/列表形态）：**不阻断保存与交付**，仅供人工查看 */
    style_notes?: string[];
  };
  /** 篇幅诊断：生成时记录；历史报告为 not_checked（字符数由后端读取时补算） */
  length_status?: "passed" | "short" | "long" | "hard_limit_exceeded" | "not_checked" | string;
  visible_content_chars?: number;
  raw_output_chars?: number;
  length_repair_attempted?: boolean;
  length_policy?: LengthPolicy;
  report_revision?: number;
  system_tables?: { snapshot_hash: string; current_hash: string; rule_version: string; updated_at: string | null; outdated: boolean };
}

export interface Citation {
  id: string;
  label: string;
  quote: string;
  locator_type: string;
  locator: string;
  page: number | null;
  bbox: number[];
  extraction_method: string;
}

export interface Run {
  id: string;
  project_id: string;
  status: string;
  current_node: string;
  progress: number;
  message: string;
  latency_ms: number;
  input_tokens: number;
  output_tokens: number;
  cost: number;
  error: string;
  started_at?: string;
  finished_at?: string | null;
}

export interface RunNode {
  id: string;
  node_key: string;
  status: string;
  progress: number;
  attempt: number;
  output_summary: string;
  error: string;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface RunArtifact {
  id: string;
  run_id: string;
  node_key: string;
  artifact_type: string;
  artifact_ids: string[];
  status: string;
  source_run_id: string;
  details_json: Record<string, unknown>;
  created_at: string;
}

export interface LatestRun {
  run: Run | null;
  nodes: RunNode[];
}

/** 历史报告快照摘要：不含 payload，供列表直接渲染。 */
export interface ReportSnapshotSummary {
  id: string;
  run_id: string;
  created_at: string;
  title: string;
  report_level: string;
  validation_status: string;
  support_rate: number;
  report_revision: number;
  review_revision: number;
}

/**
 * 快照 payload：完成任务瞬间冻结的完整报告表示。
 * `sections` 与 `GET /report` 同形，因此可直接复用同一套报告渲染组件。
 */
export interface ReportSnapshotPayload {
  sections: ReportSection[];
  groups: Array<{ label: string; chapters: Array<{ label: string; section: ReportSection }>; section?: ReportSection }>;
  citations: Array<{ id: string; label: string; quote?: string; locator?: string; page?: number | null }>;
  open_items: string[];
  quality: Record<string, unknown>;
  material_labels?: Record<string, string>;
  frozen_at?: string;
}

export interface ReportSnapshot extends ReportSnapshotSummary {
  project_id: string;
  subject: string;
  schema_version: string;
  content_sha256: string;
  payload: ReportSnapshotPayload;
}

export type WorkflowStatus = "pending" | "current" | "completed" | "blocked";
export interface WorkflowStage {
  key: "materials" | "analysis" | "review" | "delivery";
  label: string;
  status: WorkflowStatus;
  count?: number;
  reason: string;
  view: "evidence" | "analysis" | "review" | "report";
}

export interface Health {
  status: string; modelConfigured: boolean; model: string; version: string;
  policy?: string; policyVersion?: string; ocrConfigured?: boolean; ocrModel?: string;
  uploadLimits?: { maxFileMb: number; maxFiles: number; formats: string[] };
  retrieval?: { mode: "hybrid" | "bm25_only"; model: string; modelRevision: string; modelReady: boolean; dependenciesReady?: boolean; indexVersion: string; errorType: string };
}

export interface MaterialQACitation {
  id: string;
  chunk_id: string;
  source_id: string;
  document_id: string;
  filename: string;
  page: number | null;
  locator: string;
  quote: string;
  chunk_type: "text" | "table" | string;
  table_key: string;
  score: number;
}

export interface MaterialQAClaim {
  text: string;
  source_ids: string[];
}

export interface MaterialQAResponse {
  status: "answered" | "insufficient" | "retrieved_only" | "model_unavailable" | "retrieval_unavailable";
  answer: string;
  claims: MaterialQAClaim[];
  citations: MaterialQACitation[];
  retrieval_mode: "hybrid" | "bm25_only" | "unavailable";
  retrieval_metrics?: { chunk_count: number; index_build_ms: number; query_ms: number; indexed_at?: string };
  conflict_detected?: boolean;
  error_type?: string;
  usage?: { input_tokens: number; output_tokens: number; latency_ms: number; cost: number };
}

// ---------------------------------------------------------------------------
// 表格核对（Phase 6）：与报告成品读同一份 TableBlock
// ---------------------------------------------------------------------------

export interface TableColumnInfo {
  key: string;
  label: string;
  role: "subject" | "period" | "period_value" | "profile_value" | "uploaded_value" | "adopted_value" | "status" | "disclosure" | "reference" | "detail";
  /** 列级单位（TableBlock 契约字段；报告块与 /tables 接口都会带） */
  unit?: string;
}

export interface TableSummary {
  table_id: string;
  table_type: "annual_series" | "dual_source_reconciliation" | "conflict_comparison";
  title: string;
  unit: string;
  scope: string;
  origin_mode: "profile_facts" | "uploaded_table" | "merged";
  display_mode: "report" | "appendix" | "evidence";
  columns: TableColumnInfo[];
  row_count: number;
  validation_status: "pending" | "passed" | "blocked" | "needs_review";
  validation_summary: Record<string, unknown>;
  notes: string[];
  cell_reviews: number;
}

export interface TableMatrixCell {
  cell_id: string;
  raw: string;
  normalized: string | null;
  unit: string;
  status: "ok" | "zero" | "not_applicable" | "blank" | "text" | "single_source" | "conflict" | "adopted" | "needs_review";
  origin_type: string;
  fact_key: string | null;
  source_ids: string[];
  locator: Record<string, unknown>;
  adoption_basis: string;
  manual_note: string;
  /** 最新核对记录；未核对为 null */
  review?: TableCellReviewRecord | null;
  /** 有效展示值：已更正为更正值，否则为原始值 */
  display?: string;
}

export interface TableCellReviewRecord {
  id: string;
  cell_id: string;
  original_raw: string;
  corrected_raw: string;
  decision: "confirmed" | "corrected" | "rejected";
  note: string;
  reviewer: string;
  created_at: string;
}

export interface TableMatrixRow {
  row_key: string;
  kind: "normal" | "group" | "subtotal";
  cells: TableMatrixCell[];
}

export interface TableMatrix extends Omit<TableSummary, "cell_reviews" | "row_count"> {
  version: string;
  currency: string;
  source_refs: string[];
  rows: TableMatrixRow[];
  cell_reviews: Record<string, TableCellReviewRecord>;
  review_summary: { reviewed: number; corrected: number; rejected: number };
}
