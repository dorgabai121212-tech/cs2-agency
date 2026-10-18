"""Instagram Reels via the Instagram Graph API v24.0 (resumable upload) — the studio's proven flow.

Flow: POST /{ig-user-id}/media (media_type=REELS, upload_type=resumable) -> POST the file to the
returned rupload.facebook.com URI -> poll the container's status_code until FINISHED ->
POST /{ig-user-id}/media_publish -> read the permalink.
Needs an Instagram professional account linked to a Facebook Page and a (Page) token with
instagram_basic + instagram_content_publish (+ instagram_manage_comments for replies).
The token travels only in the Authorization header, so it never appears in URLs or errors.

IDs are not secrets: the IG user id comes from IG_USER_ID, else from config ``meta.ig_user_id``
(the importer copies it from the old studio's ``meta_ids``), else
``publishing.platforms.instagram_reels.ig_user_id``.

YouTube first: Instagram cannot be deleted through the API, so a Reel goes out only
``min_delay_after_youtube_min`` (default 30) minutes after the same video's YouTube post —
see :func:`meta_min_delay_ok`, which the strategist should also use when it picks times.

Retries never duplicate: the container / media id of each file is remembered (kv
``meta.pending``), so a retry of a Reel that was still processing finishes that container
instead of uploading again. A container whose bytes never arrived (``uploaded`` false) gets
them again; a container Meta reports as PUBLISHED (the publish call reached Meta but its answer
was lost) is adopted — matched in the account's recent media by caption — and is never
published a second time.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from ..config import secret
from ..models import PostPlan, PostResult
from ._common import ApiError, RealAdapter, build_caption, check_response, video_file

log = logging.getLogger("agency.platforms")

GRAPH_API_VERSION = "v24.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
RUPLOAD_BASE = f"https://rupload.facebook.com/ig-api-upload/{GRAPH_API_VERSION}"
CAPTION_LIMIT = 2200
MAX_HASHTAGS = 30
UPLOAD_TIMEOUT = (10, 1800)

MIN_DELAY_AFTER_YOUTUBE_MIN = 30
YOUTUBE_KEYS = ("youtube", "youtube_shorts")
PUBLISHED_AT_KV = "platforms.published_at"   # {"<platform>:<external_id>": ISO time}
PUBLISHED_AT_KEEP = 500
PENDING_KV = "meta.pending"                  # {"<platform>:<sha24>": {...ids..., "ts"}}
PENDING_TTL_S = 24 * 3600


# ------------------------------------------------------------------- publish-time bookkeeping
def record_published_at(db, platform: str, external_id: str, when: str | None = None) -> None:
    """Remember when a real post went (or is scheduled to go) live. Used by the 30-min rule."""
    if not external_id:
        return
    stamp = when or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    table = db.kv_get(PUBLISHED_AT_KV) or {}
    if not isinstance(table, dict):
        table = {}
    table[f"{platform}:{external_id}"] = stamp
    if len(table) > PUBLISHED_AT_KEEP:
        table = dict(list(table.items())[-PUBLISHED_AT_KEEP:])
    db.kv_set(PUBLISHED_AT_KV, table)


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def meta_delay_status(db, video_id: int, *, now: datetime | None = None,
                      min_delay_min: float = MIN_DELAY_AFTER_YOUTUBE_MIN
                      ) -> tuple[bool, datetime | None]:
    """``(ok, not_before)`` for posting this video's Instagram Reel now.

    - No real YouTube post for the video (none planned, all failed/cancelled, or training
      mode) -> ``(True, None)``.
    - A YouTube post is planned / waiting / scheduled / uploading -> ``(False, None)``:
      YouTube goes first. So is a "failed" one that Zernio still holds (it has a Zernio post
      id: "still processing" — it may go live any minute).
    - YouTube is live -> ok once ``min_delay_min`` minutes passed since the latest one;
      ``not_before`` is that earliest time.
    The publish time comes from :func:`record_published_at` (falls back to the post's
    ``updated_at``, which is never earlier than the real time, so the rule stays safe).
    """
    now = now or datetime.now(timezone.utc)
    table = db.kv_get(PUBLISHED_AT_KV) or {}
    latest: datetime | None = None
    for post in db.list_posts(int(video_id)):
        if post.get("platform") not in YOUTUBE_KEYS:
            continue
        status = post.get("status")
        result = post.get("result") or {}
        if status == "failed" and result.get("external_id") and result.get("dry_run") is False:
            return False, None  # not confirmed either way yet: YouTube may still go first
        if status in ("failed", "cancelled"):
            continue
        if status != "published":
            return False, None
        if result.get("dry_run"):
            continue  # a training-mode "post" is not on YouTube
        key = f"{post['platform']}:{post.get('external_id') or result.get('external_id')}"
        at = _parse(table.get(key)) if isinstance(table, dict) else None
        at = at or _parse(post.get("updated_at")) or now
        latest = at if latest is None or at > latest else latest
    if latest is None:
        return True, None
    not_before = latest + timedelta(minutes=float(min_delay_min))
    return now >= not_before, not_before


def meta_min_delay_ok(db, video_id: int, *, now: datetime | None = None,
                      min_delay_min: float = MIN_DELAY_AFTER_YOUTUBE_MIN) -> bool:
    """True when this video's Instagram Reel may go out now (≥30 min after its YouTube post).

    The Instagram adapter enforces it (fails with a plain message, before any network call);
    the strategist / scheduler should use it (or :func:`meta_delay_status`) to pick times.
    """
    return meta_delay_status(db, video_id, now=now, min_delay_min=min_delay_min)[0]


def _local_hhmm(settings, when: datetime) -> str:
    try:
        from zoneinfo import ZoneInfo
        when = when.astimezone(ZoneInfo(settings.get("publishing.timezone", "Asia/Jerusalem")))
    except Exception:
        pass
    return when.strftime("%H:%M")


class InstagramReelsAdapter(RealAdapter):
    key = "instagram_reels"
    display_he = "אינסטגרם רילס"
    required_secrets = ["IG_USER_ID", "META_ACCESS_TOKEN"]

    poll_interval_s: float = 5.0
    poll_max_s: float = 600.0   # the studio waits up to 10 minutes for a container

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
        """Base lookup (env → keyring → studio → config ``meta.ig_user_id``), plus
        ``publishing.platforms.instagram_reels.ig_user_id`` for the IG user id."""
        value = super().credential(name)
        if value or name != "IG_USER_ID":
            return value
        return str(self.platform_config.get("ig_user_id") or "").strip() or None

    def ig_user_id(self) -> str | None:
        """The Instagram professional account id (not a secret)."""
        value = (self.credential("IG_USER_ID") or "").strip()
        return value or None

    def _auth(self) -> dict:
        # Token in a header (not the query string) so it never shows up in URLs / errors.
        return {"Authorization": f"OAuth {secret('META_ACCESS_TOKEN')}"}

    def _graph(self, method: str, path: str, what: str, **kwargs) -> dict:
        import requests

        try:
            resp = requests.request(method, f"{GRAPH_BASE}/{path.lstrip('/')}",
                                    headers=self._auth(), timeout=self.timeout(), **kwargs)
        except requests.RequestException as exc:
            raise ApiError(f"{what}: אין חיבור לפייסבוק/אינסטגרם ({type(exc).__name__})") \
                from None
        data = check_response(resp, what)
        if isinstance(data.get("error"), dict):
            raise ApiError(f"{what} failed: {data['error'].get('message', '')}".strip())
        return data

    def _poll_max(self) -> float:
        try:
            return max(0.0, float(self.platform_config.get("poll_max_s", self.poll_max_s)))
        except (TypeError, ValueError):
            return self.poll_max_s

    # ------------------------------------------------------------------ YouTube first
    def _video_id(self, plan: PostPlan) -> int | None:
        """The agency video of this plan: ``extra["video_id"]``, else the post being published."""
        vid = (plan.extra or {}).get("video_id")
        if vid is not None and str(vid).isdigit():
            return int(vid)
        try:
            for post in self.db.list_posts():
                if post.get("platform") == self.key and post.get("status") == "publishing" \
                        and (post.get("plan") or {}).get("variant_path") == plan.variant_path:
                    return int(post["video_id"])
        except Exception:
            return None
        return None

    def _youtube_first(self, plan: PostPlan) -> None:
        """Raise (before any network call) when YouTube has not been out long enough."""
        if (plan.extra or {}).get("skip_youtube_first") is True:
            return
        video_id = self._video_id(plan)
        if video_id is None:
            log.info("instagram: video unknown, YouTube-first rule not checked")
            return
        try:
            delay = float(self.platform_config.get("min_delay_after_youtube_min",
                                                   MIN_DELAY_AFTER_YOUTUBE_MIN))
        except (TypeError, ValueError):
            delay = MIN_DELAY_AFTER_YOUTUBE_MIN
        ok, not_before = meta_delay_status(self.db, video_id, min_delay_min=delay)
        if ok:
            return
        if not_before is None:
            raise ApiError(f"אינסטגרם עולה רק אחרי יוטיוב — הסרטון עוד לא עלה ליוטיוב, "
                           f"ולכן אינסטגרם מחכה (YouTube first, {delay:g} min)")
        raise ApiError(f"אינסטגרם עולה רק {delay:g} דקות אחרי יוטיוב — אפשר לנסות שוב "
                       f"אחרי {_local_hhmm(self.settings, not_before)} (YouTube first)")

    # ------------------------------------------------------------------ upload
    def _pending(self) -> dict:
        table = self.db.kv_get(PENDING_KV) or {}
        now = time.time()
        if not isinstance(table, dict):
            return {}
        return {k: v for k, v in table.items() if now - float((v or {}).get("ts") or 0)
                < PENDING_TTL_S}

    def _set_pending(self, key: str, **fields) -> None:
        table = self._pending()
        table[key] = {**(table.get(key) or {}), **fields, "ts": time.time()}
        self.db.kv_set(PENDING_KV, table)

    def _upload(self, plan: PostPlan) -> PostResult:
        from .zernio import file_sha256

        path = video_file(plan)
        self._youtube_first(plan)
        ig_user = self.ig_user_id() or ""
        if not ig_user.isdigit():
            raise ApiError("מזהה חשבון האינסטגרם לא תקין (IG user id)")
        size = path.stat().st_size
        pend_key = f"{self.key}:{file_sha256(path)[:24]}"
        rec = self._pending().get(pend_key) or {}

        if rec.get("media_id"):  # published already (a retry): never post the same file twice
            media_id = str(rec["media_id"])
            return self.success(media_id, rec.get("url") or self._permalink(media_id))

        caption = build_caption(plan, CAPTION_LIMIT, max_hashtags=MAX_HASHTAGS)
        container_id = rec.get("container_id")
        if container_id:
            state = self._container_state(str(container_id))
            if state == "PUBLISHED":  # media_publish reached Meta; only its answer was lost
                return self._adopt_published(pend_key, rec, ig_user, caption)
            if state in ("ERROR", "EXPIRED"):
                container_id = None  # dead: start over with a fresh one
            elif rec.get("uploaded") is False:  # the bytes never arrived: send them again
                self._rupload(str(rec.get("uri") or f"{RUPLOAD_BASE}/{container_id}"), path,
                              size)
                self._set_pending(pend_key, uploaded=True)
        if not container_id:
            container = self._graph("POST", f"{ig_user}/media", "Instagram create container",
                                    data={"media_type": "REELS", "upload_type": "resumable",
                                          "caption": caption,
                                          "share_to_feed": "true" if self.platform_config.get(
                                              "share_to_feed", True) else "false"})
            container_id = container.get("id")
            if not container_id:
                raise ApiError("Instagram create container returned no id")
            container_id = str(container_id)
            uri = str(container.get("uri") or f"{RUPLOAD_BASE}/{container_id}")
            self._set_pending(pend_key, container_id=container_id, uri=uri, uploaded=False,
                              created=time.time())
            self._rupload(uri, path, size)
            self._set_pending(pend_key, uploaded=True)

        if not self._wait_finished(str(container_id)):
            waited = self._poll_max()
            return PostResult(platform=self.key, ok=False, dry_run=False,
                              external_id=str(container_id), url=None, error=(
                                  f"אינסטגרם עדיין מעבד את הסרטון אחרי {waited:.0f} שניות — "
                                  "הוא עוד לא פורסם. ניסיון חוזר ימשיך מאותה נקודה בלי להעלות "
                                  f"שוב. (still processing, container {container_id})"))
        published = self._graph("POST", f"{ig_user}/media_publish", "Instagram media_publish",
                                data={"creation_id": str(container_id)})
        media_id = published.get("id")
        if not media_id:
            raise ApiError("Instagram media_publish returned no id")
        media_id = str(media_id)
        self._set_pending(pend_key, container_id=str(container_id), media_id=media_id)
        # It is live from here on: a failed permalink lookup must not turn this into a failure.
        url = self._permalink(media_id)
        if url:
            self._set_pending(pend_key, url=url)
        record_published_at(self.db, self.key, media_id)
        return self.success(media_id, url)

    def _adopt_published(self, pend_key: str, rec: dict, ig_user: str, caption: str
                         ) -> PostResult:
        """The container is already PUBLISHED: find that Reel (recent media with the same
        caption, not older than the container) and report it. Never publishes again."""
        try:
            listing = self._graph("GET", f"{ig_user}/media", "Instagram recent media",
                                  params={"fields": "id,caption,permalink,timestamp",
                                          "limit": 10})
        except ApiError:
            listing = {}
        created = float(rec.get("created") or rec.get("ts") or 0) - 600
        for item in listing.get("data") or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            when = _parse(item.get("timestamp"))
            if (item.get("caption") or "").strip() == caption.strip() and (
                    when is None or when.timestamp() >= created):
                media_id, url = str(item["id"]), item.get("permalink")
                self._set_pending(pend_key, media_id=media_id, url=url)
                record_published_at(self.db, self.key, media_id)
                return self.success(media_id, url)
        return self.fail("נראה שהרילס כבר פורסם באינסטגרם (הבקשה הקודמת הגיעה, רק התשובה לא) "
                         "— בדוק באפליקציה. לא מפרסם שוב כדי שלא יופיע פעמיים. "
                         "(container PUBLISHED)")

    def _rupload(self, upload_uri: str, path, size: int) -> None:
        import requests

        try:
            with path.open("rb") as fh:
                resp = requests.post(upload_uri, data=fh, timeout=UPLOAD_TIMEOUT, headers={
                    **self._auth(), "offset": "0", "file_size": str(size),
                    "Content-Type": "application/octet-stream", "Content-Length": str(size)})
        except requests.RequestException as exc:
            raise ApiError(f"Instagram rupload: אין חיבור ({type(exc).__name__})") from None
        uploaded = check_response(resp, "Instagram rupload")
        if uploaded.get("success") is False:
            raise ApiError(f"Instagram rupload failed: {uploaded.get('debug_info') or ''}".strip())

    def _container_state(self, container_id: str) -> str:
        info = self._graph("GET", container_id, "Instagram container status",
                           params={"fields": "status_code,status"})
        return str(info.get("status_code") or "")

    def _wait_finished(self, container_id: str) -> bool:
        """True once FINISHED; False when still processing at the deadline; raises on ERROR."""
        deadline = time.monotonic() + self._poll_max()
        while True:
            info = self._graph("GET", container_id, "Instagram container status",
                               params={"fields": "status_code,status"})
            code = info.get("status_code")
            if code == "FINISHED":
                return True
            if code in ("ERROR", "EXPIRED"):
                raise ApiError(f"אינסטגרם לא הצליח לעבד את הסרטון ({code}: "
                               f"{info.get('status', '')})".strip())
            if time.monotonic() >= deadline:
                return False
            time.sleep(self.poll_interval_s)

    def _permalink(self, media_id: str) -> str | None:
        try:
            return self._graph("GET", media_id, "Instagram permalink",
                               params={"fields": "permalink"}).get("permalink")
        except Exception:
            return None

    # --------------------------------------------------------------- stats / comments
    def fetch_stats(self, external_id: str) -> dict:
        if not self.can_call_api(external_id):
            return {}
        stats: dict = {}
        try:
            info = self._graph("GET", external_id, "Instagram media",
                               params={"fields": "like_count,comments_count"})
            stats.update(likes=info.get("like_count", 0), comments=info.get("comments_count", 0))
            insights = self._graph("GET", f"{external_id}/insights", "Instagram insights",
                                   params={"metric": "views,shares"})
            for item in insights.get("data", []):
                value = (item.get("values") or [{}])[0].get("value", 0)
                if item.get("name"):
                    stats[item["name"]] = value
        except Exception:
            pass
        return stats

    def fetch_comments(self, external_id: str, limit: int = 20) -> list[dict]:
        if not self.can_call_api(external_id):
            return []
        try:
            data = self._graph("GET", f"{external_id}/comments", "Instagram comments", params={
                "fields": "id,text,username,timestamp,like_count", "limit": min(limit, 50)})
        except Exception:
            return []
        return [{"id": c.get("id"), "author": c.get("username", ""), "text": c.get("text", ""),
                 "published_at": c.get("timestamp"), "likes": c.get("like_count", 0)}
                for c in data.get("data", [])][:limit]

    def reply_comment(self, comment_id: str, text: str) -> bool:
        if self.dry_run or not self.configured() or not comment_id:
            return False
        try:
            self._graph("POST", f"{comment_id}/replies", "Instagram reply",
                        data={"message": text[:2200]})
            return True
        except Exception:
            return False
