"""Facebook Page videos and Reels via the Graph API v24.0 — the studio's proven flows.

- **Reels** (vertical clips up to 90 s, the owner's Page "posts Reels"):
  POST /{page-id}/video_reels upload_phase=start -> POST the file to the returned
  rupload.facebook.com URL -> POST /{page-id}/video_reels upload_phase=finish with
  video_state=PUBLISHED (or SCHEDULED + scheduled_publish_time) -> poll the video status.
- **Videos** (16:9 / long): ``/{page-id}/videos`` on graph-video.facebook.com; files up to 1GB in
  one multipart upload, larger ones through the chunked start/transfer/finish flow.

``publishing.platforms.facebook.format``: ``auto`` (default: vertical ≤90 s -> Reel) | ``reels``
| ``video``. META_ACCESS_TOKEN must be a *Page* token with pages_manage_posts +
pages_read_engagement; it travels only in the Authorization header. The Page id comes from
FB_PAGE_ID, else config ``meta.page_id`` (imported from the old studio), else
``publishing.platforms.facebook.page_id`` (ids are not secrets).
A future ``scheduled_at`` (≥10 minutes ahead) is scheduled by Facebook itself.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import secret
from ..models import PostPlan, PostResult
from ._common import (ApiError, RealAdapter, check_response, normalize_hashtags, parse_when,
                      truncate, video_file)
from .instagram import GRAPH_API_VERSION, PENDING_KV, PENDING_TTL_S, record_published_at

log = logging.getLogger("agency.platforms")

GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
VIDEO_BASE = f"https://graph-video.facebook.com/{GRAPH_API_VERSION}"
RUPLOAD_BASE = f"https://rupload.facebook.com/video-upload/{GRAPH_API_VERSION}"
TITLE_LIMIT = 255
DESCRIPTION_LIMIT = 5000
SIMPLE_UPLOAD_MAX = 1024 * 1024 * 1024
REELS_MAX_S = 90.0
MIN_SCHEDULE_AHEAD = timedelta(minutes=11)  # Meta rejects scheduled_publish_time < 10 min
UPLOAD_TIMEOUT = (10, 1800)


class FacebookAdapter(RealAdapter):
    key = "facebook"
    display_he = "פייסבוק"
    required_secrets = ["FB_PAGE_ID", "META_ACCESS_TOKEN"]

    poll_interval_s: float = 5.0
    poll_max_s: float = 300.0

    def __init__(self, settings):
        super().__init__(settings)
        self._db = None

    @property
    def db(self):
        if self._db is None:
            from ..db import DB
            self._db = DB(self.settings.db_path)
        return self._db

    # ------------------------------------------------------------------ ids / auth
    def credential(self, name: str) -> str | None:
        """Base lookup (env → keyring → studio → config ``meta.page_id``), plus
        ``publishing.platforms.facebook.page_id`` for the Page id."""
        value = super().credential(name)
        if value or name != "FB_PAGE_ID":
            return value
        return str(self.platform_config.get("page_id") or "").strip() or None

    def page_id(self) -> str | None:
        """The Facebook Page id (not a secret)."""
        value = (self.credential("FB_PAGE_ID") or "").strip()
        return value or None

    def _auth(self) -> dict:
        return {"Authorization": f"OAuth {secret('META_ACCESS_TOKEN')}"}

    def _request(self, method: str, url: str, what: str, timeout=None, **kwargs) -> dict:
        import requests

        try:
            resp = requests.request(method, url, headers=self._auth(),
                                    timeout=timeout or self.timeout(), **kwargs)
        except requests.RequestException as exc:
            raise ApiError(f"{what}: אין חיבור לפייסבוק ({type(exc).__name__})") from None
        data = check_response(resp, what)
        if isinstance(data.get("error"), dict):
            raise ApiError(f"{what} failed: {data['error'].get('message', '')}".strip())
        return data

    def _poll_max(self) -> float:
        try:
            return max(0.0, float(self.platform_config.get("poll_max_s", self.poll_max_s)))
        except (TypeError, ValueError):
            return self.poll_max_s

    # ------------------------------------------------------------------ text / time
    def _description(self, plan: PostPlan) -> str:
        tags = " ".join(normalize_hashtags(plan.hashtags, 10))
        description = (plan.description or "").strip()
        if tags:
            description = f"{truncate(description, DESCRIPTION_LIMIT - len(tags) - 2)}\n\n{tags}"
        return truncate(description.strip(), DESCRIPTION_LIMIT)

    def _schedule_at(self, plan: PostPlan) -> datetime | None:
        if (plan.extra or {}).get("publish_now"):
            return None
        when = parse_when(plan.scheduled_at, self.settings.get("publishing.timezone", "UTC"))
        if when and when > datetime.now(timezone.utc) + MIN_SCHEDULE_AHEAD:
            return when
        return None

    def _fields(self, plan: PostPlan) -> dict:
        fields = {"title": truncate(plan.title, TITLE_LIMIT),
                  "description": self._description(plan)}
        when = self._schedule_at(plan)
        if when:
            fields.update(published="false", scheduled_publish_time=str(int(when.timestamp())))
        return fields

    def use_reels(self, plan: PostPlan, path: Path) -> bool:
        """Reel for vertical clips up to 90 s (``format: auto``), or as configured."""
        fmt = str(self.platform_config.get("format", "auto") or "auto").strip().lower()
        if fmt in ("reels", "reel"):
            return True
        if fmt == "video":
            return False
        aspect = str((plan.extra or {}).get("aspect") or "")
        info: dict = {}
        try:
            from ..media import probe
            info = probe(path)
        except Exception:
            info = {}
        vertical = aspect == "9:16" or bool(info.get("width") and info.get("height")
                                            and info["height"] > info["width"])
        duration = info.get("duration")
        return vertical and (duration is None or float(duration) <= REELS_MAX_S)

    # ------------------------------------------------------------------ upload
    def _upload(self, plan: PostPlan) -> PostResult:
        path = video_file(plan)
        page_id = self.page_id() or ""
        if not page_id.isdigit():
            raise ApiError("מזהה עמוד הפייסבוק לא תקין (Page id)")
        if self.use_reels(plan, path):
            return self._reel(plan, path, page_id)
        return self._video(plan, path, page_id)

    def _video(self, plan: PostPlan, path: Path, page_id: str) -> PostResult:
        fields = self._fields(plan)
        if path.stat().st_size <= SIMPLE_UPLOAD_MAX:
            with path.open("rb") as fh:
                data = self._request("POST", f"{VIDEO_BASE}/{page_id}/videos", "Facebook upload",
                                     timeout=UPLOAD_TIMEOUT, data=fields,
                                     files={"source": (path.name, fh, "video/mp4")})
            video_id = data.get("id")
        else:
            video_id = self._chunked(page_id, path, fields)
        if not video_id:
            raise ApiError("Facebook upload returned no video id")
        if "scheduled_publish_time" not in fields:
            record_published_at(self.db, self.key, str(video_id))
        return self.success(str(video_id), f"https://www.facebook.com/watch/?v={video_id}")

    def _chunked(self, page_id: str, path: Path, fields: dict) -> str:
        url = f"{VIDEO_BASE}/{page_id}/videos"
        size = path.stat().st_size
        start = self._request("POST", url, "Facebook upload start",
                              data={"upload_phase": "start", "file_size": str(size)})
        session_id, video_id = start.get("upload_session_id"), start.get("video_id")
        if not session_id:
            raise ApiError("Facebook upload start returned no session")
        offset, end = int(start.get("start_offset", 0)), int(start.get("end_offset", 0))
        with path.open("rb") as fh:
            while offset < end:
                fh.seek(offset)
                chunk = fh.read(end - offset)
                part = self._request("POST", url, "Facebook upload transfer",
                                     timeout=UPLOAD_TIMEOUT, data={
                                         "upload_phase": "transfer",
                                         "upload_session_id": session_id,
                                         "start_offset": str(offset)},
                                     files={"video_file_chunk": (path.name, chunk,
                                                                 "application/octet-stream")})
                offset, end = int(part.get("start_offset", end)), int(part.get("end_offset", end))
        done = self._request("POST", url, "Facebook upload finish", data={
            "upload_phase": "finish", "upload_session_id": session_id, **fields})
        if done.get("success") is False:
            raise ApiError("Facebook upload finish was not successful")
        return str(video_id)

    # ------------------------------------------------------------------ reels
    def _pending(self) -> dict:
        table = self.db.kv_get(PENDING_KV) or {}
        now = time.time()
        if not isinstance(table, dict):
            return {}
        return {k: v for k, v in table.items()
                if now - float((v or {}).get("ts") or 0) < PENDING_TTL_S}

    def _set_pending(self, key: str, **fields) -> None:
        table = self._pending()
        table[key] = {**(table.get(key) or {}), **fields, "ts": time.time()}
        self.db.kv_set(PENDING_KV, table)

    def _reel(self, plan: PostPlan, path: Path, page_id: str) -> PostResult:
        from .zernio import file_sha256

        pend_key = f"{self.key}:{file_sha256(path)[:24]}"
        rec = self._pending().get(pend_key) or {}
        video_id = rec.get("video_id") if rec.get("finished") else None
        when = self._schedule_at(plan)
        if not video_id and rec.get("video_id") and rec.get("uploaded"):
            # The bytes are there and "finish" was sent, but its answer never came back:
            # ask Facebook whether it took effect before doing anything again.
            if self._finish_took_effect(str(rec["video_id"])):
                video_id = str(rec["video_id"])
                self._set_pending(pend_key, finished=True)
            else:
                video_id = self._finish_reel(plan, page_id, str(rec["video_id"]), when,
                                             pend_key)
        if not video_id:  # a retry after "finish" never uploads the same Reel again
            start = self._request("POST", f"{GRAPH_BASE}/{page_id}/video_reels",
                                  "Facebook reel start", data={"upload_phase": "start"})
            video_id = start.get("video_id")
            if not video_id:
                raise ApiError("Facebook reel start returned no video id")
            video_id = str(video_id)
            self._rupload(start.get("upload_url") or f"{RUPLOAD_BASE}/{video_id}", path)
            # remembered BEFORE finish: a lost finish answer must never mean a second Reel
            self._set_pending(pend_key, video_id=video_id, uploaded=True, finished=False,
                              scheduled=bool(when))
            video_id = self._finish_reel(plan, page_id, video_id, when, pend_key)
        if when or rec.get("scheduled"):
            return self.success(video_id, None)  # Facebook publishes it at the scheduled time
        state, detail = self._wait_reel(str(video_id))
        if state == "error":
            raise ApiError(f"פייסבוק לא הצליח לעבד את הרילס{': ' + detail if detail else ''}")
        if state != "published":
            return PostResult(platform=self.key, ok=False, dry_run=False,
                              external_id=str(video_id), url=None, error=(
                                  f"פייסבוק עדיין מעבד את הרילס אחרי {self._poll_max():.0f} "
                                  "שניות — הוא עוד לא אושר כפורסם. ניסיון חוזר רק יבדוק שוב, "
                                  f"בלי להעלות פעמיים. (still processing, video {video_id})"))
        url = self._permalink(str(video_id))
        record_published_at(self.db, self.key, str(video_id))
        return self.success(str(video_id), url or f"https://www.facebook.com/reel/{video_id}")

    def _finish_reel(self, plan: PostPlan, page_id: str, video_id: str, when, pend_key: str
                     ) -> str:
        params = {"upload_phase": "finish", "video_id": video_id,
                  "description": self._description(plan)}
        if plan.title:
            params["title"] = truncate(plan.title, TITLE_LIMIT)
        if when:
            params.update(video_state="SCHEDULED",
                          scheduled_publish_time=str(int(when.timestamp())))
        else:
            params["video_state"] = "PUBLISHED"
        done = self._request("POST", f"{GRAPH_BASE}/{page_id}/video_reels",
                             "Facebook reel finish", data=params)
        if done.get("success") is False:
            raise ApiError("Facebook reel finish was not successful")
        self._set_pending(pend_key, video_id=video_id, finished=True, scheduled=bool(when))
        return video_id

    def _finish_took_effect(self, video_id: str) -> bool:
        """True when Facebook already started (or finished) publishing this Reel."""
        info = self._request("GET", f"{GRAPH_BASE}/{video_id}", "Facebook reel status",
                             params={"fields": "status"})
        status = info.get("status") or {}
        publishing = status.get("publishing_phase") or {}
        phase = str(publishing.get("status") or "").lower()
        if phase and phase != "not_started":
            return True
        return bool(publishing) and self.reel_state(status)[0] == "published"

    def _rupload(self, upload_url: str, path: Path) -> None:
        import requests

        size = path.stat().st_size
        try:
            with path.open("rb") as fh:
                resp = requests.post(upload_url, data=fh, timeout=UPLOAD_TIMEOUT, headers={
                    **self._auth(), "offset": "0", "file_size": str(size)})
        except requests.RequestException as exc:
            raise ApiError(f"Facebook rupload: אין חיבור ({type(exc).__name__})") from None
        data = check_response(resp, "Facebook rupload")
        if data.get("success") is False:
            raise ApiError("Facebook rupload was not successful")

    @staticmethod
    def reel_state(status: dict) -> tuple[str, str]:
        """(published | error | processing, detail) from a video ``status`` object."""
        status = status or {}
        for phase in ("uploading_phase", "processing_phase", "publishing_phase"):
            info = status.get(phase) or {}
            if str(info.get("status") or "").lower() == "error":
                err = info.get("errors") or info.get("error") or ""
                return "error", str(err)[:200]
        if str(status.get("video_status") or "").lower() in ("error", "expired"):
            return "error", str(status.get("video_status"))
        publishing = status.get("publishing_phase") or {}
        if str(publishing.get("status") or "").lower() == "complete" \
                or str(publishing.get("publish_status") or "").lower() == "published":
            return "published", ""
        if not publishing and str(status.get("video_status") or "").lower() == "ready":
            return "published", ""
        return "processing", ""

    def _wait_reel(self, video_id: str) -> tuple[str, str]:
        deadline = time.monotonic() + self._poll_max()
        while True:
            info = self._request("GET", f"{GRAPH_BASE}/{video_id}", "Facebook reel status",
                                 params={"fields": "status"})
            state, detail = self.reel_state(info.get("status") or {})
            if state != "processing" or time.monotonic() >= deadline:
                return state, detail
            time.sleep(self.poll_interval_s)

    def _permalink(self, video_id: str) -> str | None:
        try:
            url = self._request("GET", f"{GRAPH_BASE}/{video_id}", "Facebook permalink",
                                params={"fields": "permalink_url"}).get("permalink_url")
        except Exception:
            return None
        if url and str(url).startswith("/"):
            url = "https://www.facebook.com" + str(url)
        return url or None

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
