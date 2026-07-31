export type Severity = "high" | "medium" | "low";
export type CheckStatus = "passed" | "warning" | "insufficient";

export interface ProjectSummary {
  id: string;
  company_name: string;
  amount: number;
  term_months: number;
  purpose: string;
  status: string;
}

export interface Project extends ProjectSummary {
  created_at: string;
  updated_at: string;
  stats: { documents: number; facts: number; checks: number; risks: number; highRisks: number };
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
}

export interface MaterialGap { group: string; expectedKinds: string[]; status: "missing" }
export interface Inventory { documents: DocumentItem[]; gaps: MaterialGap[]; readiness: "empty" | "partial" | "ready" }

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
}

export interface ReportSection {
  id: string;
  section_key: string;
  title: string;
  content: string;
  citation_ids: string[];
  review_status: string;
  updated_at: string;
}

export interface Citation {
  id: string;
  label: string;
  quote: string;
  locator_type: string;
  locator: string;
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
}

export interface RunNode {
  id: string;
  node_key: string;
  status: string;
  progress: number;
  attempt: number;
  output_summary: string;
  error: string;
}

export interface Health { status: string; modelConfigured: boolean; model: string; version: string }
