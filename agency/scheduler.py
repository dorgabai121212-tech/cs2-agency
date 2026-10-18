"""Scheduler: publishes posts whose time has come. Run by the dashboard in a background thread."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from .events import emit
from .models import PostResult
from .staff._playbook import parse_iso
from .staff.base import Context
from .staff.publisher import Publisher

log = logging.getLogger("agency.scheduler")

_tick_lock = threading.Lock()


def due_posts(ctx: Context, now: datetime | None = None) -> list[dict]:
    """Posts with status ``scheduled`` and ``scheduled_at <= now`` (unscheduled = due now)."""
    now = now or datetime.now(timezone.utc)
    due = []
    for post in ctx.db.list_posts():
        if post.get("status") != "scheduled":
            continue
        when = parse_iso(post.get("scheduled_at"))
        if when is None or when <= now:
            due.append(post)
    return sorted(due, key=lambda p: (p.get("scheduled_at") or "", p["id"]))


DEFAULT_STALE_MINUTES = 120


def stale_after_s(ctx: Context) -> float:
    """How long a post may sit in ``publishing`` before we call it interrupted (config)."""
    try:
        minutes = float(ctx.settings.get("publishing.stale_publishing_minutes",
                                         DEFAULT_STALE_MINUTES))
    except (TypeError, ValueError):
        minutes = DEFAULT_STALE_MINUTES
    return max(1.0, minutes) * 60


def recover_interrupted(ctx: Context, older_than_s: float | None = None) -> dict:
    """Un-stick work left behind by a crash / closed window / sleep mid-upload.

    - posts stuck in ``publishing`` -> ``failed`` with an honest error (the upload may or may
      not have reached the platform, so we never re-upload automatically; the owner checks and
      runs ``agency retry``). A post claimed atomically is only reset when its row went stale.
    - approvals stuck in ``executing`` -> ``approved``, so ``agency approve N`` can resume them.
    Never raises.
    """
    older = stale_after_s(ctx) if older_than_s is None else older_than_s
    out: dict[str, list[int]] = {"posts": [], "approvals": []}
    try:
        for post in ctx.db.stale_posts("publishing", older):
            if not ctx.db.claim_post(post["id"], ("publishing",), "failed"):
                continue
            error = ("ההעלאה נקטעה (התוכנה נסגרה באמצע). בדוק בפלטפורמה אם הסרטון עלה; "
                     f"אם לא — agency retry {post['id']}")
            ctx.db.update_post(post["id"], result=PostResult(
                platform=post["platform"], ok=False, dry_run=ctx.settings.dry_run, error=error))
            Publisher(ctx).refresh_video_status(post["video_id"])
            emit(ctx.db, "scheduler", f"פוסט #{post['id']}: {error}", "warning", post["video_id"])
            out["posts"].append(post["id"])
        for approval in ctx.db.stale_approvals("executing", older):
            if ctx.db.release_approval(approval.id):
                emit(ctx.db, "scheduler", f"ביצוע אישור #{approval.id} נקטע. להמשך: "
                     f"agency approve {approval.id} (או אישור שוב בלוח הבקרה)", "warning")
                out["approvals"].append(approval.id)
    except Exception:  # recovery is best-effort; never block publishing
        log.exception("recovery of interrupted work failed")
    return out


def tick(ctx: Context) -> list[PostResult]:
    """Recover interrupted work, then publish every due scheduled post once. Never raises."""
    if not _tick_lock.acquire(blocking=False):
        return []  # another tick is already running in this process
    try:
        recover_interrupted(ctx)
        posts = due_posts(ctx)
        if not posts:
            return []
        publisher = Publisher(ctx)
        results: list[PostResult] = []
        for post in posts:
            try:
                result = publisher.publish(post["id"])
            except Exception as exc:  # one bad post must not block the others
                log.exception("publishing post %s failed", post["id"])
                result = PostResult(platform=post.get("platform", "?"), ok=False,
                                    error=f"{type(exc).__name__}: {exc}")
                ctx.db.update_post(post["id"], status="failed", result=result)
            results.append(result)
        emit(ctx.db, "scheduler", f"תזמון: {sum(r.ok for r in results)}/{len(results)} "
             "פוסטים פורסמו")
        return results
    finally:
        _tick_lock.release()


def run_forever(ctx: Context, interval_s: float = 60, stop: threading.Event | None = None) -> None:
    """Call :func:`tick` every ``interval_s`` seconds until ``stop`` is set."""
    stop = stop or threading.Event()
    emit(ctx.db, "scheduler", f"המתזמן התחיל (כל {interval_s:g} שניות)")
    while not stop.is_set():
        try:
            tick(ctx)
        except Exception:  # pragma: no cover - tick already guards; belt and braces
            log.exception("scheduler tick crashed")
        stop.wait(interval_s)
    emit(ctx.db, "scheduler", "המתזמן נעצר")
