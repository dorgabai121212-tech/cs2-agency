"""The team's "brain": brand rules, copy, default schedule, manager voice and the home screen."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from agency.brand import ALWAYS_BLOCKED, DEFAULT_HASHTAGS, load_brand
from agency.models import Approval, PostPlan, PostResult, Variant, VideoStatus
from agency.staff._playbook import default_times, local_tz
from agency.staff.copywriter import Copywriter, finalize_copy

HEBREW_LONG = ("אייס מטורף במיראז' עם AWP, ה-1v4 הכי חולני שראיתם. " * 120).strip()


def _set(ctx, dotted: str, value) -> None:
    node = ctx.settings.raw
    *parents, last = dotted.split(".")
    for p in parents:
        node = node.setdefault(p, {})
    node[last] = value


def _local(ctx, dt: datetime) -> str:
    return dt.astimezone(local_tz(ctx.settings)).strftime("%H:%M")


def _at_local(ctx, day: str, hhmm: str) -> datetime:
    tz = local_tz(ctx.settings)
    return datetime.fromisoformat(f"{day}T{hhmm}").replace(tzinfo=tz)


def _published(ctx, vid: int, platform: str, *, real: bool, stats: dict | None = None,
               when: datetime | None = None, url: str | None = None) -> int:
    """A published post; ``real=False`` = training mode (dry run). Publish time = ``when``."""
    when = when or datetime.now(timezone.utc) - timedelta(seconds=1)
    plan = PostPlan(platform=platform, variant_path="v.mp4", title=f"{platform} title",
                    scheduled_at=when.astimezone(timezone.utc).isoformat())
    pid = ctx.db.add_post(vid, plan, status="published")
    ext = f"real-{pid}" if real else f"dryrun-{platform}-{pid}"
    ctx.db.update_post(pid, result=PostResult(platform=platform, ok=True, dry_run=not real,
                                              external_id=ext, url=url), stats=stats or {})
    if stats:  # stats were fetched later: the publish time is the slot, not updated_at
        p = ctx.db.get_post(pid)
        ctx.db.update_post(pid, stats={**p["stats"], "fetched_at": p["updated_at"]})
    return pid


def _backdate(ctx, post_id: int, when: datetime) -> None:
    iso = when.astimezone(timezone.utc).isoformat(timespec="seconds")
    with ctx.db.immediate() as conn:
        conn.execute("UPDATE posts SET scheduled_at=?, created_at=?, updated_at=? WHERE id=?",
                     (iso, iso, iso, post_id))


# ============================================================================ brand
def test_brand_defaults_from_config_and_fallbacks(settings):
    brand = load_brand(settings)
    assert brand.name == "Imagination" and brand.handle == "@imaginationcs2"
    assert brand.bio == "Legendary CS2 moments. Real demo. Shot like a movie."
    assert brand.hashtags == DEFAULT_HASHTAGS == ["#CS2", "#CounterStrike2", "#CS2Edit"]
    assert "pro + crazy AI" in brand.style_notes and "self-mocking" in brand.style_notes
    bare = load_brand(None)  # no config at all -> the owner's proven defaults
    assert bare.name == "Imagination" and bare.hashtags == DEFAULT_HASHTAGS
    assert {"aimbot", "hack", "case opening", "csgoroll", "skin gambling"} <= set(
        bare.blocked_words)


def test_blocked_words_found_removed_and_not_overmatched():
    brand = load_brand(None)
    hits = brand.blocked_in("FREE SKINS on CSGORoll, no aimbot just hacks #CaseOpening")
    assert {"free skins", "csgoroll", "aimbot", "hack", "case opening"} <= set(hits)
    assert brand.blocked_in("case-opening") and brand.blocked_in("Cheaters everywhere")
    # real CS2 phrasing must survive
    for ok in ("High stakes clutch on Mirage", "That AWP peek was a gamble",
               "Showcase opening night", "Deagle 3K, shot like a movie"):
        assert brand.blocked_in(ok) == [], ok
    text = "Line one has aimbot and free skins.\n- untouched bullet: stays exactly"
    cleaned = brand.clean(text)
    assert brand.blocked_in(cleaned) == []
    assert cleaned.splitlines()[1] == "- untouched bullet: stays exactly"
    assert brand.clean("#freeskins #CS2 #mirage") == "#CS2 #mirage"


def test_enforce_appends_brand_hashtags_within_limits():
    brand = load_brand(None)
    ig = brand.enforce("Deagle 3K on Mirage #fyp #a #b #c #d #e #aimbot", "instagram_reels")
    tags = re.findall(r"#\w+", ig)
    assert len(tags) <= 5 and "#fyp" not in tags and "#aimbot" not in tags
    assert {"#CS2", "#CounterStrike2", "#CS2Edit"} <= set(tags)
    x = brand.enforce("y" * 400, "x")
    assert len(x) <= 280 and x.endswith("#CS2 #CounterStrike2")
    assert "#" not in brand.enforce("Nice clip #cs2 #mirage", "reddit")
    yt = brand.enforce(HEBREW_LONG, "youtube")  # Hebrew = 2 bytes per letter
    assert len(yt.encode("utf-8")) <= 5000 and yt.endswith("#CS2 #CounterStrike2 #CS2Edit")
    assert len(yt) > 2000  # cut by bytes, not needlessly short
    once = brand.enforce("Real demo #CS2 #CounterStrike2 #CS2Edit", "youtube")
    assert once.count("#CS2Edit") == 1


def test_config_brand_overrides(ctx):
    _set(ctx, "brand.blocked_words", ["mozart"])
    _set(ctx, "brand.hashtags", ["#Imagination", "CS2 Edit"])
    _set(ctx, "brand.ai_label", True)
    _set(ctx, "brand.music_credit", "YouTube Audio Library")
    brand = load_brand(ctx.settings)
    assert brand.blocked_in("Mozart phonk") == ["mozart"]
    assert brand.blocked_in("aimbot")  # always blocked, whatever the owner's list says
    assert set(ALWAYS_BLOCKED) <= set(brand.blocked_words)
    assert brand.hashtags == ["#Imagination", "#CS2Edit"]
    assert brand.ai_line(False)  # ai_label: true = label every clip
    assert brand.music_line(None) == "Music: YouTube Audio Library"
    _set(ctx, "brand.ai_label", False)
    assert load_brand(ctx.settings).ai_line(True) == ""


def test_finalize_copy_rejects_blocked_title_and_orders_hashtags():
    brand = load_brand(None)
    raw = {"title": "Free skins! AWP ace", "description": "Insane aimbot vibes. Real demo.",
           "tags": ["cs2", "cs2 hacks", "mirage"],
           "hashtags": ["#fyp", "#freeskins", "#mirage", "#awp", "#ace", "#cs2clips"]}
    for platform in ("youtube", "youtube_shorts", "tiktok", "instagram_reels", "facebook",
                     "x", "reddit"):
        out = finalize_copy(platform, raw, brand, has_ai=True, music="Mozart phonk",
                            fallback_title="AWP ACE on Mirage")
        text = " ".join([out["title"], out["description"], *out["tags"], *out["hashtags"]])
        assert brand.blocked_in(text) == [], platform
        assert out["title"] == "AWP ACE on Mirage"  # rejected, not patched
        assert "#fyp" not in out["hashtags"]
        if platform == "reddit":
            assert out["hashtags"] == []
            continue
        assert out["description"].endswith("A short part was restyled with AI. The rest is "
                                           "the real demo.")
        assert "Music: Mozart phonk" in out["description"]
        brand_tags = [h for h in out["hashtags"] if h != "#Shorts"]
        assert brand_tags[:2] == ["#CS2", "#CounterStrike2"]
    yt = finalize_copy("youtube", raw, brand)
    assert yt["hashtags"] == ["#CS2", "#CounterStrike2", "#CS2Edit"]  # exactly the brand 3
    assert finalize_copy("youtube_shorts", raw, brand)["hashtags"][0] == "#Shorts"
    assert len(finalize_copy("tiktok", raw, brand)["hashtags"]) <= 5


def test_ai_label_survives_youtube_byte_limit():
    brand = load_brand(None)
    out = finalize_copy("youtube", {"title": "t", "description": HEBREW_LONG, "tags": [],
                                    "hashtags": []}, brand, has_ai=True)
    assert len(out["description"].encode("utf-8")) <= 5000
    assert out["description"].endswith(brand.ai_line(True))
    cap = finalize_copy("instagram_reels", {"title": "t", "description": "a" * 5000,
                                            "tags": [], "hashtags": ["#x"]}, brand, has_ai=True)
    assert len(cap["description"] + " " + " ".join(cap["hashtags"])) <= 2200
    assert cap["description"].endswith(brand.ai_line(True))


def test_copywriter_english_brand_copy_and_rogue_llm(ctx, tiny_video, monkeypatch):
    vid = ctx.db.add_video(str(tiny_video), "Deagle 3K on Mirage, AI")
    video = ctx.db.get_video(vid)
    analysis = {"clip_type": "highlight", "map": "Mirage", "weapons": ["Desert Eagle"],
                "summary_en": "A Deagle 3K on Mirage."}
    platforms = ["youtube", "youtube_shorts", "tiktok", "instagram_reels", "x", "reddit"]
    copy = Copywriter(ctx).write(video, analysis, platforms)
    assert copy["youtube"]["title"] == "Deagle 3K on Mirage"  # owner's hint, AI marker dropped
    for p in platforms:
        assert not re.search(r"[֐-׿]", copy[p]["title"] + copy[p]["description"]), p
    assert copy["youtube"]["hashtags"] == ["#CS2", "#CounterStrike2", "#CS2Edit"]
    assert "restyled with AI" in copy["tiktok"]["description"]  # note said "AI"
    assert copy["reddit"]["title"].isascii()

    def rogue(system, user, schema, *, mock_value):
        assert "Imagination" in system and "aimbot" in system  # brand is in the prompt
        return {"posts": [{"platform": p["platform"], "title": "aimbot ACE " * 30,
                           "description": "Use promo code CSGOROLL! " + HEBREW_LONG,
                           "tags": ["cs2 cheats", "mirage"],
                           "hashtags": ["#fyp", "#casebattle", *[f"#t{i}" for i in range(40)]]}
                          for p in mock_value["posts"]]}
    monkeypatch.setattr(ctx.llm, "complete_json", rogue)
    copy = Copywriter(ctx).write(video, analysis, platforms)
    brand = load_brand(ctx.settings)
    for p in platforms:
        c = copy[p]
        assert brand.blocked_in(" ".join([c["title"], c["description"], *c["tags"],
                                          *c["hashtags"]])) == [], p
    assert len(copy["youtube"]["description"].encode("utf-8")) <= 5000
    assert len(copy["instagram_reels"]["hashtags"]) <= 5
    assert len(copy["x"]["description"] + " " + " ".join(copy["x"]["hashtags"])) <= 280


# ============================================================================ schedule
def test_default_times_from_config(ctx):
    times = default_times(ctx.settings)
    assert times["youtube"] == (17, 0) and times["youtube_shorts"] == (17, 0)
    assert times["facebook"] == (17, 40) and times["instagram_reels"] == (20, 0)
    assert times["tiktok"] == (20, 40)
    _set(ctx, "publishing.default_times", {"tiktok": "21:15", "facebook": "bad"})
    times = default_times(ctx.settings)
    assert times["tiktok"] == (21, 15) and times["facebook"] == (17, 40)


def test_default_schedule_proven_times_and_instagram_after_youtube(ctx):
    from agency.staff.strategist import default_schedule

    now = _at_local(ctx, "2026-10-19", "10:00")
    plats = ["youtube_shorts", "tiktok", "instagram_reels", "facebook"]
    s = default_schedule(ctx.settings, plats, now)
    assert {p: _local(ctx, s[p]) for p in plats} == {
        "youtube_shorts": "17:00", "tiktok": "20:40", "instagram_reels": "20:00",
        "facebook": "17:40"}
    assert len({s[p].astimezone(local_tz(ctx.settings)).date() for p in plats}) == 1

    # after today's YouTube slot: the whole package moves to tomorrow, same proven times
    late = _at_local(ctx, "2026-10-19", "16:45")
    s = default_schedule(ctx.settings, plats, late)
    assert all(s[p].astimezone(local_tz(ctx.settings)).date().isoformat() == "2026-10-20"
               for p in plats)

    # YouTube late in the evening: Instagram/TikTok wait ≥30 min after the LAST YouTube post
    _set(ctx, "publishing.default_times", {"youtube": "19:50", "youtube_shorts": "19:50"})
    s = default_schedule(ctx.settings, ["youtube", "youtube_shorts", "instagram_reels",
                                        "tiktok"], now)
    last_yt = max(s["youtube"], s["youtube_shorts"])
    assert s["youtube"] != s["youtube_shorts"]  # same channel, never the same minute
    assert s["instagram_reels"] - last_yt >= timedelta(minutes=30)
    assert s["tiktok"] - last_yt >= timedelta(minutes=30)
    assert s["instagram_reels"] != s["tiktok"]


def _strategist_plan(ctx, tiny_video, monkeypatch, posts_fn, platforms):
    from agency.staff.strategist import Strategist

    def fake(system, user, schema, *, mock_value):
        return {"posts": posts_fn(mock_value), "overall": "o"}
    monkeypatch.setattr(ctx.llm, "complete_json", fake)
    for p in platforms:
        _set(ctx, f"publishing.platforms.{p}.enabled", True)
    vid = ctx.db.add_video(str(tiny_video), "ace")
    variants = [Variant(platform=p, path=str(tiny_video),
                        aspect="16:9" if p == "youtube" else "9:16") for p in platforms]
    plans = Strategist(ctx).plan(ctx.db.get_video(vid), {"clip_type": "ace"}, variants, {})
    return {p.platform: p for p in plans}


def test_strategist_deviation_needs_reason_and_gap_is_enforced(ctx, tiny_video, monkeypatch):
    plats = ["youtube_shorts", "instagram_reels", "tiktok"]

    def no_reason(mock):
        out = []
        for item in mock["posts"]:
            when = datetime.fromisoformat(item["publish_at_local"]) + timedelta(hours=2)
            out.append({**item, "publish_at_local": when.strftime("%Y-%m-%dT%H:%M"),
                        "deviation_reason": ""})
        return out
    plans = _strategist_plan(ctx, tiny_video, monkeypatch, no_reason, plats)
    assert {p: _local(ctx, datetime.fromisoformat(pl.scheduled_at))
            for p, pl in plans.items()} == {"youtube_shorts": "17:00",
                                            "instagram_reels": "20:00", "tiktok": "20:40"}
    assert all("deviation_reason" not in pl.extra for pl in plans.values())

    def shorts_late_with_reason(mock):
        out = []
        for item in mock["posts"]:
            if item["platform"] == "youtube_shorts":
                when = datetime.fromisoformat(item["publish_at_local"]) + timedelta(hours=3,
                                                                                    minutes=20)
                item = {**item, "publish_at_local": when.strftime("%Y-%m-%dT%H:%M"),
                        "deviation_reason": "גמר מייג'ור בשידור חי עד 20:00"}
            out.append(item)
        return out
    plans = _strategist_plan(ctx, tiny_video, monkeypatch, shorts_late_with_reason, plats)
    yt = datetime.fromisoformat(plans["youtube_shorts"].scheduled_at)
    assert _local(ctx, yt) == "20:20"
    assert "גמר מייג'ור" in plans["youtube_shorts"].extra["why"]
    assert plans["youtube_shorts"].extra["deviation_reason"]
    for p in ("instagram_reels", "tiktok"):  # pushed after the moved YouTube post
        assert datetime.fromisoformat(plans[p].scheduled_at) - yt >= timedelta(minutes=30)
    assert "חצי שעה אחרי יוטיוב" in plans["instagram_reels"].extra["why"]


def test_strategist_mock_plan_uses_default_times(ctx, tiny_video):
    from agency.staff.strategist import Strategist

    _set(ctx, "publishing.platforms.facebook.enabled", True)
    vid = ctx.db.add_video(str(tiny_video), "ACE on Mirage")
    variants = [Variant(platform=p, path=str(tiny_video), aspect="9:16")
                for p in ("youtube_shorts", "tiktok", "instagram_reels", "facebook")]
    plans = {p.platform: p for p in Strategist(ctx).plan(
        ctx.db.get_video(vid), {"clip_type": "ace", "ai_shots": True}, variants, {})}
    when = {p: datetime.fromisoformat(pl.scheduled_at) for p, pl in plans.items()}
    assert {p: _local(ctx, w) for p, w in when.items()} == {
        "youtube_shorts": "17:00", "facebook": "17:40", "instagram_reels": "20:00",
        "tiktok": "20:40"}
    assert when["instagram_reels"] - when["youtube_shorts"] >= timedelta(minutes=30)
    assert all(pl.extra["ai_label"] is True for pl in plans.values())  # AI clip flagged
    for pl in plans.values():
        assert pl.extra["why"] and not re.search(r"#\d|config|yaml", pl.extra["why"])


def test_strategist_respects_quiet_hours(ctx, tiny_video, monkeypatch):
    _set(ctx, "publishing.quiet_hours", {"enabled": True, "start": "16:30", "end": "18:10"})
    plans = _strategist_plan(ctx, tiny_video, monkeypatch, lambda mock: mock["posts"],
                             ["youtube_shorts", "instagram_reels"])
    yt = datetime.fromisoformat(plans["youtube_shorts"].scheduled_at)
    assert _local(ctx, yt) == "18:10"
    assert datetime.fromisoformat(plans["instagram_reels"].scheduled_at) - yt >= \
        timedelta(minutes=30)


# ============================================================================ manager
def test_manager_has_no_approve_tool_and_speaks_plainly(ctx):
    from agency.staff.manager import Manager

    m = Manager(ctx)
    names = {t.name for t in m.tools()}
    assert names == {"get_status", "list_approvals", "budget_status", "list_videos",
                     "list_posts", "run_analytics", "propose_promotion",
                     "schedule_reschedule", "explain_plan"}
    for n in names:
        assert not any(w in n for w in ("approve", "spend", "publish", "pay", "execute",
                                        "delete"))
    prompt = m.role_prompt
    assert "Hebrew" in prompt and "ONE question" in prompt and "yes/no" in prompt
    for q in ("מה המצב?", "מה עלה היום?", "תן לי רעיון לקליפ הבא", "כמה הוצאנו החודש?",
              "תאשר את הכל"):
        reply = m.chat(q)
        assert reply and reply.count("?") <= 1, q
        assert len([s for s in re.split(r"[.!?]\s", reply) if s.strip()]) <= 4, q
        assert not re.search(r"#\d|dry.?run|\bapi\b|token|json|config|pipeline", reply, re.I)
    assert "לא יכול לאשר" in m.chat("תאשר את הכל")
    assert "₪" in m.chat("כמה הוצאנו החודש?")


def test_manager_today_answer_is_truthful_in_training(ctx):
    from agency.staff.manager import Manager

    vid = ctx.db.add_video("clip.mp4", "Deagle 3K")
    _published(ctx, vid, "tiktok", real=False)
    reply = Manager(ctx).chat("מה עלה היום?")
    assert "אימון" in reply and "עלה ל" not in reply


def test_daily_brief_at_most_five_bullets(ctx, monkeypatch):
    from agency.staff.manager import Manager, limit_bullets

    brief = Manager(ctx).daily_brief()
    bullets = [ln for ln in brief.splitlines() if ln.startswith("- ")]
    assert 1 <= len(bullets) <= 5 and not re.search(r"#\d", brief)
    assert len(brief.splitlines()) <= 6

    def chatty(system, user, schema, *, mock_value):
        return {"brief_he": "בוקר טוב!\n" + "\n".join(f"• נקודה {i}" for i in range(9))}
    monkeypatch.setattr(ctx.llm, "complete_json", chatty)
    out = Manager(ctx).daily_brief()
    assert out.splitlines()[0] == "בוקר טוב!"
    assert len([ln for ln in out.splitlines() if ln.startswith("- ")]) == 5
    assert limit_bullets("a\nb\nc\nd\ne\nf\ng\nh").count("\n") == 5


def test_manager_status_lines_use_titles_not_ids(ctx, tiny_video):
    from agency.staff.manager import Manager

    Manager(ctx).handle_new_video(tiny_video, "Deagle 3K on Mirage")
    for row in ctx.db.list_staff_status():
        assert not re.search(r"#\d", row["detail"] or ""), row


# ============================================================================ promoter
def test_promoter_texts_follow_brand(ctx, tiny_video, monkeypatch):
    from agency.staff.promoter import Promoter

    vid = ctx.db.add_video(str(tiny_video), "AWP ace")
    video = ctx.db.get_video(vid)
    assert "Imagination" in Promoter(ctx).role_prompt
    Promoter(ctx).propose(video, [])
    promo = ctx.db.get_video(vid).meta["promotion"]
    assert promo["pinned_comment"].isascii() and "?" in promo["pinned_comment"]

    def rogue(system, user, schema, *, mock_value):
        return {**mock_value, "pinned_comment": "Free skins at csgoroll, who wants?",
                "community_post": "No aimbot needed"}
    monkeypatch.setattr(ctx.llm, "complete_json", rogue)
    Promoter(ctx).propose(video, [])
    promo = ctx.db.get_video(vid).meta["promotion"]
    brand = load_brand(ctx.settings)
    assert brand.blocked_in(promo["pinned_comment"] + promo["community_post"]) == []


# ============================================================================ home
def test_greeting_by_hour_in_channel_time_zone(ctx):
    from agency.home import home_sentence

    for hhmm, word in (("08:00", "בוקר טוב"), ("13:30", "צהריים טובים"),
                       ("19:00", "ערב טוב"), ("02:00", "לילה טוב")):
        line = home_sentence(ctx, _at_local(ctx, "2026-10-19", hhmm))
        assert line["greeting"] == word and line["text"].startswith(word)


def test_home_sentence_truth_table(ctx):
    from agency.home import home_sentence

    empty = home_sentence(ctx)
    assert "הראשון" in empty["sentence"] and empty["mood"] == "calm" and empty["training"]

    # training mode: a post "published" in training is ready, never "went up"
    a = ctx.db.add_video("a.mp4", "Deagle 3K")
    _published(ctx, a, "tiktok", real=False)
    line = home_sentence(ctx)
    assert "מוכן באימון" in line["sentence"] and "עלה" not in line["text"]
    assert line["counts"]["training_today"] == 1 and line["counts"]["live_today"] == 0

    # + a pending approval
    b = ctx.db.add_video("b.mp4", "AWP ace")
    ctx.db.add_approval(Approval(id=None, kind="publish", title="t",
                                 payload={"video_id": b, "post_ids": []}))
    line = home_sentence(ctx)
    assert "אחד מחכה לך" in line["sentence"] and "עלה" not in line["text"]
    assert line["mood"] == "attention"

    # really live posts today: now it may say "עלה"
    c = ctx.db.add_video("c.mp4", "Clutch")
    d = ctx.db.add_video("d.mp4", "Ace")
    _published(ctx, c, "youtube_shorts", real=True)
    _published(ctx, d, "instagram_reels", real=True)
    line = home_sentence(ctx)
    assert "שני סרטונים עלו היום" in line["sentence"]
    assert "אחד מחכה לך" in line["sentence"]
    assert line["counts"]["live_today"] == 2

    # a failure today is named first and plainly
    e = ctx.db.add_video("e.mp4", "Mozart phonk")
    pid = ctx.db.add_post(e, PostPlan(platform="tiktok", variant_path="v.mp4"))
    ctx.db.update_post(pid, status="failed")
    line = home_sentence(ctx)
    assert line["mood"] == "problem" and "נתקע" in line["sentence"]
    assert '"Mozart phonk"' in line["sentence"] and not re.search(r"#\d", line["text"])


def test_home_sentence_training_never_says_went_up(ctx):
    from agency.home import home_sentence

    for i in range(3):
        vid = ctx.db.add_video(f"{i}.mp4", f"clip {i}")
        _published(ctx, vid, "youtube_shorts", real=False)
        ctx.db.update_video(vid, status=VideoStatus.PUBLISHED)
    ctx.db.add_approval(Approval(id=None, kind="spend", title="ads", cost_ils=10))
    fresh = ctx.db.add_video("new.mp4", "Brand new clip")  # in progress (NEW)
    old = ctx.db.add_video("old.mp4", "old")
    ctx.db.update_video(old, status=VideoStatus.FAILED, meta={"failed_step": "editor"})
    line = home_sentence(ctx)
    assert "עלה" not in line["text"] and "עלו" not in line["text"]
    assert "שלושה סרטונים מוכנים באימון היום" in line["sentence"]
    assert "בקשה אחת" in line["sentence"]
    assert '"Brand new clip"' in line["sentence"]
    assert line["counts"]["problems"] == 1 and fresh


def test_home_sentence_names_scheduled_posts(ctx):
    """After the owner approves, the line says when the clip goes out (not "all calm")."""
    from agency.home import home_sentence

    now = _at_local(ctx, "2026-10-18", "10:00")
    vid = ctx.db.add_video("a.mp4", "Deagle 3K")
    for platform, hhmm in (("youtube_shorts", "17:00"), ("tiktok", "20:40")):
        ctx.db.add_post(vid, PostPlan(platform=platform, variant_path="v.mp4",
                                      scheduled_at=_at_local(ctx, "2026-10-18", hhmm).isoformat()),
                        status="scheduled")
    other = ctx.db.add_video("b.mp4", "Mozart phonk")
    ctx.db.add_post(other, PostPlan(platform="tiktok", variant_path="v.mp4",
                                    scheduled_at=_at_local(ctx, "2026-10-19", "20:40").isoformat()),
                    status="scheduled")
    line = home_sentence(ctx, now)
    assert "שני סרטונים מתוזמנים, הראשון להיום 17:00" in line["sentence"]
    assert line["counts"]["scheduled"] == 2 and "עלה" not in line["sentence"]
    one = home_sentence(ctx, _at_local(ctx, "2026-10-18", "21:00"))  # the first one went
    assert '"Mozart phonk" מתוזמן למחר 20:40' in one["sentence"]


def test_home_old_failures_do_not_nag(ctx):
    from agency.home import home_sentence

    vid = ctx.db.add_video("x.mp4", "old clip")
    pid = ctx.db.add_post(vid, PostPlan(platform="tiktok", variant_path="v.mp4"))
    ctx.db.update_post(pid, status="failed")
    later = datetime.now(timezone.utc) + timedelta(days=2)
    line = home_sentence(ctx, later)
    assert line["mood"] != "problem" and "נתקע" not in line["sentence"]


def test_results_summary_math(ctx):
    from agency.home import results_summary

    now = _at_local(ctx, "2026-10-18", "21:00")
    a = ctx.db.add_video("a.mp4", "Deagle 3K")
    b = ctx.db.add_video("b.mp4", "Mozart phonk")
    rows = [  # (video, platform, real, local day, stats)
        (a, "youtube_shorts", True, "2026-10-18", {"views": 1000, "likes": 50, "comments": 5}),
        (a, "tiktok", True, "2026-10-18", {"views": "500", "likes": 20, "comments": 2}),
        (b, "instagram_reels", True, "2026-10-14", {"views": 300, "likes": 30, "comments": 3}),
        (b, "youtube_shorts", True, "2026-10-10", {"views": 9999, "likes": 1, "comments": 1}),
        (b, "tiktok", False, "2026-10-17", {"views": 77777, "likes": 7, "comments": 7}),
    ]
    for vid, platform, real, day, stats in rows:
        pid = _published(ctx, vid, platform, real=real, stats=stats,
                         url=f"https://example.com/{platform}/{vid}")
        _backdate(ctx, pid, _at_local(ctx, day, "17:00"))
    r = results_summary(ctx, 7, now)
    assert (r["views"], r["likes"], r["comments"]) == (1800, 100, 10)
    assert r["series_days"][0] == "2026-10-12" and r["series_days"][-1] == "2026-10-18"
    assert r["series"] == [0, 0, 300, 0, 0, 0, 1500] and len(r["series"]) == 7
    assert r["best_video"] == {"title": "Deagle 3K", "views": 1500, "video_id": a,
                               "url": f"https://example.com/youtube_shorts/{a}"}
    assert r["per_platform"][0] == {"key": "youtube_shorts", "name_he": "יוטיוב Shorts",
                                    "views": 1000}
    assert [p["key"] for p in r["per_platform"]] == ["youtube_shorts", "tiktok",
                                                     "instagram_reels"]
    assert r["all_time"] == {"views": 11799, "likes": 101, "comments": 11, "posts": 4,
                             "videos": 2}
    assert r["has_data"] is True and r["stats_pending"] is False
    assert results_summary(ctx, 30, now)["views"] == 11799  # wider window
    empty = results_summary(ctx, 7, now + timedelta(days=30))
    assert empty["views"] == 0 and empty["best_video"] is None and empty["has_data"]


def test_results_include_imported_studio_history(ctx, studio_dir):
    from agency.home import results_summary
    from agency.importer import import_post_history

    assert import_post_history(ctx.db, studio_dir)["imported"] == 1
    published = datetime.fromtimestamp(1760000000, tz=timezone.utc)
    before = results_summary(ctx, 7, published + timedelta(days=2))
    # right after the import nothing was counted yet: "numbers on the way", not zeros
    assert before["has_data"] and before["stats_pending"] and before["all_time"]["videos"] == 1
    post = ctx.db.list_posts()[0]
    ctx.db.update_post(post["id"], stats={"views": 7600, "likes": 120, "comments": 9,
                                          "fetched_at": "2026-10-18T10:00:00+00:00"})
    r = results_summary(ctx, 7, published + timedelta(days=2))
    assert r["views"] == 7600 and r["all_time"]["posts"] == 1
    assert r["best_video"]["title"] == "SHORT EP3 MOZART PHONK"
    assert r["best_video"]["url"] == "https://www.youtube.com/shorts/w-rEUe6qMN8"
    later = results_summary(ctx, 7, published + timedelta(days=20))
    assert later["views"] == 0 and later["all_time"]["views"] == 7600


def test_team_view_statuses_and_plain_sentences(ctx):
    from agency.home import team_view

    ctx.db.set_staff_status("analyst", "working", 'צופה ב"Deagle 3K"')
    ctx.db.set_staff_status("publisher", "working", "[DRY RUN] מעלה פוסט #3 ל-youtube")
    ctx.db.set_staff_status("editor", "error", "FFmpegError: exit 1")
    ctx.db.set_staff_status("copywriter", "working", "כותב")
    with ctx.db.immediate() as conn:  # a "working" status nobody updated for hours = idle
        conn.execute("UPDATE staff_status SET updated_at=? WHERE name='copywriter'",
                     ((datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(),))
    team = team_view(ctx)
    assert team[0]["name"] == "manager" and team[0]["title_he"] == 'המנכ"ל'
    by = {t["name"]: t for t in team}
    assert by["analyst"] == {"name": "analyst", "title_he": "אנליסט וידאו",
                             "status": "working", "sentence": 'צופה ב"Deagle 3K"'}
    assert by["publisher"]["sentence"] == "מתרגל העלאה במצב אימון"
    assert by["editor"]["status"] == "problem" and "FFmpeg" not in by["editor"]["sentence"]
    assert by["copywriter"]["status"] == "idle"
    assert {t["status"] for t in team} <= {"idle", "working", "problem"}
    for t in team:
        assert not re.search(r"#\d|dry.?run|error", t["sentence"], re.I)


def test_processing_view_follows_real_status(ctx):
    from agency.home import processing_view

    new = ctx.db.add_video("n.mp4", "New clip")
    analyzed = ctx.db.add_video("a.mp4", "Analyzed clip")
    ctx.db.update_video(analyzed, status=VideoStatus.ANALYZED)
    edited = ctx.db.add_video("e.mp4", "Edited clip")
    ctx.db.update_video(edited, status=VideoStatus.EDITED)
    ctx.db.set_staff_status("strategist", "working", "בוחר")
    failed = ctx.db.add_video("f.mp4", "Broken clip")
    ctx.db.update_video(failed, status=VideoStatus.FAILED, meta={"failed_step": "editor"})
    ready = ctx.db.add_video("r.mp4", "Ready clip")
    ctx.db.update_video(ready, status=VideoStatus.AWAITING_APPROVAL)
    ctx.db.add_approval(Approval(id=None, kind="publish", title="t",
                                 payload={"video_id": ready, "post_ids": []}))
    rejected = ctx.db.add_video("x.mp4", "Not now clip")
    ctx.db.add_post(rejected, PostPlan(platform="tiktok", variant_path="v"), status="cancelled")
    ctx.db.update_video(rejected, status=VideoStatus.PLANNED)
    studio = ctx.db.add_video("studio:a1", "From studio")
    ctx.db.update_video(studio, meta={"source": "studio"})

    view = {item["video_title"]: item for item in processing_view(ctx)}
    assert set(view) == {"New clip", "Analyzed clip", "Edited clip", "Broken clip",
                         "Ready clip"}

    def states(title):
        return [s["state"] for s in view[title]["steps"]]
    assert [s["key"] for s in view["New clip"]["steps"]] == ["watch", "cut", "write", "plan",
                                                             "ready"]
    assert [s["label_he"] for s in view["New clip"]["steps"]] == [
        "צופה בקליפ", "חותך לאנכי", "כותב כותרות", "בוחר פלטפורמות", "מוכן לאישור"]
    assert states("New clip") == ["active", "todo", "todo", "todo", "todo"]
    assert states("Analyzed clip") == ["done", "active", "todo", "todo", "todo"]
    assert states("Edited clip") == ["done", "done", "done", "active", "todo"]
    assert states("Broken clip") == ["done", "failed", "todo", "todo", "todo"]
    assert states("Ready clip") == ["done"] * 5 and view["Ready clip"]["done"] is True
    assert view["New clip"]["video_id"] == new
    assert view["Broken clip"]["can_retry"] is True and view["New clip"]["can_retry"] is False
    later = datetime.now(timezone.utc) + timedelta(hours=13)
    # in-flight timelines go stale; a failed one stays (with "נסה שוב") as long as the
    # sentence says "משהו נתקע … אפשר לנסות שוב" (24 hours)
    assert [i["video_title"] for i in processing_view(ctx, later)] == ["Broken clip"]
    assert processing_view(ctx, later + timedelta(hours=12)) == []


def test_home_payload_shape(ctx):
    from agency.home import home_payload

    data = home_payload(ctx)
    assert set(data) == {"manager", "results", "team", "processing"}
    assert {"greeting", "sentence", "text", "mood", "training", "live_platforms",
            "counts"} <= set(data["manager"])
    assert {"views", "likes", "comments", "series", "best_video", "per_platform"} <= set(
        data["results"])


# ============================================================================ config
def test_example_config_brand_and_times_pass_the_settings_writer(settings):
    from agency.config import PACKAGE_ROOT
    from agency.settings_store import update_config

    example = yaml.safe_load((PACKAGE_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    assert example["publishing"]["default_times"] == {
        "youtube": "17:00", "youtube_shorts": "17:00", "facebook": "17:40",
        "instagram_reels": "20:00", "tiktok": "20:40"}
    assert example["brand"]["hashtags"] == ["#CS2", "#CounterStrike2", "#CS2Edit"]
    update_config(settings, {"brand": example["brand"],
                             "publishing": {"default_times": example["publishing"][
                                 "default_times"]}})
    assert load_brand(settings).name == "Imagination"


@pytest.mark.parametrize("platform", ["youtube", "youtube_shorts", "tiktok", "instagram_reels",
                                      "facebook", "x", "reddit", "discord"])
def test_mock_copy_is_brand_clean_for_every_platform(ctx, tiny_video, platform):
    vid = ctx.db.add_video(str(tiny_video), "")
    copy = Copywriter(ctx).write(ctx.db.get_video(vid), {"clip_type": "fail", "map": "Inferno"},
                                 [platform])[platform]
    brand = load_brand(ctx.settings)
    assert copy["title"] and brand.blocked_in(copy["title"] + copy["description"]) == []
    assert "my fail" not in copy["title"].lower()  # never a self-mocking gag


def test_internal_notes_never_reach_public_copy_and_go_to_the_owner(ctx, tiny_video, monkeypatch):
    """A worried writer's HOLD/"do not publish" notes are stripped; its concern is kept for Dor."""
    from agency.dashboard.views import approval_card
    from agency.models import Approval
    from agency.staff.copywriter import strip_internal_markers

    assert strip_internal_markers("[HOLD] Deagle 3K on Mirage") == "Deagle 3K on Mirage"
    assert strip_internal_markers("Clean line\nHOLD - DO NOT PUBLISH\nBio") == "Clean line\nBio"
    assert strip_internal_markers("Nothing to remove here") == "Nothing to remove here"

    vid = ctx.db.add_video(str(tiny_video), "Deagle 3K on Mirage")
    video = ctx.db.get_video(vid)
    analysis = {"clip_type": "highlight", "map": "Mirage", "weapons": ["Desert Eagle"]}

    def worried(system, user, schema, *, mock_value):
        assert "note_for_owner" in schema["properties"]
        return {"posts": [{"platform": p["platform"], "title": "[HOLD] Deagle 3K on Mirage",
                           "description": "HOLD - DO NOT PUBLISH\nA Deagle 3K on Mirage.",
                           "tags": ["cs2"], "hashtags": []} for p in mock_value["posts"]],
                "note_for_owner": "נראה שזה קליפ בדיקה, לא משחק."}
    monkeypatch.setattr(ctx.llm, "complete_json", worried)
    copy = Copywriter(ctx).write(video, analysis, ["youtube", "tiktok"])
    for p, c in copy.items():
        public = c["title"] + "\n" + c["description"]
        assert "HOLD" not in public and "DO NOT PUBLISH" not in public.upper(), p
    assert copy["youtube"]["title"] == "Deagle 3K on Mirage"
    assert ctx.db.get_video(vid).meta["copy_note_he"] == "נראה שזה קליפ בדיקה, לא משחק."

    aid = ctx.db.add_approval(Approval(id=None, kind="publish", title="t",
                                       payload={"video_id": vid, "plans": []}, cost_ils=0,
                                       status="pending", requested_by="manager",
                                       created_at="2026-10-18T10:00:00+00:00", decided_at=None,
                                       note=""))
    card = approval_card(ctx, ctx.db.get_approval(aid))
    assert card["note_he"] == "נראה שזה קליפ בדיקה, לא משחק."

    def calm(system, user, schema, *, mock_value):
        return {**mock_value, "note_for_owner": ""}
    monkeypatch.setattr(ctx.llm, "complete_json", calm)
    Copywriter(ctx).write(ctx.db.get_video(vid), analysis, ["youtube"])
    assert not ctx.db.get_video(vid).meta.get("copy_note_he")  # a rewrite without worries clears it
