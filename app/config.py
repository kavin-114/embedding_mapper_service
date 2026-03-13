"""Application configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


SCHEMAS_DIR = Path(__file__).parent / "schemas"


class Settings(BaseSettings):
    """Service-wide settings sourced from env / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- API ---
    app_name: str = "Embedding Mapper Service"
    debug: bool = False

    # --- ChromaDB ---
    chroma_host: str = "localhost"
    chroma_port: int = 8000

    # --- Embedding model ---
    embedding_model: str = "all-MiniLM-L6-v2"

    # --- Company context (used for tax scope determination) ---
    company_country: str = "IN"
    company_region_code: str = "29"  # Karnataka default

    # --- Logging ---
    log_format: str = "json"
    log_level: str = "INFO"

    # --- ERPNext connection (for review ground truth) ---
    erpnext_url: str = ""
    erpnext_api_key: str = ""
    erpnext_api_secret: str = ""

    # --- Review UI ---
    review_files_dir: str = ""

    # --- Sync staleness ---
    sync_stale_hours: int = 6

    # --- Confidence thresholds ---
    hard_key_threshold: float = 0.90
    filter_threshold: float = 0.70
    hint_threshold: float = 0.50

    context_match_threshold: float = 0.75

    auto_map_threshold: float = 0.88
    suggest_threshold: float = 0.70
    review_threshold: float = 0.50


@lru_cache
def get_settings() -> Settings:
    return Settings()
