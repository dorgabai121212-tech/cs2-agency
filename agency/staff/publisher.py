"""Publisher: uploads a planned post through its platform adapter (DRY_RUN respected)."""
from __future__ import annotations

from ..models import PostPlan, PostResult, VideoStatus
from ..platforms import registry
from .base import StaffMember

# Post statuses that will never change again on their own.
TERMINAL_POST_STATUSES = {"published", "failed", "cancelled"}

# Statuses from which a post may be claimed for upload (atomic UPDATE ... WHERE status IN).
CLAIMABLE_POST_STATUSES = ("planned", "awaiting_approval", "scheduled", "failed")


class Publisher(StaffMember):
    """The only staff member that touches platform upload APIs."""

    name = "publisher"
    title_he = "מפרסם"
    role_prompt = (
        "You are the Publisher of a CS2 creator's agency. You upload exactly what was approved, "
        "to exactly the platform it was approved for, at the planned time. You respect DRY_RUN, "
        "platform rate limits and ToS, never retry in a loop, never report a success that did "
        "not happen, and record every error verbatim so the owner can fix it.")

    def publish(self, post_id: int, *, publish_now: bool = False) -> PostResult:
        """Upload one post now. Idempotent: an already published post is not uploaded again.

        The post is claimed atomically in the DB (``-> publishing``) before any upload, so two
        threads or two processes (dashboard scheduler + ``agency tick``) can never both upload
        it. ``publish_now`` makes adapters ignore a future ``scheduled_at`` (go public now).
        """
        post = self.db.get_post(post_id)
        if post is None:
            return PostResult(platform="?", ok=False, error=f"post {post_id} not found")
        platform = post["platform"]
        if post["status"] == "published":
            prev = post.get("result") or {}
            return PostResult.from_dict({**prev, "platform": platform, "ok": True})
        if not self.db.claim_post(post_id, CLAIMABLE_POST_STATUSES):
            current = self.db.get_post(post_id) or post
            if current["status"] == "published":
                prev = current.get("result") or {}
                return PostResult.from_dict({**prev, "platform": platform, "ok": True})
            return PostResult(platform=platform, ok=False,
                              error=f"post {post_id} is {current['status']}")

        video_id = post["video_id"]
        self.db.update_video(video_id, status=VideoStatus.PUBLISHING)
        mode = "DRY RUN" if self.settings.dry_run else "LIVE"
        self.status("working", f"[{mode}] מעלה פוסט #{post_id} ל-{platform}")
        plan = PostPlan.from_dict(post.get("plan") or {})
        if publish_now:
            plan.extra = {**(plan.extra or {}), "publish_now": True}
        result = self._upload(platform, plan)

        if result.ok:
            self.db.update_post(post_id, status="published", result=result)
            note = " (מצב ניסיון — לא באמת עלה)" if result.dry_run else ""
            self.emit(f"פוסט #{post_id} ב-{platform} פורסם{note}", "success", video_id)
        else:
            self.db.update_post(post_id, status="failed", result=result)
            self.emit(f"פוסט #{post_id} ב-{platform} נכשל: {result.error} "
                      f"(לניסיון חוזר: agency retry {post_id})", "error", video_id)
        self.refresh_video_status(video_id)
        self.status("idle")
        return result

    def retry(self, post_id: int) -> PostResult:
        """Owner-requested retry of a ``failed`` post: claim it and upload it again now.

        The post was already approved once; retrying does not need a new approval.
        """
        post = self.db.get_post(post_id)
        if post is None:
            return PostResult(platform="?", ok=False, error=f"post {post_id} not found")
        if post["status"] != "failed":
            return PostResult(platform=post["platform"], ok=False,
                              error=f"post {post_id} is {post['status']}, only failed posts "
                                    "can be retried")
        # failed -> scheduled atomically (only one retry wins), then the normal publish claim.
        if not self.db.claim_post(post_id, ("failed",), "scheduled"):
            return PostResult(platform=post["platform"], ok=False,
                              error=f"post {post_id} is already being retried")
        self.emit(f"ניסיון חוזר לפוסט #{post_id} ב-{post['platform']}", "info", post["video_id"])
        return self.publish(post_id)

    def _upload(self, platform: str, plan: PostPlan) -> PostResult:
        """Run the adapter; any exception (incl. missing adapter module) becomes ok=False."""
        try:
            adapter = registry.get_adapter(platform, self.settings)
            if not plan.platform:
                plan.platform = platform
            result = adapter.upload(plan)
        except Exception as exc:  # fail closed and honest
            return PostResult(platform=platform, ok=False, dry_run=self.settings.dry_run,
                              error=f"{type(exc).__name__}: {exc}")
        if not isinstance(result, PostResult):
            return PostResult(platform=platform, ok=False, error="adapter returned no result")
        return result

    def refresh_video_status(self, video_id: int) -> VideoStatus | None:
        """Derive the video status from its posts (published / failed / scheduled)."""
        posts = [p for p in self.db.list_posts(video_id) if p["status"] != "cancelled"]
        if not posts:
            return None
        statuses = {p["status"] for p in posts}
        if statuses <= TERMINAL_POST_STATUSES:
            status = VideoStatus.PUBLISHED if "published" in statuses else VideoStatus.FAILED
        elif "publishing" in statuses:
            status = VideoStatus.PUBLISHING
        elif "scheduled" in statuses:
            status = VideoStatus.SCHEDULED
        elif "awaiting_approval" in statuses:
            status = VideoStatus.AWAITING_APPROVAL
        else:
            status = VideoStatus.PLANNED
        self.db.update_video(video_id, status=status)
        return status
