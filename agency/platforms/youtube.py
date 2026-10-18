"""YouTube + YouTube Shorts via the YouTube Data API v3 (resumable ``videos.insert``).

OAuth: an installed-app ("Desktop") client JSON from Google Cloud Console, path in
YOUTUBE_CLIENT_SECRETS_FILE. The first ``authorize()`` (or first real upload) opens the browser
once; the token is stored in ``<data_dir>/youtube_token.json`` and refreshed automatically.
A future ``scheduled_at`` uploads as private with ``status.publishAt`` (YouTube publishes it).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from ..config import secret
from ..models import PostPlan, PostResult
from ._common import (ApiError, RealAdapter, normalize_hashtags, parse_when, truncate,
                      truncate_utf8, utf8_len, video_file)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.force-ssl"]
TITLE_LIMIT = 100  # characters
DESCRIPTION_LIMIT = 5000  # BYTES of UTF-8 (API: "maximum length of 5000 bytes")
TAGS_TOTAL_LIMIT = 500
GAMING_CATEGORY_ID = "20"
CHUNK_SIZE = 8 * 1024 * 1024
HTTP_TIMEOUT_S = 120
OAUTH_TIMEOUT_S = 300


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


class YouTubeAdapter(RealAdapter):
    key = "youtube"
    display_he = "יוטיוב"
    required_secrets = ["YOUTUBE_CLIENT_SECRETS_FILE"]
    shorts = False

    @property
    def token_path(self) -> Path:
        return self.settings.data_dir / "youtube_token.json"

    # ------------------------------------------------------------------ auth
    def _credentials(self, interactive: bool):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        creds = None
        if self.token_path.exists():
            creds = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)
        if creds and creds.valid:
            return creds
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        elif interactive:
            from google_auth_oauthlib.flow import InstalledAppFlow

            secrets_file = Path(secret("YOUTUBE_CLIENT_SECRETS_FILE") or "")
            if not secrets_file.is_file():
                raise FileNotFoundError("קובץ YOUTUBE_CLIENT_SECRETS_FILE לא נמצא")
            flow = InstalledAppFlow.from_client_secrets_file(str(secrets_file), SCOPES)
            creds = flow.run_local_server(port=0, open_browser=True,
                                          timeout_seconds=OAUTH_TIMEOUT_S)
        else:
            raise ApiError("YouTube not authorized yet — run the YouTube login first")
        if creds is None:
            raise ApiError("YouTube authorization did not complete")
        self.token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds

    def authorize(self) -> bool:
        """Interactive one-time login (opens the browser). Returns True on success."""
        self._credentials(interactive=True)
        return True

    def _service(self, interactive: bool = False):
        import google_auth_httplib2
        import httplib2
        from googleapiclient.discovery import build

        http = google_auth_httplib2.AuthorizedHttp(self._credentials(interactive),
                                                   http=httplib2.Http(timeout=HTTP_TIMEOUT_S))
        return build("youtube", "v3", http=http, cache_discovery=False)

    # ---------------------------------------------------------------- upload
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
        tags_line = " ".join(hashtags)
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
                   "categoryId": str(plan.extra.get("category_id", GAMING_CATEGORY_ID))}
        language = self.settings.get("channel.language")
        if language:
            snippet["defaultLanguage"] = snippet["defaultAudioLanguage"] = language
        return {"snippet": snippet, "status": status}

    def _upload(self, plan: PostPlan) -> PostResult:
        from googleapiclient.http import MediaFileUpload

        path = video_file(plan)
        youtube = self._service(interactive=True)
        media = MediaFileUpload(str(path), mimetype="video/*", chunksize=CHUNK_SIZE, resumable=True)
        request = youtube.videos().insert(part="snippet,status", body=self.build_body(plan),
                                          media_body=media, notifySubscribers=True)
        response = None
        while response is None:
            _status, response = request.next_chunk(num_retries=3)
        video_id = (response or {}).get("id")
        if not video_id:
            raise ApiError("YouTube upload returned no video id")
        thumb = plan.extra.get("thumbnail")
        if thumb and Path(thumb).is_file():
            try:  # needs a verified channel; failure must not fail the upload
                youtube.thumbnails().set(videoId=video_id,
                                         media_body=MediaFileUpload(str(thumb))).execute()
            except Exception:
                pass
        url = (f"https://www.youtube.com/shorts/{video_id}" if self.shorts
               else f"https://www.youtube.com/watch?v={video_id}")
        return self.success(video_id, url)

    # --------------------------------------------------------------- stats / comments
    def _ready(self, external_id: str) -> bool:
        return self.can_call_api(external_id) and self.token_path.exists()

    def fetch_stats(self, external_id: str) -> dict:
        if not self._ready(external_id):
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
        if not self._ready(external_id):
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
        if self.dry_run or not self.configured() or not comment_id \
                or not self.token_path.exists():
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
