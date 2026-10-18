"""Integration / owner-experience fixes found in the end-to-end owner demo:

- an offline check is a calm "not checked right now" tile, not "needs fixing", and is
  re-checked soon (and the quick read keeps showing the last known state);
- one YouTube upload per clip (YouTube and Shorts are the same channel);
- the manager's offline reply to "שנה משהו" points to the card's editor;
- the scheduler pulls stats every 6 hours (read-only, quiet), so the home numbers move;
- the studio import reports what it brought; the activity feed is not flooded by statuses.
"""
from __future__ import annotations

import json
import shutil
import time
from datetime import datetime, timedelta, timezone

import pytest
import requests

from agency import connect
from agency.dashboard import views
from agency.models import Approval, PostPlan, PostResult, Variant

ZKEY = "zr_SECRET_zernio_key_0001"


class _Resp:
    def __init__(self, status: int = 200, payload=None):
        self.status_code = status
        self._payload = {} if payload is None else payload
        self.content = json.dumps(self._payload).encode()

    def json(self):
        return self._payload


def _offline(*_a, **_k):
    raise requests.ConnectionError("no route to host")


# ----------------------------------------------------------------- offline = "unchecked"
def test_offline_check_is_calm_not_needs_fix(settings, fake_keyring, monkeypatch):
    fake_keyring.store[("cs2studio", "zernio")] = ZKEY
    monkeypatch.setattr(requests, "request", _offline)
    result = connect.verify("youtube", settings, use_cache=False)
    assert result["connected"] is False and result["unreachable"] is True
    assert result["problem"] == connect.NO_NET_HE

    tile = views.tile_view(connect.platform_state("youtube", settings))
    assert tile["state"] == "unchecked" and tile["status_he"] == views.UNCHECKED_STATUS_HE
    assert tile["note_he"] == views.UNCHECKED_NOTE_HE
    assert tile["source_he"] == views.STUDIO_SOURCE_HE  # still "via the existing studio"
    assert tile["unreachable"] is True and tile["checked"] is True
    assert ZKEY not in json.dumps(tile, ensure_ascii=False)


def test_service_down_is_unreachable_but_bad_key_needs_fix(settings, monkeypatch):
    monkeypatch.setenv("ZERNIO_API_KEY", ZKEY)
    monkeypatch.setattr(requests, "request", lambda *a, **k: _Resp(503))
    down = connect.verify("youtube", settings, use_cache=False)
    assert down["unreachable"] is True and down["problem"] == connect.DOWN_HE
    monkeypatch.setattr(requests, "request", lambda *a, **k: _Resp(401))
    bad = connect.verify("youtube", settings, use_cache=False)
    assert "unreachable" not in bad and bad["problem"] == connect.BAD_KEY_HE
    assert views.tile_view(connect.platform_state("youtube", settings))["state"] == "needs_fix"


def test_unreachable_is_rechecked_soon_and_quick_read_keeps_it(settings, monkeypatch, db):
    monkeypatch.setenv("ZERNIO_API_KEY", ZKEY)
    calls = []

    def offline(*a, **k):
        calls.append(1)
        raise requests.ConnectionError("down")

    monkeypatch.setattr(requests, "request", offline)
    connect.verify("tiktok", settings, db=db)
    connect.verify("tiktok", settings, db=db)
    assert len(calls) == 1  # cached for a minute
    real_time = time.time
    monkeypatch.setattr(connect.time, "time", lambda: real_time() + connect.UNREACHABLE_TTL_S + 1)
    connect.verify("tiktok", settings, db=db)
    assert len(calls) == 2  # ...then checked again (not after 10 minutes)
    monkeypatch.setattr(connect.time, "time", lambda: real_time() + 3600)
    quick = connect.platform_state("tiktok", settings, db, check=False)  # no network
    assert len(calls) == 2 and quick["unreachable"] is True and quick["connected"] is False


def test_half_filled_connection_is_not_a_red_fix(settings, no_network):
    """Meta ids came from the studio, the key is not pasted yet: "almost connected"."""
    assert views.PARTIAL_HE == connect.PARTIAL_HE
    settings.raw["meta"] = {"page_id": "102030405060708", "ig_user_id": "17841400000000001"}
    tile = views.tile_view(connect.platform_state("instagram_reels", settings))
    assert tile["missing"] == ["META_ACCESS_TOKEN"]
    assert tile["state"] == "not_connected" and tile["status_he"] == views.PARTIAL_STATUS_HE


