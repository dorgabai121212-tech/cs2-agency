"""YouTube, YouTube Shorts and TikTok through Zernio (zernio.com social API), SPEC_V2 §11.2.

Why Zernio: direct YouTube Data API uploads from the owner's unverified Google Cloud project are
locked private for good, so his studio publishes YouTube + TikTok through Zernio (free plan =
2 accounts). Zernio also schedules in its cloud (``scheduledFor``): a post goes out even when
the PC is off. Google OAuth stays for stats / comments / making videos private (youtube.py).

Flow (as proven in the owner's studio, see ZERNIO_API.md):
  POST /v1/media/presign -> PUT the file to ``uploadUrl`` (no auth header on the PUT)
  -> POST /v1/posts with ONE platform entry and ``Idempotency-Key: <sha256[:24]>:<platform>``
     (a repeated POST within 24 h returns the same post, never a second one)
  -> poll GET /v1/posts/{id} until published / failed, bounded by ``poll_max_s``.
Still pending when the wait ends = ``ok=False`` with a plain "still processing" error. Never a
fake success. The API key travels only in the Authorization header and never reaches logs or
error text. Every request has a timeout.

Retries never duplicate: before a new ``POST /v1/posts`` the post Zernio already has for the
same file + platform (kv ``zernio.posts``) is checked first and adopted when it is published
or still on its way (the Idempotency-Key alone lasts only 24 hours). A post handed to Zernio's
cloud schedule is NOT a published post: the adapter flags it (``remote_scheduled``), the
publisher keeps it ``scheduled``, and the scheduler reconciles it (and "still processing"
failures) with :meth:`ZernioAdapter.post_status` until Zernio reports the real outcome.

Routing is done by the registry (``Settings.platform_via``: ``publishing.platforms.<key>.via:
zernio | direct``, default ``zernio`` for youtube / youtube_shorts / tiktok), which builds
``ZernioAdapter(settings, key)``.
"""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..config import ZERNIO_PLATFORMS, Settings, secret
from ..models import PostPlan, PostResult
from ._common import ApiError, RealAdapter, build_caption, parse_when, video_file

log = logging.getLogger("agency.platforms")

API_BASE = "https://zernio.com/api"
ZERNIO_TARGETS = tuple(ZERNIO_PLATFORMS)  # youtube, youtube_shorts, tiktok
ZERNIO_PLATFORM = {"youtube": "youtube", "youtube_shorts": "youtube", "tiktok": "tiktok"}
DISPLAY_HE = {"youtube": "יוטיוב", "youtube_shorts": "יוטיוב שורטס", "tiktok": "טיקטוק"}

READ_TIMEOUT = (10, 60)        # GET accounts / creator-info / post status, presign
POST_TIMEOUT = (10, 900)       # POST /v1/posts with publishNow is synchronous at Zernio
PUT_TIMEOUT = (10, 1800)       # streaming the video file to the presigned URL
DELETE_TIMEOUT = (10, 60)
VERIFY_TIMEOUT = 15

ACCOUNTS_KV = "zernio.accounts"    # {"ts": epoch, "map": {platform: {id, username, url, ...}}}
ACCOUNTS_TTL_S = 6 * 3600
UPLOADS_KV = "zernio.uploads"      # {sha256: {"url": publicUrl, "ts": epoch}}
UPLOAD_TTL_S = 20 * 3600
POSTS_KV = "zernio.posts"          # {zernio post id: {platform, sha, native_id, url, state, ts}}
ATTEMPTS_KV = "zernio.attempts"    # {"<sha24>:<platform>": n} bumped after a confirmed failure
POSTS_KEEP_S = 90 * 24 * 3600
IDEMPOTENCY_WINDOW_S = 24 * 3600
MIN_SCHEDULE_AHEAD_S = 120         # closer than this = publish now (the upload takes time)

TIKTOK_DEFAULT_PRIVACY = "PUBLIC_TO_EVERYONE"
YOUTUBE_VISIBILITY = ("public", "unlisted", "private")

CONTENT_TYPES = {".mp4": "video/mp4", ".m4v": "video/x-m4v", ".mov": "video/quicktime",
                 ".webm": "video/webm", ".mpeg": "video/mpeg", ".mpg": "video/mpeg",
                 ".avi": "video/x-msvideo"}

