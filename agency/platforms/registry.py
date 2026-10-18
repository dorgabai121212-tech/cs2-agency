"""Maps platform keys to adapter classes. Adapter modules are imported lazily.

Routing (SPEC_V2 §11.2): ``youtube``, ``youtube_shorts`` and ``tiktok`` publish through Zernio
by default (``publishing.platforms.<key>.via: zernio``); ``via: direct`` uses the platform's
own API adapter (direct YouTube uploads from an unverified Google project end up private).
"""
from __future__ import annotations

import importlib

from ..config import ZERNIO_PLATFORMS, Settings
from ..models import PostPlan, PostResult
from .base import PlatformAdapter

ALL_KEYS: list[str] = ["youtube", "youtube_shorts", "tiktok", "instagram_reels", "facebook",
                       "x", "reddit", "discord"]

# key -> "module:ClassName" inside agency.platforms (the DIRECT adapters)
ADAPTERS: dict[str, str] = {
    "youtube": "youtube:YouTubeAdapter",
    "youtube_shorts": "youtube:YouTubeShortsAdapter",
    "tiktok": "tiktok:TikTokAdapter",
    "instagram_reels": "instagram:InstagramReelsAdapter",
    "facebook": "facebook:FacebookAdapter",
    "x": "x_twitter:XAdapter",
    "reddit": "reddit:RedditAdapter",
    "discord": "discord:DiscordAdapter",
}

ZERNIO_ADAPTER = "zernio:ZernioAdapter"  # class ZernioAdapter(settings, target_platform)
ZERNIO_SECRETS = ["ZERNIO_API_KEY"]


def _load(spec: str) -> type:
    module_name, class_name = spec.split(":", 1)
    module = importlib.import_module(f"{__package__}.{module_name}")
    return getattr(module, class_name)


def adapter_class(key: str) -> type[PlatformAdapter]:
    """Import and return the DIRECT adapter class for ``key`` (KeyError if unknown)."""
    if key not in ADAPTERS:
        raise KeyError(f"unknown platform {key!r}; expected one of {ALL_KEYS}")
    return _load(ADAPTERS[key])


def zernio_class() -> type[PlatformAdapter]:
    """Import and return the Zernio adapter class (ImportError if the module is missing)."""
    return _load(ZERNIO_ADAPTER)


def route(key: str, settings: Settings) -> str:
    """``"zernio"`` or ``"direct"`` — how ``key`` uploads with these settings."""
    if key not in ADAPTERS:
        raise KeyError(f"unknown platform {key!r}; expected one of {ALL_KEYS}")
    return settings.platform_via(key) if key in ZERNIO_PLATFORMS else "direct"


class UnavailableAdapter(PlatformAdapter):
    """Stand-in when the Zernio module cannot be imported: training mode still works (no
    network); a live upload fails honestly instead of silently going direct."""

    required_secrets = list(ZERNIO_SECRETS)

    def __init__(self, settings: Settings, target_platform: str, reason: str = ""):
        super().__init__(settings)
        self.key = target_platform
        self.display_he = target_platform
        self.reason = reason

    def configured(self) -> bool:
        return False

    def upload(self, plan: PostPlan) -> PostResult:
        if self.dry_run:
            return self.dry_run_result(plan)
        return PostResult(platform=self.key, ok=False, dry_run=False, external_id=None,
                          url=None, error="רכיב ההעלאה דרך Zernio חסר בהתקנה — עדכן את הסוכנות")


def get_adapter(key: str, settings: Settings) -> PlatformAdapter:
    """Instantiate the adapter that publishes ``key`` (Zernio route or direct)."""
    if route(key, settings) == "zernio":
        try:
            cls = zernio_class()
        except (ImportError, AttributeError) as exc:
            return UnavailableAdapter(settings, key, type(exc).__name__)
        return cls(settings, key)
    return adapter_class(key)(settings)


def enabled_keys(settings: Settings) -> list[str]:
    """Platform keys enabled in config.yaml, in canonical order."""
    platforms = settings.get("publishing.platforms", {}) or {}
    return [k for k in ALL_KEYS if (platforms.get(k) or {}).get("enabled")]
