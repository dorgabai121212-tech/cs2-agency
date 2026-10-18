"""Base class for all platform adapters. Enforces training mode (dry run) and credential checks."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from ..config import Settings, secret
from ..models import PostPlan, PostResult

log = logging.getLogger("agency.platforms")

HTTP_TIMEOUT = (10, 120)  # (connect, read) seconds — use on every requests call

# Non-secret values an adapter lists in ``required_secrets`` that may also live in
# config.yaml (the importer copies the Meta ids there from the old studio's DB).
CONFIG_FALLBACKS: dict[str, str] = {
    "IG_USER_ID": "meta.ig_user_id",
    "FB_PAGE_ID": "meta.page_id",
}
# Secret *file* names that are satisfied by a file in ``<data_dir>/secrets``.
SECRET_FILE_FALLBACKS: dict[str, str] = {
    "YOUTUBE_CLIENT_SECRETS_FILE": "youtube_client_secret.json",
}


class MissingCredentials(Exception):
    """Required secrets for a platform are not available (env / keyring / studio)."""


class PlatformAdapter:
    """One social platform. Subclasses implement ``_upload`` (real network upload)."""

    key: str = "base"
    display_he: str = "פלטפורמה"
    required_secrets: list[str] = []

    def __init__(self, settings: Settings):
        self.settings = settings

    # ------------------------------------------------------------------ config
    def credential(self, name: str) -> str | None:
        """Value for a required name: a secret (env → keyring → studio), else for ids the
        config fallback (``meta.page_id`` / ``meta.ig_user_id``), else for the YouTube client
        file ``<data_dir>/secrets/youtube_client_secret.json``. Never logged."""
        value = secret(name)
        if value:
            return value
        dotted = CONFIG_FALLBACKS.get(name)
        if dotted:
            text = str(self.settings.get(dotted, "") or "").strip()
            if text:
                return text
        filename = SECRET_FILE_FALLBACKS.get(name)
        if filename:
            path = Path(self.settings.data_dir) / "secrets" / filename
            if path.is_file():
                return str(path)
        return None

    def configured(self) -> bool:
        """True when every required secret is available."""
        return all(self.credential(name) for name in self.required_secrets)

    def missing_secrets(self) -> list[str]:
        """NAMES of required secrets that are missing (never values)."""
        return [name for name in self.required_secrets if not self.credential(name)]

    def require_credentials(self) -> None:
        """Raise MissingCredentials listing the missing secret NAMES (never values)."""
        missing = self.missing_secrets()
        if missing:
            raise MissingCredentials(f"{self.key}: missing secrets: {', '.join(missing)}")

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
        """Effective live state: switched live AND connected (credentials present).

        ``effective_live = configured() and (not settings.dry_run or <key>.live is true)``.
        """
        return (not self.dry_run) and self.configured()

    # ------------------------------------------------------------------ actions
    def dry_run_result(self, plan: PostPlan) -> PostResult:
        """Fake, clearly-marked result for training mode. No network."""
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
            log.warning("%s upload failed: %s", self.key, type(exc).__name__)
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
