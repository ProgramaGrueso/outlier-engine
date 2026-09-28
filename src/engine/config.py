"""Configuración leída de `.env` (nunca secretos en el código)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / ".env"
PROMPTS_DIR = ROOT / "prompts"
WORKFLOWS_DIR = ROOT / "workflows"

# Roles de modelo cheap: cada uno puede usar un modelo de Ollama distinto.
ModelRole = Literal["classify", "write", "critic", "scenes"]
MODEL_ROLES: tuple[ModelRole, ...] = ("classify", "write", "critic", "scenes")

# ANTHROPIC_API_KEY es opcional: solo se exige al llamar al tier premium.
REQUIRED_KEYS = ("youtube_api_key",)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    youtube_api_key: str = ""
    anthropic_api_key: str = ""

    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b"
    ollama_model_classify: str = ""
    ollama_model_write: str = ""
    ollama_model_critic: str = ""
    ollama_model_scenes: str = ""

    anthropic_model: str = "claude-sonnet-5"

    db_path: Path = Path("data/outliers.db")
    outlier_ratio_min: float = 5.0
    yt_daily_quota: int = 10_000

    @field_validator("db_path")
    @classmethod
    def _abs_db_path(cls, v: Path) -> Path:
        return v if v.is_absolute() else ROOT / v

    def ollama_model_for(self, role: ModelRole) -> str:
        """Modelo de Ollama para un rol; cae en OLLAMA_MODEL si no hay override."""
        return getattr(self, f"ollama_model_{role}").strip() or self.ollama_model

    def ollama_models_in_use(self) -> set[str]:
        return {self.ollama_model_for(r) for r in MODEL_ROLES}

    def missing_keys(self) -> list[str]:
        return [k.upper() for k in REQUIRED_KEYS if not getattr(self, k).strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
