"""Configuration loading for SupportPulse-Analytics.

Loads ``config/config.yaml`` and ``config/sla_policies.yaml`` into strongly
typed Pydantic models, with environment-variable overrides of the form
``SUPPORTPULSE_<SECTION>__<FIELD>`` (double underscore separates nesting).
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

ENV_PREFIX = "SUPPORTPULSE_"


class JiraConfig(BaseModel):
    """Connection settings for the Jira REST API."""

    base_url: str = "https://supportpulse.atlassian.net"
    api_version: int = 3
    email: str | None = None
    api_token: str | None = None
    verify_ssl: bool = True
    timeout: int = 30
    page_size: int = 50
    max_retries: int = 4
    backoff_factor: float = 0.75
    rate_limit_pause: float = 2.0
    fields: list[str] = Field(
        default_factory=lambda: [
            "key",
            "summary",
            "priority",
            "status",
            "created",
            "resolutiondate",
            "assignee",
            "components",
            "labels",
        ]
    )

    @property
    def search_path(self) -> str:
        """REST path used for JQL search on the configured API version."""

        return f"/rest/api/{self.api_version}/search"


class ProjectConfig(BaseModel):
    """Support project descriptor."""

    key: str = "SUP"
    name: str = "Enterprise Software Support"
    default_days: int = 30
    support_tiers: list[str] = Field(
        default_factory=lambda: ["Tier 1", "Tier 2", "Development"]
    )


class ForecastConfig(BaseModel):
    """Tuning parameters for the SLA breach forecaster."""

    imminent_risk_threshold: float = 60.0
    critical_risk_threshold: float = 85.0
    minimum_history_samples: int = 8
    ping_pong_threshold: int = 3


class AppSettings(BaseModel):
    """Top-level application settings."""

    name: str = "SupportPulse-Analytics"
    version: str = "1.0.0"
    mock_mode: bool = True
    output_dir: str = "examples/generated_reports"
    timezone: str = "Europe/Berlin"
    business_hours_per_day: float = 8.0


class AppConfig(BaseModel):
    """Aggregate configuration object."""

    app: AppSettings = Field(default_factory=AppSettings)
    jira: JiraConfig = Field(default_factory=JiraConfig)
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    forecasting: ForecastConfig = Field(default_factory=ForecastConfig)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------
def project_root() -> Path:
    """Return the repository root.

    Resolution order: ``SUPPORTPULSE_ROOT`` env var, then walk upwards from
    this file looking for a directory containing ``pyproject.toml``, finally
    falling back to the current working directory.
    """

    env_root = os.environ.get(f"{ENV_PREFIX}ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    here = Path(__file__).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "pyproject.toml").exists():
            return candidate
    return Path.cwd()


def config_dir() -> Path:
    """Return the ``config/`` directory, honouring an env override."""

    env_dir = os.environ.get(f"{ENV_PREFIX}CONFIG_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return project_root() / "config"


# ---------------------------------------------------------------------------
# Environment overrides
# ---------------------------------------------------------------------------
def _coerce(value: str) -> Any:
    """Coerce an environment string into a bool / int / float / str."""

    lowered = value.strip().lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """Apply ``SUPPORTPULSE_<SECTION>__<FIELD>`` overrides onto *data*."""

    for key, raw in os.environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        path = key[len(ENV_PREFIX) :]
        if "__" not in path:
            continue
        section, _, field = path.partition("__")
        section = section.lower()
        field = field.lower()
        node = data.setdefault(section, {})
        if isinstance(node, dict):
            node[field] = _coerce(raw)
    return data


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------
def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Configuration file {path} must contain a mapping")
    return loaded


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load the application configuration.

    Args:
        path: Optional explicit path to ``config.yaml``.

    Returns:
        A fully validated :class:`AppConfig`.
    """

    config_path = Path(path) if path is not None else config_dir() / "config.yaml"
    data = _read_yaml(config_path)
    data = _apply_env_overrides(data)
    return AppConfig.model_validate(data)


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """Return a cached default configuration instance."""

    return load_config()


def resolve_output_dir(settings: AppConfig | None = None) -> Path:
    """Return (and create) the configured output directory."""

    cfg = settings or get_config()
    env_dir = os.environ.get(f"{ENV_PREFIX}OUTPUT_DIR")
    base = Path(env_dir) if env_dir else project_root() / cfg.app.output_dir
    base.mkdir(parents=True, exist_ok=True)
    return base
