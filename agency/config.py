"""Configuration loading: config.yaml (non-secret settings) + .env (secrets only)."""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PACKAGE_ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, Any] = {
    "channel": {"name": "", "language": "he", "secondary_language": "en",
                "niche": "Counter-Strike 2", "handle": ""},
    "dry_run": True,
    "llm": {"model": "claude-opus-5-5", "effort": "medium", "max_tokens": 16000, "fallbacks": True},
    "publishing": {"mode": "approve", "platforms": {}, "timezone": "Asia/Jerusalem"},
    "finance": {"currency": "ILS", "monthly_budget_ils": 0, "max_single_spend_ils": 0},
    "staff": {"enabled": ["analyst", "editor", "copywriter", "strategist", "publisher",
                          "promoter", "community", "analytics", "finance"]},
    "community": {"auto_reply": False},
    "promotion": {"discord_webhook_enabled": False},
    "dashboard": {"host": "127.0.0.1", "port": 8765},
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Return a new dict: ``base`` recursively updated with ``override``."""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


@dataclass
class Settings:
    """Loaded configuration. ``raw`` is the merged YAML dict, ``root`` the project dir."""

    raw: dict = field(default_factory=dict)
    root: Path = field(default_factory=Path.cwd)

    def get(self, dotted: str, default: Any = None) -> Any:
        """Look up a dotted key such as ``"finance.monthly_budget_ils"``."""
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return default if node is None else node

    @property
    def dry_run(self) -> bool:
        """True unless config explicitly sets ``dry_run: false`` (fail safe)."""
        value = self.raw.get("dry_run", True)
        if isinstance(value, str):
            return value.strip().lower() not in ("false", "0", "no", "off")
        return value is not False

    @property
    def data_dir(self) -> Path:
        """Data directory (default ``<root>/data``); created on access."""
        configured = self.get("data_dir") or os.environ.get("AGENCY_DATA_DIR") or "data"
        path = Path(configured)
        if not path.is_absolute():
            path = self.root / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def db_path(self) -> Path:
        """SQLite database file inside the data dir."""
        return self.data_dir / "agency.db"


def resolve_root() -> Path:
    """The project directory: ``AGENCY_ROOT``, else the current directory if it has a
    config.yaml, else the install directory when *it* has one (so running ``agency`` from
    another folder still uses the owner's real data, .env and config), else the cwd."""
    env_root = os.environ.get("AGENCY_ROOT")
    if env_root:
        return Path(env_root).resolve()
    cwd = Path.cwd().resolve()
    if not (cwd / "config.yaml").exists() and (PACKAGE_ROOT / "config.yaml").exists():
        return PACKAGE_ROOT
    return cwd


def has_config(settings: "Settings") -> bool:
    """True when a real config.yaml exists at the settings root."""
    return (settings.root / "config.yaml").exists()


class ConfigError(ValueError):
    """config.yaml is unreadable or has a bad value; the message is owner-facing Hebrew."""


_NUMERIC_KEYS = ("finance.monthly_budget_ils", "finance.max_single_spend_ils",
                 "dashboard.port", "dashboard.max_upload_gb",
                 "publishing.stale_publishing_minutes")


def validate_settings(settings: "Settings") -> None:
    """Raise :class:`ConfigError` (Hebrew) for value types that would crash later."""
    for key in _NUMERIC_KEYS:
        value = settings.get(key)
        if value is None or isinstance(value, bool):
            if isinstance(value, bool):
                raise ConfigError(f"הערך של {key} חייב להיות מספר (כתוב: {value})")
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ConfigError(f"הערך של {key} חייב להיות מספר, אבל כתוב בו: {value!r}") from None
        if number != number or number < 0:
            raise ConfigError(f"הערך של {key} חייב להיות מספר חיובי (כתוב: {value!r})")
    port = settings.get("dashboard.port")
    if port is not None and (float(port) != int(float(port)) or not 0 < int(float(port)) < 65536):
        raise ConfigError(f"dashboard.port חייב להיות מספר שלם בין 1 ל-65535 (כתוב: {port!r})")
    for section in ("channel", "llm", "publishing", "finance", "staff", "dashboard"):
        if not isinstance(settings.raw.get(section), dict):
            raise ConfigError(f"החלק {section} ב-config.yaml חייב להיות רשימת הגדרות "
                              "(בדוק רווחים והזחות)")


def load_settings(path: Path | None = None) -> Settings:
    """Load settings from ``path`` or ``./config.yaml`` (fallback ``config.example.yaml``).

    Also loads ``.env`` from the project root (never overrides real environment variables).
    """
    if path is not None:
        path = Path(path)
        root = path.resolve().parent
    else:
        root = resolve_root()
        candidates = [root / "config.yaml", root / "config.example.yaml",
                      PACKAGE_ROOT / "config.example.yaml"]
        path = next((c for c in candidates if c.exists()), None)

    env_file = root / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=False)

    data: dict = {}
    if path is not None and Path(path).exists():
        with open(path, "r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"config file {path} must contain a mapping at top level")
        data = loaded
    return Settings(raw=_deep_merge(DEFAULTS, data), root=root)


def secret(name: str) -> str | None:
    """Return a secret from the environment (``.env`` is loaded by load_settings); None if empty."""
    value = os.environ.get(name)
    return value if value else None
