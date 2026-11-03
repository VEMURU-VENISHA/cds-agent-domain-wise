"""Application Configuration Module using Pydantic Settings."""

from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Union
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(BACKEND_DIR / ".env", BACKEND_DIR.parent / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Core Application Settings
    APP_NAME: str = "SAP CDS Intelligence & Automation Platform"
    APP_VERSION: str = "0.1.0"
    ENVIRONMENT: str = "development"
    DEBUG: bool = True
    APP_HOST: str = "127.0.0.1"
    APP_PORT: int = 8000
    LOG_LEVEL: str = "INFO"

    # CORS Settings
    CORS_ORIGINS: Union[str, List[str]] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
    ]

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def assemble_cors_origins(cls, v: Union[str, List[str]]) -> List[str]:
        if isinstance(v, str):
            return [i.strip() for i in v.split(",") if i.strip()]
        return v

    # Database Settings
    # Default to sqlite+aiosqlite for zero-config local development when Postgres is not running
    DATABASE_URL: str = Field(
        default="sqlite+aiosqlite:///./platform_dev.db",
        description="Async connection string for database",
    )
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20

    # SAP Environment Configuration
    SAP_CONNECTOR_ENABLED: bool = False
    SAP_CONNECTOR_TYPE: str = "stub"  # "stub" or "adt"
    SAP_SYSTEM_ID: str = "DEV"
    SAP_DEV_SYSTEM_ID: str = ""
    SAP_CLIENT: str = "100"
    SAP_S4HANA_RELEASE: str = "2023"
    SAP_S4CORE_RELEASE: str = "108 SP03"
    SAP_BASIS_RELEASE: str = "758 SP03"

    # Mandatory Live Read-Only Safety Gates
    SAP_LIVE_READ_ONLY: bool = Field(
        default=True,
        description="Read-only live SAP mode. Writes remain blocked unless explicitly disabled together with SAP_DEV_WRITE_ENABLED.",
    )
    SAP_LIVE_TEST_ENABLED: bool = Field(
        default=False,
        description="Explicit opt-in flag required to un-skip live integration test suite.",
    )
    SAP_DEV_WRITE_ENABLED: bool = Field(
        default=False,
        description="Administrator opt-in required for controlled SAP DEV write operations. Defaults to read-only.",
    )
    SAP_CREATION_TEST_BYPASS_VALIDATION: bool = Field(
        default=False,
        description="Explicit opt-in flag for testing bypass of validation gates.",
    )
    SAP_DEV_PACKAGE: str = Field(
        default="ZTRAINING_2026",
        description="Target SAP ABAP package for controlled CDS creation in DEV.",
    )

    # SAP ADT Network & Auth Settings
    SAP_BASE_URL: str = Field(
        default="",
        description="Base URL of SAP host e.g. https://s4dev.company.corp:44300",
    )
    SAP_AUTH_TYPE: str = Field(
        default="basic",
        description="Authentication mechanism: 'basic' or 'token' (configurable, not hard-coded protocol requirement)",
    )
    SAP_USERNAME: str = Field(
        default="",
        description="SAP username with ADT display authorizations",
    )
    SAP_PASSWORD: SecretStr = Field(
        default=SecretStr(""),
        description="SAP user password, masked to avoid accidental disclosure",
    )
    SAP_API_TOKEN: SecretStr = Field(
        default=SecretStr(""),
        description="Optional API bearer token if token-based auth is configured",
    )
    SAP_VERIFY_SSL: bool = Field(
        default=True,
        description="Whether to enforce SSL/TLS certificate validation (secure default: True)",
    )
    SAP_CA_BUNDLE_PATH: Optional[str] = Field(
        default=None,
        description="Optional filesystem path to custom corporate CA root certificate bundle",
    )
    SAP_SEND_CLIENT_PARAM: bool = Field(
        default=False,
        description="Whether to append sap-client query parameter (configuration-dependent, not system-verified requirement)",
    )
    SAP_REQUEST_TIMEOUT_SECONDS: int = Field(
        default=15,
        description="HTTP request timeout in seconds for SAP ADT calls",
    )

    # Gemini semantic mapping (API key must come from environment/.env)
    GEMINI_API_KEY: SecretStr = Field(default=SecretStr(""), description="Gemini API key")
    GEMINI_MODEL: str = "gemini-2.5-flash"
    GEMINI_TIMEOUT_SECONDS: int = 20

    # Agent Orchestration Settings
    AGENT_MAX_ITERATIONS: int = 10
    AGENT_TIMEOUT_SECONDS: int = 60


@lru_cache
def get_settings() -> Settings:
    """Returns cached application settings."""
    return Settings()
