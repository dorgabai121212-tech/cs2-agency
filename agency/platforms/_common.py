"""Helpers shared by the real platform adapters (caption limits, time parsing, safe errors)."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..config import secret
from ..models import PostPlan, PostResult
from .base import HTTP_TIMEOUT, PlatformAdapter

log = logging.getLogger("agency.platforms")

MISSING_CREDS_HE = "חסרים פרטי התחברות"

_TOKEN_PARAM_RE = re.compile(r"(access_token|token|key|secret|password)=([^&\s'\"]+)", re.I)
_DISCORD_WEBHOOK_RE = re.compile(r"(discord(?:app)?\.com/api/webhooks/\d+/)[\w-]+", re.I)


class ApiError(RuntimeError):
    """A platform API answered with an error. Message must never contain secrets."""


class RealAdapter(PlatformAdapter):
    """PlatformAdapter with Hebrew missing-credential errors and secret-scrubbed failures.

    Subclasses implement ``_upload``; ``upload`` keeps the base DRY_RUN guarantee (no network).
    """

    def upload(self, plan: PostPlan) -> PostResult:
        if self.settings.dry_run:
            return self.dry_run_result(plan)
        missing = self.missing_secrets()
        if missing:
            return self.fail(f"{MISSING_CREDS_HE}: {', '.join(missing)}")
        try:
            return self._upload(plan)
        except Exception as exc:  # fail closed and honest
            message = self.scrub(f"{type(exc).__name__}: {exc}")
            log.warning("%s upload failed: %s", self.key, message)
            return self.fail(message)

    # ---------------------------------------------------------------- helpers
    def fail(self, error: str) -> PostResult:
        return PostResult(platform=self.key, ok=False, dry_run=False, external_id=None,
                          url=None, error=error)

    def success(self, external_id: str, url: str | None) -> PostResult:
        return PostResult(platform=self.key, ok=True, dry_run=False,
                          external_id=str(external_id), url=url, error=None)

    def secret_values(self) -> list[str]:
        values = [secret(name) for name in self.required_secrets]
        return [v for v in values if v and len(v) >= 4]

    def scrub(self, text: str) -> str:
        """Remove secret values and token-looking query params from ``text``."""
        for value in sorted(self.secret_values(), key=len, reverse=True):
            text = text.replace(value, "***")
        text = _TOKEN_PARAM_RE.sub(lambda m: f"{m.group(1)}=***", text)
        text = _DISCORD_WEBHOOK_RE.sub(lambda m: f"{m.group(1)}***", text)
        return text[:1000]

    def can_call_api(self, external_id: str | None) -> bool:
        """Stats/comments only for real ids, outside dry run, with credentials."""
        return bool(external_id) and not str(external_id).startswith("dryrun-") \
            and not self.settings.dry_run and self.configured()

    def timeout(self) -> tuple[int, int]:
        return HTTP_TIMEOUT


# -------------------------------------------------------------------- text limits
def truncate(text: str, limit: int) -> str:
    """Cut ``text`` to ``limit`` characters, preferring a word boundary, with an ellipsis."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip() + "…"


def utf8_len(text: str) -> int:
    """Length of ``text`` in UTF-8 bytes (YouTube limits descriptions in bytes)."""
    return len((text or "").encode("utf-8"))


def truncate_utf8(text: str, max_bytes: int,
                  cut: Callable[[str, int], str] | None = None) -> str:
    """Like :func:`truncate` but the result is at most ``max_bytes`` UTF-8 bytes.

    Hebrew letters take 2 bytes each, so a character count is not enough. ``cut`` is the
    character-based truncation to reuse (default :func:`truncate`).
    """
    cut = cut or truncate
    text = (text or "").strip()
    if max_bytes <= 0:
        return ""
    if utf8_len(text) <= max_bytes:
        return text
    chars = min(len(text), max_bytes)
    while chars > 0:
        out = cut(text, chars)
        excess = utf8_len(out) - max_bytes
        if excess <= 0:
            return out
        chars -= max(1, (excess + 3) // 4)  # each char is 1-4 bytes: never overshoot much
    return ""


def normalize_hashtags(tags: list[str], max_count: int | None = None) -> list[str]:
    """'#tag' form, no spaces, de-duplicated (case-insensitive), optionally capped."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags or []:
        tag = re.sub(r"\s+", "", str(raw)).lstrip("#")
        if not tag or tag.lower() in seen:
            continue
        seen.add(tag.lower())
        out.append(f"#{tag}")
        if max_count is not None and len(out) >= max_count:
            break
    return out


def limit_hashtags_in_text(text: str, max_count: int) -> str:
    """Drop hashtags beyond ``max_count`` from free text (Instagram allows 30)."""
    count = 0

    def _keep(match: re.Match) -> str:
        nonlocal count
        count += 1
        return match.group(0) if count <= max_count else ""

    return re.sub(r"#\w+", _keep, text).strip()


def build_caption(plan: PostPlan, limit: int, max_hashtags: int | None = None,
                  include_title: bool = True) -> str:
    """Title + description + hashtags, trimmed to ``limit`` keeping hashtags if possible."""
    tags = " ".join(normalize_hashtags(plan.hashtags, max_hashtags))
    parts = [p for p in ((plan.title if include_title else ""), plan.description) if p]
    body = "\n\n".join(dict.fromkeys(p.strip() for p in parts if p.strip()))
    if max_hashtags is not None:
        body = limit_hashtags_in_text(body, max(0, max_hashtags - len(tags.split())))
    if not tags:
        return truncate(body, limit)
    room = limit - len(tags) - 2
    if room < 20:
        return truncate(f"{body}\n\n{tags}".strip(), limit)
    return f"{truncate(body, room)}\n\n{tags}".strip()


# -------------------------------------------------------------------- misc
def parse_when(value: str | None, tz_name: str = "UTC") -> datetime | None:
    """Parse an ISO time to an aware UTC datetime; naive times use ``tz_name``."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        try:
            from zoneinfo import ZoneInfo
            dt = dt.replace(tzinfo=ZoneInfo(tz_name))
        except Exception:
            dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def video_file(plan: PostPlan) -> Path:
    """The variant file, or FileNotFoundError (raised before any network call)."""
    path = Path(plan.variant_path)
    if not path.is_file():
        raise FileNotFoundError(f"קובץ הווידאו לא נמצא: {path}")
    return path


def check_response(resp, what: str) -> dict:
    """Return JSON of a requests response or raise ApiError without echoing the URL."""
    try:
        data = resp.json() if resp.content else {}
    except ValueError:
        data = {}
    if resp.status_code >= 400:
        detail = ""
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                detail = err.get("message") or err.get("code") or ""
            elif err:
                detail = str(err)
            detail = detail or data.get("message") or data.get("detail") or ""
        raise ApiError(f"{what} failed: HTTP {resp.status_code} {detail}".strip())
    return data if isinstance(data, dict) else {"data": data}
