"""Core layer tests: config, DB CRUD, LLM mock mode, ffmpeg helpers, registry, adapters base."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agency.config import Settings, load_settings, secret
from agency.db import DB
from agency.llm import LLM, Tool
from agency.media import ffmpeg
from agency.models import Approval, LedgerEntry, PostPlan, PostResult, Video, VideoStatus, now_iso


# ---------------------------------------------------------------- config
def test_settings_loaded(settings: Settings, project_dir: Path):
    assert settings.dry_run is True
    assert settings.root == project_dir
    assert settings.get("llm.model") == "claude-opus-5-5"
    assert settings.get("llm.fallbacks") is True
    assert settings.get("publishing.mode") == "approve"
    assert settings.get("community.auto_reply") is False
    assert settings.get("promotion.discord_webhook_enabled") is False
    assert settings.get("finance.monthly_budget_ils", 99) == 0
    assert settings.get("does.not.exist", "dflt") == "dflt"
    assert settings.data_dir.is_dir() and settings.data_dir == project_dir / "data"
    assert settings.db_path.parent == settings.data_dir


def test_load_settings_fallback_to_example(project_dir: Path):
    (project_dir / "config.yaml").unlink()
    s = load_settings()
    assert s.dry_run is True
    assert s.get("dashboard.port") == 8765


def test_dry_run_false_and_env(project_dir: Path, monkeypatch):
    (project_dir / "config.yaml").write_text("dry_run: false\nllm: {effort: high}\n", "utf-8")
    (project_dir / ".env").write_text("SOME_TEST_SECRET=abc123\n", "utf-8")
    monkeypatch.delenv("SOME_TEST_SECRET", raising=False)
    s = load_settings(project_dir / "config.yaml")
    assert s.dry_run is False
    assert s.get("llm.effort") == "high"
    assert s.get("llm.model") == "claude-opus-5-5"  # defaults merged
    assert secret("SOME_TEST_SECRET") == "abc123"
    assert secret("DEFINITELY_NOT_SET_XYZ") is None


def test_example_config_has_no_secrets():
    text = (Path(__file__).resolve().parent.parent / "config.example.yaml").read_text("utf-8")
    for name in ("ANTHROPIC_API_KEY", "META_ACCESS_TOKEN", "DISCORD_WEBHOOK_URL"):
        assert name not in text


# ---------------------------------------------------------------- models
def test_models_to_dict_json():
    v = Video(id=1, path="a.mp4", title_hint="x", status=VideoStatus.NEW, created_at=now_iso(),
              analysis={}, meta={})
    d = v.to_dict()
    assert d["status"] == "new"
    json.dumps(d)
    plan = PostPlan(platform="youtube", variant_path="a.mp4", title="t", description="d",
                    tags=["cs2"], hashtags=["#cs2"], scheduled_at=None, extra={"why": "x"})
    assert PostPlan.from_dict(plan.to_dict()) == plan


# ---------------------------------------------------------------- db
def test_db_videos(db: DB):
    vid = db.add_video("/v/a.mp4", "ace on mirage")
    v = db.get_video(vid)
    assert v is not None and v.status == VideoStatus.NEW and v.title_hint == "ace on mirage"
    db.update_video(vid, status=VideoStatus.ANALYZED, analysis={"clip_type": "ace"},
                    meta={"a": 1})
    db.update_video(vid, meta={"b": 2})
    v = db.get_video(vid)
    assert v.status == VideoStatus.ANALYZED
    assert v.analysis == {"clip_type": "ace"}
    assert v.meta == {"a": 1, "b": 2}
    db.add_video("/v/b.mp4")
    assert [x.path for x in db.list_videos()] == ["/v/b.mp4", "/v/a.mp4"]
    assert len(db.list_videos(limit=1)) == 1
    assert db.get_video(999) is None


def test_db_posts(db: DB):
    vid = db.add_video("/v/a.mp4")
    plan = PostPlan(platform="tiktok", variant_path="/v/a_v.mp4", title="t", description="d",
                    tags=[], hashtags=["#cs2"], scheduled_at="2026-01-01T10:00:00+00:00",
                    extra={})
    pid = db.add_post(vid, plan)
    posts = db.list_posts(vid)
    assert len(posts) == 1 and posts[0]["status"] == "planned"
    assert posts[0]["plan"]["hashtags"] == ["#cs2"]
    assert posts[0]["scheduled_at"] == "2026-01-01T10:00:00+00:00"
    res = PostResult(platform="tiktok", ok=True, dry_run=True, external_id="x1", url=None,
                     error=None)
    db.update_post(pid, status="published", result=res, stats={"views": 5})
    p = db.get_post(pid)
    assert p["status"] == "published" and p["external_id"] == "x1"
    assert p["result"]["dry_run"] is True and p["stats"] == {"views": 5}
    assert len(db.list_posts()) == 1
    assert db.list_posts(video_id=12345) == []


def test_db_approvals(db: DB):
    a = Approval(id=None, kind="spend", title="TikTok promo", payload={"x": 1}, cost_ils=50.0,
                 status="approved", requested_by="promoter", created_at=now_iso(),
                 decided_at=None, note="")
    aid = db.add_approval(a)
    got = db.get_approval(aid)
    assert got.status == "pending"  # can never be created pre-approved
    assert got.cost_ils == 50.0 and got.payload == {"x": 1}
    assert [x.id for x in db.list_approvals()] == [aid]
    with pytest.raises(ValueError):
        db.mark_approval_executed(aid)  # not approved yet
    decided = db.decide_approval(aid, True, note="ok")
    assert decided.status == "approved" and decided.decided_at and decided.note == "ok"
    with pytest.raises(ValueError):
        db.decide_approval(aid, False)  # already decided
    db.mark_approval_executed(aid)
    assert db.get_approval(aid).status == "executed"
    assert db.list_approvals() == []
    assert len(db.list_approvals(None)) == 1
    aid2 = db.add_approval(Approval(id=None, kind="publish", title="p", payload={}, cost_ils=0,
                                    status="pending", requested_by="manager",
                                    created_at=now_iso(), decided_at=None, note=""))
    assert db.decide_approval(aid2, False).status == "rejected"
    assert len(db.list_approvals("rejected")) == 1
    with pytest.raises(KeyError):
        db.decide_approval(4242, True)


def test_db_ledger(db: DB):
    month = now_iso()[:7]
    db.add_ledger(LedgerEntry(id=None, kind="expense", amount_ils=30, category="ads",
                              description="d", platform="tiktok", video_id=None,
                              created_at=now_iso()))
    db.add_ledger(LedgerEntry(id=None, kind="income", amount_ils=100.5, category="adsense",
                              description="", platform="youtube", video_id=None,
                              created_at=now_iso()))
    db.add_ledger(LedgerEntry(id=None, kind="expense", amount_ils=10, category="old",
                              description="", platform=None, video_id=None,
                              created_at="2020-01-05T00:00:00+00:00"))
    assert len(db.list_ledger()) == 3
    assert len(db.list_ledger(month)) == 2
    assert db.month_totals(month) == {"expense": 30.0, "income": 100.5, "net": 70.5}
    assert db.month_totals("2020-01") == {"expense": 10.0, "income": 0.0, "net": -10.0}
    with pytest.raises(ValueError):
        db.add_ledger(LedgerEntry(id=None, kind="gift", amount_ils=1, category="",
                                  description="", platform=None, video_id=None,
                                  created_at=now_iso()))


def test_db_events_staff_kv(db: DB):
    db.log_event("manager", "hello", video_id=3)
    db.log_event("editor", "cut", level="warning")
    ev = db.list_events()
    assert ev[0]["actor"] == "editor" and ev[0]["level"] == "warning"
    assert ev[1]["video_id"] == 3
    assert len(db.list_events(limit=1)) == 1
    db.set_staff_status("editor", "working", "cutting")
    db.set_staff_status("editor", "idle")
    st = db.list_staff_status()
    assert len(st) == 1 and st[0]["status"] == "idle" and st[0]["detail"] == ""
    assert db.kv_get("missing", "d") == "d"
    db.kv_set("k", {"a": [1, 2]})
    assert db.kv_get("k") == {"a": [1, 2]}
    db.kv_set("k", 5)
    assert db.kv_get("k") == 5


def test_db_reopen_persists(settings):
    DB(settings.db_path).add_video("x.mp4")
    assert len(DB(settings.db_path).list_videos()) == 1


# ---------------------------------------------------------------- events / context
def test_emit_and_staff_status(ctx):
    from agency.events import emit
    from agency.staff.base import StaffMember

    emit(ctx.db, "manager", "boot")
    member = StaffMember(ctx)
    member.name = "copywriter"
    member.status("working", "writing titles")
    assert ctx.db.list_staff_status()[0]["name"] == "copywriter"
    msgs = [e["message"] for e in ctx.db.list_events()]
    assert "boot" in msgs and any("writing titles" in m for m in msgs)


# ---------------------------------------------------------------- llm (mock)
def test_llm_mock_detection(settings, monkeypatch):
    assert LLM(settings).mock is True
    monkeypatch.delenv("AGENCY_MOCK_LLM")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert LLM(settings).mock is False
    monkeypatch.setenv("AGENCY_MOCK_LLM", "1")
    assert LLM(settings).mock is True


def test_llm_mock_complete(ctx, no_network):
    a = ctx.llm.complete("sys", "hello there")
    b = ctx.llm.complete("sys", "hello there")
    assert a == b and isinstance(a, str) and a
    assert ctx.llm.complete("sys", [{"type": "text", "text": "hi"}])


def test_llm_mock_complete_json(ctx, no_network):
    mock = {"clip_type": "ace", "weapons": ["ak47"]}
    out = ctx.llm.complete_json("sys", "u", {"type": "object"}, mock_value=mock)
    assert out == mock
    out["weapons"].append("awp")
    assert mock["weapons"] == ["ak47"]  # deep copy


def test_llm_mock_run_tools_does_not_call_tools(ctx, no_network):
    called = []
    tool = Tool(name="get_status", description="status", fn=lambda i: called.append(i) or "x")
    assert ctx.llm.run_tools("sys", "status?", [tool], mock_text="הכול תקין") == "הכול תקין"
    assert called == []


def test_image_block(tmp_path):
    p = tmp_path / "f.png"
    p.write_bytes(b"\x89PNG\r\n")
    blk = LLM.image_block(p)
    assert blk["type"] == "image" and blk["source"]["media_type"] == "image/png"


# ---------------------------------------------------------------- llm (real mode, fake client)
class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Resp:
    def __init__(self, stop_reason, content):
        self.stop_reason = stop_reason
        self.content = content


class _FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class _FakeClient:
    def __init__(self, responses):
        self.messages = _FakeMessages(responses)
        self.beta = type("B", (), {"messages": self.messages})()


def _real_llm(settings, monkeypatch, responses):
    monkeypatch.delenv("AGENCY_MOCK_LLM")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    llm = LLM(settings)
    llm._client = _FakeClient(responses)
    return llm


def test_llm_real_run_tools_loop(settings, monkeypatch):
    responses = [
        _Resp("tool_use", [_Block(type="text", text="checking"),
                           _Block(type="tool_use", id="t1", name="ok_tool", input={"a": 1}),
                           _Block(type="tool_use", id="t2", name="bad_tool", input={})]),
        _Resp("end_turn", [_Block(type="text", text="done")]),
    ]
    llm = _real_llm(settings, monkeypatch, responses)

    def bad(_):
        raise RuntimeError("boom")

    tools = [Tool("ok_tool", "ok", {"type": "object", "properties": {}}, lambda i: f"got {i['a']}"),
             Tool("bad_tool", "bad", {"type": "object", "properties": {}}, bad)]
    assert llm.run_tools("sys", "go", tools) == "done"
    calls = llm.client.messages.calls
    first = calls[0]
    assert first["betas"] == ["server-side-fallback-2026-07-01"] and first["fallbacks"] == "default"
    assert first["output_config"] == {"effort": "medium"}
    assert "temperature" not in first and "thinking" not in first and "tool_choice" not in first
    second_msgs = calls[1]["messages"]
    assert second_msgs[1]["role"] == "assistant"
    results = second_msgs[2]["content"]
    assert second_msgs[2]["role"] == "user" and len(results) == 2
    assert results[0]["content"] == "got 1" and "is_error" not in results[0]
    assert results[1]["is_error"] is True and "boom" in results[1]["content"]


def test_llm_real_json_and_refusal(settings, monkeypatch):
    from agency.llm import LLMRefusal

    llm = _real_llm(settings, monkeypatch, [
        _Resp("end_turn", [_Block(type="text", text='{"score": 7}')]),
        _Resp("refusal", []),
    ])
    schema = {"type": "object", "properties": {"score": {"type": "integer"}}, "required": ["score"]}
    assert llm.complete_json("s", "u", schema, mock_value={}) == {"score": 7}
    fmt = llm.client.messages.calls[0]["output_config"]
    assert fmt["effort"] == "medium" and fmt["format"]["type"] == "json_schema"
    assert fmt["format"]["schema"]["additionalProperties"] is False
    with pytest.raises(LLMRefusal):
        llm.complete("s", "u")


# ---------------------------------------------------------------- ffmpeg
needs_ffmpeg = pytest.mark.skipif(not ffmpeg.have_ffmpeg(), reason="ffmpeg not installed")


def test_probe_missing_file(tmp_path):
    assert ffmpeg.probe(tmp_path / "nope.mp4") == {}


@needs_ffmpeg
def test_ffmpeg_helpers(tiny_video: Path, tmp_path: Path):
    info = ffmpeg.probe(tiny_video)
    assert info["width"] == 1280 and info["height"] == 720
    assert 2.5 <= info["duration"] <= 3.5 and info["fps"] == 30

    vert = ffmpeg.vertical_clip(tiny_video, tmp_path / "out" / "v.mp4", 0.5, 2.5)
    vinfo = ffmpeg.probe(vert)
    assert (vinfo["width"], vinfo["height"]) == (1080, 1920)
    assert 1.5 <= vinfo["duration"] <= 2.5

    cut = ffmpeg.trim(tiny_video, tmp_path / "cut.mp4", 1, 2)
    assert ffmpeg.probe(cut)["width"] == 1280

    thumb = ffmpeg.thumbnail(tiny_video, tmp_path / "t.jpg", 1.0)
    assert thumb.exists() and thumb.stat().st_size > 0

    frames = ffmpeg.grab_frames(tiny_video, tmp_path / "frames", n=4)
    assert len(frames) == 4 and all(f.exists() for f in frames)


def test_ffmpeg_missing(monkeypatch, tiny_video, tmp_path):
    monkeypatch.setattr(ffmpeg.shutil, "which", lambda name: None)
    assert ffmpeg.have_ffmpeg() is False
    assert ffmpeg.probe(tiny_video) == {}
    assert ffmpeg.grab_frames(tiny_video, tmp_path) == []
    with pytest.raises(ffmpeg.FFmpegError):
        ffmpeg.thumbnail(tiny_video, tmp_path / "t.jpg", 0)


# ---------------------------------------------------------------- platforms base / registry
def test_registry_mapping():
    from agency.platforms.registry import ADAPTERS, ALL_KEYS

    assert ALL_KEYS == ["youtube", "youtube_shorts", "tiktok", "instagram_reels", "facebook",
                        "x", "reddit", "discord"]
    assert set(ADAPTERS) == set(ALL_KEYS)
    assert ADAPTERS["x"] == "x_twitter:XAdapter"


def test_registry_enabled_and_unknown(settings):
    from agency.platforms.registry import enabled_keys, get_adapter

    assert "youtube" in enabled_keys(settings) and "facebook" not in enabled_keys(settings)
    with pytest.raises(KeyError):
        get_adapter("myspace", settings)


def test_base_adapter_dry_run_and_fail_closed(settings, no_network, project_dir):
    from agency.config import load_settings
    from agency.platforms.base import PlatformAdapter

    class Dummy(PlatformAdapter):
        key = "dummy"
        required_secrets = ["DUMMY_TOKEN_FOR_TEST"]

    plan = PostPlan(platform="dummy", variant_path="a.mp4", title="t", description="",
                    tags=[], hashtags=[], scheduled_at=None, extra={})
    res = Dummy(settings).upload(plan)
    assert res.ok and res.dry_run and res.external_id.startswith("dryrun-")

    (project_dir / "config.yaml").write_text("dry_run: false\n", "utf-8")
    live = load_settings(project_dir / "config.yaml")
    res = Dummy(live).upload(plan)
    assert res.ok is False and "DUMMY_TOKEN_FOR_TEST" in res.error
    assert Dummy(live).configured() is False
