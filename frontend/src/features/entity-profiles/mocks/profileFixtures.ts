import type {
  EntityProfileDetail,
  EntityProfileFact,
  EntityProfileReadiness,
  EntityProfileVersionDetail,
  EntityProfileVersionSummary,
  ProjectProfileSnapshot,
  ProjectProfileSnapshotStatus,
} from "../contracts";

const source = {
  id: "profile-source-huayuan-financial",
  filename: "华源基础设施2025年度审计报告.pdf",
  kind: "审计报告",
  locator: "第8页资产负债表",
  page: 8,
  quote: "截至2025年末，资产总额320,000万元，负债总额208,000万元。",
};

const identitySource = {
  id: "profile-source-huayuan-identity",
  filename: "华源基础设施主体档案.pdf",
  kind: "工商资料",
  locator: "第2页工商登记",
  page: 2,
  quote: "华源基础设施有限公司法定代表人为林泽，公司住所为北京市朝阳区小营北路6号。",
};

const financialSources = [
  { ...source, id: "profile-source-huayuan-financial-2023", filename: "华源基础设施2023年度审计报告.pdf", quote: "2023年度主要财务报表。" },
  { ...source, id: "profile-source-huayuan-financial-2024", filename: "华源基础设施2024年度审计报告.pdf", quote: "2024年度主要财务报表。" },
  source,
];

function financialFacts(): EntityProfileFact[] {
  const values: Record<string, number[]> = {
    "2023": [276000, 180000, 96000, 198000, 12500, 16900, 108000],
    "2024": [298000, 192000, 106000, 221000, 14300, 18400, 116000],
    "2025": [320000, 208000, 112000, 246000, 15800, 20100, 126000],
  };
  const keys = ["total_assets", "total_liabilities", "equity", "revenue", "net_profit", "operating_cash_flow", "interest_bearing_debt"];
  const labels = ["资产总额", "负债总额", "所有者权益", "营业收入", "净利润", "经营活动现金流量净额", "有息负债"];
  return Object.entries(values).flatMap(([period, row], periodIndex) => keys.map((factKey, index) => ({
    id: `profile-fact-${factKey}-${period}`,
    fact_key: factKey,
    name: labels[index],
    category: factKey === "interest_bearing_debt" ? "debt" : "financial",
    value: row[index],
    text_value: "",
    unit: "万元",
    period,
    scope: "consolidated",
    status: "confirmed",
    valid_until: "2026-12-31",
    source_ids: [financialSources[periodIndex].id],
    source_id: financialSources[periodIndex].id,
    source_label: financialSources[periodIndex].filename,
    page: financialSources[periodIndex].page,
  })));
}

export const profileFixtures: EntityProfileDetail[] = [{
  id: "profile-fixture-huayuan",
  name: "华源基础设施有限公司",
  unified_code: "91110108PROFILE001",
  baseline_date: "2025-12-31",
  latest_financial_period: "2025年度",
  completeness: 0.86,
  status: "ready",
  version: 3,
  project_count: 2,
  counts: { confirmed: 9, single_source: 2, stale: 1, conflicted: 0, insufficient: 0 },
  aliases: ["华源基建"],
  facts: [
    {
      id: "profile-fact-legal-representative",
      fact_key: "legal_representative",
      name: "法定代表人",
      category: "identity",
      value: null,
      text_value: "林泽",
      unit: "",
      period: null,
      scope: "entity",
      status: "confirmed",
      valid_until: "2027-06-30",
      source_ids: [identitySource.id],
      source_id: identitySource.id,
      source_label: identitySource.filename,
      page: identitySource.page,
    },
    {
      id: "profile-fact-registered-address",
      fact_key: "registered_address",
      name: "注册地址",
      category: "identity",
      value: null,
      text_value: "北京市朝阳区小营北路6号京投大厦",
      unit: "",
      period: null,
      scope: "entity",
      status: "confirmed",
      valid_until: "2027-06-30",
      source_ids: [identitySource.id],
      source_id: identitySource.id,
      source_label: identitySource.filename,
      page: identitySource.page,
    },
    {
      id: "profile-fact-controller",
      fact_key: "controller",
      name: "实际控制人",
      category: "ownership",
      value: null,
      text_value: "华源投资控股集团有限公司",
      unit: "",
      period: null,
      scope: "entity",
      status: "confirmed",
      valid_until: "2027-06-30",
      source_ids: [source.id],
      source_id: source.id,
      source_label: source.filename,
      page: source.page,
    },
    ...financialFacts(),
  ],
  shareholders: [],
  financial_periods: [],
  debt_instruments: [],
  credit_facilities: [],
  concentrations: [],
  related_transactions: [],
  legal_events: [],
  material_packages: [],
  sources: [identitySource, ...financialSources],
  data: { industry: "基础设施建设" },
  version_created_at: "2026-04-15T09:30:00+08:00",
  updated_at: "2026-04-15T09:30:00+08:00",
}];

