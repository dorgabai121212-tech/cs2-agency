"""YouTube + YouTube Shorts.

Publishing goes through **Zernio** by default (``publishing.platforms.<key>.via: zernio``;
the registry then builds ``zernio.ZernioAdapter``): uploads from an unverified Google Cloud
project are locked private for good. This module is the *direct* YouTube Data API v3 path:

- ``via: direct``: resumable ``videos.insert`` (the connect wizard warns that unverified
  projects publish as private). A future ``scheduled_at`` uploads as private with
  ``status.publishAt``.
- always: stats, comments, replies and :meth:`YouTubeAdapter.make_private` (used instead of
  deletes — videos are never hard-deleted) through the owner's Google connection.

Credentials (never logged): ``data/secrets/youtube_token.json`` (authorized-user JSON; refreshed
automatically and written back; an old ``data/youtube_token.json`` is moved there), else the
``YOUTUBE_OAUTH_JSON`` secret (``{client_id, client_secret, refresh_token}``, e.g. the old
studio's ``youtube_oauth`` keyring entry; read-only, never written back). The one-time login
(:meth:`YouTubeAdapter.authorize`) uses the client secrets from
``data/secrets/youtube_client_secret.json`` or ``YOUTUBE_CLIENT_SECRETS_FILE``.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from ..config import secret
from ..models import PostPlan, PostResult
from ._common import (ApiError, RealAdapter, normalize_hashtags, parse_when, truncate,
                      truncate_utf8, utf8_len, video_file)

log = logging.getLogger("agency.platforms")

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly",
          "https://www.googleapis.com/auth/youtube.force-ssl"]
TOKEN_URI = "https://oauth2.googleapis.com/token"
TOKEN_FILE = "youtube_token.json"
CLIENT_SECRET_FILE = "youtube_client_secret.json"
TITLE_LIMIT = 100  # characters
DESCRIPTION_LIMIT = 5000  # BYTES of UTF-8 (API: "maximum length of 5000 bytes")
TAGS_TOTAL_LIMIT = 500
GAMING_CATEGORY_ID = "20"
CHUNK_SIZE = 8 * 1024 * 1024
HTTP_TIMEOUT_S = 120
OAUTH_TIMEOUT_S = 300

NOT_CONNECTED_HE = "יוטיוב לא מחובר — התחבר עם גוגל בעמוד החיבורים"


def clean_tags(tags: list[str], limit: int = TAGS_TOTAL_LIMIT) -> list[str]:
    """Tags without '<>' and '#', total length (quoted multi-word tags count +2) ≤ ``limit``."""
    out: list[str] = []
    total = 0
    for raw in tags or []:
        tag = re.sub(r"[<>,#]", "", str(raw)).strip()
        if not tag or tag in out:
            continue
        cost = len(tag) + (2 if " " in tag else 0) + (1 if out else 0)
        if total + cost > limit:
            break
        out.append(tag)
        total += cost
    return out


def _oauth_info(raw: str | None) -> dict | None:
    """Authorized-user info from a JSON secret, or None when unusable (never raises)."""
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(info, dict):
        return None
    if all(info.get(k) for k in ("refresh_token", "client_id", "client_secret")):
        return info
    return None


class YouTubeAdapter(RealAdapter):
    key = "youtube"
    display_he = "יוטיוב"
    required_secrets = ["YOUTUBE_CLIENT_SECRETS_FILE"]
    # Also read (not required): YOUTUBE_OAUTH_JSON (an existing Google login).
    optional_secrets = ["YOUTUBE_OAUTH_JSON"]
    shorts = False

    # ------------------------------------------------------------------ connection
    def missing_secrets(self) -> list[str]:
        """Direct path needs a Google login (token file or YOUTUBE_OAUTH_JSON); until then the
        missing piece is the client file used to log in."""
        return [] if self.has_google_credentials() else ["YOUTUBE_CLIENT_SECRETS_FILE"]

    def configured(self) -> bool:
        """A Google login is available (uploads via direct, stats, comments, make_private)."""
        return self.has_google_credentials()

    # ------------------------------------------------------------------ credentials
    @property
    def secrets_path(self) -> Path:
        from ..settings_store import secrets_dir
        return secrets_dir(self.settings)

    @property
    def token_path(self) -> Path:
        """``data/secrets/youtube_token.json`` (the old ``data/youtube_token.json`` moves here)."""
        path = self.secrets_path / TOKEN_FILE
        self._migrate_old_token(path)
        return path

    def _migrate_old_token(self, new: Path) -> None:
        old = self.settings.data_dir / TOKEN_FILE
        if not old.is_file() or new.exists():
            return
        try:
            from ..settings_store import write_private_file
            write_private_file(new, old.read_text(encoding="utf-8"))
            old.unlink()
            log.info("moved the YouTube login to the secrets folder")
        except OSError as exc:  # keep the old one; it still works
            log.warning("could not move the YouTube login file: %s", type(exc).__name__)

    def client_secrets_path(self) -> Path | None:
        """Google OAuth client JSON: YOUTUBE_CLIENT_SECRETS_FILE, else data/secrets."""
        value = self.credential("YOUTUBE_CLIENT_SECRETS_FILE")
        if value:
            path = Path(value).expanduser()
            if path.is_file():
                return path
        local = self.secrets_path / CLIENT_SECRET_FILE
        return local if local.is_file() else None

    def has_google_credentials(self) -> bool:
        """A Google login is available (token file or YOUTUBE_OAUTH_JSON). No network."""
        return self.token_path.is_file() or _oauth_info(secret("YOUTUBE_OAUTH_JSON")) is not None

    def _credentials(self, interactive: bool = False):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        creds, source = None, ""
        if self.token_path.is_file():
            info = json.loads(self.token_path.read_text(encoding="utf-8"))
            creds, source = Credentials.from_authorized_user_info(info), "file"
        else:
            info = _oauth_info(secret("YOUTUBE_OAUTH_JSON"))
            if info:
                creds = Credentials(token=None, refresh_token=info["refresh_token"],
                                    client_id=info["client_id"],
                                    client_secret=info["client_secret"],
                                    token_uri=info.get("token_uri") or TOKEN_URI,
                                    scopes=info.get("scopes"))
                source = "secret"
        if creds and creds.valid:
            return creds
        if creds and creds.refresh_token:
            creds.refresh(Request())
            if source == "file":  # persist the refreshed token (never into the old studio)
                self._save(creds)
            return creds
        if not interactive:
            raise ApiError(NOT_CONNECTED_HE)
        from google_auth_oauthlib.flow import InstalledAppFlow

        client = self.client_secrets_path()
        if client is None:
            raise FileNotFoundError("קובץ ההתחברות של גוגל (client_secret) לא נמצא")
        flow = InstalledAppFlow.from_client_secrets_file(str(client), SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True, timeout_seconds=OAUTH_TIMEOUT_S)
        if creds is None:
            raise ApiError("ההתחברות לגוגל לא הושלמה")
        self._save(creds)
        return creds

    def _save(self, creds) -> None:
        from ..settings_store import write_private_file
        write_private_file(self.secrets_path / TOKEN_FILE, creds.to_json())

    def authorize(self) -> bool:
        """Interactive one-time Google login (opens the browser). Returns True on success."""
        self._credentials(interactive=True)
        return True

    def _service(self, interactive: bool = False):
        import google_auth_httplib2
        import httplib2
        from googleapiclient.discovery import build

        http = google_auth_httplib2.AuthorizedHttp(self._credentials(interactive),
                                                   http=httplib2.Http(timeout=HTTP_TIMEOUT_S))
        return build("youtube", "v3", http=http, cache_discovery=False)

    def _google_ready(self, external_id: str | None) -> bool:
        """Real id, platform live, Google login present (no network)."""
        return bool(external_id) and not str(external_id).startswith("dryrun-") \
            and not self.dry_run and self.has_google_credentials()

    # ---------------------------------------------------------------- upload (via: direct)
    def build_body(self, plan: PostPlan) -> dict:
        """videos.insert request body with YouTube limits enforced."""
        title = re.sub(r"[<>]", "", plan.title or "").strip() or "Counter-Strike 2"
        description = re.sub(r"[<>]", "", plan.description or "").strip()
        hashtags = normalize_hashtags(plan.hashtags, 15)
        if self.shorts and "#shorts" not in (title + " " + description).lower():
            if len(title) + len(" #Shorts") <= TITLE_LIMIT:
                title = f"{title} #Shorts"
            else:
                hashtags = ["#Shorts", *[h for h in hashtags if h.lower() != "#shorts"]]
        present = {t.lower() for t in re.findall(r"#\w+", description)}
        tags_line = " ".join(h for h in hashtags if h.lower() not in present)
        if tags_line:
            room = DESCRIPTION_LIMIT - utf8_len(tags_line) - 2
            description = f"{truncate_utf8(description, room)}\n\n{tags_line}"
        status: dict = {"privacyStatus": self.platform_config.get("privacy", "public"),
                        "selfDeclaredMadeForKids": False, "embeddable": True}
        when = parse_when(plan.scheduled_at, self.settings.get("publishing.timezone", "UTC"))
        publish_now = bool((plan.extra or {}).get("publish_now"))
        if when and when > datetime.now(timezone.utc) and not publish_now:
            status["privacyStatus"] = "private"
            status["publishAt"] = when.strftime("%Y-%m-%dT%H:%M:%SZ")
        snippet = {"title": truncate(title, TITLE_LIMIT),
                   "description": truncate_utf8(description.strip(), DESCRIPTION_LIMIT),
                   "tags": clean_tags(plan.tags),
                   "categoryId": str((plan.extra or {}).get("category_id", GAMING_CATEGORY_ID))}
        # Public copy is English by brand rule (the owner-facing side is Hebrew), so the
        # metadata language is the copy's language, not ``channel.language``.
        snippet["defaultLanguage"] = str((plan.extra or {}).get("copy_language") or "en")
        return {"snippet": snippet, "status": status}

    def _upload(self, plan: PostPlan) -> PostResult:
        from googleapiclient.http import MediaFileUpload

        path = video_file(plan)
        log.warning("%s: direct upload (via: direct) — unverified Google projects publish as "
                    "private", self.key)
        youtube = self._service(interactive=False)  # never opens a browser from a worker
        media = MediaFileUpload(str(path), mimetype="video/*", chunksize=CHUNK_SIZE, resumable=True)
        request = youtube.videos().insert(part="snippet,status", body=self.build_body(plan),
                                          media_body=media, notifySubscribers=True)
        response = None
        while response is None:
            _status, response = request.next_chunk(num_retries=3)
        video_id = (response or {}).get("id")
        if not video_id:
            raise ApiError("YouTube upload returned no video id")
        thumb = (plan.extra or {}).get("thumbnail")
        if thumb and Path(thumb).is_file():
            try:  # needs a verified channel; failure must not fail the upload
                youtube.thumbnails().set(videoId=video_id,
                                         media_body=MediaFileUpload(str(thumb))).execute()
            except Exception:
                pass
        try:
            from ..db import DB
            from .instagram import record_published_at
            record_published_at(DB(self.settings.db_path), self.key, str(video_id))
        except Exception:  # bookkeeping only
            log.debug("could not record publish time", exc_info=True)
        url = (f"https://www.youtube.com/shorts/{video_id}" if self.shorts
               else f"https://www.youtube.com/watch?v={video_id}")
        return self.success(video_id, url)

    # --------------------------------------------------------------- channel / privacy
    def channel_info(self) -> dict:
        """``{"ok", "id", "title", "thumbnail", "problem"}`` for the connect wizard (read-only)."""
        if not self.has_google_credentials():
            return {"ok": False, "id": "", "title": "", "thumbnail": "",
                    "problem": NOT_CONNECTED_HE}
        try:
            items = self._service().channels().list(part="id,snippet", mine=True) \
                .execute().get("items", [])
        except Exception as exc:
            return {"ok": False, "id": "", "title": "", "thumbnail": "",
                    "problem": self.scrub(f"החיבור לגוגל לא עבד ({type(exc).__name__})")}
        if not items:
            return {"ok": False, "id": "", "title": "", "thumbnail": "",
                    "problem": "לחשבון הגוגל הזה אין ערוץ יוטיוב"}
        sn = items[0].get("snippet") or {}
        thumbs = sn.get("thumbnails") or {}
        thumb = (thumbs.get("default") or thumbs.get("medium") or {}).get("url", "")
        return {"ok": True, "id": items[0].get("id", ""), "title": sn.get("title", ""),
                "thumbnail": thumb, "problem": None}

    def make_private(self, external_id: str) -> bool:
        """Make a published video PRIVATE (reversible; used instead of deleting). True on success.

        Only for real video ids on a live platform with a Google login; never deletes.
        """
        if not self._google_ready(external_id):
            return False
        try:
            service = self._service()
            items = service.videos().list(part="status", id=str(external_id)) \
                .execute().get("items", [])
            if not items:
                return False
            status = dict(items[0].get("status") or {})
            if status.get("privacyStatus") == "private" and "publishAt" not in status:
                return True
            status["privacyStatus"] = "private"
            status.pop("publishAt", None)  # a pending scheduled release must not undo it
            service.videos().update(part="status",
                                    body={"id": str(external_id), "status": status}).execute()
            return True
        except Exception as exc:
            log.warning("%s: make_private failed: %s", self.key, type(exc).__name__)
            return False

    # --------------------------------------------------------------- stats / comments
    def fetch_stats(self, external_id: str) -> dict:
        if not self._google_ready(external_id):
            return {}
        try:
            items = self._service().videos().list(part="statistics", id=external_id) \
                .execute().get("items", [])
        except Exception:
            return {}
        if not items:
            return {}
        s = items[0].get("statistics", {})
        return {"views": int(s.get("viewCount", 0)), "likes": int(s.get("likeCount", 0)),
                "comments": int(s.get("commentCount", 0)), "shares": 0}

    def fetch_comments(self, external_id: str, limit: int = 20) -> list[dict]:
        if not self._google_ready(external_id):
            return []
        try:
            data = self._service().commentThreads().list(
                part="snippet", videoId=external_id, maxResults=max(1, min(limit, 100)),
                order="time", textFormat="plainText").execute()
        except Exception:
            return []
        out = []
        for item in data.get("items", []):
            top = item["snippet"]["topLevelComment"]
            sn = top.get("snippet", {})
            out.append({"id": top.get("id"), "author": sn.get("authorDisplayName", ""),
                        "text": sn.get("textDisplay", ""), "published_at": sn.get("publishedAt"),
                        "likes": sn.get("likeCount", 0)})
        return out[:limit]

    def reply_comment(self, comment_id: str, text: str) -> bool:
        if self.dry_run or not comment_id or not self.has_google_credentials():
            return False
        try:
            self._service().comments().insert(part="snippet", body={
                "snippet": {"parentId": comment_id, "textOriginal": text[:10000]}}).execute()
            return True
        except Exception:
            return False


class YouTubeShortsAdapter(YouTubeAdapter):
    """Same API; vertical ≤60s variant, '#Shorts' added to title or description."""

    key = "youtube_shorts"
    display_he = "יוטיוב שורטס"
    shorts = True
