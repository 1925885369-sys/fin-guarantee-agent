from __future__ import annotations

import math
from typing import Any, Literal, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


Severity = Literal["high", "medium", "low"]


class ProjectCreate(BaseModel):
    company_name: str = Field(min_length=2, max_length=80)
    amount: float = Field(gt=0)
    term_months: int = Field(ge=1, le=120)
    purpose: str = Field(min_length=2, max_length=300)
    data_consent: bool
    project_type: Literal["bond_guarantee"] = "bond_guarantee"
    bond_name: str = Field(default="", max_length=160)
    issuer_profile_id: Optional[str] = None
    guarantor_name: str = Field(default="", max_length=120)
    counter_guarantor_names: list[str] = Field(default_factory=list, max_length=20)
    # Legacy role bindings remain accepted for existing clients. New clients
    # submit project-level names and only the issuer enters the profile snapshot.
    guarantor_profile_id: Optional[str] = None
    counter_guarantor_profile_ids: list[str] = Field(default_factory=list, max_length=20)
    counter_guarantee_status: Literal["exists", "not_applicable", "undetermined"] = "undetermined"
    # ``core_pre_review`` is accepted only for clients created before the
    # unified bond diligence mode. It is normalized at project creation.
    analysis_target: Literal["core_pre_review", "full_due_diligence"] = "full_due_diligence"

    @field_validator("amount")
    @classmethod
    def finite_amount(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("本期规模必须是有限数值")
        return value


class ProjectUpdate(BaseModel):
    """项目基础信息修改：三个字段都可选，只提交真正要改的字段。

    ``str_strip_whitespace`` 让「发行人去除首尾空白后为 2–80 字」在长度校验之前生效，
    否则 `"  "` 这种全空白输入会被当成合法值写进项目名。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    company_name: Optional[str] = Field(default=None, min_length=2, max_length=80)
    bond_name: Optional[str] = Field(default=None, max_length=160)
    amount: Optional[float] = Field(default=None, gt=0)

    @field_validator("amount")
    @classmethod
    def finite_amount(cls, value: Optional[float]) -> Optional[float]:
        if value is not None and not math.isfinite(value):
            raise ValueError("发行规模必须是有限数值")
        return value


class EntityProfileSourceInput(BaseModel):
    id: Optional[str] = None
    filename: str = Field(min_length=1, max_length=240)
    kind: str = Field(default="other", max_length=80)
    locator: str = Field(default="", max_length=200)
    page: Optional[int] = Field(default=None, ge=1)
    quote: str = Field(default="", max_length=4000)
    file_id: str = Field(default="", max_length=80)
    locator_type: str = Field(default="profile_locator", max_length=40)
    content_hash: str = Field(default="", max_length=128)


class EntityProfileFactInput(BaseModel):
    id: Optional[str] = None
    fact_key: str = Field(min_length=2, max_length=80)
    category: str = Field(default="", max_length=80)
    value: Optional[float] = None
    unit: str = Field(default="", max_length=20)
    text_value: str = Field(default="", max_length=2000)
    period: str = Field(default="", max_length=20)
    as_of_date: Optional[str] = Field(default=None, max_length=20)
    period_type: Literal["duration", "point_in_time", "undated", "unknown"] = "unknown"
    scope: Literal["standalone", "consolidated", "entity", "transaction", "unknown"] = "entity"
    subject_role: Literal["issuer", "guarantor", "counter_guarantor"] = "issuer"
    status: Literal["confirmed", "single_source", "stale", "conflicted", "insufficient"] = "single_source"
    valid_until: Optional[str] = Field(default=None, max_length=20)
    source_ids: list[str] = Field(default_factory=list, max_length=20)
    raw_value: str = Field(default="", max_length=500)
    confidence: float = Field(default=0.8, ge=0, le=1)
    currency: str = Field(default="CNY", max_length=8)
    reviewer: str = Field(default="", max_length=80)
    reviewed_at: Optional[str] = Field(default=None, max_length=40)

    @model_validator(mode="after")
    def validate_value_and_evidence(self) -> "EntityProfileFactInput":
        if self.status != "insufficient" and self.value is None and not self.text_value.strip():
            raise ValueError("非材料不足事实必须提供数值或文本值")
        if self.status == "confirmed" and not self.source_ids:
            raise ValueError("已确认事实必须关联来源")
        return self


class EntityProfileRecordInput(BaseModel):
    id: Optional[str] = None
    period: str = Field(default="", max_length=20)
    as_of_date: Optional[str] = Field(default=None, max_length=20)
    scope: Literal["standalone", "consolidated", "entity", "transaction", "unknown"] = "entity"
    status: Literal["confirmed", "single_source", "stale", "conflicted", "insufficient"] = "single_source"
    valid_until: Optional[str] = Field(default=None, max_length=20)
    source_ids: list[str] = Field(default_factory=list, max_length=20)
    reviewer: str = Field(default="", max_length=80)
    reviewed_at: Optional[str] = Field(default=None, max_length=40)
    unit: str = Field(default="", max_length=20)
    currency: str = Field(default="CNY", max_length=8)
    data: dict[str, Any] = Field(default_factory=dict)


class EntityProfileCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    unified_code: str = Field(min_length=8, max_length=32)
    baseline_date: Optional[str] = Field(default=None, max_length=20)
    latest_financial_period: Optional[str] = Field(default=None, max_length=20)
    status: Literal["ready", "stale", "conflicted", "incomplete"] = "incomplete"
    completeness: Optional[float] = Field(default=None, ge=0, le=1)
    facts: list[EntityProfileFactInput] = Field(default_factory=list, max_length=500)
    sources: list[EntityProfileSourceInput] = Field(default_factory=list, max_length=100)
    data: dict[str, Any] = Field(default_factory=dict)
    shareholders: list[EntityProfileRecordInput] = Field(default_factory=list, max_length=200)
    financial_periods: list[EntityProfileRecordInput] = Field(default_factory=list, max_length=100)
    debt_instruments: list[EntityProfileRecordInput] = Field(default_factory=list, max_length=300)
    credit_facilities: list[EntityProfileRecordInput] = Field(default_factory=list, max_length=200)
    concentrations: list[EntityProfileRecordInput] = Field(default_factory=list, max_length=200)
    related_transactions: list[EntityProfileRecordInput] = Field(default_factory=list, max_length=300)
    legal_events: list[EntityProfileRecordInput] = Field(default_factory=list, max_length=300)
    material_packages: list[EntityProfileRecordInput] = Field(default_factory=list, max_length=100)
    change_note: str = Field(default="初始建档", max_length=500)


class EntityProfileUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=120)
    baseline_date: Optional[str] = Field(default=None, max_length=20)
    latest_financial_period: Optional[str] = Field(default=None, max_length=20)
    status: Optional[Literal["ready", "stale", "conflicted", "incomplete"]] = None
    completeness: Optional[float] = Field(default=None, ge=0, le=1)
    facts: Optional[list[EntityProfileFactInput]] = Field(default=None, max_length=500)
    sources: Optional[list[EntityProfileSourceInput]] = Field(default=None, max_length=100)
    data: Optional[dict[str, Any]] = None
    shareholders: Optional[list[EntityProfileRecordInput]] = Field(default=None, max_length=200)
    financial_periods: Optional[list[EntityProfileRecordInput]] = Field(default=None, max_length=100)
    debt_instruments: Optional[list[EntityProfileRecordInput]] = Field(default=None, max_length=300)
    credit_facilities: Optional[list[EntityProfileRecordInput]] = Field(default=None, max_length=200)
    concentrations: Optional[list[EntityProfileRecordInput]] = Field(default=None, max_length=200)
    related_transactions: Optional[list[EntityProfileRecordInput]] = Field(default=None, max_length=300)
    legal_events: Optional[list[EntityProfileRecordInput]] = Field(default=None, max_length=300)
    material_packages: Optional[list[EntityProfileRecordInput]] = Field(default=None, max_length=100)
    change_note: str = Field(default="更新主体档案", max_length=500)


class ProfileImportCorrection(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=100)
    data: Optional[dict[str, Any]] = None
    value: Optional[float] = None
    text_value: Optional[str] = Field(default=None, max_length=2000)
    unit: Optional[str] = Field(default=None, max_length=20)
    period: Optional[str] = Field(default=None, max_length=20)
    scope: Optional[Literal["standalone", "consolidated", "entity", "transaction", "unknown"]] = None
    status: Optional[Literal["confirmed", "single_source", "stale", "conflicted", "insufficient"]] = None

    def patch_data(self) -> dict[str, Any]:
        if self.data is not None:
            return self.data
        return self.model_dump(exclude={"candidate_id", "data"}, exclude_none=True)


class ProfileImportCommit(BaseModel):
    profile_id: Optional[str] = Field(default=None, validation_alias=AliasChoices("profile_id", "target_profile_id"))
    selected_candidate_ids: list[str] = Field(default_factory=list, max_length=2000)
    corrections: list[ProfileImportCorrection] = Field(default_factory=list, max_length=500)
    change_note: str = Field(
        default="结构化导入", max_length=500,
        validation_alias=AliasChoices("change_note", "change_summary"),
    )

    @model_validator(mode="after")
    def validate_selection(self) -> "ProfileImportCommit":
        if not self.selected_candidate_ids and not self.corrections:
            raise ValueError("至少选择或修正一条导入候选记录")
        return self


class DocumentUpdate(BaseModel):
    kind: Literal["募集说明书", "信用评级报告", "其他材料"]
    document_role: Literal["prospectus", "rating_report", "other"]


class SectionUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=30000)
    review_status: Literal["draft", "reviewed"] = "draft"


class SectionRegenerateRequest(BaseModel):
    """单章再生请求体（第三点细化方案 §四）。

    新前端必须发送预期报告版本与正文哈希用于并发校验；旧调用可整体省略请求体，此时由
    服务端捕获起始版本（兼容策略，不强制破坏旧客户端）。
    """

    model_config = ConfigDict(extra="forbid")

    expected_revision: Optional[int] = Field(default=None, ge=0)
    expected_content_sha256: Optional[str] = Field(default=None, max_length=128)


class PromptNarrativeConfig(BaseModel):
    """The writing-shape controls intentionally exposed in the web editor."""

    model_config = ConfigDict(extra="forbid")

    tone: str = Field(default="professional_due_diligence", min_length=2, max_length=80)
    paragraph_order: list[str] = Field(default_factory=list, max_length=12)
    min_paragraphs: int = Field(default=1, ge=1, le=8)
    max_paragraphs: int = Field(default=6, ge=1, le=12)
    preferred_min_chars: int = Field(default=0, ge=0)
    preferred_max_chars: int = Field(default=2400, ge=200)
    list_mode: Literal["allowed", "limited", "prohibited"] = "limited"

    @field_validator("paragraph_order")
    @classmethod
    def clean_paragraph_order(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        if len(cleaned) != len(value):
            raise ValueError("行文顺序不能包含空项")
        if any(len(item) > 120 for item in cleaned):
            raise ValueError("行文顺序单项不得超过 120 字符")
        return cleaned

    @model_validator(mode="after")
    def validate_ranges(self) -> "PromptNarrativeConfig":
        if self.min_paragraphs > self.max_paragraphs:
            raise ValueError("最少段落数不得超过最多段落数")
        if self.preferred_min_chars > self.preferred_max_chars:
            raise ValueError("建议最小篇幅不得超过建议最大篇幅")
        return self


class PromptMaterialScope(BaseModel):
    """材料范围（方案 §二 line 16 / §四 line 56）：约束检索、事实、指标、规则与计算输入。

    全部默认为空 = 不额外收窄（沿用章节契约的 `input_contract`）。
    ⚠️ `document_ids` 只允许出现在**项目级**配置：全局默认存项目文件 ID 没有意义，
    保存时会剥离（`_compile_prompt_payload` / `normalize_outline`）。
    """

    model_config = ConfigDict(extra="forbid")

    source_kinds: list[str] = Field(default_factory=list, max_length=28)
    material_roles: list[str] = Field(default_factory=list, max_length=20)
    document_ids: list[str] = Field(default_factory=list, max_length=30)


class PromptAnalysisPeriod(BaseModel):
    """分析期间：`materials` = 跟随材料实际期间；`configured` = 只按 `periods` 分析。"""

    model_config = ConfigDict(extra="forbid")

    periods: list[str] = Field(default_factory=list, max_length=8)
    mode: Literal["materials", "configured"] = "materials"


class PromptEditableConfig(BaseModel):
    """Business-writing fields that may be changed without changing guardrails."""

    model_config = ConfigDict(extra="forbid")

    role: str = Field(min_length=2, max_length=240)
    goal: str = Field(min_length=2, max_length=2000)
    required_sections: list[str] = Field(default_factory=list, max_length=20)
    rules: list[str] = Field(default_factory=list, max_length=30)
    narrative_contract: PromptNarrativeConfig = Field(default_factory=PromptNarrativeConfig)
    additional_requirements: str = Field(default="", max_length=12000)
    # 业务生成规则（B1 新增）：默认值与 `chapter_prompts.DEFAULT_*` 一致，
    # 历史 `structured_json` 缺这些键时按默认值解析（向后兼容）。
    material_scope: PromptMaterialScope = Field(default_factory=PromptMaterialScope)
    analysis_period: PromptAnalysisPeriod = Field(default_factory=PromptAnalysisPeriod)
    calculation_mode: Literal["off", "system_only", "model"] = "system_only"
    recommendation_mode: Literal["off", "qualitative", "structured"] = "off"

    @field_validator("role", "goal", "additional_requirements")
    @classmethod
    def trim_text(cls, value: str) -> str:
        cleaned = value.strip()
        if cls.__name__ == "PromptEditableConfig" and value != "" and not cleaned:
            raise ValueError("文本不能只包含空白字符")
        return cleaned

    @field_validator("required_sections", "rules")
    @classmethod
    def clean_string_list(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        if len(cleaned) != len(value):
            raise ValueError("列表不能包含空项")
        if any(len(item) > 600 for item in cleaned):
            raise ValueError("列表单项不得超过 600 字符")
        return cleaned


class ReportPromptUpdate(BaseModel):
    # ``prompt`` remains available to older clients. New clients submit a
    # structured configuration, which the server compiles with fixed controls.
    prompt: Optional[str] = Field(default=None, min_length=1, max_length=30000)
    config: Optional[PromptEditableConfig] = None
    # 乐观并发基准（`PLAN_new_p0_p1.md` P0-1 / `PLAN.md` §四.2）：客户端提交"它读到的
    # 配置哈希 + 编译契约版本"，任一与库中现状不一致 → 409，客户端重新读取后再保存。
    # 不传则跳过相应检查（兼容旧客户端）。
    expected_config_sha256: Optional[str] = Field(default=None, max_length=128)
    expected_prompt_contract_version: Optional[str] = Field(default=None, max_length=64)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def exactly_one_payload(self) -> "ReportPromptUpdate":
        if (self.prompt is None) == (self.config is None):
            raise ValueError("请仅提交 prompt 或 config 其中之一")
        return self


class ReportPromptPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: PromptEditableConfig


class ReportOutlineUpdate(BaseModel):
    """报告目录保存请求体（docs/report-outline-spec.md §四）。

    ``outline`` 用宽松 dict —— 目录结构由服务端 ``validate_outline`` 做业务校验并给出
    中文原因（章节/小节 ID 唯一性、template_key 对应关系、标题与条数上限等），比在
    Pydantic 里逐字段声明更贴近 spec 且消息更明确。并发基准两值可选，不传则跳过并发检查。
    """

    model_config = ConfigDict(extra="forbid")

    outline: dict
    expected_outline_version: Optional[str] = Field(default=None, max_length=128)
    expected_outline_sha256: Optional[str] = Field(default=None, max_length=128)

    @field_validator("outline")
    @classmethod
    def outline_is_object(cls, value: dict) -> dict:
        if not isinstance(value, dict):
            raise ValueError("outline 必须是目录 JSON 对象")
        return value


class ReportRecommendationDecision(BaseModel):
    """建议的逐条复核结论（方案 §四 line 54）。

    `confirmed` 只表示"报告采用该建议"，**不代表业务担保审批**——这一点由
    `decision_scope='report_adoption'` 落库固定，不接受客户端改写。
    """

    model_config = ConfigDict(extra="forbid")

    decision: Literal["confirmed", "rejected"]
    note: str = Field(default="", max_length=500)
    # 乐观并发：客户端带上它读到的正文哈希，正文已变则 409（避免给旧正文背书）。
    expected_section_sha256: Optional[str] = Field(default=None, max_length=128)


class ReportTableRefresh(BaseModel):
    chapters: list[Literal["financial", "cashflow", "debt", "rating"]] = Field(min_length=1, max_length=4)
    expected_report_revision: int = Field(ge=0)
    section_hashes: dict[str, str] = Field(min_length=1, max_length=4)

    @field_validator("chapters")
    @classmethod
    def unique_chapters(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("章节不能重复")
        return value


class RunCreate(BaseModel):
    focus: str = Field(default="全面尽调", max_length=300)
    policy_profile: str = Field(default="", max_length=50)


class LlmSubsection(BaseModel):
    """小节响应条目（P1-2）：有小节配置的章节按小节返回正文与引用。"""

    subsection_id: str
    content: str = ""
    citation_ids: list[str] = Field(default_factory=list)


class LlmCalculation(BaseModel):
    """模型自主返回的一条计算（《风控约束分层》方案 §三.1）。

    ⚠️ 输入字段名**必须是 `input_record_ids`，不能叫 `source_ids`**：
    `llm._unknown_source_ids`（llm.py:379）会递归扫描 `source_ids`/`citation_ids` 等键并
    按 citation 别名做白名单校验，取名 `source_ids` 会让整章被判 `invalid_source`。

    ⚠️ 约束**刻意放宽**（只限长度，不设 min_length）：结构不完整应走"同一次结构修复"并带上
    可读原因，而不是在 Pydantic 层变成 `schema_error` 盲改。业务校验在
    `chapter_calculations.prepare_candidate`（B2）用 `verify_calculation` 做。
    """

    model_config = ConfigDict(extra="ignore")

    calculation_id: str = Field(default="", max_length=32)
    name: str = Field(default="", max_length=120)
    definition: str = Field(default="", max_length=400)
    formula: str = Field(default="", max_length=400)
    input_record_ids: list[str] = Field(default_factory=list, max_length=12)
    value: str = Field(default="", max_length=40)
    unit: str = Field(default="", max_length=20)
    subject: str = Field(default="", max_length=120)
    period: str = Field(default="", max_length=20)
    scope: str = Field(default="", max_length=40)
    precision: Optional[int] = Field(default=None, ge=0, le=6)
    cross_period_reason: str = Field(default="", max_length=300)
    # 正文里依赖该指标的原句：B2 用它定位"哪句分析必须跟着改"，也为人工复核指路。
    content_anchor: str = Field(default="", max_length=300)
    applies_to: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("value", mode="before")
    @classmethod
    def _coerce_value_to_text(cls, value: Any) -> str:
        """模型常把数值写成 number：统一转字符串，保留它给的精度（不在这里做舍入）。"""
        if value is None:
            return ""
        if isinstance(value, bool):
            return ""
        if isinstance(value, (int, float)):
            return repr(value)
        return str(value)


class LlmRecommendation(BaseModel):
    """承保/拒保/额度/费率建议（方案 §四 line 52）。

    建议值是**提议**，不是披露事实；`basis` 里的主张仍走支持校验（B3）。
    """

    model_config = ConfigDict(extra="ignore")

    recommendation_id: str = Field(default="", max_length=32)
    recommendation_type: str = Field(default="other", max_length=32)
    content: str = Field(default="", max_length=800)
    basis: str = Field(default="", max_length=800)
    basis_record_ids: list[str] = Field(default_factory=list, max_length=12)
    preconditions: list[str] = Field(default_factory=list, max_length=8)
    amount_value: str = Field(default="", max_length=40)
    amount_unit: str = Field(default="", max_length=20)
    rate_value: str = Field(default="", max_length=40)
    rate_unit: str = Field(default="", max_length=20)
    parameters: dict[str, str] = Field(default_factory=dict)
    content_anchor: str = Field(default="", max_length=200)


class LlmSection(BaseModel):
    # ``content``/``citation_ids`` 放宽为带默认值（P1-2）：有小节配置时模型只返回
    # ``subsections``。空值/缺失的强约束由 ``llm._finalize_report_section`` 承接
    # （缺失小节 → report_repair_required 走唯一一次结构修复），避免 Pydantic 层
    # 把合法的小节响应直接判成 schema_error。
    #
    # `extra="ignore"` 是**显式契约**：模型可能多返回字段，静默忽略优于报错；
    # 但要收下的字段必须在下面声明——Pydantic v2 默认也是 ignore，不声明就会在
    # `llm.py:687` 的 `model_dump()` 里被丢掉（B1 前 `calculations` 正是这样丢的）。
    model_config = ConfigDict(extra="ignore")

    content: str = ""
    citation_ids: list[str] = Field(default_factory=list)
    subsections: list[LlmSubsection] = Field(default_factory=list)
    # 方案 §三.1 / §四：与正文**同一次响应**返回，不额外增加模型调用。
    calculations: list[LlmCalculation] = Field(default_factory=list)
    recommendations: list[LlmRecommendation] = Field(default_factory=list)


class ExtractedFact(BaseModel):
    fact_key: str = Field(min_length=2, max_length=80, validation_alias=AliasChoices("fact_key", "field"))
    section_role: str = Field(default="", max_length=80)
    subject: str = Field(default="", max_length=120)
    scope: Literal["standalone", "consolidated", "entity", "transaction", "unknown"] = "unknown"
    period: str = Field(default="", max_length=20)
    period_type: Literal["duration", "point_in_time", "undated", "unknown"] = "unknown"
    value: Optional[float] = None
    unit: str = Field(default="", max_length=20)
    text_value: str = Field(default="", max_length=1000)
    raw_value: str = Field(default="", max_length=200)
    source_ids: list[str] = Field(
        min_length=1,
        max_length=8,
        validation_alias=AliasChoices("source_ids", "source_id", "citation_ids"),
    )
    confidence: float = Field(default=0.8, ge=0, le=1)

    @field_validator("subject", "period", "unit", "text_value", "raw_value", mode="before")
    @classmethod
    def normalize_nullable_text(cls, value: object) -> object:
        return "" if value is None else value

    @field_validator("confidence", mode="before")
    @classmethod
    def normalize_nullable_confidence(cls, value: object) -> object:
        return 0.8 if value is None else value

    @field_validator("source_ids", mode="before")
    @classmethod
    def normalize_single_source_id(cls, value: object) -> object:
        return [value] if isinstance(value, str) else value


class ExtractedFactBatch(BaseModel):
    facts: list[ExtractedFact] = Field(default_factory=list, max_length=100)


class DocumentSectionLocation(BaseModel):
    section_role: str = Field(min_length=2, max_length=80)
    title: str = Field(min_length=2, max_length=200)
    start_source_id: str = Field(min_length=1, max_length=160)
    end_source_id: str = Field(default="", max_length=160)
    confidence: float = Field(default=0.8, ge=0, le=1)


class DocumentSectionLocationBatch(BaseModel):
    sections: list[DocumentSectionLocation] = Field(default_factory=list, max_length=40)


class FactUpdate(BaseModel):
    period: str = Field(min_length=4, max_length=20)
    value: Optional[float] = None
    unit: str = Field(max_length=20)
    text_value: str = Field(default="", max_length=1000)


class OneShotResult(BaseModel):
    facts: list[ExtractedFact] = Field(default_factory=list, max_length=100)
    warning_keys: list[str] = Field(default_factory=list, max_length=19)
    citation_ids: list[str] = Field(default_factory=list, max_length=100)


class TrancheCandidate(BaseModel):
    tranche_name: str = Field(min_length=2, max_length=80)
    term_years: int
    maturity_date: str = Field(default="", max_length=20)
    issue_amount: Optional[float] = None
    amount_unit: str = Field(default="万元", max_length=10)
    source_ids: list[str] = Field(min_length=1, max_length=8)


class TrancheCandidateBatch(BaseModel):
    tranches: list[TrancheCandidate] = Field(default_factory=list, max_length=12)


class CandidateFindingOutput(BaseModel):
    taxonomy: Literal["governance", "continuity", "authenticity", "fund_diversion", "key_person", "collateral_enforceability"]
    title: str = Field(min_length=2, max_length=100)
    rationale: str = Field(min_length=2, max_length=2000)
    potential_impact: str = Field(min_length=2, max_length=1000)
    likelihood: int = Field(ge=1, le=3)
    impact: int = Field(ge=1, le=3)
    support_source_ids: list[str] = Field(min_length=1, max_length=8)
    counter_source_ids: list[str] = Field(default_factory=list, max_length=8)
    questions: list[str] = Field(min_length=1, max_length=8)
    mitigation: str = Field(min_length=2, max_length=1000)
    confidence: float = Field(default=0.6, ge=0, le=1)


class CandidateFindingBatch(BaseModel):
    findings: list[CandidateFindingOutput] = Field(default_factory=list, max_length=12)


class MaterialQAClaim(BaseModel):
    text: str = Field(min_length=2, max_length=1500)
    source_ids: list[str] = Field(min_length=1, max_length=8)


class MaterialQAResult(BaseModel):
    answer: str = Field(min_length=2, max_length=6000)
    claims: list[MaterialQAClaim] = Field(default_factory=list, max_length=16)


class MaterialQARequest(BaseModel):
    question: str = Field(min_length=2, max_length=1000)
    document_ids: list[str] = Field(default_factory=list, max_length=16)


class FactGroupResolve(BaseModel):
    assertion_id: Optional[str] = None
    manual: Optional[dict] = None
    note: str = Field(min_length=2, max_length=1000)


class TableCellReview(BaseModel):
    """单元格核对结论。更正值与原始值分开存，历史不被覆盖。"""

    decision: Literal["confirmed", "corrected", "rejected"] = "confirmed"
    corrected_raw: Optional[str] = Field(default=None, max_length=200)
    note: str = Field(default="", max_length=1000)
    reviewer: str = Field(default="", max_length=120)


class ReviewTaskUpdate(BaseModel):
    status: Literal["open", "resolved", "waived"]
    resolution_note: str = Field(default="", max_length=2000)
    assignee: str = Field(default="", max_length=80)
    due_date: str = Field(default="", max_length=20)


class CandidateDecision(BaseModel):
    status: Literal["proposed", "confirmed", "rejected"]
    decision_note: str = Field(default="", max_length=2000)