export const readinessFixtures: Record<string, EntityProfileReadiness> = {
  "profile-fixture-huayuan": {
    profile_id: "profile-fixture-huayuan",
    ready_for_project: true,
    ready_for_agent: false,
    complete_due_diligence_ready: false,
    completeness: 0.86,
    status: "ready",
    module_scores: [
      { key: "identity", label: "工商与身份", score: 1, weight: 0.1, status: "ready" },
      { key: "ownership", label: "股权与控制", score: 0.87, weight: 0.15, status: "ready" },
      { key: "financial", label: "三个完整年度财务", score: 0.92, weight: 0.3, status: "ready" },
      { key: "debt", label: "债务与或有事项", score: 0.55, weight: 0.2, status: "blocking" },
    ],
    hard_vetoes: [{ key: "current_debt", label: "当前债务结构", reason: "债务到期结构已超过有效期" }],
    blocking_reasons: ["当前债务和到期结构需要更新"],
    notices: ["2项事实仅有单一来源，尚未独立验证"],
    counts: { confirmed: 9, single_source: 2, stale: 1, conflicted: 0, insufficient: 0 },
    requirements: [{
      key: "debt_maturity",
      label: "债务与到期结构",
      category: "debt",
      status: "blocking",
      blocking_level: "blocking",
      required_fact_keys: ["interest_bearing_debt", "debt_due_12m"],
      missing_fact_keys: ["debt_due_12m"],
      stale_fact_keys: ["interest_bearing_debt"],
      conflicted_fact_keys: [],
      source_ids: [source.id],
      impact: "阻塞 Agent 对短期偿债压力的确定性计算",
    }],
  },
};

export const versionFixtures: Record<string, EntityProfileVersionSummary[]> = {
  "profile-fixture-huayuan": [
    { version: 3, baseline_date: "2025-12-31", latest_financial_period: "2025年度", status: "ready", completeness: 0.86, created_at: "2026-04-15T09:30:00+08:00", created_by: "项目经理", change_summary: "更新2025年度审计数据", source_count: 4, fact_count: 12, current: true },
    { version: 2, baseline_date: "2024-12-31", latest_financial_period: "2024年度", status: "stale", completeness: 0.74, created_at: "2025-11-01T11:00:00+08:00", created_by: "项目经理", change_summary: "补充债务明细", source_count: 3, fact_count: 10, current: false },
  ],
};

export const versionDetailFixtures: Record<string, EntityProfileVersionDetail> = {
  "profile-fixture-huayuan:3": { ...profileFixtures[0], profile_id: "profile-fixture-huayuan", version_created_at: "2026-04-15T09:30:00+08:00", change_note: "更新2025年度审计数据" },
  "profile-fixture-huayuan:2": { ...profileFixtures[0], profile_id: "profile-fixture-huayuan", version: 2, baseline_date: "2024-12-31", latest_financial_period: "2024年度", completeness: 0.74, status: "stale", version_created_at: "2025-11-01T11:00:00+08:00", change_note: "补充债务明细", facts: profileFixtures[0].facts.filter((fact) => fact.period !== "2025").map((fact) => fact.fact_key === "total_assets" && fact.period === "2024" ? { ...fact, value: 305000 } : fact) },
};

export const snapshotFixture: ProjectProfileSnapshot = {
  id: "snapshot-fixture-v1",
  version: 1,
  baseline_date: "2024-12-31",
  created_at: "2026-01-10T10:00:00+08:00",
  roles: [{ role: "issuer", profile_id: "profile-fixture-huayuan", name: "华源基础设施有限公司", profile_version: 2, status: "stale", completeness: 0.74, baseline_date: "2024-12-31", latest_financial_period: "2024年度" }],
  counts: { confirmed: 8, single_source: 2, stale: 1, conflicted: 0, insufficient: 0 },
  facts: profileFixtures[0].facts.filter((fact) => fact.period !== "2025").map((fact) => ({ ...fact, profile_id: "profile-fixture-huayuan", role: "issuer" as const })),
  records: [],
  sources: profileFixtures[0].sources.map((item) => ({ ...item, profile_id: "profile-fixture-huayuan", role: "issuer" as const })),
  profile_versions: { "profile-fixture-huayuan": 2 },
  content_hash: "93ad48f1431c726798b3bcd39eb33d45c91bb1e014edc2fe2a2a19ff550d2a1b",
};

export const snapshotStatusFixture: ProjectProfileSnapshotStatus = {
  project_id: "project-fixture-profile",
  snapshot_id: snapshotFixture.id,
  snapshot_version: snapshotFixture.version,
  snapshot_profile_version: 2,
  current_profile_version: 3,
  has_newer_version: true,
  refresh_required: false,
  analysis_stale: false,
  evidence_status: "available",
  roles: [{ role: "issuer", profile_id: "profile-fixture-huayuan", name: "华源基础设施有限公司", snapshot_version: 2, current_version: 3, has_newer_version: true }],
  reason: "主体库存在更新版本，项目仍使用已冻结的v2快照",
};