# Zernio's per-platform errorCategory -> plain Hebrew (owner-facing).
CATEGORY_HE = {
    "auth_expired": "החיבור של {p} ב-Zernio פג — צריך לחבר מחדש את החשבון באתר Zernio",
    "user_content": "{p} דחה את התוכן של הסרטון",
    "user_abuse": "{p} חסם את הפוסט בגלל הכללים שלו",
    "account_issue": "יש בעיה בחשבון {p} — כדאי לבדוק באפליקציה",
    "platform_rejected": "{p} דחה את הפוסט",
    "platform_error": "תקלה זמנית אצל {p} — נסה שוב מאוחר יותר",
    "platform_rate_limit": "{p}: יותר מדי העלאות כרגע — נסה שוב מאוחר יותר",
    "quota_exhausted": "{p}: נגמרה המכסה היומית להעלאות — נסה שוב מחר",
    "system_error": "תקלה זמנית ב-Zernio — נסה שוב מאוחר יותר",
    "unknown": "ההעלאה ל{p} נכשלה",
}
NETWORK_HE = "אין חיבור ל-Zernio כרגע — בדוק את האינטרנט ונסה שוב"


class ZernioError(ApiError):
    """Zernio refused or failed. Message is Hebrew + a short technical tail, never secrets."""

    def __init__(self, message: str, *, status: int | None = None, body: Any = None,
                 category: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body if isinstance(body, dict) else {}
        self.category = category


# ------------------------------------------------------------------------------- helpers
def file_sha256(path: str | Path, chunk: int = 1 << 20) -> str:
    """Hex SHA-256 of a file (streamed)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _json(resp) -> dict:
    try:
        data = resp.json() if resp.content else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {"data": data}


def _detail(body: dict) -> str:
    """Short human detail from a Zernio error body (no URLs/headers are ever in it)."""
    for key in ("error", "message", "detail"):
        value = body.get(key)
        if isinstance(value, dict):
            value = value.get("message") or value.get("code")
        if value:
            return str(value)[:200]
    return ""


class ZernioAdapter(RealAdapter):
    """One Zernio target platform (``youtube`` | ``youtube_shorts`` | ``tiktok``).

    ``key`` is the agency platform key, so the owner's per-platform live switch, config
    (``publishing.platforms.<key>``) and the dry-run guarantee work exactly as for any adapter.
    """

    key = "youtube"
    display_he = DISPLAY_HE["youtube"]
    required_secrets = ["ZERNIO_API_KEY"]

    poll_interval_s: float = 5.0
    poll_max_s: float = 300.0
    backoff_s: tuple[float, ...] = (3.0, 6.0)   # read retries (network / 429 / 5xx)

    def __init__(self, settings: Settings, target_platform: str = "youtube"):
        if target_platform not in ZERNIO_TARGETS:
            raise ValueError(f"Zernio cannot publish {target_platform!r}; "
                             f"expected one of {ZERNIO_TARGETS}")
        super().__init__(settings)
        self.key = target_platform
        self.display_he = DISPLAY_HE[target_platform]
        self.zernio_platform = ZERNIO_PLATFORM[target_platform]
        self._db = None
        # Set by _upload when the post went to Zernio's cloud schedule instead of out now:
        # {"post_id", "scheduled_for"} (read by the publisher right after upload()).
        self.remote_scheduled: dict | None = None

    # ------------------------------------------------------------------ plumbing
    @property
    def db(self):
        if self._db is None:
            from ..db import DB
            self._db = DB(self.settings.db_path)
        return self._db

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {secret('ZERNIO_API_KEY')}",
                "Accept": "application/json"}

    def _request(self, method: str, path: str, what: str, *, retry: bool = True,
                 timeout: Any = READ_TIMEOUT, headers: dict | None = None, **kwargs) -> dict:
        """One Zernio call. Reads retry on network/429/5xx; the post-creating POST does not."""
        import requests

        tries = 1 + len(self.backoff_s) if retry else 1
        for attempt in range(tries):
            try:
                resp = requests.request(method, f"{API_BASE}{path}", timeout=timeout,
                                        headers={**self._headers(), **(headers or {})}, **kwargs)
            except requests.RequestException as exc:
                if attempt < tries - 1:
                    time.sleep(self.backoff_s[attempt])
                    continue
                raise ZernioError(f"{NETWORK_HE} ({what}: {type(exc).__name__})") from None
            status = resp.status_code
            if (status == 429 or status >= 500) and attempt < tries - 1:
                time.sleep(self.backoff_s[attempt])
                continue
            data = _json(resp)
            if status >= 400:
                raise self._http_error(status, data, what)
            return data
        raise ZernioError(f"{NETWORK_HE} ({what})")  # pragma: no cover - loop always returns

    def _http_error(self, status: int, body: dict, what: str) -> ZernioError:
        detail = self.scrub(_detail(body))
        if status in (401, 403):
            msg = "מפתח Zernio לא תקין או שפג תוקפו — חבר מחדש בעמוד החיבורים"
        elif status == 402:
            msg = "החבילה ב-Zernio לא מאפשרת את זה (מכסה או תשלום)"
        elif status == 404:
            msg = "Zernio לא מצא את מה שביקשנו"
        elif status == 409:
            msg = "Zernio חושב שזה פוסט כפול"
        elif status == 413:
            msg = "הקובץ גדול מדי בשביל Zernio"
        elif status == 429:
            msg = "יותר מדי בקשות ל-Zernio — נסה שוב בעוד כמה דקות"
        elif status >= 500:
            msg = "Zernio לא זמין כרגע — נסה שוב מאוחר יותר"
        else:
            msg = "Zernio דחה את הבקשה"
        tail = f"{what}: HTTP {status}" + (f" {detail}" if detail else "")
        return ZernioError(f"{msg} ({tail})", status=status, body=body)

    def _platform_error(self, entry: dict) -> str:
        category = str(entry.get("errorCategory") or "unknown")
        template = CATEGORY_HE.get(category, CATEGORY_HE["unknown"])
        message = template.format(p=self.display_he)
        detail = self.scrub(str(entry.get("errorMessage") or ""))[:300]
        return f"{message}" + (f": {detail}" if detail else "") + f" ({category})"

    def _poll_max(self) -> float:
        try:
            return max(0.0, float(self.platform_config.get("poll_max_s", self.poll_max_s)))
        except (TypeError, ValueError):
            return self.poll_max_s

    # ------------------------------------------------------------------ accounts
    def accounts(self, refresh: bool = False, timeout: Any = READ_TIMEOUT) -> dict:
        """Connected Zernio accounts ``{platform: {id, username, url, needs_reconnection}}``.

        Cached for 6 hours in the DB (kv ``zernio.accounts``); ids are not secrets.
        """
        cached = self.db.kv_get(ACCOUNTS_KV) or {}
        if not refresh and isinstance(cached, dict) and cached.get("map") is not None \
                and time.time() - float(cached.get("ts") or 0) < ACCOUNTS_TTL_S:
            return cached["map"]
        data = self._request("GET", "/v1/accounts", "accounts", timeout=timeout)
        found: dict[str, dict] = {}
        for acc in data.get("accounts") or []:
            if not isinstance(acc, dict) or acc.get("platform") not in ("youtube", "tiktok"):
                continue
            if acc.get("isActive", True) is False or acc.get("enabled", True) is False:
                continue
            entry = {"id": str(acc.get("_id") or ""), "username": acc.get("username") or "",
                     "display_name": acc.get("displayName") or "",
                     "url": acc.get("profileUrl") or "",
                     "needs_reconnection": bool(acc.get("needsReconnection"))}
            if not entry["id"]:
                continue
            current = found.get(acc["platform"])
            if current is None or (current["needs_reconnection"]
                                   and not entry["needs_reconnection"]):
                found[acc["platform"]] = entry
        self.db.kv_set(ACCOUNTS_KV, {"ts": time.time(), "map": found})
        return found

    def _account(self) -> dict:
        override = str(self.platform_config.get("zernio_account_id") or "").strip()
        if override:
            return {"id": override, "username": "", "needs_reconnection": False}
        acc = self.accounts().get(self.zernio_platform)
        if acc is None or acc.get("needs_reconnection"):
            # the cache may predate a new connection or the owner's reconnect on zernio.com
            acc = self.accounts(refresh=True).get(self.zernio_platform)
        if acc is None:
            raise ZernioError(f"אין חשבון {self.display_he} מחובר ב-Zernio — חבר אותו באתר "
                              f"Zernio (no {self.zernio_platform} account)")
        if acc.get("needs_reconnection"):
            raise ZernioError(f"החשבון של {self.display_he} ב-Zernio צריך חיבור מחדש — "
                              "היכנס לאתר Zernio וחבר אותו שוב (needsReconnection)")
        return acc

    def verify(self) -> dict:
        """Read-only connection check for the connect wizard (15 s timeout, no retries).

        Returns ``{"ok", "account_name", "problem"}``; never returns or logs the key.
        """
        if not secret("ZERNIO_API_KEY"):
            return {"ok": False, "account_name": "", "problem": "חסר מפתח Zernio"}
        saved, self.backoff_s = self.backoff_s, ()
        try:
            acc = self.accounts(refresh=True, timeout=VERIFY_TIMEOUT).get(self.zernio_platform)
        except ZernioError as exc:
            return {"ok": False, "account_name": "", "problem": str(exc)}
        finally:
            self.backoff_s = saved
        if not acc:
            return {"ok": False, "account_name": "",
                    "problem": f"המפתח תקין, אבל אין חשבון {self.display_he} מחובר ב-Zernio"}
        name = acc.get("username") or acc.get("display_name") or ""
        if acc.get("needs_reconnection"):
            return {"ok": False, "account_name": name,
                    "problem": f"החשבון של {self.display_he} ב-Zernio צריך חיבור מחדש"}
        return {"ok": True, "account_name": f"@{name}" if name and not name.startswith("@")
                else name, "problem": None}

    # ------------------------------------------------------------------ media
    def _media_url(self, path: Path, sha: str) -> str:
        """Presign + PUT once per file content (Shorts and TikTok reuse one upload)."""
        cache = self.db.kv_get(UPLOADS_KV) or {}
        hit = cache.get(sha) if isinstance(cache, dict) else None
        if hit and time.time() - float(hit.get("ts") or 0) < UPLOAD_TTL_S and hit.get("url"):
            return str(hit["url"])
        import requests

        size = path.stat().st_size
        content_type = CONTENT_TYPES.get(path.suffix.lower(), "video/mp4")
        pre = self._request("POST", "/v1/media/presign", "media presign",
                            json={"filename": path.name, "contentType": content_type,
                                  "size": size})
        upload_url, public_url = pre.get("uploadUrl"), pre.get("publicUrl")
        if not upload_url or not public_url:
            raise ZernioError("Zernio לא החזיר כתובת להעלאת הקובץ (media presign)")
        try:
            with path.open("rb") as fh:  # streamed; the presigned URL needs no auth header
                resp = requests.put(upload_url, data=fh, timeout=PUT_TIMEOUT, headers={
                    "Content-Type": content_type, "Content-Length": str(size)})
        except requests.RequestException as exc:
            raise ZernioError(f"{NETWORK_HE} (media upload: {type(exc).__name__})") from None
        if resp.status_code >= 300:
            raise ZernioError(f"העלאת הקובץ ל-Zernio נכשלה (media upload: HTTP "
                              f"{resp.status_code})", status=resp.status_code)
        cache = {k: v for k, v in (cache if isinstance(cache, dict) else {}).items()
                 if time.time() - float((v or {}).get("ts") or 0) < UPLOAD_TTL_S}
        cache[sha] = {"url": public_url, "ts": time.time()}
        self.db.kv_set(UPLOADS_KV, cache)
        return str(public_url)

    # ------------------------------------------------------------------ body
    def ai_disclosure(self, plan: PostPlan) -> bool:
        """True when the brand's AI label applies to this clip.

        Applies when the plan says the clip has AI shots (``extra`` keys ``ai_label``,
        ``ai_generated``, ``synthetic_media`` or ``ai_disclosure``), or when ``brand.ai_label``
        is the boolean ``true`` (label every clip).
        """
        extra = plan.extra or {}
        if any(bool(extra.get(k)) for k in ("ai_label", "ai_generated", "synthetic_media",
                                            "ai_disclosure")):
            return True
        return self.settings.get("brand.ai_label") is True

    def schedule_time(self, plan: PostPlan) -> datetime | None:
        """Aware UTC time for Zernio's ``scheduledFor``; None = publish now."""
        if (plan.extra or {}).get("publish_now"):
            return None
        when = parse_when(plan.scheduled_at, self.settings.get("publishing.timezone", "UTC"))
        now = datetime.now(timezone.utc)
        if when and when > now + timedelta(seconds=MIN_SCHEDULE_AHEAD_S):
            return when
        return None

    def _owner_consented(self, plan: PostPlan) -> bool:
        """TikTok's consent flags may be true only for content the owner approved himself:
        the plan carries ``extra.owner_approved`` (set by the pipeline when it carries out the
        owner's publish approval). The CURRENT ``publishing.mode`` says nothing about this
        post, so it is not consulted."""
        return (plan.extra or {}).get("owner_approved") is True

    def _youtube_fields(self, plan: PostPlan) -> tuple[dict, str, list[str]]:
        from .youtube import YouTubeAdapter, YouTubeShortsAdapter

        direct = (YouTubeShortsAdapter if self.key == "youtube_shorts"
                  else YouTubeAdapter)(self.settings)
        snippet = direct.build_body(plan)["snippet"]  # same limits as the direct uploads
        visibility = str(self.platform_config.get("privacy", "public") or "public")
        psd = {"title": snippet["title"],
               "visibility": visibility if visibility in YOUTUBE_VISIBILITY else "public",
               "madeForKids": False,
               "containsSyntheticMedia": self.ai_disclosure(plan),
               "categoryId": snippet.get("categoryId") or "20"}
        return psd, snippet["description"], list(snippet.get("tags") or [])

    def _tiktok_fields(self, plan: PostPlan, account_id: str) -> tuple[dict, str]:
        if not self._owner_consented(plan):
            raise ZernioError("טיקטוק דורש שתאשר בעצמך כל העלאה — עבור למצב \"אני מאשר כל "
                              "העלאה\" ואשר את הפוסט (TikTok consent)")
        info = self._request("GET", f"/v1/accounts/{account_id}/tiktok/creator-info",
                             "TikTok creator info")
        levels = [str(lv.get("value")) for lv in info.get("privacyLevels") or []
                  if isinstance(lv, dict) and lv.get("value")]
        want = str(self.platform_config.get("privacy_level") or TIKTOK_DEFAULT_PRIVACY)
        if levels and want not in levels:
            raise ZernioError(f"טיקטוק לא מאפשר לחשבון הזה את הפרטיות {want} "
                              f"(אפשר: {', '.join(levels)})")
        if (info.get("creator") or {}).get("canPostMore") is False:
            raise ZernioError("טיקטוק אומר שהחשבון לא יכול להעלות עוד כרגע — נסה שוב מחר "
                              "(canPostMore=false)")
        try:
            cover_ms = max(0, int((plan.extra or {}).get("cover_ms", 1000)))
        except (TypeError, ValueError):
            cover_ms = 1000
        # The two consent flags are true only because the owner approved this exact post
        # (preview + caption) — _owner_consented() refuses otherwise.
        psd = {"privacyLevel": want, "allowComment": True, "allowDuet": False,
               "allowStitch": False, "commercialContentType": "none",
               "contentPreviewConfirmed": True, "expressConsentGiven": True,
               "videoMadeWithAi": self.ai_disclosure(plan), "videoCoverTimestampMs": cover_ms}
        return psd, build_caption(plan, 2200)

    def build_post_body(self, plan: PostPlan, media_url: str, sha: str,
                        account_id: str) -> dict:
        """The ``POST /v1/posts`` body: one platform entry (TikTok also checks creator info)."""
        tags: list[str] = []
        if self.zernio_platform == "youtube":
            psd, content, tags = self._youtube_fields(plan)
        else:
            psd, content = self._tiktok_fields(plan, account_id)
        body: dict = {
            "content": content,
            "mediaItems": [{"type": "video", "url": media_url}],
            "platforms": [{"platform": self.zernio_platform, "accountId": account_id,
                           "platformSpecificData": psd}],
            "timezone": self.settings.get("publishing.timezone", "Asia/Jerusalem"),
            "metadata": {"agency_sha": sha[:16], "agency_platform": self.key},
        }
        if tags:
            body["tags"] = tags
        when = self.schedule_time(plan)
        if when is not None:
            body["scheduledFor"] = when.replace(microsecond=0).isoformat()
        else:
            body["publishNow"] = True
        return body

    # ------------------------------------------------------------------ post state
    def _entry(self, post: dict) -> dict:
        for entry in post.get("platforms") or []:
            if isinstance(entry, dict) and entry.get("platform") == self.zernio_platform:
                return entry
        return {}

    @staticmethod
    def state_of(post: dict, entry: dict) -> str:
        """published | failed | cancelled | draft | scheduled | processing."""
        st = str(entry.get("status") or "").lower()
        post_st = str(post.get("status") or "").lower()
        if st in ("published", "failed", "cancelled"):
            return st
        if not st and post_st in ("published", "failed", "cancelled", "draft"):
            return post_st
        if post_st == "draft":
            return "draft"
        if post_st == "scheduled" or st == "scheduled":
            return "scheduled"
        return "processing"

    def _native_url(self, entry: dict) -> str | None:
        url = entry.get("platformPostUrl")
        if url:
            return str(url)
        native = entry.get("platformPostId")
        if not native:
            return None
        if self.zernio_platform == "youtube":
            return (f"https://www.youtube.com/shorts/{native}" if self.key == "youtube_shorts"
                    else f"https://www.youtube.com/watch?v={native}")
        username = ((self.db.kv_get(ACCOUNTS_KV) or {}).get("map") or {}) \
            .get("tiktok", {}).get("username")
        return f"https://www.tiktok.com/@{username}/video/{native}" if username else None

    def _remember(self, post_id: str, **fields) -> None:
        posts = self.db.kv_get(POSTS_KV) or {}
        if not isinstance(posts, dict):
            posts = {}
        now = time.time()
        posts = {k: v for k, v in posts.items()
                 if now - float((v or {}).get("ts") or now) < POSTS_KEEP_S}
        rec = {**(posts.get(post_id) or {}), "platform": self.key, **fields, "ts": now}
        posts[post_id] = rec
        self.db.kv_set(POSTS_KV, posts)

    def _find_post(self, external_id: str) -> tuple[str, dict]:
        """Zernio post id + record for an external id (native id or Zernio id)."""
        posts = self.db.kv_get(POSTS_KV) or {}
        if isinstance(posts, dict):
            if external_id in posts:
                return external_id, posts[external_id]
            for post_id, rec in posts.items():
                if (rec or {}).get("native_id") == external_id:
                    return post_id, rec
        return external_id, {}

    def _idempotency_key(self, sha: str) -> str:
        base = f"{sha[:24]}:{self.key}"
        attempts = (self.db.kv_get(ATTEMPTS_KV) or {}).get(base, 0)
        return base if not attempts else f"{base}:r{attempts}"

    def _bump_attempt(self, sha: str) -> None:
        """After a CONFIRMED failure a retry needs a fresh key (the old one replays the failure)."""
        attempts = self.db.kv_get(ATTEMPTS_KV) or {}
        base = f"{sha[:24]}:{self.key}"
        attempts[base] = int(attempts.get(base, 0)) + 1
        self.db.kv_set(ATTEMPTS_KV, attempts)

    def _poll(self, post_id: str, scheduled: bool = False) -> tuple[dict, dict, str]:
        """GET /v1/posts/{id} until a final state or ``poll_max_s`` runs out.

        For a post sent with ``scheduledFor``, "scheduled" is final too (it waits in the cloud).
        """
        final = {"published", "failed", "cancelled", "draft"} | ({"scheduled"} if scheduled
                                                                 else set())
        deadline = time.monotonic() + self._poll_max()
        while True:
            post = self._request("GET", f"/v1/posts/{post_id}", "post status").get("post") or {}
            entry = self._entry(post)
            state = self.state_of(post, entry)
            if state in final or time.monotonic() >= deadline:
                return post, entry, state
            time.sleep(self.poll_interval_s)

    # ------------------------------------------------------------------ upload
    def _earlier_post(self, sha: str) -> tuple[str, dict] | None:
        """The newest Zernio post we created for this file + platform that was not seen to
        fail (created / processing / scheduled / published), or None."""
        posts = self.db.kv_get(POSTS_KV) or {}
        if not isinstance(posts, dict):
            return None
        mine = [(pid, rec) for pid, rec in posts.items() if isinstance(rec, dict)
                and rec.get("platform") == self.key and rec.get("sha") == sha[:24]
                and rec.get("state") in ("created", "processing", "scheduled", "published")]
        if not mine:
            return None
        return max(mine, key=lambda kv: float(kv[1].get("ts") or 0))

    def _adopt_earlier(self, sha: str, plan: PostPlan) -> PostResult | None:
        """Before a new POST: what Zernio did with our earlier post of the same file.

        Published -> that post is the result (never a second upload). Scheduled -> reported
        again, unless the owner said "publish now": then that scheduled post is cancelled
        first (so it cannot go out a second time later) and a new one is made. Still
        processing -> reported again (nothing new is created). Failed / cancelled -> None, a
        fresh post may be made. Zernio cannot be asked -> fail closed (never guess).
        """
        earlier = self._earlier_post(sha)
        if earlier is None:
            return None
        post_id, rec = earlier
        post = self._request("GET", f"/v1/posts/{post_id}", "post status").get("post") or {}
        entry = self._entry(post)
        state = self.state_of(post, entry)
        if state == "published":
            native = str(entry.get("platformPostId") or post_id)
            url = self._native_url(entry)
            self._remember(post_id, state="published", native_id=native, url=url)
            self._record_time(native, None)
            return self.success(native, url)
        if state in ("failed", "cancelled", "draft"):
            if rec.get("state") != state:
                self._remember(post_id, state=state)
                self._bump_attempt(sha)
            return None
        if state == "scheduled" and (plan.extra or {}).get("publish_now"):
            self._request("DELETE", f"/v1/posts/{post_id}", "cancel post", retry=False,
                          timeout=DELETE_TIMEOUT)
            self._remember(post_id, state="cancelled")
            self._bump_attempt(sha)
            return None
        if state == "scheduled":
            when = str(post.get("scheduledFor") or rec.get("scheduled_for") or "")
            self._remember(post_id, state="scheduled")
            self.remote_scheduled = {"post_id": post_id, "scheduled_for": when or None}
            return self.success(post_id, None)
        self._remember(post_id, state="processing")
        return PostResult(platform=self.key, ok=False, dry_run=False, external_id=post_id,
                          url=None, error=(
                              f"{self.display_he}: Zernio עדיין מעבד את ההעלאה הקודמת של הקליפ "
                              "הזה — לא מעלה שוב כדי שלא יופיע פעמיים. בדוק שוב עוד כמה דקות. "
                              f"(still processing, post {post_id})"))

    def _upload(self, plan: PostPlan) -> PostResult:
        self.remote_scheduled = None
        path = video_file(plan)
        sha = file_sha256(path)
        earlier = self._adopt_earlier(sha, plan)
        if earlier is not None:
            return earlier
        account = self._account()
        media_url = self._media_url(path, sha)
        body = self.build_post_body(plan, media_url, sha, account["id"])
        scheduled = "scheduledFor" in body
        try:
            try:
                data = self._create_post(body, sha)
            except ZernioError as exc:
                # A stale cached account (reconnected / new key): refresh once and retry.
                if exc.status not in (400, 404) or self.platform_config.get("zernio_account_id"):
                    raise
                fresh = (self.accounts(refresh=True).get(self.zernio_platform) or {}).get("id")
                if not fresh or fresh == account["id"]:
                    raise
                account = {**account, "id": fresh}
                body = self.build_post_body(plan, media_url, sha, fresh)
                data = self._create_post(body, sha)
            post = data.get("post") or {}
        except ZernioError as exc:
            existing = ((exc.body or {}).get("details") or {}).get("existingPostId")
            if exc.status != 409 or not existing:
                raise
            post = {"_id": str(existing)}  # same content already created: adopt, then poll
        post_id = str(post.get("_id") or "")
        if not post_id:
            raise ZernioError("Zernio לא החזיר מזהה פוסט (create post)")
        self._remember(post_id, sha=sha[:24], state="created")

        entry = self._entry(post)
        state = self.state_of(post, entry) if (entry or post.get("status")) else "processing"
        final = ("published", "failed", "cancelled", "draft") + (("scheduled",) if scheduled
                                                                 else ())
        if state not in final:
            try:
                post, entry, state = self._poll(post_id, scheduled=scheduled)
            except ZernioError as exc:
                return PostResult(platform=self.key, ok=False, dry_run=False,
                                  external_id=post_id, url=None, error=(
                                      f"{self.display_he}: Zernio קיבל את הפוסט אבל לא הצלחתי "
                                      f"לבדוק אם עלה — {exc}. בדוק באפליקציה לפני ניסיון "
                                      "חוזר (ניסיון חוזר לא יעלה פעמיים)."))
        if state == "published":
            native = str(entry.get("platformPostId") or post_id)
            url = self._native_url(entry)
            self._remember(post_id, state="published", native_id=native, url=url)
            self._record_time(native, None)
            return self.success(native, url)
        if state in ("failed", "cancelled", "draft"):
            self._remember(post_id, state=state)
            self._bump_attempt(sha)
            if state == "failed":
                return self.fail(self._platform_error(entry))
            if state == "cancelled":
                return self.fail(f"הפוסט ל{self.display_he} בוטל ב-Zernio (cancelled)")
            return self.fail(f"Zernio שמר את הפוסט ל{self.display_he} כטיוטה בלבד — הוא לא "
                             "יעלה לבד (draft)")
        if scheduled and state == "scheduled":
            when = body["scheduledFor"]
            self._remember(post_id, state="scheduled", scheduled_for=when)
            log.info("%s: scheduled at Zernio for %s (post %s)", self.key, when, post_id)
            # Zernio's cloud publishes it at that time; it is NOT published yet: the publisher
            # keeps the post "scheduled" and the scheduler reconciles it (post_status).
            self.remote_scheduled = {"post_id": post_id, "scheduled_for": when}
            return self.success(post_id, None)
        waited = self._poll_max()
        self._remember(post_id, state="processing")
        return PostResult(platform=self.key, ok=False, dry_run=False, external_id=post_id,
                          url=None, error=(
                              f"{self.display_he}: Zernio עדיין מעבד את ההעלאה אחרי "
                              f"{waited:.0f} שניות — זה עוד לא אושר כפורסם. אבדוק שוב לבד "
                              "בעוד כמה דקות; ניסיון חוזר רק בודק את אותו פוסט ולא מעלה "
                              f"פעמיים. (still processing, post {post_id})"))

    def _create_post(self, body: dict, sha: str) -> dict:
        return self._request("POST", "/v1/posts", "create post", retry=False,
                             timeout=POST_TIMEOUT, json=body,
                             headers={"Idempotency-Key": self._idempotency_key(sha)})

    def _record_time(self, external_id: str, when: str | None) -> None:
        try:
            from .instagram import record_published_at
            record_published_at(self.db, self.key, external_id, when)
        except Exception:  # bookkeeping only; never fail an upload for it
            log.debug("could not record publish time", exc_info=True)

    # ------------------------------------------------------------------ after publishing
    def post_status(self, external_id: str) -> dict:
        """Read-only refresh of a Zernio post: ``{state, url, native_id, error}``.

        Use it to reconcile posts scheduled in Zernio's cloud (their native id is known
        only once they are out). ``{}`` in training mode / without the key.
        """
        if not self.can_call_api(external_id):
            return {}
        post_id, rec = self._find_post(str(external_id))
        post = self._request("GET", f"/v1/posts/{post_id}", "post status").get("post") or {}
        entry = self._entry(post)
        state = self.state_of(post, entry)
        native = entry.get("platformPostId")
        out = {"state": state, "url": self._native_url(entry), "native_id": native,
               "error": self._platform_error(entry) if state == "failed" else None}
        if state == "published" and rec.get("state") != "published":
            self._record_time(str(native or post_id), None)
        if state in ("failed", "cancelled", "draft") and rec.get("state") not in (
                "failed", "cancelled", "draft") and rec.get("sha"):
            self._bump_attempt(str(rec["sha"]))  # a retry needs a fresh Idempotency-Key
        self._remember(post_id, state=state, **({"native_id": str(native)} if native else {}))
        return out

    def unpublish(self, external_id: str) -> dict:
        """Take a post down WITHOUT hard deletes: ``{"ok", "action", "error"}``.

        Not out yet -> the Zernio post is cancelled (DELETE /v1/posts/{id}). Already live on
        YouTube -> made PRIVATE through the owner's Google connection (Zernio's unpublish on
        YouTube is a hard delete, so it is never used). TikTok cannot be taken down through
        Zernio: the owner gets a plain instruction instead. Action: cancelled | made_private
        | none.
        """
        if not self.can_call_api(external_id):
            return {"ok": False, "action": "none",
                    "error": "מצב אימון או חיבור חסר — לא בוצע כלום"}
        post_id, rec = self._find_post(str(external_id))
        try:
            post = self._request("GET", f"/v1/posts/{post_id}", "post status").get("post") or {}
        except ZernioError as exc:
            return {"ok": False, "action": "none", "error": str(exc)}
        entry = self._entry(post)
        state = self.state_of(post, entry)
        if state in ("failed", "cancelled"):
            return {"ok": True, "action": "none", "error": None}
        if state != "published":
            try:
                self._request("DELETE", f"/v1/posts/{post_id}", "cancel post", retry=False,
                              timeout=DELETE_TIMEOUT)
            except ZernioError as exc:
                return {"ok": False, "action": "none", "error": str(exc)}
            self._remember(post_id, state="cancelled")
            return {"ok": True, "action": "cancelled", "error": None}
        native = str(entry.get("platformPostId") or rec.get("native_id") or "")
        if self.zernio_platform == "youtube" and native:
            if self._direct_youtube().make_private(native):
                return {"ok": True, "action": "made_private", "error": None}
            return {"ok": False, "action": "none",
                    "error": "כדי להסתיר סרטון ביוטיוב צריך חיבור ישיר לגוגל (עמוד החיבורים)"}
        return {"ok": False, "action": "none",
                "error": "סרטון בטיקטוק אפשר להסתיר רק באפליקציה: פתח את הסרטון ← ⋯ ← "
                         "הגדרות פרטיות ← רק אני"}

    def _direct_youtube(self):
        """The owner's Google connection for THIS key (Shorts follows its own live switch, so
        a Shorts-only live switch still gets its stats / comments / make-private)."""
        from .youtube import YouTubeAdapter, YouTubeShortsAdapter
        return (YouTubeShortsAdapter if self.key == "youtube_shorts"
                else YouTubeAdapter)(self.settings)

    def fetch_stats(self, external_id: str) -> dict:
        """YouTube stats come from the owner's Google connection (native video id);
        Zernio's analytics add-on is not used, so TikTok returns {}."""
        if not self.can_call_api(external_id) or self.zernio_platform != "youtube":
            return {}
        return self._direct_youtube().fetch_stats(self._native_id(external_id))

    def fetch_comments(self, external_id: str, limit: int = 20) -> list[dict]:
        if not self.can_call_api(external_id) or self.zernio_platform != "youtube":
            return []
        return self._direct_youtube().fetch_comments(self._native_id(external_id), limit)

    def _native_id(self, external_id: str) -> str:
        """The platform's own id (a post scheduled in Zernio's cloud is stored by its Zernio
        id until :meth:`post_status` learns the native one)."""
        _post_id, rec = self._find_post(str(external_id))
        return str(rec.get("native_id") or external_id)

    def reply_comment(self, comment_id: str, text: str) -> bool:
        if self.dry_run or self.zernio_platform != "youtube":
            return False
        return self._direct_youtube().reply_comment(comment_id, text)


def zernio_adapter(settings: Settings, key: str) -> ZernioAdapter:
    """Convenience constructor (``key`` in :data:`ZERNIO_TARGETS`)."""
    return ZernioAdapter(settings, key)
