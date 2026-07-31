from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


Severity = Literal["high", "medium", "low"]


class ProjectCreate(BaseModel):
    company_name: str = Field(min_length=2, max_length=80)
    amount: float = Field(gt=0, le=100000)
    term_months: int = Field(ge=1, le=120)
    purpose: str = Field(min_length=2, max_length=300)
    data_consent: bool


class SectionUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=30000)
    review_status: Literal["draft", "reviewed"] = "draft"


class RunCreate(BaseModel):
    focus: str = Field(default="全面尽调", max_length=300)


class LlmSection(BaseModel):
    content: str
    citation_ids: list[str]


class ExtractedFact(BaseModel):
    fact_key: str = Field(min_length=2, max_length=80)
    period: str = Field(default="", max_length=20)
    value: Optional[float] = None
    unit: str = Field(default="", max_length=20)
    text_value: str = Field(default="", max_length=1000)
    raw_value: str = Field(default="", max_length=200)
    source_ids: list[str] = Field(min_length=1, max_length=8)
    confidence: float = Field(default=0.8, ge=0, le=1)


class ExtractedFactBatch(BaseModel):
    facts: list[ExtractedFact] = Field(default_factory=list, max_length=100)


class FactUpdate(BaseModel):
    period: str = Field(min_length=4, max_length=20)
    value: Optional[float] = None
    unit: str = Field(max_length=20)
    text_value: str = Field(default="", max_length=1000)


class OneShotResult(BaseModel):
    facts: list[ExtractedFact] = Field(default_factory=list, max_length=100)
    warning_keys: list[str] = Field(default_factory=list, max_length=10)
    citation_ids: list[str] = Field(default_factory=list, max_length=100)
