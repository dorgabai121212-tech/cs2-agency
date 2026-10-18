"""Configuration loading: config.yaml (non-secret settings) + .env (secrets only)."""
from __future__ import annotations

import copy
import os
import threading
import time
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
    # backend: auto | api | claude_cli | mock (auto: API key -> api, Claude Code -> claude_cli)
    "llm": {"model": "claude-opus-5-5", "effort": "medium", "max_tokens": 16000, "fallbacks": True,
            "backend": "auto", "claude_path": "", "cli_timeout_s": 240, "cli_daily_calls": 60},
    "publishing": {"mode": "approve", "platforms": {}, "timezone": "Asia/Jerusalem"},
    "finance": {"currency": "ILS", "monthly_budget_ils": 0, "max_single_spend_ils": 0},
    "staff": {"enabled": ["analyst", "editor", "copywriter", "strategist", "publisher",
                          "promoter", "community", "analytics", "finance"]},
    "community": {"auto_reply": False},
    "promotion": {"discord_webhook_enabled": False},
    "dashboard": {"host": "127.0.0.1", "port": 8765},
    # Meta ids are not secrets: the importer copies them here from the studio DB.
    "meta": {"page_id": "", "ig_user_id": ""},
    "studio": {"dir": "", "double_post_guard": True},
    "inbox": {"enabled": False, "folders": [], "stable_seconds": 10, "interval_s": 15},
}

DEFAULT_STUDIO_DIR = r"C:\CS2_STUDIO"
# Platforms that publish through Zernio by default (``publishing.platforms.<key>.via``).
ZERNIO_PLATFORMS = ("youtube", "youtube_shorts", "tiktok")
# key -> key whose connection (and default live/via switch) it shares.
SHARED_CONNECTION = {"youtube_shorts": "youtube"}


def _is_true(value: Any) -> bool:
    """Strict truthiness for safety switches: only True / "true" / "yes" / "on" / "1"."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "on", "1")
    return False


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
    config_path: Path | None = None  # the owner's config.yaml (target of the settings writer)

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

    def platform_live(self, key: str) -> bool:
        """Owner's "real upload" switch for one platform (credentials are checked elsewhere).

        Global ``dry_run: false`` makes every platform live. While the global ``dry_run`` is
        true (the master safety), a platform is live only with an explicit
        ``publishing.platforms.<key>.live: true``. Anything unclear means training mode.
        ``youtube_shorts`` shares YouTube's switch unless it has its own ``live`` value.
        """
        if not self.dry_run:
            return True
        value = self.get(f"publishing.platforms.{key}.live")
        if value is None and key in SHARED_CONNECTION:
            value = self.get(f"publishing.platforms.{SHARED_CONNECTION[key]}.live")
        return _is_true(value)

    def platform_via(self, key: str) -> str:
        """Upload route for ``key``: ``"zernio"`` (default for YouTube/Shorts/TikTok) or
        ``"direct"``. ``youtube_shorts`` follows YouTube's ``via`` unless it sets its own."""
        if key not in ZERNIO_PLATFORMS:
            return "direct"
        value = self.get(f"publishing.platforms.{key}.via")
        if value is None and key in SHARED_CONNECTION:
            value = self.get(f"publishing.platforms.{SHARED_CONNECTION[key]}.via")
        return "direct" if str(value or "").strip().lower() == "direct" else "zernio"

    @property
    def studio_dir(self) -> Path | None:
        """The owner's old live studio folder (``C:\\CS2_STUDIO``), or None.

        Order: config ``studio.dir`` -> env ``AGENCY_STUDIO_DIR`` -> ``C:\\CS2_STUDIO`` when it
        exists (Windows). ``"off"`` in config/env disables the link. Read-only use only.
        """
        for raw in (self.get("studio.dir"), os.environ.get("AGENCY_STUDIO_DIR")):
            text = str(raw or "").strip()
            if text.lower() in ("off", "none", "false", "0"):
                return None
            if text:
                return Path(text).expanduser()
        if os.name == "nt":
            default = Path(DEFAULT_STUDIO_DIR)
            try:
                if default.is_dir():
                    return default
            except OSError:
                return None
        return None

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
                 "publishing.stale_publishing_minutes", "llm.cli_timeout_s",
                 "llm.cli_daily_calls")


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
    config_path = Path(path) if path is not None else root / "config.yaml"
    if config_path.name == "config.example.yaml":  # never write into the example file
        config_path = root / "config.yaml"
    return Settings(raw=_deep_merge(DEFAULTS, data), root=root, config_path=config_path)


# =========================================================================== secrets
# Resolution order (SPEC_V2 §11.1): os.environ (.env is loaded into it) -> OS keyring service
# ``cs2-agency`` -> READ-ONLY fallback to the old studio's keyring service ``cs2studio`` through
# STUDIO_ALIASES. We never write to ``cs2studio``. Values are never logged.
KEYRING_SERVICE = "cs2-agency"
STUDIO_KEYRING_SERVICE = "cs2studio"
STUDIO_ALIASES: dict[str, str] = {
    "ZERNIO_API_KEY": "zernio",
    "META_ACCESS_TOKEN": "meta_page_token",
    "TELEGRAM_BOT_TOKEN": "telegram_bot",
    "YOUTUBE_OAUTH_JSON": "youtube_oauth",
    # long-lived Claude Code login token (`claude setup-token`); only ever handed to the
    # child `claude` process's environment (agency.llm_cli)
    "CLAUDE_CODE_OAUTH_TOKEN": "claude_token",
}
# Entry names the studio keeps under ``cs2studio`` (names only; used by the importer).
STUDIO_KEYRING_NAMES = ("zernio", "meta_page_token", "youtube_oauth", "telegram_bot",
                        "claude_token", "hmac_key")

