"""Base class for all platform adapters. Enforces DRY_RUN and credential checks."""
from __future__ import annotations

import hashlib
import logging

from ..config import Settings, secret
from ..models import PostPlan, PostResult

log = logging.getLogger("agency.platforms")

HTTP_TIMEOUT = (10, 120)  # (connect, read) seconds — use on every requests call


class MissingCredentials(Exception):
    """Required secrets for a platform are not set in .env."""


class PlatformAdapter:
    """One social platform. Subclasses implement ``_upload`` (real network upload)."""

    key: str = "base"
    display_he: str = "פלטפורמה"
    required_secrets: list[str] = []

    def __init__(self, settings: Settings):
        self.settings = settings

    # ------------------------------------------------------------------ config
    def configured(self) -> bool:
        """True when every required secret is present in the environment."""
        return all(secret(name) for name in self.required_secrets)

    def missing_secrets(self) -> list[str]:
        return [name for name in self.required_secrets if not secret(name)]

    def require_credentials(self) -> None:
        """Raise MissingCredentials listing the missing secret NAMES (never values)."""
        missing = self.missing_secrets()
        if missing:
            raise MissingCredentials(f"{self.key}: missing secrets in .env: {', '.join(missing)}")

    @property
    def platform_config(self) -> dict:
        return self.settings.get(f"publishing.platforms.{self.key}", {}) or {}

    @property
    def enabled(self) -> bool:
        return bool(self.platform_config.get("enabled", False))

    # ------------------------------------------------------------------ live / training
    @property
    def dry_run(self) -> bool:
        """Training mode for THIS platform: no network unless the owner switched it live.

        Global ``dry_run: false`` makes every platform live; otherwise only platforms with
        ``publishing.platforms.<key>.live: true`` (see :meth:`Settings.platform_live`).
        """
        return not self.settings.platform_live(self.key)

    def is_live(self) -> bool:
        """Effective live state: switched live AND connected (credentials present)."""
        return (not self.dry_run) and self.configured()

    # ------------------------------------------------------------------ actions
    def dry_run_result(self, plan: PostPlan) -> PostResult:
        """Fake, clearly-marked result for DRY_RUN mode. No network."""
        digest = hashlib.sha1(f"{self.key}|{plan.variant_path}|{plan.title}".encode()).hexdigest()
        fake_id = f"dryrun-{self.key}-{digest[:10]}"
        log.info("[DRY RUN] %s would upload %s titled %r (scheduled_at=%s)", self.key,
                 plan.variant_path, plan.title, plan.scheduled_at)
        return PostResult(platform=self.key, ok=True, dry_run=True, external_id=fake_id,
                          url=None, error=None)

    def upload(self, plan: PostPlan) -> PostResult:
        """Upload per ``plan``. Training mode → fake result without network. Errors → ok=False."""
        if self.dry_run:
            return self.dry_run_result(plan)
        try:
            self.require_credentials()
            return self._upload(plan)
        except Exception as exc:  # fail closed and honest
            log.warning("%s upload failed: %s", self.key, exc)
            return PostResult(platform=self.key, ok=False, dry_run=False, external_id=None,
                              url=None, error=f"{type(exc).__name__}: {exc}")

    def _upload(self, plan: PostPlan) -> PostResult:
        """Real upload; subclasses override. Called only when live and configured."""
        raise NotImplementedError(f"{self.key}: real upload not implemented")

    def fetch_stats(self, external_id: str) -> dict:
        """{views, likes, comments, shares} best-effort; {} if unsupported."""
        return {}

    def fetch_comments(self, external_id: str, limit: int = 20) -> list[dict]:
        return []

    def reply_comment(self, comment_id: str, text: str) -> bool:
        return False
