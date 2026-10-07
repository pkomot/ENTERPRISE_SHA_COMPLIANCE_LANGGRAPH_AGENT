"""Runtime configuration for the SHA compliance audit agent.

All settings are read from environment variables (prefix ``SHA_AUDIT_``) so the
same code runs locally, in CI, and in production without modification.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_ANTHROPIC_MODEL = "anthropic:claude-sonnet-5-5"

MOCK_ODPC_BASE_URL = "https://odpc.registry.mock/api/v1"
MOCK_KMPDC_BASE_URL = "https://kmpdc.registry.mock/api/v1"
MOCK_CIHIS_BASE_URL = "https://cihis.dha.mock/api/v1"


class AuditSettings(BaseModel):
    """Immutable, validated runtime settings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["mock", "live"] = Field(
        default="mock",
        description="'mock' serves synthetic registries in-process; 'live' calls real endpoints.",
    )
    http_timeout_s: float = Field(default=5.0, gt=0, le=60)
    llm_timeout_s: float = Field(default=30.0, gt=0, le=300)
    retry_max_attempts: int = Field(default=3, ge=1, le=10)
    retry_initial_interval_s: float = Field(default=0.5, gt=0, le=30)
    compliance_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    cache_ttl_s: float = Field(default=86_400.0, gt=0)
    breaker_failure_threshold: int = Field(default=3, ge=1)
    breaker_reset_timeout_s: float = Field(default=30.0, gt=0)

    odpc_base_url: str = MOCK_ODPC_BASE_URL
    kmpdc_base_url: str = MOCK_KMPDC_BASE_URL
    cihis_base_url: str = MOCK_CIHIS_BASE_URL
    registry_bearer_token: str | None = Field(default=None, repr=False)

    llm_model: str | None = Field(
        default=None,
        description="LangChain 'provider:model' string; None runs the deterministic engine.",
    )
    checkpoint_db: str | None = Field(
        default=None,
        description="SQLite path for AsyncSqliteSaver. None uses the in-memory MemorySaver.",
    )

    @model_validator(mode="after")
    def _live_mode_requires_real_endpoints(self) -> AuditSettings:
        if self.mode == "live":
            mock_urls = [
                url
                for url in (self.odpc_base_url, self.kmpdc_base_url, self.cihis_base_url)
                if ".mock" in url
            ]
            if mock_urls:
                raise ValueError(
                    "SHA_AUDIT_MODE=live requires real registry base URLs; still mocked: "
                    + ", ".join(mock_urls)
                )
        return self

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AuditSettings:
        """Build settings from environment variables, ignoring unset values."""
        env = os.environ if env is None else env
        mapping = {
            "mode": "SHA_AUDIT_MODE",
            "http_timeout_s": "SHA_AUDIT_HTTP_TIMEOUT_S",
            "llm_timeout_s": "SHA_AUDIT_LLM_TIMEOUT_S",
            "retry_max_attempts": "SHA_AUDIT_RETRY_MAX_ATTEMPTS",
            "retry_initial_interval_s": "SHA_AUDIT_RETRY_INITIAL_INTERVAL_S",
            "compliance_threshold": "SHA_AUDIT_COMPLIANCE_THRESHOLD",
            "cache_ttl_s": "SHA_AUDIT_CACHE_TTL_S",
            "breaker_failure_threshold": "SHA_AUDIT_BREAKER_FAILURE_THRESHOLD",
            "breaker_reset_timeout_s": "SHA_AUDIT_BREAKER_RESET_TIMEOUT_S",
            "odpc_base_url": "SHA_AUDIT_ODPC_BASE_URL",
            "kmpdc_base_url": "SHA_AUDIT_KMPDC_BASE_URL",
            "cihis_base_url": "SHA_AUDIT_CIHIS_BASE_URL",
            "registry_bearer_token": "SHA_AUDIT_REGISTRY_BEARER_TOKEN",
            "checkpoint_db": "SHA_AUDIT_CHECKPOINT_DB",
        }
        values: dict[str, str | None] = {
            field: env[var] for field, var in mapping.items() if env.get(var)
        }

        llm_model = env.get("SHA_AUDIT_LLM_MODEL")
        if llm_model is None and env.get("ANTHROPIC_API_KEY"):
            llm_model = DEFAULT_ANTHROPIC_MODEL
        if llm_model and llm_model.lower() != "offline":
            values["llm_model"] = llm_model

        return cls.model_validate(values)
