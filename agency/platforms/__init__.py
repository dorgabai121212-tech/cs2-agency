"""Platform adapters (YouTube, TikTok, Instagram, Facebook, X, Reddit, Discord)."""
from .base import MissingCredentials, PlatformAdapter
from .registry import ALL_KEYS, get_adapter

__all__ = ["ALL_KEYS", "MissingCredentials", "PlatformAdapter", "get_adapter"]
