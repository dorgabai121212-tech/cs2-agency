"""Safe writers for config.yaml (non-secret settings) and .env (secrets), plus the secrets dir.

- :func:`update_config` deep-merges a patch into config.yaml after checking every key against
  :data:`SCHEMA` (unknown keys and anything that looks like a secret are refused), writes the
  file atomically and keeps the previous version as ``config.yaml.bak``.
- :func:`env_set` / :func:`env_unset` edit ``.env`` atomically, keep comments and line order,
  quote values safely and update ``os.environ`` of this process. Only the secret names the
  platform adapters declare (plus ``ANTHROPIC_API_KEY``) are accepted.

Values are never logged or put in exception messages (only names are).
"""
from __future__ import annotations

import copy
import math
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from .config import PACKAGE_ROOT, DEFAULTS, ConfigError, Settings, _deep_merge, validate_settings

_config_lock = threading.Lock()
_env_lock = threading.Lock()

EXTRA_ENV_NAMES = ("ANTHROPIC_API_KEY",)
MAX_ENV_VALUE_LEN = 8192
MAX_TEXT_LEN = 500

# Kinds: "bool" | "int" | "number" (>= 0) | "str" | "str_list" | ("enum", {...}) | "hhmm"
PLATFORM_FIELDS: dict[str, Any] = {
    "enabled": "bool",
    "live": "bool",
    "subreddits": "str_list",
    "privacy": ("enum", {"public", "unlisted", "private"}),
    "privacy_level": ("enum", {"SELF_ONLY", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR",
                               "PUBLIC_TO_EVERYONE"}),
    "share_to_feed": "bool",
    "max_file_mb": "number",
    "poll_max_s": "number",
}

SCHEMA: dict[str, Any] = {
    "channel.name": "str",
    "channel.handle": "str",
    "channel.language": "str",
    "channel.secondary_language": "str",
    "channel.niche": "str",
    "dry_run": "bool",
    "data_dir": "str",
    "llm.model": "str",
    "llm.effort": ("enum", {"low", "medium", "high", "xhigh", "max"}),
    "llm.max_tokens": "int",
    "llm.fallbacks": "bool",
    "publishing.mode": ("enum", {"approve", "auto"}),
    "publishing.timezone": "str",
    "publishing.stale_publishing_minutes": "number",
    "publishing.quiet_hours.enabled": "bool",
    "publishing.quiet_hours.start": "hhmm",
    "publishing.quiet_hours.end": "hhmm",
    "finance.currency": "str",
    "finance.monthly_budget_ils": "number",
    "finance.max_single_spend_ils": "number",
    "staff.enabled": "str_list",
    "community.auto_reply": "bool",
    "promotion.discord_webhook_enabled": "bool",
    "dashboard.host": "str",
    "dashboard.port": "int",
    "dashboard.max_upload_gb": "number",
    "dashboard.remote_access": "bool",
    "importer.extra_roots": "str_list",
}

# Key names that must never be stored in config.yaml (secrets belong in .env).
_SECRET_KEY_RE = re.compile(
    r"token|secret|password|passwd|api_?key|webhook|credential|(?:^|_)pin(?:_|$)", re.I)
_SECRET_VALUE_RES = (
    re.compile(r"discord(?:app)?\.com/api/webhooks/", re.I),
    re.compile(r"\bsk-ant-[\w-]{8,}"),
    re.compile(r"\b1//[\w-]{20,}"),  # Google refresh token
    re.compile(r"\bya29\.[\w-]{20,}"),  # Google access token
)
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


class SettingsError(ConfigError):
    """A settings/.env change was refused. The message is owner-facing Hebrew (names only)."""


# --------------------------------------------------------------------------- helpers
def _atomic_write(path: Path, text: str, *, private: bool = False) -> None:
    """Write ``text`` to ``path`` via a temp file in the same folder + ``os.replace``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if private:
            _chmod_private(Path(tmp))
        elif path.exists():
            try:
                shutil.copymode(path, tmp)
            except OSError:
                pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _chmod_private(path: Path) -> None:
    """Owner-only permissions where the OS supports it (no-op on Windows)."""
    if os.name == "nt":
        return
    try:
        os.chmod(path, 0o600 if Path(path).is_file() else 0o700)
    except OSError:
        pass


def write_private_file(path: Path, text: str) -> None:
    """Atomically write a secret file (token / client JSON) with owner-only permissions."""
    _atomic_write(Path(path), text, private=True)


def secrets_dir(settings: Settings) -> Path:
    """``<data_dir>/secrets`` (created, owner-only where possible). Never committed."""
    path = settings.data_dir / "secrets"
    path.mkdir(parents=True, exist_ok=True)
    _chmod_private(path)
    return path


def allowed_env_names() -> list[str]:
    """Secret names we may write: every adapter's ``required_secrets`` + ANTHROPIC_API_KEY."""
    from .platforms import registry

    names: list[str] = list(EXTRA_ENV_NAMES)
    for key in registry.ALL_KEYS:
        try:
            cls = registry.adapter_class(key)
        except Exception:  # a broken adapter module must not break the settings page
            continue
        for name in getattr(cls, "required_secrets", []) or []:
            if name not in names:
                names.append(name)
    return names