def test_quick_read_without_any_check_is_marked_unchecked(settings, monkeypatch, db, no_network):
    monkeypatch.setenv("ZERNIO_API_KEY", ZKEY)
    quick = connect.platform_state("youtube", settings, db, check=False)
    assert quick["connected"] is True and quick["checked"] is False


# ----------------------------------------------------------------- one YouTube upload
@pytest.mark.parametrize("aspect,duration,keeps_youtube", [
    ("9:16", 25.0, False),     # a vertical short: Shorts only
    ("16:9", 40.0, False),     # a short horizontal clip: Shorts only (its vertical cut)
    ("16:9", 600.0, True),     # a long video: YouTube in full + a Shorts cut
    ("16:9", None, True),      # unknown length: keep both
])
def test_one_youtube_upload_per_clip(aspect, duration, keeps_youtube):
    from agency.staff.strategist import one_youtube_upload

    variants = [Variant(platform="youtube", path="y.mp4", aspect=aspect, duration_s=duration),
                Variant(platform="youtube_shorts", path="s.mp4", aspect="9:16")]
    out = one_youtube_upload(["youtube_shorts", "youtube", "tiktok"], variants)
    assert ("youtube" in out) is keeps_youtube and "youtube_shorts" in out and "tiktok" in out


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg (clip length)")
def test_pipeline_plans_a_short_clip_once_per_channel(ctx, tiny_video):
    from agency.pipeline import run_pipeline

    vid = ctx.db.add_video(str(tiny_video), "ACE on Mirage")
    run_pipeline(ctx, vid)
    platforms = [p["platform"] for p in ctx.db.list_posts(vid)]
    assert "youtube_shorts" in platforms and "youtube" not in platforms
    assert "x" not in platforms  # not enabled by default (the owner has no X yet)


def test_a_second_clip_takes_the_next_free_day(settings):
    from agency.staff._playbook import local_tz
    from agency.staff.strategist import default_schedule

    tz = local_tz(settings)
    now = datetime(2026, 10, 18, 10, 0, tzinfo=tz)
    plats = ["youtube_shorts", "instagram_reels", "tiktok"]
    first = default_schedule(settings, plats, now)
    second = default_schedule(settings, plats, now,
                              busy={"youtube_shorts": [first["youtube_shorts"]]})
    assert second["youtube_shorts"] - first["youtube_shorts"] == timedelta(days=1)
    assert {w.astimezone(tz).date() for w in second.values()} == {
        first["youtube_shorts"].astimezone(tz).date() + timedelta(days=1)}
    assert second["youtube_shorts"].astimezone(tz).strftime("%H:%M") == "17:00"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg (clip length)")
def test_two_clips_in_a_row_never_share_a_slot(ctx, tiny_video):
    from agency.pipeline import run_pipeline

    a = ctx.db.add_video(str(tiny_video), "first")
    run_pipeline(ctx, a)
    b = ctx.db.add_video(str(tiny_video), "second")
    run_pipeline(ctx, b)
    when = {(p["video_id"], p["platform"]): p["scheduled_at"] for p in ctx.db.list_posts()}
    assert when[(a, "youtube_shorts")] != when[(b, "youtube_shorts")]
    assert when[(b, "youtube_shorts")][:10] > when[(a, "youtube_shorts")][:10]  # a later day


def test_title_without_the_music_tail():
    from agency.home import video_title
    from agency.models import Video

    v = Video(id=1, path="x.mp4", title_hint="ACE עם AK בנוק — music: Mozart phonk")
    assert video_title(v) == "ACE עם AK בנוק"


# ----------------------------------------------------------------- manager (offline)
def test_offline_manager_points_to_the_card_editor(ctx):
    from agency.staff.manager import Manager

    vid = ctx.db.add_video("a.mp4", "Deagle 3K")
    ctx.db.add_approval(Approval(id=None, kind="publish", title="x", status="pending",
                                 payload={"video_id": vid, "post_ids": []}))
    mgr = Manager(ctx)
    reply = mgr._mock_chat("לגבי Deagle 3K: תשנה את הכותרת למשהו מצחיק")
    assert '"Deagle 3K"' in reply and "פרטים לכל פלטפורמה" in reply
    assert "מחכה לאישור שלך" in mgr._mock_chat("מה עלה היום?")


# ----------------------------------------------------------------- periodic stats pull
def _real_post(ctx, platform="youtube_shorts"):
    vid = ctx.db.add_video("a.mp4", "Deagle 3K")
    pid = ctx.db.add_post(vid, PostPlan(platform=platform, variant_path="a.mp4"),
                          status="published")
    ctx.db.update_post(pid, result=PostResult(platform=platform, ok=True, dry_run=False,
                                              external_id="abc123", url="https://x.y/abc"))
    return pid


