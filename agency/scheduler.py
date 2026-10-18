"""Scheduler: publishes posts whose time has come. Run by the dashboard in a background thread."""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone

from .events import emit
from .models import PostResult, VideoStatus
from .staff._playbook import parse_iso
from .staff.base import Context
from .staff.publisher import Publisher

log = logging.getLogger("agency.scheduler")

_tick_lock = threading.Lock()


def _held_remotely(post: dict) -> bool:
    """A post handed to Zernio's cloud schedule: Zernio publishes it, never us again."""
    return bool(((post.get("plan") or {}).get("extra") or {}).get("remote"))


def due_posts(ctx: Context, now: datetime | None = None) -> list[dict]:
    """Posts with status ``scheduled`` and ``scheduled_at <= now`` (unscheduled = due now).
    Posts waiting in Zernio's cloud schedule are not ours to upload (see reconcile_remote)."""
    now = now or datetime.now(timezone.utc)
    due = []
    for post in ctx.db.list_posts():
        if post.get("status") != "scheduled" or _held_remotely(post):
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


# ----------------------------------------------------------------------------- Zernio reconcile
RECONCILE_KV = "zernio.reconcile"          # {post id: last check (epoch)}
RECONCILE_EVERY_S = 5 * 60
RECONCILE_GRACE = timedelta(minutes=2)     # after the slot before asking Zernio
RECONCILE_MAX_AGE = timedelta(days=3)


def _reconcile_candidates(ctx: Context, now: datetime) -> list[dict]:
    """Zernio posts whose real outcome we do not know yet: held in Zernio's cloud schedule
    (slot passed), or recorded failed while Zernio still had them ("still processing")."""
    from .platforms.zernio import ZERNIO_TARGETS

    out = []
    for post in ctx.db.list_posts():
        if post.get("platform") not in ZERNIO_TARGETS:
            continue
        result = post.get("result") or {}
        if not (result.get("external_id") or post.get("external_id")) \
                or result.get("dry_run") is not False:
            continue
        if post.get("status") == "scheduled" and _held_remotely(post):
            remote = ((post.get("plan") or {}).get("extra") or {}).get("remote") or {}
            due = parse_iso(remote.get("scheduled_for") if isinstance(remote, dict) else None) \
                or parse_iso(post.get("scheduled_at"))
            if due is not None and now < due + RECONCILE_GRACE:
                continue
            since = due or parse_iso(post.get("updated_at"))
        elif post.get("status") == "failed" and result.get("ok") is False:
            since = parse_iso(post.get("updated_at"))
        else:
            continue
        if since is not None and now - since > RECONCILE_MAX_AGE:
            continue
        out.append(post)
    return out


def reconcile_remote(ctx: Context, now: datetime | None = None) -> dict:
    """Ask Zernio (read-only, at most every 5 minutes per post) what became of posts it holds,
    and record the truth: published (with the real link and id), or failed with Zernio's
    reason. Training mode / no key / Zernio unreachable -> nothing changes. Never raises.
    Returns ``{"published": [ids], "failed": [ids], "checked": n}``."""
    from .platforms import registry
    from .platforms.zernio import ZernioAdapter

    now = now or datetime.now(timezone.utc)
    out: dict = {"published": [], "failed": [], "checked": 0}
    try:
        candidates = _reconcile_candidates(ctx, now)
        if not candidates:
            return out
        checks = ctx.db.kv_get(RECONCILE_KV) or {}
        checks = checks if isinstance(checks, dict) else {}
        stamp = now.timestamp()
        publisher = Publisher(ctx)
        for post in candidates:
            pid = str(post["id"])
            if stamp - float(checks.get(pid) or 0) < RECONCILE_EVERY_S:
                continue
            adapter = registry.get_adapter(post["platform"], ctx.settings)
            if not isinstance(adapter, ZernioAdapter) or adapter.dry_run:
                continue
            checks[pid] = stamp
            external = str((post.get("result") or {}).get("external_id")
                           or post.get("external_id"))
            try:
                info = adapter.post_status(external)
            except Exception as exc:  # Zernio unreachable: ask again later
                log.info("zernio reconcile of post %s failed: %s", pid, type(exc).__name__)
                continue
            out["checked"] += 1
            state = info.get("state")
            if state == "published":
                result = PostResult(platform=post["platform"], ok=True, dry_run=False,
                                    external_id=str(info.get("native_id") or external),
                                    url=info.get("url"), error=None)
                plan = Publisher._saved_plan(post, remote=None,
                                             published_at=now.isoformat(timespec="seconds"))
                ctx.db.update_post(post["id"], status="published", result=result, plan=plan)
                emit(ctx.db, "publisher", f"Zernio אישר: פוסט #{post['id']} ב-{post['platform']} "
                     "עלה", "success", post["video_id"])
                out["published"].append(post["id"])
            elif state in ("failed", "cancelled", "draft"):
                error = info.get("error") or {
                    "cancelled": "הפוסט בוטל ב-Zernio (cancelled)",
                    "draft": "Zernio שמר את הפוסט כטיוטה בלבד — הוא לא עלה (draft)"}.get(
                        state, "ההעלאה נכשלה ב-Zernio")
                result = PostResult(platform=post["platform"], ok=False, dry_run=False,
                                    external_id=None, url=None, error=error)
                plan = Publisher._saved_plan(post, remote=None)
                ctx.db.update_post(post["id"], status="failed", result=result, plan=plan)
                emit(ctx.db, "publisher", f"פוסט #{post['id']} ב-{post['platform']} לא עלה: "
                     f"{error} (לניסיון חוזר: agency retry {post['id']})", "error",
                     post["video_id"])
                out["failed"].append(post["id"])
            else:
                continue
            publisher.refresh_video_status(post["video_id"])
        keep = {str(p["id"]) for p in candidates}
        ctx.db.kv_set(RECONCILE_KV, {k: v for k, v in checks.items() if k in keep})
    except Exception:  # reconciling is best-effort; never block publishing
        log.exception("reconciling Zernio posts failed")
    return out


