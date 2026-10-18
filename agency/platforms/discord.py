"""Discord: post a message (and the clip, when small enough) through the owner's webhook.

Only the single webhook configured in ``.env`` (DISCORD_WEBHOOK_URL) is ever used, so posts go
only to the channel the owner chose. Mentions are disabled (no @everyone / @here pings).
"""
from __future__ import annotations

import json

from ..config import secret
from ..models import PostPlan, PostResult
from ._common import (ApiError, RealAdapter, check_response, normalize_hashtags, truncate,
                      video_file)

CONTENT_LIMIT = 2000           # Discord message content limit
DEFAULT_MAX_FILE_MB = 10       # current default upload cap for servers without boosts


class DiscordAdapter(RealAdapter):
    key = "discord"
    display_he = "דיסקורד"
    required_secrets = ["DISCORD_WEBHOOK_URL"]

    def _content(self, plan: PostPlan) -> str:
        link = plan.extra.get("link")
        links = plan.extra.get("links")
        if not link and isinstance(links, dict) and links:
            link = next(iter(links.values()))
        tags = " ".join(normalize_hashtags(plan.hashtags, 5))
        parts = [f"**{plan.title.strip()}**" if plan.title.strip() else "", plan.description.strip()]
        tail = "\n".join(p for p in (link or "", tags) if p)
        body = "\n\n".join(p for p in parts if p)
        room = CONTENT_LIMIT - len(tail) - 2 if tail else CONTENT_LIMIT
        return "\n\n".join(p for p in (truncate(body, max(room, 0)), tail) if p)[:CONTENT_LIMIT]

    def _upload(self, plan: PostPlan) -> PostResult:
        import requests

        url = secret("DISCORD_WEBHOOK_URL") or ""
        if not url.startswith("https://"):
            return self.fail("DISCORD_WEBHOOK_URL חייב להתחיל ב-https://")
        payload = {"content": self._content(plan), "allowed_mentions": {"parse": []}}
        max_mb = float(self.platform_config.get("max_file_mb", DEFAULT_MAX_FILE_MB))
        attach = None
        if plan.variant_path:
            try:
                path = video_file(plan)
                if path.stat().st_size < max_mb * 1024 * 1024:
                    attach = path
            except FileNotFoundError:
                attach = None  # message + link only
        if not payload["content"] and attach is None:
            return self.fail("אין תוכן לשליחה לדיסקורד")

        params = {"wait": "true"}
        try:  # the webhook URL IS the secret: never let a requests error text carry it
            if attach is not None:
                with attach.open("rb") as fh:
                    resp = requests.post(
                        url, params=params, timeout=self.timeout(),
                        data={"payload_json": json.dumps(payload, ensure_ascii=False)},
                        files={"files[0]": (attach.name, fh, "video/mp4")})
            else:
                resp = requests.post(url, params=params, json=payload, timeout=self.timeout())
        except requests.RequestException as exc:
            raise ApiError(f"Discord webhook: אין חיבור לדיסקורד ({type(exc).__name__})") \
                from None
        data = check_response(resp, "Discord webhook")
        message_id = data.get("id") or "sent"
        return self.success(str(message_id), None)