def test_stats_refresh_every_six_hours(ctx, monkeypatch):
    from agency import scheduler
    from agency.platforms import registry

    now = datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc)
    assert scheduler.maybe_refresh_stats(ctx, now) is None  # nothing really published yet
    pid = _real_post(ctx)

    class Adapter:
        def fetch_stats(self, ext):
            return {"views": 1234, "likes": 50, "comments": 4}

    monkeypatch.setattr(registry, "get_adapter", lambda key, settings: Adapter())
    assert scheduler.maybe_refresh_stats(ctx, now) == {"fetched": 1, "errors": 0}
    assert ctx.db.get_post(pid)["stats"]["views"] == 1234
    assert scheduler.maybe_refresh_stats(ctx, now + timedelta(hours=5)) is None
    assert scheduler.maybe_refresh_stats(ctx, now + timedelta(hours=6, minutes=1)) is not None


def test_stats_refresh_offline_is_one_quiet_line(ctx, monkeypatch):
    from agency import scheduler
    from agency.platforms import registry

    _real_post(ctx)
    _real_post(ctx, "tiktok")

    class Down:
        def fetch_stats(self, ext):
            raise requests.ConnectionError("offline")

    monkeypatch.setattr(registry, "get_adapter", lambda key, settings: Down())
    before = len(ctx.db.list_events(1000))
    assert scheduler.maybe_refresh_stats(ctx) == {"fetched": 0, "errors": 2}
    new = ctx.db.list_events(1000)[: len(ctx.db.list_events(1000)) - before]
    assert len(new) == 1 and "#" not in new[0]["message"]
    assert ctx.db.list_staff_status() and all(
        r["status"] != "working" for r in ctx.db.list_staff_status() if r["name"] == "analytics")


# ----------------------------------------------------------------- studio import + feed
def test_studio_import_result_says_what_came(settings, studio_dir):
    from agency.importer import Found, import_found

    [res] = import_found([Found("studio", str(studio_dir), "")], settings)
    assert res["history"] == {"imported": 1, "skipped": 0}
    assert any(k.startswith("meta.") for k in res["imported"])
    assert "linked" in res


def test_studio_card_message_before_and_after_import(ctx, studio_dir, monkeypatch):
    from agency import importer

    monkeypatch.setattr(importer, "next_connect_steps", list)  # its keys are there: no steps
    views.clear_studio_cache()
    ctx.settings.raw.setdefault("studio", {})["dir"] = ""
    monkeypatch.setenv("AGENCY_STUDIO_DIR", str(studio_dir))
    assert views.studio_summary(ctx)["message_he"] == views.STUDIO_FOUND_HE
    ctx.settings.raw["studio"]["dir"] = str(studio_dir)
    assert views.studio_summary(ctx)["message_he"] == importer.STUDIO_LINKED_HE


def test_card_plans_in_time_order_without_repeated_lead(ctx):
    now = datetime.now(timezone.utc)
    vid = ctx.db.add_video("a.mp4", "Deagle 3K")
    ids = []
    for platform, hours in (("tiktok", 5), ("youtube_shorts", 2), ("instagram_reels", 4)):
        plan = PostPlan(platform=platform, variant_path="a.mp4", title="t",
                        scheduled_at=(now + timedelta(hours=hours)).isoformat(),
                        extra={"why": f"{views.PLATFORM_HE[platform]} ב-17:00: השעה שעובדת"})
        ids.append(ctx.db.add_post(vid, plan, status="awaiting_approval"))
    aid = ctx.db.add_approval(Approval(id=None, kind="publish", title="x",
                                       payload={"video_id": vid, "post_ids": ids}))
    card = views.approval_card(ctx, ctx.db.get_approval(aid), now)
    assert [p["platform"] for p in card["plans"]] == ["youtube_shorts", "instagram_reels",
                                                       "tiktok"]
    assert all(p["why"] == "השעה שעובדת" for p in card["plans"])


def test_bare_and_quiet_statuses_do_not_flood_the_feed(ctx):
    from agency.staff.base import StaffMember

    member = StaffMember(ctx)
    before = len(ctx.db.list_events(1000))
    member.status("idle")
    member.status("working", "מדבר איתך", quiet=True)
    assert len(ctx.db.list_events(1000)) == before
    member.status("working", "חותך גרסאות")
    assert len(ctx.db.list_events(1000)) == before + 1
    assert ctx.db.list_staff_status()[0]["detail"] == "חותך גרסאות"
