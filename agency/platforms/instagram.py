"""Instagram Reels via the Instagram Graph API (resumable upload).

Flow: POST /{ig-user-id}/media (media_type=REELS, upload_type=resumable) -> POST the file to the
returned rupload.facebook.com URI -> poll the container's status_code until FINISHED ->
POST /{ig-user-id}/media_publish -> read the permalink.
Needs an Instagram professional account linked to a Facebook Page and a token with
instagram_basic + instagram_content_publish (+ instagram_manage_comments for replies).
"""
from __future__ import annotations

import time

from ..config import secret
from ..models import PostPlan, PostResult
from ._common import ApiError, RealAdapter, build_caption, check_response, video_file

GRAPH_API_VERSION = "v23.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
RUPLOAD_BASE = f"https://rupload.facebook.com/ig-api-upload/{GRAPH_API_VERSION}"
CAPTION_LIMIT = 2200
MAX_HASHTAGS = 30


class InstagramReelsAdapter(RealAdapter):
    key = "instagram_reels"
    display_he = "אינסטגרם רילס"
    required_secrets = ["IG_USER_ID", "META_ACCESS_TOKEN"]

    poll_interval_s: float = 5.0
    poll_max_s: float = 300.0

    def _auth(self) -> dict:
        # Token in a header (not the query string) so it never shows up in URLs / errors.
        return {"Authorization": f"OAuth {secret('META_ACCESS_TOKEN')}"}

    def _graph(self, method: str, path: str, what: str, **kwargs) -> dict:
        import requests

        resp = requests.request(method, f"{GRAPH_BASE}/{path.lstrip('/')}", headers=self._auth(),
                                timeout=self.timeout(), **kwargs)
        return check_response(resp, what)

    def _upload(self, plan: PostPlan) -> PostResult:
        import requests

        path = video_file(plan)
        size = path.stat().st_size
        ig_user = secret("IG_USER_ID")
        caption = build_caption(plan, CAPTION_LIMIT, max_hashtags=MAX_HASHTAGS)

        container = self._graph("POST", f"{ig_user}/media", "Instagram create container", data={
            "media_type": "REELS", "upload_type": "resumable", "caption": caption,
            "share_to_feed": "true" if self.platform_config.get("share_to_feed", True) else "false",
        })
        container_id = container.get("id")
        if not container_id:
            raise ApiError("Instagram create container returned no id")
        upload_uri = container.get("uri") or f"{RUPLOAD_BASE}/{container_id}"

        with path.open("rb") as fh:
            resp = requests.post(upload_uri, data=fh, timeout=self.timeout(), headers={
                **self._auth(), "offset": "0", "file_size": str(size),
                "Content-Type": "application/octet-stream", "Content-Length": str(size)})
        uploaded = check_response(resp, "Instagram rupload")
        if uploaded.get("success") is False:
            raise ApiError(f"Instagram rupload failed: {uploaded.get('debug_info') or uploaded}")

        self._wait_finished(container_id)
        published = self._graph("POST", f"{ig_user}/media_publish", "Instagram media_publish",
                                data={"creation_id": container_id})
        media_id = published.get("id")
        if not media_id:
            raise ApiError("Instagram media_publish returned no id")
        url = None
        try:
            url = self._graph("GET", str(media_id), "Instagram permalink",
                              params={"fields": "permalink"}).get("permalink")
        except Exception:
            pass  # published; permalink is optional
        return self.success(str(media_id), url)

    def _wait_finished(self, container_id: str) -> None:
        deadline = time.monotonic() + self.poll_max_s
        while True:
            info = self._graph("GET", container_id, "Instagram container status",
                               params={"fields": "status_code,status"})
            code = info.get("status_code")
            if code in ("FINISHED", "PUBLISHED"):
                return
            if code in ("ERROR", "EXPIRED"):
                raise ApiError(f"Instagram processing {code}: {info.get('status', '')}".strip())
            if time.monotonic() >= deadline:
                raise ApiError("Instagram processing timed out (container not FINISHED)")
            time.sleep(self.poll_interval_s)

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
        if self.settings.dry_run or not self.configured() or not comment_id:
            return False
        try:
            self._graph("POST", f"{comment_id}/replies", "Instagram reply",
                        data={"message": text[:2200]})
            return True
        except Exception:
            return False
