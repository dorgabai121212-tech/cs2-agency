"""TikTok.

Publishing goes through **Zernio** by default (``publishing.platforms.tiktok.via: zernio``;
the registry then builds ``zernio.ZernioAdapter`` — the owner's TikTok is connected there).
This module is the *direct* TikTok Content Posting API v2 path (``via: direct``, Direct Post,
FILE_UPLOAD):

creator_info/query -> video/init (FILE_UPLOAD) -> PUT chunks to upload_url with
Content-Range -> poll status/fetch.

NOTE: apps that have not passed TikTok's audit can only post with privacy_level SELF_ONLY
(visible only to the account owner). The direct path defaults to SELF_ONLY and falls back to it
whenever the requested level is not offered by creator_info. After the audit set
``publishing.platforms.tiktok.privacy_level: PUBLIC_TO_EVERYONE`` in config.yaml.
TikTok's API has no scheduled publishing; timing is handled by the agency scheduler.
"""
from __future__ import annotations

import math
import time

from ..config import secret
from ..models import PostPlan, PostResult
from ._common import ApiError, RealAdapter, build_caption, check_response, video_file

API_BASE = "https://open.tiktokapis.com/v2"
CAPTION_LIMIT = 2200
MIN_CHUNK = 5 * 1024 * 1024
DEFAULT_CHUNK = 10 * 1024 * 1024
MAX_SINGLE_UPLOAD = 64 * 1024 * 1024
DONE_STATES = {"PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"}


def chunk_plan(size: int, chunk_size: int = DEFAULT_CHUNK) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) per TikTok rules.

    Files up to 64MB go as one chunk (files under 5MB must). Larger files use 5-64MB chunks;
    total = floor(size / chunk_size) and the last chunk absorbs the remainder.
    """
    if size <= 0:
        raise ValueError("empty video file")
    if size <= MAX_SINGLE_UPLOAD:
        return size, 1
    chunk_size = min(max(chunk_size, MIN_CHUNK), MAX_SINGLE_UPLOAD)
    return chunk_size, max(1, math.floor(size / chunk_size))


class TikTokAdapter(RealAdapter):
    key = "tiktok"
    display_he = "טיקטוק"
    required_secrets = ["TIKTOK_ACCESS_TOKEN"]

    poll_interval_s: float = 5.0
    poll_max_s: float = 300.0

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {secret('TIKTOK_ACCESS_TOKEN')}",
                "Content-Type": "application/json; charset=UTF-8"}

    def _post(self, path: str, body: dict, what: str) -> dict:
        import requests

        resp = requests.post(f"{API_BASE}{path}", json=body, headers=self._headers(),
                             timeout=self.timeout())
        data = check_response(resp, what)
        error = data.get("error") or {}
        if isinstance(error, dict) and error.get("code") not in (None, "", "ok"):
            raise ApiError(f"{what} failed: {error.get('code')} {error.get('message', '')}".strip())
        return data.get("data") or {}

    def _privacy_level(self, creator: dict) -> str:
        wanted = str(self.platform_config.get("privacy_level", "SELF_ONLY"))
        options = creator.get("privacy_level_options") or []
        if not options or wanted in options:
            return wanted
        return "SELF_ONLY" if "SELF_ONLY" in options else options[0]

    def _upload(self, plan: PostPlan) -> PostResult:
        import requests

        path = video_file(plan)
        size = path.stat().st_size
        chunk_size, total = chunk_plan(size)

        creator = self._post("/post/publish/creator_info/query/", {}, "TikTok creator_info")
        caption = build_caption(plan, CAPTION_LIMIT)
        init = self._post("/post/publish/video/init/", {
            "post_info": {
                "title": caption,
                "privacy_level": self._privacy_level(creator),
                "disable_duet": bool(creator.get("duet_disabled", False)),
                "disable_comment": bool(creator.get("comment_disabled", False)),
                "disable_stitch": bool(creator.get("stitch_disabled", False)),
                "video_cover_timestamp_ms": int(plan.extra.get("cover_ms", 1000)),
            },
            "source_info": {"source": "FILE_UPLOAD", "video_size": size,
                            "chunk_size": chunk_size, "total_chunk_count": total},
        }, "TikTok video init")
        publish_id, upload_url = init.get("publish_id"), init.get("upload_url")
        if not publish_id or not upload_url:
            raise ApiError("TikTok video init returned no publish_id/upload_url")

        with path.open("rb") as fh:
            for index in range(total):
                start = index * chunk_size
                end = size - 1 if index == total - 1 else start + chunk_size - 1
                fh.seek(start)
                chunk = fh.read(end - start + 1)
                resp = requests.put(upload_url, data=chunk, timeout=self.timeout(), headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end}/{size}",
                })
                if resp.status_code not in (200, 201, 206):
                    raise ApiError(f"TikTok chunk {index + 1}/{total} upload failed: "
                                   f"HTTP {resp.status_code}")

        return self._wait_for_publish(publish_id)

    def _wait_for_publish(self, publish_id: str) -> PostResult:
        """Poll status/fetch until TikTok confirms or rejects the post.

        If TikTok is still processing when the wait runs out, the result is NOT a success
        (iron rule 7): ok=False with the publish_id kept, so the owner can check the TikTok
        app before deciding to retry.
        """
        try:
            max_wait = float(self.platform_config.get("poll_max_s", self.poll_max_s))
        except (TypeError, ValueError):
            max_wait = self.poll_max_s
        deadline = time.monotonic() + max_wait
        while True:
            status = self._post("/post/publish/status/fetch/", {"publish_id": publish_id},
                                "TikTok status fetch")
            state = status.get("status", "")
            if state == "FAILED":
                return self.fail(f"TikTok publish failed: {status.get('fail_reason', 'unknown')}")
            if state in DONE_STATES:
                # (sic) TikTok's field name is "publicaly_available_post_id"
                post_ids = status.get("publicaly_available_post_id") or []
                if post_ids:
                    post_id = str(post_ids[0])
                    return self.success(post_id, f"https://www.tiktok.com/video/{post_id}")
                return self.success(publish_id, None)
            if time.monotonic() >= deadline:
                return PostResult(
                    platform=self.key, ok=False, dry_run=False, external_id=str(publish_id),
                    url=None, error=(f"TikTok still processing after {max_wait:.0f}s "
                                     f"(state {state or 'unknown'}, publish_id={publish_id}). "
                                     "Not confirmed as published - check the TikTok app "
                                     "before retrying, to avoid a duplicate post."))
            time.sleep(self.poll_interval_s)

    def fetch_stats(self, external_id: str) -> dict:
        """Needs the video.list scope and a public post id (not a publish id)."""
        if not self.can_call_api(external_id) or not str(external_id).isdigit():
            return {}
        try:
            import requests

            resp = requests.post(
                f"{API_BASE}/video/query/",
                params={"fields": "id,view_count,like_count,comment_count,share_count"},
                json={"filters": {"video_ids": [str(external_id)]}},
                headers=self._headers(), timeout=self.timeout())
            videos = (check_response(resp, "TikTok video query").get("data") or {}).get("videos")
            if not videos:
                return {}
            v = videos[0]
            return {"views": v.get("view_count", 0), "likes": v.get("like_count", 0),
                    "comments": v.get("comment_count", 0), "shares": v.get("share_count", 0)}
        except Exception:
            return {}
