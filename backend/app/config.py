from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    root_dir: Path = Path(__file__).resolve().parents[2]
    data_dir: Path = Path(os.getenv("APP_DATA_DIR", ".data"))
    llm_base_url: str = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "deepseek-v4-flash")
    ocr_enabled: bool = _bool("OCR_ENABLED")
    ocr_base_url: str = os.getenv("OCR_BASE_URL", os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"))
    ocr_api_key: str = os.getenv("OCR_API_KEY", "")
    ocr_model: str = os.getenv("OCR_MODEL", "")
    ocr_max_pages: int = int(os.getenv("OCR_MAX_PAGES", "30"))
    policy_profile: str = os.getenv("POLICY_PROFILE", "default-v1")
    input_price_per_m: float = float(os.getenv("LLM_INPUT_PRICE_PER_M", "2"))
    output_price_per_m: float = float(os.getenv("LLM_OUTPUT_PRICE_PER_M", "8"))
    max_run_model_cost_cny: float = float(os.getenv("MAX_RUN_MODEL_COST_CNY", "2"))
    max_run_model_calls: int = int(os.getenv("MAX_RUN_MODEL_CALLS", "40"))
    llm_batch_max_chars: int = int(os.getenv("LLM_BATCH_MAX_CHARS", "24000"))
    # 章节改写要带上该章的事实、指标、核验与证据上下文，规模远大于抽取批次：
    # 抽取批次按材料切分，单批可控；报告上下文整章一次送入。用同一个上限会让
    # 事实密集的章节（财务/债务）在裁剪到 128 字符后仍然超限而被直接放弃，
    # 整章退回确定性草稿。这里给报告链路单独一个更大的预算。
    llm_report_max_chars: int = int(os.getenv("LLM_REPORT_MAX_CHARS", "64000"))
    llm_extract_batch_max_sources: int = int(os.getenv("LLM_EXTRACT_BATCH_MAX_SOURCES", "3"))
    llm_extract_max_batches: int = int(os.getenv("LLM_EXTRACT_MAX_BATCHES", "16"))
    llm_extract_sources_per_topic: int = int(os.getenv("LLM_EXTRACT_SOURCES_PER_TOPIC", "2"))
    llm_downstream_reserved_calls: int = int(os.getenv("LLM_DOWNSTREAM_RESERVED_CALLS", "12"))
    llm_extract_max_cost_ratio: float = float(os.getenv("LLM_EXTRACT_MAX_COST_RATIO", "0.6"))
    llm_max_output_tokens: int = int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "5000"))
    # 报告章节单独放行：逐章正文要写到用户在 Prompt 里设置的目标字数，5000 tokens 的
    # 通用上限会先撞墙（一个汉字约 1 token，还要额外产出结构化字段）。它**不**受
    # ``llm_max_output_tokens`` 钳制，但抽取等其他任务仍然受。
    llm_report_max_output_tokens: int = int(os.getenv("LLM_REPORT_MAX_OUTPUT_TOKENS", "10000"))
    llm_connect_timeout_seconds: float = float(os.getenv("LLM_CONNECT_TIMEOUT_SECONDS", "10"))
    llm_read_timeout_seconds: float = float(os.getenv("LLM_READ_TIMEOUT_SECONDS", "60"))
    llm_write_timeout_seconds: float = float(os.getenv("LLM_WRITE_TIMEOUT_SECONDS", "30"))
    llm_pool_timeout_seconds: float = float(os.getenv("LLM_POOL_TIMEOUT_SECONDS", "10"))
    llm_latency_anomaly_seconds: float = float(os.getenv("LLM_LATENCY_ANOMALY_SECONDS", "90"))
    llm_disable_thinking: bool = _bool("LLM_DISABLE_THINKING", True)
    retrieval_model_dir: Path = Path(os.getenv("RETRIEVAL_MODEL_DIR", ".models/bge-base-zh-v1.5"))
    retrieval_query_prefix: str = os.getenv(
        "RETRIEVAL_QUERY_PREFIX", "为这个句子生成表示以用于检索相关文章："
    )
    retrieval_max_chunks: int = int(os.getenv("RETRIEVAL_MAX_CHUNKS", "30"))
    retrieval_max_query_chars: int = int(os.getenv("RETRIEVAL_MAX_QUERY_CHARS", "1000"))
    retrieval_index_batch_size: int = int(os.getenv("RETRIEVAL_INDEX_BATCH_SIZE", "16"))
    retrieval_chunk_max_tokens: int = int(os.getenv("RETRIEVAL_CHUNK_MAX_TOKENS", "384"))
    retrieval_chunk_overlap_tokens: int = int(os.getenv("RETRIEVAL_CHUNK_OVERLAP_TOKENS", "64"))
    # 抽取检索配额：**跨所有章节累计**，用满后后续字段连检索都不发起（workflow.py 的
    # missing_keys 循环直接 `continue`）。实测各 section 的 allowed_fact_keys 合计 118 个槽位，
    # 配额 24 时诊断出 `retrieval_unchecked_fields=66`——评级/债务/现金流/控制人字段全被跳过，
    # 评级章因此拿不到任何评级事实、只能写"材料不足"。默认 120 覆盖 118 个槽位并留少量余量。
    # 提高它只多跑短文本向量化（不调用 LLM），模型调用仍由批次与费用预算单独封顶。
    retrieval_extract_query_limit: int = int(os.getenv("RETRIEVAL_EXTRACT_QUERY_LIMIT", "120"))
    public_demo: bool = _bool("PUBLIC_DEMO")
    allow_real_reference_export: bool = _bool("ALLOW_REAL_REFERENCE_EXPORT")
    max_files: int = int(os.getenv("MAX_FILES", "8"))
    max_file_mb: int = int(os.getenv("MAX_FILE_MB", "15"))
    # Bond prospectuses commonly run several hundred pages. Keep the limit
    # high enough for complete disclosure while retaining file/character caps.
    max_pages: int = int(os.getenv("MAX_PAGES", "600"))
    max_total_chars: int = int(os.getenv("MAX_TOTAL_CHARS", "300000"))
    max_zip_entries: int = int(os.getenv("MAX_ZIP_ENTRIES", "3000"))
    max_zip_uncompressed_mb: int = int(os.getenv("MAX_ZIP_UNCOMPRESSED_MB", "120"))
    retention_hours: int = int(os.getenv("RETENTION_HOURS", "24"))

    @property
    def resolved_data_dir(self) -> Path:
        path = self.data_dir if self.data_dir.is_absolute() else self.root_dir / self.data_dir
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def database_path(self) -> Path:
        return self.resolved_data_dir / "agent.db"

    @property
    def resolved_retrieval_model_dir(self) -> Path:
        path = self.retrieval_model_dir
        return path if path.is_absolute() else self.root_dir / path


settings = Settings()
