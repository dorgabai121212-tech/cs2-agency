"""Reddit via praw ``subreddit.submit_video`` — ONLY to subreddits the owner listed in config.

``publishing.platforms.reddit.subreddits: ["GlobalOffensive", ...]``. A plan may narrow the target
with ``extra["subreddit"]`` / ``extra["subreddits"]``; anything not in the owner's list is refused.
Without a narrowing, only the FIRST configured subreddit is used (no mass cross-posting).
Uses a "script" app (REDDIT_CLIENT_ID/SECRET + the owner's username/password).
"""
from __future__ import annotations

from ..config import secret
from ..models import PostPlan, PostResult
from ._common import RealAdapter, truncate, video_file

TITLE_LIMIT = 300
REQUEST_TIMEOUT_S = 60


class RedditAdapter(RealAdapter):
    key = "reddit"
    display_he = "רדיט"
    required_secrets = ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USERNAME",
                        "REDDIT_PASSWORD"]

    def allowed_subreddits(self) -> list[str]:
        subs = self.platform_config.get("subreddits") or []
        return [str(s).strip().removeprefix("r/").strip("/") for s in subs if str(s).strip()]

    def targets(self, plan: PostPlan) -> tuple[list[str], list[str]]:
        """(allowed targets, refused names) for this plan."""
        allowed = {s.lower(): s for s in self.allowed_subreddits()}
        requested = plan.extra.get("subreddits") or plan.extra.get("subreddit") or []
        if isinstance(requested, str):
            requested = [requested]
        requested = [str(s).strip().removeprefix("r/").strip("/") for s in requested]
        if not requested:
            return (list(allowed.values())[:1], [])
        ok = [allowed[s.lower()] for s in requested if s.lower() in allowed]
        refused = [s for s in requested if s.lower() not in allowed]
        return (list(dict.fromkeys(ok)), refused)

    def _client(self):
        import praw

        username = secret("REDDIT_USERNAME")
        return praw.Reddit(client_id=secret("REDDIT_CLIENT_ID"),
                           client_secret=secret("REDDIT_CLIENT_SECRET"),
                           username=username, password=secret("REDDIT_PASSWORD"),
                           user_agent=f"python:cs2-agency:0.1 (by u/{username})",
                           timeout=REQUEST_TIMEOUT_S, check_for_updates=False)

    def _upload(self, plan: PostPlan) -> PostResult:
        targets, refused = self.targets(plan)
        if refused:
            return self.fail("קהילות שלא אושרו בהגדרות (config.yaml): " + ", ".join(refused))
        if not targets:
            return self.fail("לא הוגדרו קהילות רדיט ב-config.yaml "
                             "(publishing.platforms.reddit.subreddits)")
        path = video_file(plan)
        title = truncate(plan.title or plan.description, TITLE_LIMIT)
        thumb = plan.extra.get("thumbnail")
        reddit = self._client()
        ids: list[str] = []
        urls: list[str] = []
        for name in targets:
            submission = reddit.subreddit(name).submit_video(
                title=title, video_path=str(path), thumbnail_path=thumb or None,
                flair_id=(plan.extra.get("flair_ids") or {}).get(name), timeout=REQUEST_TIMEOUT_S)
            if submission is None:  # posted without websocket confirmation
                continue
            ids.append(submission.id)
            urls.append(f"https://www.reddit.com{submission.permalink}")
        if not ids:
            return self.fail("רדיט לא החזיר מזהה פוסט (ייתכן שהסרטון עדיין בעיבוד)")
        return self.success(",".join(ids), urls[0])

    def fetch_stats(self, external_id: str) -> dict:
        if not self.can_call_api(external_id):
            return {}
        try:
            reddit = self._client()
            likes = comments = 0
            for sid in str(external_id).split(","):
                post = reddit.submission(id=sid)
                likes += int(post.score)
                comments += int(post.num_comments)
            return {"likes": likes, "comments": comments}
        except Exception:
            return {}

    def fetch_comments(self, external_id: str, limit: int = 20) -> list[dict]:
        if not self.can_call_api(external_id):
            return []
        try:
            out: list[dict] = []
            reddit = self._client()
            for sid in str(external_id).split(","):
                post = reddit.submission(id=sid)
                post.comments.replace_more(limit=0)
                for c in post.comments.list():
                    out.append({"id": c.id, "author": str(c.author or ""), "text": c.body,
                                "published_at": c.created_utc})
            return out[:limit]
        except Exception:
            return []

    def reply_comment(self, comment_id: str, text: str) -> bool:
        if self.settings.dry_run or not self.configured() or not comment_id:
            return False
        try:
            self._client().comment(id=comment_id).reply(text[:10000])
            return True
        except Exception:
            return False
