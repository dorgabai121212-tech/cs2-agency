"""Maps platform keys to adapter classes. Adapter modules are imported lazily."""
from __future__ import annotations

import importlib

from ..config import Settings
from .base import PlatformAdapter

ALL_KEYS: list[str] = ["youtube", "youtube_shorts", "tiktok", "instagram_reels", "facebook",
                       "x", "reddit", "discord"]

# key -> "module:ClassName" inside agency.platforms
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


def adapter_class(key: str) -> type[PlatformAdapter]:
    """Import and return the adapter class for ``key`` (KeyError if unknown)."""
    if key not in ADAPTERS:
        raise KeyError(f"unknown platform {key!r}; expected one of {ALL_KEYS}")
    module_name, class_name = ADAPTERS[key].split(":", 1)
    module = importlib.import_module(f"{__package__}.{module_name}")
    return getattr(module, class_name)


def get_adapter(key: str, settings: Settings) -> PlatformAdapter:
    """Instantiate the adapter for ``key``."""
    return adapter_class(key)(settings)


def enabled_keys(settings: Settings) -> list[str]:
    """Platform keys enabled in config.yaml, in canonical order."""
    platforms = settings.get("publishing.platforms", {}) or {}
    return [k for k in ALL_KEYS if (platforms.get(k) or {}).get("enabled")]
