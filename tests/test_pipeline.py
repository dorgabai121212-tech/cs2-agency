"""Pipeline + scheduler tests: mock LLM, dry-run, no network."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agency import pipeline, scheduler
from agency.models import Approval, PostPlan, PostResult, VideoStatus
from agency.platforms import registry
from agency.staff.manager import Manager


class FakeAdapter:
    """Records uploads; never touches the network."""

    uploads: list[PostPlan] = []

    def __init__(self, key: str, settings, ok: bool = True):
        self.key, self.settings, self.ok = key, settings, ok

    def upload(self, plan: PostPlan) -> PostResult:
        FakeAdapter.uploads.append(plan)
        if not self.ok:
            return PostResult(platform=self.key, ok=False, error="boom")
        return PostResult(platform=self.key, ok=True, dry_run=self.settings.dry_run,
                          external_id=f"dryrun-{self.key}-1")


@pytest.fixture
def fake_adapters(monkeypatch):
    FakeAdapter.uploads = []
    monkeypatch.setattr(registry, "get_adapter", lambda key, s: FakeAdapter(key, s))
    return FakeAdapter


def _set(ctx, dotted: str, value) -> None:
    node = ctx.settings.raw
    *parents, last = dotted.split(".")
    for p in parents:
        node = node.setdefault(p, {})
    node[last] = value


def _run(ctx, tiny_video, hint="funny moment on mirage"):
    return Manager(ctx).handle_new_video(tiny_video, hint)


# ---------------------------------------------------------------- approve mode
def test_full_pipeline_approve_then_execute(ctx, tiny_video, fake_adapters, no_network):
    res = _run(ctx, tiny_video)
    assert res["ok"], res
    assert res["status"] == VideoStatus.AWAITING_APPROVAL.value
    approval = ctx.db.get_approval(res["approval_id"])
    assert approval.kind == "publish" and approval.status == "pending"
    assert approval.payload["post_ids"] == res["post_ids"]
    posts = ctx.db.list_posts(res["video_id"])
    assert posts and all(p["status"] == "awaiting_approval" for p in posts)
    assert fake_adapters.uploads == []

    # Not approved yet → refused.
    assert pipeline.execute_approval(ctx, approval.id)["ok"] is False
    assert fake_adapters.uploads == []

    ctx.db.decide_approval(approval.id, True)
    out = pipeline.execute_approval(ctx, approval.id, publish_now=True)
    assert out["ok"] and out["published"] == len(posts)
    assert all(p["status"] == "published" for p in ctx.db.list_posts(res["video_id"]))
    assert ctx.db.get_video(res["video_id"]).status == VideoStatus.PUBLISHED
    assert ctx.db.get_approval(approval.id).status == "executed"
    # Executed approvals cannot be run twice.
    assert pipeline.execute_approval(ctx, approval.id)["ok"] is False
    assert len(fake_adapters.uploads) == len(posts)


def test_execute_keeps_future_posts_scheduled(ctx, tiny_video, fake_adapters):
    res = _run(ctx, tiny_video)
    ctx.db.decide_approval(res["approval_id"], True)
    out = pipeline.execute_approval(ctx, res["approval_id"])
    assert out["ok"] and out["published"] == 0
    assert set(out["scheduled_post_ids"]) == set(res["post_ids"])
    assert ctx.db.get_video(res["video_id"]).status == VideoStatus.SCHEDULED
    assert scheduler.tick(ctx) == []  # nothing due yet


def test_rejected_publish_cancels_posts(ctx, tiny_video, fake_adapters):
    res = _run(ctx, tiny_video)
    ctx.db.decide_approval(res["approval_id"], False, "not this one")
    out = pipeline.execute_approval(ctx, res["approval_id"])
    assert out["ok"] is False
    assert all(p["status"] == "cancelled" for p in ctx.db.list_posts(res["video_id"]))
    assert fake_adapters.uploads == []


def test_failed_upload_is_reported_honestly(ctx, tiny_video, monkeypatch):
    monkeypatch.setattr(registry, "get_adapter", lambda k, s: FakeAdapter(k, s, ok=False))
    res = _run(ctx, tiny_video)
    ctx.db.decide_approval(res["approval_id"], True)
    out = pipeline.execute_approval(ctx, res["approval_id"], publish_now=True)
    assert out["ok"] is False and "boom" in out["error"]
    posts = ctx.db.list_posts(res["video_id"])
    assert all(p["status"] == "failed" and p["error"] == "boom" for p in posts)
    assert ctx.db.get_video(res["video_id"]).status == VideoStatus.FAILED


# ---------------------------------------------------------------- auto mode
def test_auto_mode_schedules_without_approval(ctx, tiny_video, fake_adapters):
    _set(ctx, "publishing.mode", "auto")
    res = _run(ctx, tiny_video)
    assert res["ok"] and res["approval_id"] is None
    assert res["status"] == VideoStatus.SCHEDULED.value
    posts = ctx.db.list_posts(res["video_id"])
    assert posts and all(p["status"] == "scheduled" and p["scheduled_at"] for p in posts)
    assert [a for a in ctx.db.list_approvals("pending") if a.kind == "publish"] == []
    assert fake_adapters.uploads == []


# ---------------------------------------------------------------- robustness
def test_step_failure_marks_video_failed(ctx, tiny_video, monkeypatch):
    from agency.llm import LLMError
    from agency.staff.analyst import VideoAnalyst

    def broken(self, video):
        raise LLMError("API down")
    monkeypatch.setattr(VideoAnalyst, "analyze", broken)
    res = _run(ctx, tiny_video)
    assert res["ok"] is False and res["failed_step"] == "analyst"
    video = ctx.db.get_video(res["video_id"])
    assert video.status == VideoStatus.FAILED and "API down" in video.meta["error"]
    statuses = {s["name"]: s["status"] for s in ctx.db.list_staff_status()}
    assert statuses["analyst"] == "error"


def test_pipeline_unknown_video(ctx):
    assert pipeline.run_pipeline(ctx, 9999)["ok"] is False


# ---------------------------------------------------------------- spend approvals
def test_spend_approval_rechecks_caps_at_execution(ctx):
    _set(ctx, "finance.monthly_budget_ils", 100)
    _set(ctx, "finance.max_single_spend_ils", 60)
    a1 = ctx.db.add_approval(Approval(id=None, kind="spend", title="YT ads", cost_ils=60,
                                      payload={"platform": "youtube"}))
    a2 = ctx.db.add_approval(Approval(id=None, kind="spend", title="TT ads", cost_ils=60,
                                      payload={"platform": "tiktok"}))
    ctx.db.decide_approval(a1, True)
    ctx.db.decide_approval(a2, True)  # both approved, but together they exceed the budget
    r1 = pipeline.execute_approval(ctx, a1)
    assert r1["ok"] and r1["instructions"]
    r2 = pipeline.execute_approval(ctx, a2)
    assert r2["ok"] is False and r2["refused_by"] == "finance"
    assert ctx.db.month_totals(datetime.now(timezone.utc).strftime("%Y-%m"))["expense"] == 60
    assert ctx.db.get_approval(a1).status == "executed"
    assert ctx.db.get_approval(a2).status == "approved"  # not executed


def test_promoter_offers_paid_only_within_budget(ctx, tiny_video, fake_adapters):
    res = _run(ctx, tiny_video)
    assert [a for a in ctx.db.list_approvals("pending") if a.kind == "spend"] == []
    _set(ctx, "finance.monthly_budget_ils", 200)
    _set(ctx, "finance.max_single_spend_ils", 30)
    from agency.staff.promoter import Promoter
    video = ctx.db.get_video(res["video_id"])
    approvals = Promoter(ctx).propose(video, ctx.db.list_posts(video.id))
    spends = [a for a in approvals if a.kind == "spend"]
    assert spends and all(0 < a.cost_ils <= 30 for a in spends)
    assert all(ctx.db.get_approval(a.id).status == "pending" for a in spends)
    assert ctx.db.get_video(video.id).meta["promotion"]["pinned_comment"]


# ---------------------------------------------------------------- scheduler
def test_scheduler_publishes_only_due_posts(ctx, tiny_video, fake_adapters):
    vid = ctx.db.add_video(str(tiny_video))
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
    due = ctx.db.add_post(vid, PostPlan(platform="tiktok", variant_path=str(tiny_video),
                                        scheduled_at=past), status="scheduled")
    later = ctx.db.add_post(vid, PostPlan(platform="x", variant_path=str(tiny_video),
                                          scheduled_at=future), status="scheduled")
    waiting = ctx.db.add_post(vid, PostPlan(platform="youtube", variant_path=str(tiny_video),
                                            scheduled_at=past), status="awaiting_approval")
    results = scheduler.tick(ctx)
    assert [r.platform for r in results] == ["tiktok"] and results[0].ok
    assert ctx.db.get_post(due)["status"] == "published"
    assert ctx.db.get_post(later)["status"] == "scheduled"
    assert ctx.db.get_post(waiting)["status"] == "awaiting_approval"
    assert scheduler.tick(ctx) == []  # exactly once
    assert len(fake_adapters.uploads) == 1


def test_run_forever_stops(ctx, fake_adapters):
    import threading
    stop = threading.Event()
    t = threading.Thread(target=scheduler.run_forever, args=(ctx, 0.01, stop), daemon=True)
    t.start()
    stop.set()
    t.join(timeout=5)
    assert not t.is_alive()


# ---------------------------------------------------------------- real registry, dry-run
def test_full_pipeline_with_real_registry_dry_run(ctx, tiny_video, no_network):
    try:
        for key in registry.enabled_keys(ctx.settings):
            registry.adapter_class(key)
    except ImportError as exc:
        pytest.skip(f"platform adapters not available: {exc}")
    res = _run(ctx, tiny_video)
    assert res["ok"], res
    ctx.db.decide_approval(res["approval_id"], True)
    out = pipeline.execute_approval(ctx, res["approval_id"], publish_now=True)
    assert out["ok"], out
    for r in out["results"]:
        assert r["ok"] and r["dry_run"] and r["external_id"].startswith("dryrun-")
    assert ctx.db.get_video(res["video_id"]).status == VideoStatus.PUBLISHED
