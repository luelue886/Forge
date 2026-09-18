from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_temperature: float = 0.3
    llm_timeout_s: float = 120.0

    font_path: str = "C:/Windows/Fonts/msyh.ttc"
    fill_workers: int = 4
    com_timeout_s: int = 120
    capacity_warn: float = 0.90
    page_char_budget: int = 200
    max_repair_rounds_page: int = 2
    max_repair_rounds_global: int = 3

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SKINS_DIR = ROOT / "templates" / "skins"
PROMPTS_DIR = ROOT / "prompts"


@lru_cache
def get_settings() -> Settings:
    return Settings()
