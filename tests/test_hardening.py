"""Regression tests for concurrency, recovery, limits and robustness fixes."""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from agency import pipeline
from agency.cli import app as cli_app
from agency.models import Approval, PostPlan, PostResult
from agency.platforms import registry

HEADERS = {"X-Agency-Request": "1"}
HEBREW_LONG = ("שחקן אחד נגד חמישה במיראז' עם אקדח בלבד ולא האמנתי שזה קרה " * 200).strip()


# ---------------------------------------------------------------- helpers
class CountingAdapter:
    """Fake adapter that records uploads (slowly, to widen race windows)."""

    uploads: list[str] = []
    lock = threading.Lock()

    def __init__(self, key, settings):
        self.key = key

    def upload(self, plan: PostPlan) -> PostResult:
        time.sleep(0.02)
        with self.lock:
            CountingAdapter.uploads.append(self.key)
        return PostResult(platform=self.key, ok=True, dry_run=True, external_id="x", url=None)


@pytest.fixture
def counting(monkeypatch):
    CountingAdapter.uploads = []
    monkeypatch.setattr(registry, "get_adapter", lambda key, s: CountingAdapter(key, s))
    return CountingAdapter


def _race(*fns):
    """Run callables at the same moment in threads; return their results / exceptions."""
    barrier = threading.Barrier(len(fns))
    out: list = [None] * len(fns)

    def wrap(i, fn):
        barrier.wait()
        try:
            out[i] = fn()
        except Exception as exc:  # noqa: BLE001 - collected for assertions
            out[i] = exc

    threads = [threading.Thread(target=wrap, args=(i, f)) for i, f in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


def _set(ctx, dotted: str, value) -> None:
    node = ctx.settings.raw
    *parents, last = dotted.split(".")
    for p in parents:
        node = node.setdefault(p, {})
    node[last] = value


def _post(ctx, status="scheduled", platform="youtube", scheduled_at=None) -> int:
    plan = PostPlan(platform=platform, variant_path="/nope.mp4", title="t", description="d",
                    tags=[], hashtags=[], scheduled_at=scheduled_at, extra={})
    vid = ctx.db.add_video("/nope.mp4", "x")
    return ctx.db.add_post(vid, plan, status=status)


def _age(ctx, table: str, row_id: int, column: str, iso: str = "2000-01-01T00:00:00+00:00"):
    with sqlite3.connect(ctx.db.path) as c:
        c.execute(f"UPDATE {table} SET {column}=? WHERE id=?", (iso, row_id))


# ---------------------------------------------------------------- approvals are atomic
def test_concurrent_decisions_only_one_wins(db):
    for _ in range(25):
        aid = db.add_approval(Approval(id=None, kind="other", title="x"))
        results = _race(lambda: db.decide_approval(aid, True),
                        lambda: db.decide_approval(aid, False))
        wins = [r for r in results if isinstance(r, Approval)]
        assert len(wins) == 1, results
        assert all(isinstance(r, ValueError) for r in results if r not in wins)
        assert db.get_approval(aid).status == wins[0].status


def test_mark_executed_and_claim_are_atomic(db):
    aid = db.add_approval(Approval(id=None, kind="other", title="x"))
    with pytest.raises(ValueError):
        db.mark_approval_executed(aid)
    db.decide_approval(aid, True)
    results = _race(lambda: db.claim_approval(aid), lambda: db.claim_approval(aid))
    assert sorted(results) == [False, True]
    db.mark_approval_executed(aid)
    with pytest.raises(ValueError):
        db.mark_approval_executed(aid)


def test_concurrent_execution_spends_once(ctx):
    _set(ctx, "finance.monthly_budget_ils", 100)
    _set(ctx, "finance.max_single_spend_ils", 60)
    for _ in range(10):
        aid = ctx.db.add_approval(Approval(id=None, kind="spend", title="ads", cost_ils=1))
        ctx.db.decide_approval(aid, True)
        results = _race(lambda: pipeline.execute_approval(ctx, aid),
                        lambda: pipeline.execute_approval(ctx, aid))
        assert sum(1 for r in results if r["ok"]) == 1, results
        assert ctx.db.get_approval(aid).status == "executed"
    assert len(ctx.db.list_ledger()) == 10


def test_concurrent_spends_never_exceed_monthly_cap(ctx):
    _set(ctx, "finance.monthly_budget_ils", 100)
    _set(ctx, "finance.max_single_spend_ils", 60)
    ids = []
    for name in ("a", "b"):
        aid = ctx.db.add_approval(Approval(id=None, kind="spend", title=name, cost_ils=60))
        ctx.db.decide_approval(aid, True)
        ids.append(aid)
    results = _race(*[lambda a=a: pipeline.execute_approval(ctx, a) for a in ids])
    assert sorted(r["ok"] for r in results) == [False, True]
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    assert ctx.db.month_totals(month)["expense"] == 60
    refused = next(r for r in results if not r["ok"])
    assert refused["refused_by"] == "finance"
    assert ctx.db.get_approval(refused["approval_id"]).status == "approved"


def test_reject_racing_approve_never_spends_after_reject(ctx):
    _set(ctx, "finance.monthly_budget_ils", 100)
    _set(ctx, "finance.max_single_spend_ils", 60)
    for _ in range(15):
        aid = ctx.db.add_approval(Approval(id=None, kind="spend", title="ads", cost_ils=1))

        def approve(aid=aid):
            ctx.db.decide_approval(aid, True)
            return pipeline.execute_approval(ctx, aid)

        def reject(aid=aid):
            ctx.db.decide_approval(aid, False)
            return pipeline.execute_approval(ctx, aid)

        _race(approve, reject)
        status = ctx.db.get_approval(aid).status
        spent = [e for e in ctx.db.list_ledger() if f"#{aid})" in e.description]
        assert (status == "executed") == bool(spent) and len(spent) <= 1


# ---------------------------------------------------------------- posts are claimed atomically
def test_concurrent_publish_uploads_once(ctx, counting):
    from agency.staff.publisher import Publisher

    for _ in range(10):
        pid = _post(ctx)
        results = _race(lambda: Publisher(ctx).publish(pid), lambda: Publisher(ctx).publish(pid))
        assert any(r.ok for r in results)
    assert len(counting.uploads) == 10


def test_concurrent_crosspost_execution_posts_once(ctx, counting):
    plan = PostPlan(platform="discord", variant_path="/nope.mp4", title="t", description="d",
                    tags=[], hashtags=[], scheduled_at=None, extra={})
    aid = ctx.db.add_approval(Approval(id=None, kind="other", title="crosspost",
                                       payload={"action": "crosspost", "video_id": 1,
                                                "plan": plan.to_dict()}))
    ctx.db.decide_approval(aid, True)
    _race(lambda: pipeline.execute_approval(ctx, aid), lambda: pipeline.execute_approval(ctx, aid))
    assert counting.uploads == ["discord"]


# ---------------------------------------------------------------- retry + recovery
def test_failed_post_can_be_retried(ctx, counting):
    from agency.staff.publisher import Publisher

    pid = _post(ctx, status="failed")
    result = Publisher(ctx).retry(pid)
    assert result.ok and ctx.db.get_post(pid)["status"] == "published"
    again = Publisher(ctx).retry(pid)
    assert not again.ok and "only failed" in again.error
    assert counting.uploads == ["youtube"]


def test_cli_retry(project_dir, counting):
    from agency.staff.base import build_context

    ctx = build_context()
    pid = _post(ctx, status="failed")
    result = CliRunner().invoke(cli_app, ["retry", str(pid)], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert ctx.db.get_post(pid)["status"] == "published"
    bad = CliRunner().invoke(cli_app, ["retry", str(pid)], catch_exceptions=False)
    assert bad.exit_code == 1


def test_dashboard_retry_endpoint(ctx, counting, no_network):
    from agency.dashboard.app import create_app

    application = create_app(ctx)
    application.state.inline_jobs = True
    pid = _post(ctx, status="failed")
    with TestClient(application, base_url="http://127.0.0.1:8765", headers=HEADERS) as c:
        assert c.post(f"/api/posts/{pid}/retry").json()["ok"] is True
        assert c.post(f"/api/posts/{pid}/retry").status_code == 409
    assert ctx.db.get_post(pid)["status"] == "published"


def test_recover_interrupted_upload_and_approval(ctx, counting):
    from agency.scheduler import recover_interrupted

    stuck = _post(ctx, status="publishing")
    fresh = _post(ctx, status="publishing")
    _age(ctx, "posts", stuck, "updated_at")
    aid = ctx.db.add_approval(Approval(id=None, kind="other", title="x"))
    ctx.db.decide_approval(aid, True)
    assert ctx.db.claim_approval(aid)
    _age(ctx, "approvals", aid, "decided_at")

    out = recover_interrupted(ctx)
    assert out == {"posts": [stuck], "approvals": [aid]}
    post = ctx.db.get_post(stuck)
    assert post["status"] == "failed" and "agency retry" in post["error"]
    assert ctx.db.get_post(fresh)["status"] == "publishing"  # may still be uploading
    assert ctx.db.get_approval(aid).status == "approved"
    assert counting.uploads == []  # never re-uploads on its own


def test_cli_approve_resumes_interrupted_approval(project_dir, counting):
    from agency.staff.base import build_context

    ctx = build_context()
    aid = ctx.db.add_approval(Approval(id=None, kind="other", title="manual"))
    ctx.db.decide_approval(aid, True)  # approved, then the process died before executing
    result = CliRunner().invoke(cli_app, ["approve", str(aid)], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert ctx.db.get_approval(aid).status == "executed"
    again = CliRunner().invoke(cli_app, ["approve", str(aid)], catch_exceptions=False)
    assert again.exit_code == 1


def test_dashboard_resumes_interrupted_approval(ctx, no_network):
    from agency.dashboard.app import create_app

    application = create_app(ctx)
    application.state.inline_jobs = True
    aid = ctx.db.add_approval(Approval(id=None, kind="other", title="manual"))
    ctx.db.decide_approval(aid, True)
    with TestClient(application, base_url="http://127.0.0.1:8765", headers=HEADERS) as c:
        assert [a["id"] for a in c.get("/api/approvals?status=open").json()] == [aid]
        assert c.post(f"/api/approvals/{aid}/approve", json={}).status_code == 200
        assert c.post(f"/api/approvals/{aid}/approve", json={}).status_code == 409
    assert ctx.db.get_approval(aid).status == "executed"


# ---------------------------------------------------------------- dashboard on a wildcard bind
def test_wildcard_bind_keeps_host_check_and_requires_key(ctx, no_network):
    from agency.dashboard.app import create_app

    _set(ctx, "dashboard.host", "0.0.0.0")
    application = create_app(ctx)
    application.state.inline_jobs = True
    key = application.state.access_key
    assert key
    aid = ctx.db.add_approval(Approval(id=None, kind="other", title="x"))

    with TestClient(application, base_url="http://attacker.example:8765", headers=HEADERS) as c:
        assert c.get("/api/status").status_code == 403
        c.cookies.set("agency_key", key)
        assert c.post(f"/api/approvals/{aid}/approve", json={}).status_code == 403
    assert ctx.db.get_approval(aid).status == "pending"

    with TestClient(application, base_url="http://127.0.0.1:8765", headers=HEADERS) as c:
        assert c.get("/api/status").status_code == 401  # no key
        assert c.get("/?key=wrong").status_code == 401
        page = c.get(f"/?key={key}")
        assert page.status_code == 200 and "agency_key" in page.headers.get("set-cookie", "")
        assert c.get("/api/status").status_code == 200  # cookie now carries the key
        res = c.post(f"/api/approvals/{aid}/approve", json={},
                     headers={"Origin": "http://evil.example"})
        assert res.status_code == 403
        assert c.post(f"/api/approvals/{aid}/approve", json={}).status_code == 200


def test_loopback_bind_needs_no_key(app_client_default):
    assert app_client_default.get("/api/status").status_code == 200


@pytest.fixture
def app_client_default(ctx, no_network):
    from agency.dashboard.app import create_app

    application = create_app(ctx)
    assert application.state.access_key is None
    with TestClient(application, base_url="http://127.0.0.1:8765", headers=HEADERS) as c:
        yield c


# ---------------------------------------------------------------- YouTube limits / --now
def test_youtube_description_fits_5000_utf8_bytes(settings):
    from agency.platforms.youtube import YouTubeAdapter
    from agency.staff.copywriter import enforce_limits

    copy = enforce_limits("youtube", {"title": "אייס", "description": HEBREW_LONG,
                                      "tags": ["CS2"], "hashtags": ["#CS2", "#קאונטר"]})
    assert len(copy["description"].encode("utf-8")) <= 5000
    plan = PostPlan(platform="youtube", variant_path="v.mp4", title=copy["title"],
                    description=HEBREW_LONG, tags=copy["tags"], hashtags=copy["hashtags"],
                    scheduled_at=None, extra={})
    body = YouTubeAdapter(settings).build_body(plan)
    desc = body["snippet"]["description"]
    assert len(desc.encode("utf-8")) <= 5000 and desc.endswith("#CS2 #קאונטר")
    assert len(desc) > 2000  # not cut needlessly short


def test_youtube_publish_now_skips_publish_at(settings):
    from agency.platforms.youtube import YouTubeAdapter

    future = "2099-01-01T20:00:00+00:00"
    plan = PostPlan(platform="youtube", variant_path="v.mp4", title="t", description="d",
                    tags=[], hashtags=[], scheduled_at=future, extra={})
    adapter = YouTubeAdapter(settings)
    assert "publishAt" in adapter.build_body(plan)["status"]
    plan.extra["publish_now"] = True
    status = adapter.build_body(plan)["status"]
    assert "publishAt" not in status and status["privacyStatus"] == "public"


def test_execute_publish_now_reaches_adapter(ctx, monkeypatch):
    seen = []

    class Spy(CountingAdapter):
        def upload(self, plan):
            seen.append(plan.extra.get("publish_now"))
            return super().upload(plan)

    monkeypatch.setattr(registry, "get_adapter", lambda key, s: Spy(key, s))
    pid = _post(ctx, status="awaiting_approval", scheduled_at="2099-01-01T20:00:00+00:00")
    aid = ctx.db.add_approval(Approval(id=None, kind="publish", title="p",
                                       payload={"post_ids": [pid]}))
    ctx.db.decide_approval(aid, True)
    assert pipeline.execute_approval(ctx, aid, publish_now=True)["ok"]
    assert seen == [True]


# ---------------------------------------------------------------- TikTok timeout is not success
def test_tiktok_processing_timeout_is_not_reported_published(settings, monkeypatch):
    from agency.platforms.tiktok import TikTokAdapter

    adapter = TikTokAdapter(settings)
    adapter.poll_interval_s = 0
    adapter.poll_max_s = 0
    monkeypatch.setattr(adapter, "_post", lambda *a, **k: {"status": "PROCESSING_UPLOAD"})
    result = adapter._wait_for_publish("v_pub_9")
    assert result.ok is False
    assert result.external_id == "v_pub_9" and "v_pub_9" in result.error


# ---------------------------------------------------------------- ffmpeg decoding
def test_ffmpeg_output_decoded_as_utf8(monkeypatch):
    from agency.media import ffmpeg

    captured = {}

    class Proc:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def fake_run(args, **kwargs):
        captured.update(kwargs, args=args)
        return Proc()

    monkeypatch.setattr(ffmpeg, "_bin", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(ffmpeg.subprocess, "run", fake_run)
    ffmpeg._run(["ffmpeg", "-i", "אייס.mp4", "out.jpg"])
    assert captured["encoding"] == "utf-8" and captured["errors"] == "replace"
    assert captured["args"][1] == "-hide_banner"


def test_probe_hebrew_filename(tmp_path):
    from agency.media import ffmpeg

    if not ffmpeg.have_ffmpeg():
        pytest.skip("ffmpeg not installed")
    import subprocess
    src = tmp_path / "אייס_מירז.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=duration=1:size=320x240:rate=10", "-pix_fmt", "yuv420p", str(src)],
                   check=True, capture_output=True, timeout=60)
    assert ffmpeg.probe(src)["width"] == 320
    thumb = ffmpeg.thumbnail(src, tmp_path / "תמונה.jpg", 0.2)
    assert thumb.exists()


# ---------------------------------------------------------------- config root + errors
def test_root_falls_back_to_install_dir(tmp_path, monkeypatch):
    import agency.config as config

    install = tmp_path / "install"
    elsewhere = tmp_path / "Videos"
    install.mkdir()
    elsewhere.mkdir()
    (install / "config.yaml").write_text("channel: {name: real}\n", encoding="utf-8")
    monkeypatch.setattr(config, "PACKAGE_ROOT", install)
    monkeypatch.delenv("AGENCY_ROOT", raising=False)
    monkeypatch.delenv("AGENCY_DATA_DIR", raising=False)
    monkeypatch.chdir(elsewhere)
    settings = config.load_settings()
    assert settings.root == install and settings.get("channel.name") == "real"
    assert settings.data_dir == install / "data"
    assert not (elsewhere / "data").exists()


def test_cli_reports_broken_yaml_in_hebrew(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENCY_MOCK_LLM", "1")
    bad = tmp_path / "bad.yaml"
    bad.write_text('channel: {name: "unclosed}\n', encoding="utf-8")
    result = CliRunner().invoke(cli_app, ["-c", str(bad), "status"])
    assert result.exit_code == 1
    assert "שגיאת כתיבה" in result.output and "Traceback" not in result.output


def test_cli_reports_bad_port_in_hebrew(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENCY_MOCK_LLM", "1")
    bad = tmp_path / "bad2.yaml"
    bad.write_text("dashboard: {port: abc}\nfinance: {monthly_budget_ils: lots}\n",
                   encoding="utf-8")
    result = CliRunner().invoke(cli_app, ["-c", str(bad), "dashboard", "--no-browser"])
    assert result.exit_code == 1
    assert "ערך לא תקין" in result.output and "Traceback" not in result.output


def test_status_prints_root_and_data_dir(project_dir: Path):
    result = CliRunner().invoke(cli_app, ["status"], catch_exceptions=False)
    assert result.exit_code == 0
    assert str(project_dir) in result.output.replace("\n", "")
