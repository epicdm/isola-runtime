"""Application configuration."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings

from app.services.sandbox.config import SandboxConfig, SandboxType


def _running_in_container() -> bool:
    """Best-effort container runtime detection."""
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return True

    cgroup = Path("/proc/1/cgroup")
    if not cgroup.exists():
        return False

    try:
        content = cgroup.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False

    return any(token in content for token in ("docker", "containerd", "kubepods", "podman"))


def _default_agent_data_dir() -> str:
    """Use Docker path in containers, user-writable path on local hosts."""
    if _running_in_container():
        return "/data/agents"
    return str(Path.home() / ".clawith" / "data" / "agents")


def _default_agent_template_dir() -> str:
    """Locate the agent template directory for both Docker and source deployments.

    In a Docker container the backend source is copied to /app, so the template
    lives at /app/agent_template.  In a source deployment it sits next to the
    backend/ package root, i.e. <repo>/backend/agent_template.
    """
    if _running_in_container():
        return "/app/agent_template"
    # Source layout: backend/app/config.py -> ../.. = backend/ -> agent_template
    source_path = Path(__file__).resolve().parent.parent / "agent_template"
    return str(source_path)


def _read_version() -> str:
    """Read version from local VERSION file, fallback to root."""
    for candidate in [Path(__file__).resolve().parent.parent / "VERSION",
                      Path(__file__).resolve().parent.parent.parent / "VERSION",
                      Path("/app/VERSION"), Path("/VERSION")]:
        try:
            return candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return "0.0.0"


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # App
    APP_NAME: str = "Clawith"
    APP_VERSION: str = _read_version()
    DEBUG: bool = False
    SECRET_KEY: str = "change-me-in-production"
    API_PREFIX: str = "/api"

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://clawith:clawith@localhost:5432/clawith"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # JWT
    # Phase E.6c: shared service secret for apps/isola <-> isola-runtime
    # internal API calls (e.g. tenant-runtime-mode proxy). Blank in dev
    # disables the /api/internal/* routes so they cannot be called.
    ISOLA_INTERNAL_SECRET: str = ""
    # L4 S1: shared service secret for outbound isola-runtime -> BFF
    # /api/internal/* calls (cross-store reads of tenant_registry, etc.).
    # Same value as bff-v2's INTERNAL_SECRET. Blank disables outbound BFF calls.
    BFF_INTERNAL_SECRET: str = ""
    JWT_SECRET_KEY: str = "change-me-jwt-secret"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 24 hours
    PASSWORD_RESET_TOKEN_EXPIRE_MINUTES: int = 60
    EMAIL_VERIFICATION_TOKEN_EXPIRE_MINUTES: int = 60  # 1 hour
    EMAIL_VERIFICATION_REQUIRED: bool = False  # Require email verification for login

    # File Storage
    AGENT_DATA_DIR: str = _default_agent_data_dir()
    AGENT_TEMPLATE_DIR: str = _default_agent_template_dir()

    # Docker (for Agent containers)
    DOCKER_NETWORK: str = "clawith_network"
    OPENCLAW_IMAGE: str = "openclaw:local"
    OPENCLAW_GATEWAY_PORT: int = 18789

    # Feishu OAuth
    FEISHU_APP_ID: str = ""
    FEISHU_APP_SECRET: str = ""
    FEISHU_REDIRECT_URI: str = ""
    PUBLIC_BASE_URL: str = ""

    # CORS
    CORS_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:5173"]

    # Jina AI (Reader + Search APIs)
    JINA_API_KEY: str = ""

    # Exa AI (Search API)
    EXA_API_KEY: str = ""

    # Odoo (Phase F.1.b) — self-hosted multi-company ERP at odoo.epic.dm.
    # ensure-agent calls OdooService.ensure_company() to create a res.company
    # for each Isola tenant, storing the returned id on tenants.odoo_company_id.
    ODOO_URL: str = "https://odoo.epic.dm"
    ODOO_DB: str = "epic"
    ODOO_ADMIN_LOGIN: str = "admin"
    ODOO_ADMIN_PASSWORD: str = ""
    # Toggle: when False, ensure-agent skips the Odoo call. Useful when Odoo
    # is down or during migrations so tenant provisioning doesn't block.
    ODOO_ENABLED: bool = True


    # Sandbox configuration
    SANDBOX_TYPE: SandboxType = SandboxType.SUBPROCESS
    SANDBOX_API_KEY: str = ""
    SANDBOX_API_URL: str = ""
    SANDBOX_CPU_LIMIT: str = "0.5"
    SANDBOX_MEMORY_LIMIT: str = "256m"
    SANDBOX_ALLOW_NETWORK: bool = False
    SANDBOX_DEFAULT_TIMEOUT: int = 30
    SANDBOX_MAX_TIMEOUT: int = 60

    model_config = {
        "env_file": [".env", "../.env"],
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
        "extra": "ignore",
    }


@lru_cache
def get_settings() -> Settings:
    """Get cached application settings."""
    return Settings()


def get_sandbox_config() -> SandboxConfig:
    """Create SandboxConfig from application settings."""
    settings = get_settings()
    return SandboxConfig(
        type=settings.SANDBOX_TYPE,
        enabled=True,
        api_key=settings.SANDBOX_API_KEY,
        api_url=settings.SANDBOX_API_URL,
        cpu_limit=settings.SANDBOX_CPU_LIMIT,
        memory_limit=settings.SANDBOX_MEMORY_LIMIT,
        allow_network=settings.SANDBOX_ALLOW_NETWORK,
        default_timeout=settings.SANDBOX_DEFAULT_TIMEOUT,
        max_timeout=settings.SANDBOX_MAX_TIMEOUT,
    )
