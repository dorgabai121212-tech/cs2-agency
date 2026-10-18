"""The production line: analyst → editor → copywriter → strategist → posts → approval/schedule.

Also executes owner-approved approvals (publish / spend / reply / other). Every step reports to
the activity feed; a failing step marks the video FAILED with the error and never crashes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from .events import emit
from .models import Approval, PostPlan, VideoStatus
from .staff._playbook import parse_iso
from .staff.analyst import VideoAnalyst
from .staff.base import Context, StaffMember
from .staff.community import CommunityManager
from .staff.copywriter import Copywriter
from .staff.editor import VideoEditor
from .staff.finance import CFO
from .staff.promoter import Promoter
from .staff.publisher import Publisher
from .staff.strategist import Strategist

ACTOR = "pipeline"


class StepFailed(RuntimeError):
    """A pipeline step failed; carries the step name."""

    def __init__(self, step: str, error: Exception):
        super().__init__(f"{step}: {type(error).__name__}: {error}")
        self.step = step


def _step(member: StaffMember, step: str, fn: Callable[[], Any]) -> Any:
    """Run one step with staff status bookkeeping; wrap any error in StepFailed."""
    try:
        return fn()
    except Exception as exc:
        member.db.set_staff_status(member.name, "error", f"{type(exc).__name__}: {exc}"[:300])
        raise StepFailed(step, exc) from exc


def publishing_mode(ctx: Context) -> str:
    """``approve`` (default, safe) or ``auto``; anything unknown means approve."""
    mode = str(ctx.settings.get("publishing.mode", "approve") or "approve").strip().lower()
    return "auto" if mode == "auto" else "approve"


def run_pipeline(ctx: Context, video_id: int) -> dict:
    """Process one video end-to-end. Returns a summary dict; never raises for step failures."""
    db = ctx.db
    video = db.get_video(video_id)
    if video is None:
        return {"ok": False, "video_id": video_id, "error": f"video {video_id} not found"}
    out: dict[str, Any] = {"ok": False, "video_id": video_id, "mode": publishing_mode(ctx),
                           "dry_run": ctx.settings.dry_run, "post_ids": [],
                           "approval_id": None, "promotion_approval_ids": []}
    emit(db, ACTOR, f"התחלת עבודה על סרטון #{video_id}", video_id=video_id)
    analyst, editor = VideoAnalyst(ctx), VideoEditor(ctx)
    copywriter, strategist = Copywriter(ctx), Strategist(ctx)
    try:
        analysis = _step(analyst, "analyst", lambda: analyst.analyze(video))
        db.update_video(video_id, status=VideoStatus.ANALYZED, analysis=analysis)
        video.analysis = analysis
        out["analysis"] = analysis

        variants = _step(editor, "editor", lambda: editor.make_variants(video, analysis))
        db.update_video(video_id, status=VideoStatus.EDITED)
        out["variants"] = [v.to_dict() for v in variants]

        platforms = [v.platform for v in variants]
        if not platforms:
            raise StepFailed("editor", RuntimeError("no platforms enabled in config.yaml"))
        copy = _step(copywriter, "copywriter",
                     lambda: copywriter.write(video, analysis, platforms))
        out["copy"] = copy

        plans: list[PostPlan] = _step(strategist, "strategist",
                                      lambda: strategist.plan(video, analysis, variants, copy))
        if not plans:
            raise StepFailed("strategist", RuntimeError("strategist produced no post plans"))
        db.update_video(video_id, status=VideoStatus.PLANNED)
        out["plans"] = [p.to_dict() for p in plans]

        if out["mode"] == "auto":
            post_ids = [db.add_post(video_id, p, status="scheduled") for p in plans]
            db.update_video(video_id, status=VideoStatus.SCHEDULED)
            emit(db, ACTOR, f"מצב אוטומטי: {len(post_ids)} פוסטים תוזמנו", "success", video_id)
        else:
            post_ids = [db.add_post(video_id, p, status="awaiting_approval") for p in plans]
            title = plans[0].title or video.title_hint or f"סרטון #{video_id}"
            out["approval_id"] = db.add_approval(Approval(
                id=None, kind="publish", requested_by="manager",
                title=f"פרסום \"{title}\" ב-{len(plans)} פלטפורמות",
                payload={"video_id": video_id, "post_ids": post_ids,
                         "plans": [p.to_dict() for p in plans], "publish_now": False,
                         "dry_run": ctx.settings.dry_run}))
            db.update_video(video_id, status=VideoStatus.AWAITING_APPROVAL)
            emit(db, ACTOR, f"תוכנית פרסום מחכה לאישורך (אישור #{out['approval_id']})",
                 "success", video_id)
        out["post_ids"] = post_ids
    except StepFailed as exc:
        db.update_video(video_id, status=VideoStatus.FAILED,
                        meta={"error": str(exc), "failed_step": exc.step})
        emit(db, ACTOR, f"סרטון #{video_id} נכשל בשלב {exc.step}: {exc}", "error", video_id)
        out.update(error=str(exc), failed_step=exc.step,
                   status=VideoStatus.FAILED.value)
        return out

    out["promotion_approval_ids"] = _promote(ctx, video_id)
    out["ok"] = True
    out["status"] = db.get_video(video_id).status.value  # type: ignore[union-attr]
    return out


def _promote(ctx: Context, video_id: int) -> list[int]:
    """Optional, non-fatal: promoter proposals (organic texts + approvals)."""
    if "promoter" not in (ctx.settings.get("staff.enabled", []) or []):
        return []
    video = ctx.db.get_video(video_id)
    promoter = Promoter(ctx)
    try:
        return [a.id for a in promoter.propose(video, ctx.db.list_posts(video_id))
                if a.id is not None]
    except Exception as exc:  # promotion is a bonus; never fail the video for it
        promoter.db.set_staff_status(promoter.name, "error", str(exc)[:300])
        emit(ctx.db, ACTOR, f"הצעות קידום נכשלו (לא קריטי): {exc}", "warning", video_id)
        return []


# ---------------------------------------------------------------------- approvals
def execute_approval(ctx: Context, approval_id: int, *, publish_now: bool | None = None) -> dict:
    """Carry out an owner-APPROVED approval. Pending/executed ones are refused; rejected
    publish approvals cancel their posts.

    publish: posts with a future ``scheduled_at`` become ``scheduled`` (scheduler publishes them);
    due/unscheduled ones (or all, with ``publish_now=True``) are published immediately.
    spend: finance re-checks the caps right now, then ledger + manual instructions.
    reply: community sends the reply. other/crosspost: publisher posts the stored plan.
    """
    db = ctx.db
    approval = db.get_approval(approval_id)
    if approval is None:
        return {"ok": False, "approval_id": approval_id, "error": "approval not found"}
    base = {"approval_id": approval_id, "kind": approval.kind}
    if approval.status == "rejected":
        return {**base, "ok": False, "error": "rejected by owner", **_on_rejected(ctx, approval)}
    if approval.status != "approved":
        return {**base, "ok": False, "error": f"approval is {approval.status}, not approved"}
    # Atomic claim (approved -> executing): only ONE caller - thread, double click or a second
    # process - may carry out the side effect. Everyone else stops here.
    if not db.claim_approval(approval_id):
        current = db.get_approval(approval_id)
        return {**base, "ok": False, "error": "approval is already being executed or done "
                f"({current.status if current else '?'})"}

    try:
        if approval.kind == "publish":
            result = _execute_publish(ctx, approval, publish_now)
        elif approval.kind == "spend":
            result = CFO(ctx).execute_spend(approval)  # marks executed in its own transaction
        elif approval.kind == "reply":
            sent = CommunityManager(ctx).send_reply(approval.payload or {})
            result = {"ok": sent, **({} if sent else {"error": "reply was not sent"})}
        elif (approval.payload or {}).get("action") == "crosspost":
            result = _execute_crosspost(ctx, approval)
        else:
            result = {"ok": True, "note": "manual action — nothing to execute automatically"}
    except Exception as exc:  # robust (LLMError included): report, never crash the caller
        db.release_approval(approval_id)  # nothing done -> the owner may run it again
        emit(db, ACTOR, f"ביצוע אישור #{approval_id} נכשל: {exc}", "error")
        return {**base, "ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # Publish approvals are done once every post was handled (failed posts are retried per
    # post with `agency retry`). A refused spend / unsent reply goes back to "approved" so
    # nothing is recorded as done and the owner can run it again later.
    if result.get("ok") or approval.kind == "publish":
        try:
            db.mark_approval_executed(approval_id)
        except ValueError:
            pass  # already executed (spend marks itself inside its transaction)
    else:
        db.release_approval(approval_id)
    emit(db, ACTOR, f"אישור #{approval_id} ({approval.kind}) בוצע"
         if result.get("ok") else f"אישור #{approval_id} ({approval.kind}) לא בוצע: "
         f"{result.get('error', '')}", "success" if result.get("ok") else "warning",
         (approval.payload or {}).get("video_id"))
    # Keep the manager's org-chart card current (it last said "waiting for approval #N").
    db.set_staff_status("manager", "idle",
                        f"אישור #{approval_id} בוצע" if result.get("ok")
                        else f"אישור #{approval_id} לא בוצע — ראה יומן פעילות")
    return {**base, **result}


def _execute_publish(ctx: Context, approval: Approval, publish_now: bool | None) -> dict:
    payload = approval.payload or {}
    now_flag = bool(payload.get("publish_now")) if publish_now is None else publish_now
    publisher = Publisher(ctx)
    now = datetime.now(timezone.utc)
    results: list[dict] = []
    scheduled: list[int] = []
    for pid in payload.get("post_ids") or []:
        post = ctx.db.get_post(int(pid))
        if post is None or post["status"] in ("published", "cancelled", "publishing"):
            continue
        when = parse_iso(post.get("scheduled_at"))
        if not now_flag and when is not None and when > now:
            ctx.db.update_post(post["id"], status="scheduled")
            scheduled.append(post["id"])
            continue
        results.append(publisher.publish(post["id"], publish_now=now_flag).to_dict())
    video_id = payload.get("video_id")
    if video_id is not None:
        publisher.refresh_video_status(int(video_id))
    ok = all(r.get("ok") for r in results)
    out: dict[str, Any] = {"ok": ok, "results": results, "scheduled_post_ids": scheduled,
                           "published": sum(1 for r in results if r.get("ok")),
                           "failed": sum(1 for r in results if not r.get("ok"))}
    if not ok:
        out["error"] = "; ".join(f"{r.get('platform')}: {r.get('error')}"
                                 for r in results if not r.get("ok"))
    return out


def _execute_crosspost(ctx: Context, approval: Approval) -> dict:
    payload = approval.payload or {}
    plan = PostPlan.from_dict(payload.get("plan") or {})
    if not plan.platform:
        return {"ok": False, "error": "crosspost approval has no plan"}
    post_id = ctx.db.add_post(int(payload.get("video_id") or 0), plan, status="planned")
    result = Publisher(ctx).publish(post_id)
    return {"ok": result.ok, "post_id": post_id, "results": [result.to_dict()],
            **({} if result.ok else {"error": result.error})}


def _on_rejected(ctx: Context, approval: Approval) -> dict:
    """Owner said no: cancel the posts of a rejected publish approval."""
    if approval.kind != "publish":
        return {}
    cancelled = []
    for pid in (approval.payload or {}).get("post_ids") or []:
        post = ctx.db.get_post(int(pid))
        if post and post["status"] in ("awaiting_approval", "planned", "scheduled"):
            ctx.db.update_post(post["id"], status="cancelled")
            cancelled.append(post["id"])
    vid = (approval.payload or {}).get("video_id")
    if vid is not None and cancelled:
        ctx.db.update_video(int(vid), status=VideoStatus.PLANNED)
    return {"cancelled_post_ids": cancelled}