_KEYRING_CACHE_TTL_S = 60.0
_keyring_cache: dict[tuple[str, str], tuple[float, str | None]] = {}
_keyring_lock = threading.Lock()


def keyring_disabled() -> bool:
    """``AGENCY_KEYRING=off`` (tests, or an owner who wants .env only) skips the OS keyring."""
    return os.environ.get("AGENCY_KEYRING", "").strip().lower() in ("0", "off", "false", "no")


def _keyring_module():
    """The ``keyring`` module when installed AND backed by a real store, else None. Never raises."""
    if keyring_disabled():
        return None
    try:
        import keyring  # lazy: optional dependency
        backend = keyring.get_keyring()
        if float(getattr(backend, "priority", 0) or 0) <= 0:
            return None  # keyring.backends.fail.Keyring and friends: no usable store
        name = f"{type(backend).__module__}.{type(backend).__name__}"
        if name.startswith("keyring.backends.fail") or name.endswith("null.Keyring"):
            return None
        return keyring
    except Exception:  # missing package, broken backend, D-Bus trouble...
        return None


def keyring_usable() -> bool:
    """True when secrets can be stored in the OS keyring (Windows Credential Manager etc.)."""
    return _keyring_module() is not None


def clear_secret_cache() -> None:
    """Forget cached keyring lookups (after storing/deleting a secret, and in tests)."""
    with _keyring_lock:
        _keyring_cache.clear()


def _keyring_get(service: str, name: str) -> str | None:
    """One keyring lookup (cached briefly). Any failure means "not there"."""
    now = time.monotonic()
    with _keyring_lock:
        hit = _keyring_cache.get((service, name))
        if hit is not None and now - hit[0] < _KEYRING_CACHE_TTL_S:
            return hit[1]
    value: str | None = None
    kr = _keyring_module()
    if kr is not None:
        try:
            value = kr.get_password(service, name) or None
        except Exception:
            value = None
    with _keyring_lock:
        _keyring_cache[(service, name)] = (now, value)
    return value


def _resolve_secret(name: str) -> tuple[str | None, str | None]:
    """(value, source) where source is ``env`` | ``keyring`` | ``studio`` | None."""
    value = os.environ.get(name)
    if value:
        return value, "env"
    value = _keyring_get(KEYRING_SERVICE, name)
    if value:
        return value, "keyring"
    alias = STUDIO_ALIASES.get(name)
    if alias:
        value = _keyring_get(STUDIO_KEYRING_SERVICE, alias)
        if value:
            return value, "studio"
    return None, None


def secret(name: str) -> str | None:
    """A secret by its env name: environment, then our keyring, then the studio's (read-only).

    Returns None when empty or unavailable; never raises because of the keyring.
    """
    return _resolve_secret(name)[0]


def secret_source(name: str) -> str | None:
    """Where ``secret(name)`` comes from: ``'env'``, ``'keyring'``, ``'studio'`` or None.

    For the UI ("מחובר דרך הסטודיו הקיים"); never returns the value.
    """
    return _resolve_secret(name)[1]


def store_secret(name: str, value: str, root: Path | None = None) -> str:
    """Save a secret: our keyring service ``cs2-agency`` when usable, else ``<root>/.env``.

    Only allowed names (see :func:`agency.settings_store.allowed_env_names`); newlines/NUL are
    refused. When stored in the keyring, a stale copy in ``.env`` is blanked so the new value
    wins. Returns ``"keyring"`` or ``"env"``. Never writes to the studio's keyring service.
    """
    from . import settings_store  # lazy: settings_store imports this module

    root = Path(root) if root is not None else resolve_root()
    settings_store.check_env_names([name])
    clean = settings_store.check_env_value(name, value)
    kr = _keyring_module()
    stored = "env"
    if kr is not None:
        try:
            kr.set_password(KEYRING_SERVICE, name, clean)
            stored = "keyring"
        except Exception:
            stored = "env"
    if stored == "keyring":
        if os.environ.get(name) or settings_store.env_file_has(root, name):
            settings_store.env_unset(root, [name])
    else:
        settings_store.env_set(root, {name: clean})
    clear_secret_cache()
    return stored


def delete_secret(name: str, root: Path | None = None) -> list[str]:
    """Remove OUR copy of a secret (keyring ``cs2-agency`` + ``.env`` + this process).

    The studio's ``cs2studio`` entries are never touched. Returns where it was removed from
    (``keyring`` / ``env``). A value that comes from the computer's own environment (it is in
    ``os.environ`` but not in our ``.env``) is not ours to remove: it is left alone and
    reported as ``system_env`` (it would come back at the next start anyway).
    """
    from . import settings_store

    root = Path(root) if root is not None else resolve_root()
    settings_store.check_env_names([name])
    removed: list[str] = []
    kr = _keyring_module()
    if kr is not None:
        try:
            if kr.get_password(KEYRING_SERVICE, name):
                kr.delete_password(KEYRING_SERVICE, name)
                removed.append("keyring")
        except Exception:
            pass
    if settings_store.env_file_has(root, name):
        settings_store.env_unset(root, [name])
        removed.append("env")
    elif os.environ.get(name):
        removed.append("system_env")
    clear_secret_cache()
    return removed