# --------------------------------------------------------------------------- config.yaml
def _flatten(patch: Mapping, prefix: str = "") -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for key, value in patch.items():
        if not isinstance(key, str) or not key or "." in key:
            raise SettingsError(f"שם הגדרה לא תקין: {key!r}")
        dotted = f"{prefix}{key}"
        if isinstance(value, Mapping) and value:
            out.extend(_flatten(value, dotted + "."))
        else:
            out.append((dotted, value))
    return out


def _schema_kind(dotted: str) -> Any:
    from .platforms.registry import ALL_KEYS

    if dotted in SCHEMA:
        return SCHEMA[dotted]
    parts = dotted.split(".")
    if len(parts) == 4 and parts[:2] == ["publishing", "platforms"] and parts[2] in ALL_KEYS:
        return PLATFORM_FIELDS.get(parts[3])
    return None


def _looks_secret(value: Any) -> bool:
    texts = value if isinstance(value, list) else [value]
    known = [v for n in allowed_env_names() if (v := os.environ.get(n)) and len(v) >= 8]
    for text in texts:
        if not isinstance(text, str):
            continue
        if any(rx.search(text) for rx in _SECRET_VALUE_RES):
            return True
        if any(v in text for v in known):
            return True
    return False


def _check_value(dotted: str, kind: Any, value: Any) -> Any:
    """Return the normalised value or raise SettingsError (Hebrew, never echoes secrets)."""
    bad = f"הערך של {dotted} לא תקין"
    if kind == "bool":
        if not isinstance(value, bool):
            raise SettingsError(f"{bad}: צריך להיות כן/לא")
        return value
    if kind in ("int", "number"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingsError(f"{bad}: צריך להיות מספר")
        if not math.isfinite(float(value)) or value < 0:
            raise SettingsError(f"{bad}: צריך להיות מספר חיובי")
        if kind == "int":
            if float(value) != int(value):
                raise SettingsError(f"{bad}: צריך להיות מספר שלם")
            return int(value)
        return int(value) if float(value).is_integer() else float(value)
    if kind == "str" or kind == "hhmm" or isinstance(kind, tuple):
        if not isinstance(value, str):
            raise SettingsError(f"{bad}: צריך להיות טקסט")
        value = "".join(ch for ch in value if ch == " " or ch.isprintable()).strip()
        if len(value) > MAX_TEXT_LEN:
            raise SettingsError(f"{bad}: ארוך מדי")
        if kind == "hhmm" and value and not _HHMM_RE.match(value):
            raise SettingsError(f"{bad}: צריך להיות שעה בפורמט 18:30")
        if isinstance(kind, tuple) and value not in kind[1]:
            raise SettingsError(f"{bad}: אפשר רק {', '.join(sorted(kind[1]))}")
        return value
    if kind == "str_list":
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise SettingsError(f"{bad}: צריך להיות רשימה של טקסטים")
        if len(value) > 100:
            raise SettingsError(f"{bad}: רשימה ארוכה מדי")
        return [v.strip()[:MAX_TEXT_LEN] for v in value if v.strip()]
    raise SettingsError(f"{bad}")


def validate_patch(patch: Mapping) -> dict:
    """Check a settings patch; return a normalised copy. Raises :class:`SettingsError`."""
    if not isinstance(patch, Mapping):
        raise SettingsError("ההגדרות חייבות להיות רשימת שדות")
    clean: dict = {}
    for dotted, value in _flatten(patch):
        if any(_SECRET_KEY_RE.search(part) for part in dotted.split(".")) \
                or dotted.split(".")[-1] in allowed_env_names():
            raise SettingsError(f"{dotted}: מפתחות וסיסמאות לא נשמרים בהגדרות — רק בחיבורים")
        kind = _schema_kind(dotted)
        if kind is None:
            raise SettingsError(f"הגדרה לא מוכרת: {dotted}")
        if _looks_secret(value):
            raise SettingsError(f"{dotted}: זה נראה כמו מפתח סודי — הוא לא נשמר בהגדרות")
        node = clean
        *parents, last = dotted.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[last] = _check_value(dotted, kind, value)
    return clean


def config_path(settings: Settings) -> Path:
    """The owner's config.yaml for these settings (never config.example.yaml)."""
    path = getattr(settings, "config_path", None)
    return Path(path) if path else settings.root / "config.yaml"


def _read_config_file(path: Path, root: Path) -> dict:
    """Current config.yaml content, or the example's content when there is none yet."""
    for candidate in (path, root / "config.example.yaml", PACKAGE_ROOT / "config.example.yaml"):
        if candidate.exists():
            loaded = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise SettingsError("config.yaml לא תקין (חייב להיות רשימת הגדרות)")
            return loaded
    return {}


_CONFIG_HEADER = ("# קובץ ההגדרות של סוכנות CS2 — נשמר מלוח הבקרה.\n"
                  "# הגרסה הקודמת נשמרה ב-config.yaml.bak. אין לשים כאן מפתחות או סיסמאות.\n")


def update_config(settings: Settings, patch: Mapping) -> Settings:
    """Deep-merge ``patch`` into config.yaml (validated, atomic, ``.bak`` kept).

    The same ``settings`` object is updated in place (so every holder of the shared Context
    sees the change) and returned.
    """
    clean = validate_patch(patch)
    path = config_path(settings)
    with _config_lock:
        current = _read_config_file(path, settings.root)
        merged_file = _deep_merge(current, clean)
        candidate = Settings(raw=_deep_merge(DEFAULTS, merged_file), root=settings.root,
                             config_path=path)
        validate_settings(candidate)
        text = _CONFIG_HEADER + yaml.safe_dump(merged_file, allow_unicode=True,
                                               sort_keys=False, default_flow_style=False)
        if path.exists():
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        _atomic_write(path, text)
        settings.raw.clear()
        settings.raw.update(copy.deepcopy(candidate.raw))
        settings.config_path = path
    return settings


# --------------------------------------------------------------------------- .env
def env_path(root: Path) -> Path:
    return Path(root) / ".env"


def _quote_env(value: str) -> str:
    """Single-quoted .env value (python-dotenv decodes only \\\\ and \\' inside '...')."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _check_env_names(names: Iterable[str]) -> list[str]:
    allowed = set(allowed_env_names())
    names = list(names)
    unknown = [n for n in names if n not in allowed]
    if unknown:
        raise SettingsError(f"שם מפתח לא מוכר: {', '.join(map(str, unknown))}")
    return names


def _check_env_value(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise SettingsError(f"הערך של {name} חייב להיות טקסט")
    value = value.strip()
    if not value:
        raise SettingsError(f"הערך של {name} ריק")
    if any(ch in value for ch in "\r\n\0"):
        raise SettingsError(f"הערך של {name} מכיל ירידת שורה — העתק רק את המפתח עצמו")
    if "${" in value:
        raise SettingsError(f"הערך של {name} מכיל את הרצף ${{ שלא נתמך")
    if len(value) > MAX_ENV_VALUE_LEN:
        raise SettingsError(f"הערך של {name} ארוך מדי")
    return value


def _env_lines(path: Path) -> list[str]:
    if path.exists():
        return path.read_text(encoding="utf-8-sig").splitlines()
    for example in (path.with_name(".env.example"), PACKAGE_ROOT / ".env.example"):
        if example.exists():  # start from the commented template
            return example.read_text(encoding="utf-8-sig").splitlines()
    return []


def _rewrite_env(path: Path, updates: Mapping[str, str | None]) -> None:
    """Replace/blank/append ``updates`` (None = blank) keeping every other line as is."""
    lines = _env_lines(path)
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        match = _ENV_LINE_RE.match(line)
        name = match.group(1) if match else None
        if name in updates:
            if name in seen:
                continue  # drop duplicates so the value we wrote is the one that is used
            seen.add(name)
            value = updates[name]
            out.append(f"{name}={_quote_env(value)}" if value is not None else f"{name}=")
        else:
            out.append(line)
    for name, value in updates.items():
        if name not in seen and value is not None:
            out.append(f"{name}={_quote_env(value)}")
    _atomic_write(path, "\n".join(out).rstrip("\n") + "\n", private=True)


def env_set(root: Path, mapping: Mapping[str, str]) -> None:
    """Save secrets to ``<root>/.env`` (atomic, comments kept) and to ``os.environ``."""
    names = _check_env_names(mapping.keys())
    values = {name: _check_env_value(name, mapping[name]) for name in names}
    if not values:
        return
    with _env_lock:
        _rewrite_env(env_path(root), values)
        os.environ.update(values)


def env_unset(root: Path, names: Iterable[str]) -> None:
    """Blank the given secrets in ``<root>/.env`` (the template line stays) and in os.environ."""
    names = _check_env_names(names)
    if not names:
        return
    with _env_lock:
        path = env_path(root)
        if path.exists():
            _rewrite_env(path, {name: None for name in names})
        for name in names:
            os.environ.pop(name, None)


def env_has(name: str) -> bool:
    """True when the secret ``name`` has a non-empty value in this process."""
    return bool(os.environ.get(name))
