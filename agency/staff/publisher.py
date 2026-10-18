"""Publisher: uploads a planned post through its platform adapter (training mode respected).

Per platform: an adapter is live only when the owner switched that platform live (or the
global ``dry_run`` is false) AND it is connected. Before any live upload the old studio's DB
is checked (read-only) so the same file never goes to the same platform twice (§11.10).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..models import PostPlan, PostResult, VideoStatus
from ..platforms import registry
from .base import StaffMember

log = logging.getLogger("agency.publisher")

DOUBLE_POST_HE = ("הסרטון הזה כבר עלה ל-{name} דרך הסטודיו הישן — לא מעלה אותו שוב "
                  "(כדי שלא יופיע פעמיים)")
STUDIO_UNREADABLE_HE = ("לא הצלחתי לקרוא את הסטודיו הישן כדי לוודא שהקליפ לא עלה כבר דרכו — "
                        "לא מעלה עכשיו, כדי שלא יופיע פעמיים. נסה שוב עוד מעט")
SAME_CHANNEL_HE = ("אותו קובץ כבר עלה לערוץ היוטיוב (כשורטס או כסרטון רגיל) — לא מעלה אותו "
                   "פעמיים לאותו ערוץ")
YOUTUBE_CHANNEL = ("youtube", "youtube_shorts")  # one YouTube account

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
        if post["status"] == "scheduled" and ((post.get("plan") or {}).get("extra") or {}).get(
                "remote"):
            # Zernio's cloud holds it for its slot; uploading again would post it twice.
            return PostResult(platform=platform, ok=False, dry_run=False,
                              external_id=(post.get("result") or {}).get("external_id"),
                              error="הפוסט כבר מתוזמן אצל Zernio — הוא יעלה בזמן שנקבע")
        if not self.db.claim_post(post_id, CLAIMABLE_POST_STATUSES):
            current = self.db.get_post(post_id) or post
            if current["status"] == "published":
                prev = current.get("result") or {}
                return PostResult.from_dict({**prev, "platform": platform, "ok": True})
            return PostResult(platform=platform, ok=False,
                              error=f"post {post_id} is {current['status']}")

        video_id = post["video_id"]
        self.db.update_video(video_id, status=VideoStatus.PUBLISHING)
        plan = PostPlan.from_dict(post.get("plan") or {})
        if publish_now:
            plan.extra = {**(plan.extra or {}), "publish_now": True}
        result = self._upload(platform, plan, video_id=video_id, post_id=post_id)

        remote = getattr(self._last_adapter, "remote_scheduled", None) \
            if result.ok and not result.dry_run else None
        if isinstance(remote, dict) and remote.get("post_id"):
            # Handed to the platform's cloud schedule (Zernio): NOT published yet. It stays
            # "scheduled" (the scheduler does not upload it again) until reconciled.
            saved = self._saved_plan(post, remote={"post_id": str(remote["post_id"]),
                                                   "scheduled_for": remote.get("scheduled_for")})
            self.db.update_post(post_id, status="scheduled", result=result, plan=saved)
            self.emit(f"פוסט #{post_id} ב-{platform} מתוזמן אצל Zernio ויעלה בזמן — גם אם "
                      "המחשב כבוי", "success", video_id)
        elif result.ok:
            saved = self._saved_plan(post, published_at=datetime.now(timezone.utc).isoformat(
                timespec="seconds"), remote=None)
            self.db.update_post(post_id, status="published", result=result, plan=saved)
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

    @staticmethod
    def _saved_plan(post: dict, **extra) -> PostPlan:
        """The post's stored plan with ``extra`` keys set (None removes a key); the post's own
        slot is kept as it is."""
        plan = PostPlan.from_dict(post.get("plan") or {})
        merged = {**(plan.extra or {}), **extra}
        plan.extra = {k: v for k, v in merged.items() if v is not None}
        plan.scheduled_at = post.get("scheduled_at")
        return plan

    _last_adapter = None

    def _upload(self, platform: str, plan: PostPlan, *, video_id: int | None = None,
                post_id: int | None = None) -> PostResult:
        """Run the adapter; any exception (incl. missing adapter module) becomes ok=False."""
        dry = self.settings.dry_run
        self._last_adapter = None
        try:
            adapter = registry.get_adapter(platform, self.settings)
            self._last_adapter = adapter
            dry = bool(getattr(adapter, "dry_run", dry))
            mode = "DRY RUN" if dry else "LIVE"
            self.status("working", f"[{mode}] מעלה פוסט #{post_id} ל-{platform}")
            if not plan.platform:
                plan.platform = platform
            if not dry:
                refused = self.double_post_check(platform, plan, video_id) \
                    or self.same_channel_check(platform, plan, video_id, post_id)
                if refused is not None:
                    return refused
            result = adapter.upload(plan)
        except Exception as exc:  # fail closed and honest
            return PostResult(platform=platform, ok=False, dry_run=dry,
                              error=f"{type(exc).__name__}: {exc}")
        if not isinstance(result, PostResult):
            return PostResult(platform=platform, ok=False, error="adapter returned no result")
        return result

    def double_post_check(self, platform: str, plan: PostPlan, video_id: int | None
                          ) -> PostResult | None:
        """Refuse (ok=False, plain Hebrew) when the old studio already posted this file to the
        same platform. Read-only; an unreadable studio DB never blocks publishing."""
        from ..importer import studio_conflict

        files = [plan.variant_path]
        video = self.db.get_video(video_id) if video_id is not None else None
        if video is not None and video.path and video.path not in files:
            files.append(video.path)
        try:
            conflict = studio_conflict(self.settings, platform, files)
        except Exception as exc:  # pragma: no cover - studio_conflict already guards
            log.warning("double-post check failed: %s", type(exc).__name__)
            return None
        if not conflict:
            return None
        if conflict.get("unreadable"):
            return PostResult(platform=platform, ok=False, dry_run=False,
                              error=STUDIO_UNREADABLE_HE)
        try:
            name = registry.get_adapter(platform, self.settings).display_he
        except Exception:
            name = platform
        return PostResult(platform=platform, ok=False, dry_run=False,
                          error=DOUBLE_POST_HE.format(name=name))

    def same_channel_check(self, platform: str, plan: PostPlan, video_id: int | None,
                           post_id: int | None) -> PostResult | None:
        """YouTube and YouTube Shorts are one channel: refuse a live upload of a file that
        already went (or is going) up there under the other key."""
        if platform not in YOUTUBE_CHANNEL or video_id is None or not plan.variant_path:
            return None
        for other in self.db.list_posts(video_id):
            if other["id"] == post_id or other["platform"] not in YOUTUBE_CHANNEL \
                    or other["platform"] == platform:
                continue
            if (other.get("plan") or {}).get("variant_path") != plan.variant_path:
                continue
            result = other.get("result") or {}
            live = other["status"] == "publishing" or (
                other["status"] in ("published", "scheduled") and result.get("dry_run") is False
                and result.get("ok") is not False and result.get("external_id"))
            if live:
                return PostResult(platform=platform, ok=False, dry_run=False,
                                  error=SAME_CHANNEL_HE)
        return None

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
