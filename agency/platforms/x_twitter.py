"""X (Twitter): chunked video upload + post, with OAuth 1.0a user context.

Uses the X API v2 media endpoints (the v1.1 ``media/upload`` endpoint used by tweepy's
``API.media_upload`` has been retired by X):
``POST /2/media/upload/initialize`` -> ``POST /2/media/upload/{id}/append`` (segments) ->
``POST /2/media/upload/{id}/finalize`` -> poll ``GET /2/media/upload?command=STATUS`` ->
``POST /2/tweets``. Requests are signed with ``requests_oauthlib.OAuth1`` (installed with tweepy,
part of the ``[platforms]`` extra). The app needs Read+Write permissions.
"""
from __future__ import annotations

import re
import time

from ..config import secret
from ..models import PostPlan, PostResult
from ._common import ApiError, RealAdapter, check_response, normalize_hashtags, video_file

API_BASE = "https://api.x.com/2"
TEXT_LIMIT = 280
URL_WEIGHT = 23
CHUNK_SIZE = 4 * 1024 * 1024
_URL_RE = re.compile(r"https?://\S+")
# twitter-text v3: these code point ranges weigh 1, everything else (CJK, emoji) weighs 2.
_LIGHT_RANGES = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))


def weighted_length(text: str) -> int:
    """Approximate X's weighted character count (URLs count as 23)."""
    total = 0
    pos = 0
    for match in _URL_RE.finditer(text):
        total += _plain_weight(text[pos:match.start()]) + URL_WEIGHT
        pos = match.end()
    return total + _plain_weight(text[pos:])


def _plain_weight(text: str) -> int:
    return sum(1 if any(a <= ord(ch) <= b for a, b in _LIGHT_RANGES) else 2 for ch in text)


def fit_text(text: str, limit: int = TEXT_LIMIT) -> str:
    """Trim ``text`` until its weighted length fits ``limit`` (ellipsis when cut)."""
    text = text.strip()
    if weighted_length(text) <= limit:
        return text
    while text and weighted_length(text + "…") > limit:
        text = text[:-1]
    return text.rstrip() + "…"


def build_tweet(plan: PostPlan) -> str:
    """Title (or description) + link + up to 3 hashtags, within 280 weighted chars."""
    main = plan.title.strip() or plan.description.strip()
    link = plan.extra.get("link") or ""
    tail = " ".join(p for p in (link, " ".join(normalize_hashtags(plan.hashtags, 3))) if p)
    if not tail:
        return fit_text(main)
    room = TEXT_LIMIT - weighted_length(tail) - 1
    if room < 20:
        return fit_text(f"{main} {tail}")
    return f"{fit_text(main, room)} {tail}".strip()


class XAdapter(RealAdapter):
    key = "x"
    display_he = "X (טוויטר)"
    required_secrets = ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET"]

    poll_max_s: float = 300.0

    def _oauth(self):
        from requests_oauthlib import OAuth1

        return OAuth1(secret("X_API_KEY"), secret("X_API_SECRET"),
                      secret("X_ACCESS_TOKEN"), secret("X_ACCESS_SECRET"))

    def _call(self, method: str, path: str, what: str, **kwargs) -> dict:
        import requests

        resp = requests.request(method, f"{API_BASE}{path}", auth=self._oauth(),
                                timeout=self.timeout(), **kwargs)
        data = check_response(resp, what)
        if "errors" in data and not data.get("data"):
            first = (data.get("errors") or [{}])[0]
            raise ApiError(f"{what} failed: {first.get('detail') or first.get('message') or first}")
        return data.get("data") or {}

    def _upload(self, plan: PostPlan) -> PostResult:
        path = video_file(plan)
        size = path.stat().st_size
        init = self._call("POST", "/media/upload/initialize", "X media initialize", json={
            "media_type": "video/mp4", "total_bytes": size, "media_category": "tweet_video"})
        media_id = init.get("id")
        if not media_id:
            raise ApiError("X media initialize returned no id")

        with path.open("rb") as fh:
            index = 0
            while chunk := fh.read(CHUNK_SIZE):
                self._call("POST", f"/media/upload/{media_id}/append", "X media append",
                           data={"segment_index": str(index)},
                           files={"media": (path.name, chunk, "application/octet-stream")})
                index += 1

        final = self._call("POST", f"/media/upload/{media_id}/finalize", "X media finalize")
        self._wait_processed(media_id, final.get("processing_info"))

        tweet = self._call("POST", "/tweets", "X create post", json={
            "text": build_tweet(plan), "media": {"media_ids": [str(media_id)]}})
        tweet_id = tweet.get("id")
        if not tweet_id:
            raise ApiError("X create post returned no id")
        return self.success(str(tweet_id), f"https://x.com/i/web/status/{tweet_id}")

    def _wait_processed(self, media_id: str, info: dict | None) -> None:
        deadline = time.monotonic() + self.poll_max_s
        while info and info.get("state") not in (None, "succeeded"):
            if info.get("state") == "failed":
                error = info.get("error") or {}
                raise ApiError(f"X video processing failed: {error.get('message', error)}")
            if time.monotonic() >= deadline:
                raise ApiError("X video processing timed out")
            time.sleep(min(float(info.get("check_after_secs", 5)), 30))
            status = self._call("GET", "/media/upload", "X media status",
                                params={"command": "STATUS", "media_id": str(media_id)})
            info = status.get("processing_info")

    def fetch_stats(self, external_id: str) -> dict:
        if not self.can_call_api(external_id):
            return {}
        try:
            data = self._call("GET", f"/tweets/{external_id}", "X post lookup",
                              params={"tweet.fields": "public_metrics"})
        except Exception:
            return {}
        m = data.get("public_metrics") or {}
        return {"views": m.get("impression_count", 0), "likes": m.get("like_count", 0),
                "comments": m.get("reply_count", 0),
                "shares": m.get("retweet_count", 0) + m.get("quote_count", 0)}
