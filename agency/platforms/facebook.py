"""Facebook Page videos via the Graph API ``/{page-id}/videos``.

Files up to 1GB use a single multipart upload to graph-video.facebook.com; larger files use the
chunked ``upload_phase`` start/transfer/finish flow. META_ACCESS_TOKEN must be a *Page* access
token with pages_manage_posts + pages_read_engagement. A future ``scheduled_at`` (10 minutes to
~6 months ahead) is passed as ``scheduled_publish_time`` with ``published=false``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import secret
from ..models import PostPlan, PostResult
from ._common import (ApiError, RealAdapter, check_response, normalize_hashtags, parse_when,
                      truncate, video_file)
from .instagram import GRAPH_API_VERSION

GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
VIDEO_BASE = f"https://graph-video.facebook.com/{GRAPH_API_VERSION}"
TITLE_LIMIT = 255
DESCRIPTION_LIMIT = 5000
SIMPLE_UPLOAD_MAX = 1024 * 1024 * 1024


class FacebookAdapter(RealAdapter):
    key = "facebook"
    display_he = "פייסבוק"
    required_secrets = ["FB_PAGE_ID", "META_ACCESS_TOKEN"]

    def _auth(self) -> dict:
        return {"Authorization": f"OAuth {secret('META_ACCESS_TOKEN')}"}

    def _request(self, method: str, url: str, what: str, **kwargs) -> dict:
        import requests

        resp = requests.request(method, url, headers=self._auth(), timeout=self.timeout(), **kwargs)
        return check_response(resp, what)

    def _fields(self, plan: PostPlan) -> dict:
        tags = " ".join(normalize_hashtags(plan.hashtags, 10))
        description = plan.description.strip()
        if tags:
            description = f"{truncate(description, DESCRIPTION_LIMIT - len(tags) - 2)}\n\n{tags}"
        fields = {"title": truncate(plan.title, TITLE_LIMIT),
                  "description": truncate(description.strip(), DESCRIPTION_LIMIT)}
        when = parse_when(plan.scheduled_at, self.settings.get("publishing.timezone", "UTC"))
        if when and when > datetime.now(timezone.utc) + timedelta(minutes=10):
            fields.update(published="false", scheduled_publish_time=str(int(when.timestamp())))
        return fields

    def _upload(self, plan: PostPlan) -> PostResult:
        path = video_file(plan)
        page_id = secret("FB_PAGE_ID")
        fields = self._fields(plan)
        if path.stat().st_size <= SIMPLE_UPLOAD_MAX:
            with path.open("rb") as fh:
                data = self._request("POST", f"{VIDEO_BASE}/{page_id}/videos", "Facebook upload",
                                     data=fields, files={"source": (path.name, fh, "video/mp4")})
            video_id = data.get("id")
        else:
            video_id = self._chunked(page_id, path, fields)
        if not video_id:
            raise ApiError("Facebook upload returned no video id")
        return self.success(str(video_id), f"https://www.facebook.com/watch/?v={video_id}")

    def _chunked(self, page_id: str, path: Path, fields: dict) -> str:
        url = f"{VIDEO_BASE}/{page_id}/videos"
        size = path.stat().st_size
        start = self._request("POST", url, "Facebook upload start",
                              data={"upload_phase": "start", "file_size": str(size)})
        session_id, video_id = start.get("upload_session_id"), start.get("video_id")
        offset, end = int(start.get("start_offset", 0)), int(start.get("end_offset", 0))
        with path.open("rb") as fh:
            while offset < end:
                fh.seek(offset)
                chunk = fh.read(end - offset)
                part = self._request("POST", url, "Facebook upload transfer", data={
                    "upload_phase": "transfer", "upload_session_id": session_id,
                    "start_offset": str(offset)},
                    files={"video_file_chunk": (path.name, chunk, "application/octet-stream")})
                offset, end = int(part.get("start_offset", end)), int(part.get("end_offset", end))
        done = self._request("POST", url, "Facebook upload finish", data={
            "upload_phase": "finish", "upload_session_id": session_id, **fields})
        if done.get("success") is False:
            raise ApiError("Facebook upload finish was not successful")
        return str(video_id)

    # --------------------------------------------------------------- stats / comments
    def fetch_stats(self, external_id: str) -> dict:
        if not self.can_call_api(external_id):
            return {}
        stats: dict = {}
        try:
            info = self._request("GET", f"{GRAPH_BASE}/{external_id}", "Facebook video", params={
                "fields": "likes.summary(true).limit(0),comments.summary(true).limit(0)"})
            stats["likes"] = ((info.get("likes") or {}).get("summary") or {}).get("total_count", 0)
            stats["comments"] = ((info.get("comments") or {}).get("summary") or {}).get("total_count", 0)
            insights = self._request("GET", f"{GRAPH_BASE}/{external_id}/video_insights",
                                     "Facebook insights", params={"metric": "total_video_views"})
            for item in insights.get("data", []):
                stats["views"] = (item.get("values") or [{}])[0].get("value", 0)
        except Exception:
            pass
        return stats

    def fetch_comments(self, external_id: str, limit: int = 20) -> list[dict]:
        if not self.can_call_api(external_id):
            return []
        try:
            data = self._request("GET", f"{GRAPH_BASE}/{external_id}/comments", "Facebook comments",
                                 params={"fields": "id,message,from,created_time",
                                         "limit": min(limit, 100), "order": "reverse_chronological"})
        except Exception:
            return []
        return [{"id": c.get("id"), "author": (c.get("from") or {}).get("name", ""),
                 "text": c.get("message", ""), "published_at": c.get("created_time")}
                for c in data.get("data", [])][:limit]

    def reply_comment(self, comment_id: str, text: str) -> bool:
        if self.dry_run or not self.configured() or not comment_id:
            return False
        try:
            self._request("POST", f"{GRAPH_BASE}/{comment_id}/comments", "Facebook reply",
                          data={"message": text[:8000]})
            return True
        except Exception:
            return False
