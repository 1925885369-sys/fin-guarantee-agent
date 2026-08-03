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
    input_price_per_m: float = float(os.getenv("LLM_INPUT_PRICE_PER_M", "2"))
    output_price_per_m: float = float(os.getenv("LLM_OUTPUT_PRICE_PER_M", "8"))
    public_demo: bool = _bool("PUBLIC_DEMO")
    max_files: int = int(os.getenv("MAX_FILES", "8"))
    max_file_mb: int = int(os.getenv("MAX_FILE_MB", "15"))
    max_pages: int = int(os.getenv("MAX_PAGES", "150"))
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


settings = Settings()