# ----------------------------------------------------------------------------- stalled videos
STALLED_AFTER = timedelta(hours=2)
_IN_FLIGHT = (VideoStatus.NEW, VideoStatus.ANALYZED, VideoStatus.EDITED, VideoStatus.PLANNED)
_STEP_OF_STATUS = {VideoStatus.NEW: "analyst", VideoStatus.ANALYZED: "editor",
                   VideoStatus.EDITED: "copywriter", VideoStatus.PLANNED: "strategist"}
_PIPELINE_STAFF = ("analyst", "editor", "copywriter", "strategist")


def recover_stalled_videos(ctx: Context, *, dead_pids: tuple[int, ...] = (),
                           now: datetime | None = None,
                           stalled_after: timedelta = STALLED_AFTER) -> list[int]:
    """Videos whose preparation died with its process (a restart, sleep, update) are marked
    FAILED at the step they stopped, so the home screen offers "נסה שוב" instead of claiming
    it is still being worked on. A job is dead when it ran in ``dead_pids`` (the previous
    dashboard process), or in this process but no longer runs, or when nothing moved for
    ``stalled_after``. A job running right now is never touched. Never raises."""
    from .home import video_title
    from .pipeline import running_videos

    now = now or datetime.now(timezone.utc)
    marked: list[int] = []
    try:
        running = running_videos()
        staff = {row["name"]: parse_iso(row.get("updated_at"))
                 for row in ctx.db.list_staff_status() if row.get("name") in _PIPELINE_STAFF}
        last_staff = max((t for t in staff.values() if t), default=None)
        for video in ctx.db.list_videos(limit=200):
            if video.status not in _IN_FLIGHT or video.id in running:
                continue
            if (video.meta or {}).get("source") == "studio" or ctx.db.list_posts(video.id):
                continue
            meta = video.meta or {}
            try:
                pid = int(meta.get("job_pid")) if meta.get("job_pid") is not None else None
            except (TypeError, ValueError):
                pid = None
            started = parse_iso(meta.get("job_started")) or parse_iso(video.created_at)
            activity = max((t for t in (started, last_staff) if t), default=None)
            dead = (pid is not None and pid in dead_pids) or pid == os.getpid() or (
                activity is not None and now - activity > stalled_after)
            if not dead:
                continue
            step = _STEP_OF_STATUS.get(video.status, "analyst")
            ctx.db.update_video(video.id, status=VideoStatus.FAILED, meta={
                "failed_step": step, "error": "interrupted: the preparation stopped midway"})
            emit(ctx.db, "manager", f"ההכנה של \"{video_title(video)}\" נקטעה באמצע (התוכנה "
                 "נסגרה או נרדמה). אפשר לנסות שוב.", "warning", video.id)
            marked.append(video.id)
    except Exception:  # best-effort; never block the scheduler
        log.exception("recovering stalled videos failed")
    return marked


def tick(ctx: Context) -> list[PostResult]:
    """Recover interrupted work, reconcile what Zernio holds, then publish every due scheduled
    post once. Never raises."""
    if not _tick_lock.acquire(blocking=False):
        return []  # another tick is already running in this process
    try:
        recover_interrupted(ctx)
        recover_stalled_videos(ctx)
        reconcile_remote(ctx)
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


STATS_EVERY = timedelta(hours=6)
STATS_KV = "analytics.stats_at"


def maybe_refresh_stats(ctx: Context, now: datetime | None = None) -> dict | None:
    """Every 6 hours, pull fresh views/likes/comments for really published posts (read-only,
    no LLM), so the home screen's numbers stay current without the owner asking. Imported
    studio history counts. Returns ``{fetched, errors}`` when it ran, else None. Never raises.
    """
    from .home import is_real_post
    from .staff.analytics import Analyst

    now = now or datetime.now(timezone.utc)
    try:
        last = parse_iso(ctx.db.kv_get(STATS_KV))
        if last is not None and now - last < STATS_EVERY:
            return None
        if not any(is_real_post(p) for p in ctx.db.list_posts()):
            return None
        ctx.db.kv_set(STATS_KV, now.isoformat(timespec="seconds"))  # first: no hammering
        analyst = Analyst(ctx)
        analyst.status("working", "בודק את המספרים", quiet=True)
        try:
            _posts, fetched, errors = analyst.fetch_stats(quiet=True)
        finally:
            analyst.status("idle", quiet=True)
        return {"fetched": fetched, "errors": errors}
    except Exception:  # stats are a bonus; never disturb publishing
        log.exception("periodic stats refresh failed")
        return None


def run_forever(ctx: Context, interval_s: float = 60, stop: threading.Event | None = None) -> None:
    """Call :func:`tick` every ``interval_s`` seconds until ``stop`` is set (and refresh the
    stats of published posts every 6 hours)."""
    stop = stop or threading.Event()
    emit(ctx.db, "scheduler", f"המתזמן התחיל (כל {interval_s:g} שניות)")
    while not stop.is_set():
        try:
            tick(ctx)
            maybe_refresh_stats(ctx)
        except Exception:  # pragma: no cover - tick already guards; belt and braces
            log.exception("scheduler tick crashed")
        stop.wait(interval_s)
    emit(ctx.db, "scheduler", "המתזמן נעצר")
