from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    app_env: str = "development"
    database_url: SecretStr = SecretStr("")
    db_backend: str = "sqlite"
    postgres_host: str = "localhost"
    postgres_db: str = "company_ai"
    postgres_user: str = "company_ai"
    postgres_password: SecretStr = SecretStr("")
    postgres_port: int = Field(5432, ge=1, le=65535)
    db_pool_mode: str = "pooled"
    db_pool_size: int = Field(16, ge=1)
    db_max_overflow: int = Field(48, ge=0)
    db_pool_recycle: int = Field(3600, ge=1)
    db_pool_timeout: int = Field(30, ge=1)
    redis_url: str = "redis://localhost:6379/0"
    openai_api_key: SecretStr = SecretStr("")
    anthropic_api_key: SecretStr = SecretStr("")
    tavily_api_key: SecretStr = SecretStr("")
    tavily_request_timeout_seconds: int = Field(30, ge=1)
    tavily_max_results: int = Field(5, ge=1, le=5)
    collect_max_attempts: int = Field(2, ge=1, le=10)
    openai_model: str = "gpt-4.1-mini"
    llm_routing_json: str | None = Field(default=None, repr=False)
    ollama_base_url: str = Field(default="http://localhost:11434", repr=False)
    llm_temperature: float = Field(0, ge=0, le=2, allow_inf_nan=False)
    llm_request_timeout_seconds: int = Field(90, ge=1)
    max_llm_calls_per_job: int = Field(3, ge=1)
    max_agent_iterations: int = Field(2, ge=1, le=3)
    max_input_chars: int = Field(6000, ge=100, le=20000)
    max_output_tokens_per_step: int = Field(500, ge=100, le=4000)
    max_estimated_usd_per_job: float = Field(0.20, gt=0, allow_inf_nan=False)
    input_usd_per_million: float = Field(0.40, gt=0, allow_inf_nan=False)
    output_usd_per_million: float = Field(1.60, gt=0, allow_inf_nan=False)
    lease_ttl_seconds: int = Field(300, ge=5)
    recovery_scan_seconds: int = Field(60, ge=1)
    max_step_attempts: int = Field(3, ge=1, le=10)
    event_retention_days: int = Field(30, ge=1)
    admin_token: SecretStr = SecretStr("")
    log_level: str = "INFO"
    langfuse_tracing_enabled: bool = True
    langfuse_public_key: SecretStr = SecretStr("")
    langfuse_secret_key: SecretStr = SecretStr("")
    langfuse_base_url: str = ""
    langfuse_host: str = ""  # Compatibility with older Langfuse environment files.
    langfuse_tracing_environment: str = ""
    langfuse_capture_content: bool = False
    langfuse_timeout_seconds: int = Field(2, ge=1, le=10)
    otel_sdk_disabled: bool = False
    failure_point: str = ""
    failure_step: str = "RESEARCH"

    @property
    def database_dsn(self):
        explicit = self.database_url.get_secret_value()
        if explicit:
            url = make_url(explicit)
            drivers = {
                "sqlite": "sqlite+aiosqlite",
                "postgres": "postgresql+asyncpg",
                "postgresql": "postgresql+asyncpg",
            }
            return url.set(drivername=drivers.get(url.drivername, url.drivername))
        if self.db_backend == "postgresql":
            return URL.create(
                "postgresql+asyncpg",
                username=self.postgres_user,
                password=self.postgres_password.get_secret_value(),
                host=self.postgres_host,
                port=self.postgres_port,
                database=self.postgres_db,
            )
        return make_url("sqlite+aiosqlite:///./data/ai_research.db")

    @model_validator(mode="after")
    def safe_lease(self):
        from company_ai.domain.research.llm import (
            normalize_ollama_endpoint_url,
            resolve_execution_config,
        )

        if self.db_backend not in ("sqlite", "postgresql"):
            raise ValueError("DB_BACKEND must be sqlite or postgresql")
        if self.db_pool_mode not in ("pooled", "null"):
            raise ValueError("DB_POOL_MODE must be pooled or null")
        if self.database_dsn.drivername not in ("sqlite+aiosqlite", "postgresql+asyncpg"):
            raise ValueError("Use sqlite+aiosqlite or postgresql+asyncpg")
        self.ollama_base_url = normalize_ollama_endpoint_url(self.ollama_base_url)
        # Pure configuration validation: no provider imports, credentials checks or I/O.
        resolve_execution_config(self)
        self.collect_max_attempts = min(self.collect_max_attempts, self.max_step_attempts)
        if self.app_env != "development" and self.failure_point:
            raise ValueError("Failure injection is development-only")
        return self


@lru_cache
def get_settings():
    return Settings()
