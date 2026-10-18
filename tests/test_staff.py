"""Staff tests: copywriter limits, CFO caps, manager tools, analyst/editor/strategist in mock mode."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agency.models import Approval, PostPlan, Video, VideoStatus
from agency.staff.copywriter import Copywriter, enforce_limits, tags_total_len
from agency.staff.finance import CFO
from agency.staff.manager import Manager


def _video(ctx, path, hint="") -> Video:
    vid = ctx.db.add_video(str(path), hint)
    return ctx.db.get_video(vid)


def _set(ctx, dotted: str, value) -> None:
    node = ctx.settings.raw
    *parents, last = dotted.split(".")
    for p in parents:
        node = node.setdefault(p, {})
    node[last] = value


# ---------------------------------------------------------------- copywriter
def test_enforce_limits_youtube():
    raw = {"title": "ACE " * 80, "description": "x" * 9000,
           "tags": [f"counter strike tag {i}" for i in range(80)],
           "hashtags": ["cs2", "#ace", "#ace", "mirage clip"]}
    out = enforce_limits("youtube", raw)
    assert len(out["title"]) <= 100 and out["title"].endswith("…")
    assert len(out["description"]) <= 5000
    assert tags_total_len(out["tags"]) <= 500 and out["tags"]
    assert out["hashtags"] == ["#cs2", "#ace", "#mirageclip"]


def test_enforce_limits_short_form_and_x():
    shorts = enforce_limits("youtube_shorts", {"title": "t", "description": "d", "tags": [],
                                               "hashtags": ["#CS2"]})
    assert shorts["hashtags"][0] == "#Shorts"

    x = enforce_limits("x", {"title": "y" * 400, "description": "word " * 100,
                             "tags": [], "hashtags": ["#CS2", "#ace"]})
    assert len(x["title"]) <= 280
    assert len(x["description"] + " " + " ".join(x["hashtags"])) <= 280

    tags = [f"#tag{i}" for i in range(50)]
    ig = enforce_limits("instagram_reels", {"title": "t", "description": "a" * 3000,
                                            "tags": [], "hashtags": tags})
    assert len(ig["hashtags"]) <= 30
    assert len(ig["description"] + " " + " ".join(ig["hashtags"])) <= 2200

    tt = enforce_limits("tiktok", {"title": "t" * 3000, "description": "b" * 3000, "tags": [],
                                   "hashtags": ["#cs2"]})
    assert len(tt["title"]) <= 2200
    assert len(tt["description"] + " " + " ".join(tt["hashtags"])) <= 2200

    rd = enforce_limits("reddit", {"title": "r" * 500, "description": "d", "tags": [],
                                   "hashtags": ["#nope"]})
    assert len(rd["title"]) <= 300 and rd["hashtags"] == []


def test_copywriter_truncates_llm_output(ctx, tiny_video, monkeypatch):
    """Even if the model ignores the limits, the copywriter enforces them in code."""
    def huge(system, user, schema, *, mock_value):
        return {"posts": [{"platform": p["platform"], "title": "קלאץ' " * 100,
                           "description": "תיאור " * 2000, "tags": ["cs2 tag"] * 5 +
                           [f"tag number {i}" for i in range(100)],
                           "hashtags": [f"#h{i}" for i in range(60)]}
                          for p in mock_value["posts"]]}
    monkeypatch.setattr(ctx.llm, "complete_json", huge)
    video = _video(ctx, tiny_video, "clutch on inferno")
    platforms = ["youtube", "youtube_shorts", "tiktok", "instagram_reels", "x", "reddit"]
    copy = Copywriter(ctx).write(video, {"clip_type": "clutch", "map": "Inferno"}, platforms)
    assert set(copy) == set(platforms)
    assert len(copy["youtube"]["title"]) <= 100
    assert len(copy["youtube"]["description"]) <= 5000
    assert tags_total_len(copy["youtube"]["tags"]) <= 500
    assert len(copy["instagram_reels"]["hashtags"]) <= 30
    assert len(copy["x"]["description"]) <= 280
    assert len(copy["reddit"]["title"]) <= 300


def test_copywriter_mock_is_sensible(ctx, tiny_video):
    video = _video(ctx, tiny_video)
    copy = Copywriter(ctx).write(video, {"clip_type": "ace", "map": "Mirage",
                                         "weapons": ["AWP"]}, ["youtube", "reddit"])
    assert "Mirage" in copy["youtube"]["title"]
    assert "#CS2" in copy["youtube"]["hashtags"]
    assert copy["reddit"]["title"].isascii()  # reddit copy is English


# ---------------------------------------------------------------- finance
def test_finance_caps(ctx):
    cfo = CFO(ctx)
    ok, reason = cfo.can_spend(10)
    assert not ok and "0" in reason  # default budget 0 → nothing may be spent
    _set(ctx, "finance.monthly_budget_ils", 100)
    _set(ctx, "finance.max_single_spend_ils", 40)
    assert cfo.can_spend(40)[0]
    assert not cfo.can_spend(41)[0]
    assert not cfo.can_spend(0)[0] and not cfo.can_spend(-5)[0]
    cfo.record_expense(70, "ads", "previous campaign")
    assert not cfo.can_spend(35)[0]  # 70 + 35 > 100
    assert cfo.can_spend(30)[0]
    cfo.record_income(250, "AdSense", "September")
    st = cfo.budget_status()
    assert st == {**st, "budget": 100.0, "spent": 70.0, "remaining": 30.0, "income": 250.0,
                  "net": 180.0}
    report = cfo.monthly_report()
    assert "AdSense" in report and "70.00" in report


def test_finance_refuses_over_cap_even_if_approved(ctx):
    _set(ctx, "finance.monthly_budget_ils", 100)
    _set(ctx, "finance.max_single_spend_ils", 50)
    cfo = CFO(ctx)
    aid = ctx.db.add_approval(Approval(id=None, kind="spend", title="ads", cost_ils=80,
                                       payload={"platform": "youtube"}, requested_by="test"))
    approval = ctx.db.decide_approval(aid, True)
    res = cfo.execute_spend(approval)
    assert res["ok"] is False and res["refused_by"] == "finance"
    assert ctx.db.list_ledger() == []

    # A pending (not approved) spend is never executed either.
    pid = ctx.db.add_approval(Approval(id=None, kind="spend", title="ads", cost_ils=10))
    assert cfo.execute_spend(ctx.db.get_approval(pid))["ok"] is False
    assert ctx.db.list_ledger() == []


# ---------------------------------------------------------------- manager
def test_manager_has_no_approve_tool(ctx):
    names = {t.name for t in Manager(ctx).tools()}
    assert names == {"get_status", "list_approvals", "budget_status", "list_videos",
                     "list_posts", "run_analytics", "propose_promotion",
                     "schedule_reschedule", "explain_plan"}
    for n in names:
        assert not any(w in n for w in ("approve", "spend", "publish", "pay", "execute"))


def test_manager_prompt_states_iron_rules(ctx):
    prompt = Manager(ctx).role_prompt
    assert "אישור" in prompt and "בוטים" in prompt and "קניית צפיות" in prompt


def test_manager_chat_mock_and_tools_work(ctx, tiny_video):
    m = Manager(ctx)
    reply = m.chat("מה המצב?")
    assert "סרטונים" in reply
    assert "לא יכול לאשר" in m.chat("תאשר את הכל")
    tools = {t.name: t for t in m.tools()}
    assert tools["get_status"].fn({})["dry_run"] is True
    assert tools["budget_status"].fn({})["budget"] == 0
    assert isinstance(m.daily_brief(), str) and m.daily_brief()


def test_manager_reschedule_only_future_unpublished(ctx, tiny_video):
    m = Manager(ctx)
    vid = ctx.db.add_video(str(tiny_video))
    pid = ctx.db.add_post(vid, PostPlan(platform="x", variant_path=str(tiny_video)),
                          status="scheduled")
    tool = {t.name: t for t in m.tools()}["schedule_reschedule"]
    with pytest.raises(ValueError):
        tool.fn({"post_id": pid, "iso": "2000-01-01T10:00:00"})
    res = tool.fn({"post_id": pid, "iso": "2099-01-01T20:00:00+02:00"})
    assert res["scheduled_at"] == "2099-01-01T18:00:00+00:00"
    assert ctx.db.get_post(pid)["plan"]["scheduled_at"] == "2099-01-01T18:00:00+00:00"
    ctx.db.update_post(pid, status="published")
    with pytest.raises(ValueError):
        tool.fn({"post_id": pid, "iso": "2099-01-02T20:00:00"})


# ---------------------------------------------------------------- analyst / editor / strategist
def test_analyst_editor_strategist_mock(ctx, tiny_video):
    from agency.staff.analyst import VideoAnalyst
    from agency.staff.editor import VideoEditor
    from agency.staff.strategist import Strategist

    video = _video(ctx, tiny_video, "ACE on Mirage with the AK")
    analysis = VideoAnalyst(ctx).analyze(video)
    assert analysis["clip_type"] == "ace" and analysis["map"] == "Mirage"
    win = analysis["best_short_window"]
    assert 0 <= win["start"] < win["end"] and win["end"] - win["start"] <= 60

    variants = VideoEditor(ctx).make_variants(video, analysis)
    by_platform = {v.platform: v for v in variants}
    # config.example.yaml mirrors the owner's real accounts: Facebook on, X not connected yet
    assert set(by_platform) == {"youtube", "youtube_shorts", "tiktok", "instagram_reels",
                                "facebook"}
    from agency.media import have_ffmpeg
    assert by_platform["facebook"].path == str(tiny_video)  # a sane MP4 master goes out as-is
    if have_ffmpeg():  # v2 presets: YouTube always gets the 1440p version (§11.7)
        assert by_platform["youtube"].path.endswith("youtube_1440.mp4")
    else:
        assert by_platform["youtube"].path == str(tiny_video)
    if have_ffmpeg():
        assert by_platform["tiktok"].aspect == "9:16"
        assert by_platform["tiktok"].path != str(tiny_video)

    copy = Copywriter(ctx).write(video, analysis, list(by_platform))
    plans = Strategist(ctx).plan(video, analysis, variants, copy)
    assert plans and {p.platform for p in plans} <= set(by_platform)
    now = datetime.now(timezone.utc)
    for p in plans:
        assert p.extra["why"]
        assert datetime.fromisoformat(p.scheduled_at) > now


def test_strategist_ignores_disabled_platforms_from_llm(ctx, tiny_video, monkeypatch):
    from agency.models import Variant
    from agency.staff.strategist import Strategist

    def rogue(system, user, schema, *, mock_value):
        return {"posts": [{"platform": "facebook", "publish_at_local": "2099-01-01T20:00",
                           "why": "x"},
                          {"platform": "tiktok", "publish_at_local": "garbage", "why": "y"}],
                "overall": "o"}
    monkeypatch.setattr(ctx.llm, "complete_json", rogue)
    video = _video(ctx, tiny_video)
    variants = [Variant(platform="tiktok", path=str(tiny_video), aspect="9:16")]
    plans = Strategist(ctx).plan(video, {"clip_type": "funny"}, variants, {})
    assert [p.platform for p in plans] == ["tiktok"]  # facebook is disabled
    assert plans[0].scheduled_at  # garbage time → heuristic slot


def test_video_status_enum_used(ctx, tiny_video):
    video = _video(ctx, tiny_video)
    assert video.status == VideoStatus.NEW
