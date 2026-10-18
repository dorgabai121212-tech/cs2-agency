"""Regression tests for the confirmed review findings (security, honesty, no double posts).

Every test is offline: HTTP is faked, no ffmpeg, no keyring, no studio.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agency import connect, inbox
from agency import settings_store as ss
from agency.dashboard import auth
from agency.dashboard.app import create_app
from agency.models import Approval, PostPlan, PostResult, Variant, VideoStatus
from agency.notify import telegram as tg
from agency.notify.telegram import TelegramNotifier

HEADERS = {"X-Agency-Request": "1"}
PC = ("127.0.0.1", 50000)
TOKEN = "123456:AA-SECRET-bot-token-abcdef"
OLD_CHAT, NEW_CHAT = 111, 222


class FakeBot:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.next_id = 100

    def __call__(self, method, params, files, timeout):
        self.calls.append((method, json.loads(json.dumps(params, default=str))))
        if method in ("sendMessage", "sendVideo"):
            self.next_id += 1
            return 200, {"ok": True, "result": {"message_id": self.next_id}}
        return 200, {"ok": True, "result": True}

    def sent(self):
        return [p for m, p in self.calls if m == "sendMessage"]

    def answers(self):
        return [p["text"] for m, p in self.calls if m == "answerCallbackQuery"]


@pytest.fixture
def bot(ctx, monkeypatch):
    monkeypatch.setenv("AGENCY_KEYRING", "off")
    ctx.settings.raw["notify"] = {"telegram": {"enabled": True}}
    fake = FakeBot()
    return TelegramNotifier(ctx, token=TOKEN, transport=fake,
                            run_job=lambda fn, *a: fn(*a)), fake


def start_msg(chat: int, code: str, update_id: int = 1, **frm) -> dict:
    return {"update_id": update_id, "message": {"chat": {"id": chat, "type": "private"},
                                                "from": {"id": chat, **frm},
                                                "text": f"/start {code}"}}


def tap(data: str, message_id: int, chat: int = OLD_CHAT) -> dict:
    return {"update_id": 9, "callback_query": {
        "id": "cb", "data": data, "from": {"id": chat},
        "message": {"message_id": message_id, "chat": {"id": chat, "type": "private"}}}}


def publish_approval(ctx, video: Path, platform: str = "youtube") -> tuple[int, int]:
    vid = ctx.db.add_video(str(video), "Deagle 3K")
    plan = PostPlan(platform=platform, variant_path=str(video), title="Deagle 3K",
                    description="d")
    pid = ctx.db.add_post(vid, plan, status="awaiting_approval")
    aid = ctx.db.add_approval(Approval(id=None, kind="publish", title="פרסום", requested_by="m",
                                       payload={"video_id": vid, "post_ids": [pid],
                                                "plans": [plan.to_dict()]}))
    return aid, pid


# ============================================================================== Telegram
def test_repairing_replaces_the_old_chat(bot, ctx):
    notifier, fake = bot
    notifier.handle_update(start_msg(OLD_CHAT, notifier.new_pairing_code()))
    assert notifier.chat_id() == OLD_CHAT
    # a fresh code (even without unpairing first) lets the owner move to a new chat
    code = notifier.new_pairing_code()
    notifier.handle_update(start_msg(NEW_CHAT, code, 2, username="dor_new"))
    assert notifier.chat_id() == NEW_CHAT
    assert notifier.status()["paired_name"] == "@dor_new" and notifier.status()["paired_at"]
    # while paired and no code is waiting, strangers are ignored silently
    fake.calls.clear()
    notifier.handle_update(start_msg(999, "123456", 3))
    assert notifier.chat_id() == NEW_CHAT and fake.calls == []


def test_pair_endpoint_unpairs_first_and_disconnect_forgets_the_chat(ctx, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    ctx.db.kv_set(tg.CHAT_KV, {"chat_id": str(OLD_CHAT)})
    app = create_app(ctx)
    app.state.inline_jobs = True
    with TestClient(app, base_url="http://127.0.0.1:8765", client=PC, headers=HEADERS) as c:
        assert c.get("/api/telegram/state").json()["paired"] is True
        res = c.post("/api/telegram/pair").json()
        assert res["ok"] and res["issued_at"] > 0
        state = c.get("/api/telegram/state").json()
        assert state["paired"] is False  # the old (maybe lost) phone lost its buttons at once
        ctx.db.kv_set(tg.CHAT_KV, {"chat_id": str(NEW_CHAT), "paired_at": res["issued_at"] + 1})
        assert c.get("/api/telegram/state").json()["paired_at"] > res["issued_at"]
        ss.env_set(ctx.settings.root, {"TELEGRAM_BOT_TOKEN": TOKEN})
        out = c.post("/api/connect/telegram/disconnect").json()
        assert out["ok"] and "TELEGRAM_BOT_TOKEN" in out["removed"]
    assert TelegramNotifier(ctx).paired() is False


def test_old_card_buttons_die_with_their_database(bot, ctx, tiny_video, tmp_path):
    notifier, fake = bot
    notifier.handle_update(start_msg(OLD_CHAT, notifier.new_pairing_code()))
    aid, _pid = publish_approval(ctx, tiny_video)
    old_data = notifier.callback_data("A", ctx.db.get_approval(aid))
    # a fresh database whose approval #aid is something else: the old button is dead
    from agency.db import DB
    from agency.staff.base import Context
    other = Context(settings=ctx.settings, db=DB(tmp_path / "other.db"), llm=ctx.llm)
    other.db.kv_set(tg.CHAT_KV, {"chat_id": str(OLD_CHAT)})
    spend = other.db.add_approval(Approval(id=None, kind="spend", title="קידום", cost_ils=10,
                                           requested_by="cfo"))
    assert spend == aid
    fresh = TelegramNotifier(other, token=TOKEN, transport=fake, run_job=lambda fn, *a: fn(*a))
    fake.calls.clear()
    fresh.handle_update(tap(old_data, 500))
    assert other.db.get_approval(spend).status == "pending"
    assert fake.answers() == ["הכפתור הזה לא תקף"]


def test_card_from_training_never_publishes_for_real(bot, ctx, tiny_video, monkeypatch):
    notifier, fake = bot
    notifier.handle_update(start_msg(OLD_CHAT, notifier.new_pairing_code()))
    aid, pid = publish_approval(ctx, tiny_video)
    assert notifier.send_approval_card(ctx.db.get_approval(aid))
    card = notifier._cards()[str(aid)]
    assert card["live"] == {"youtube": False}
    data = [p for p in fake.sent() if p.get("reply_markup")][-1]["reply_markup"][
        "inline_keyboard"][0][0]["callback_data"]
    # the owner switches YouTube live after the card went out
    monkeypatch.setattr(TelegramNotifier, "_platform_live", lambda self, p: True)
    fake.calls.clear()
    notifier.handle_update(tap(data, card["message_id"]))
    assert ctx.db.get_approval(aid).status == "pending"            # nothing approved
    assert ctx.db.get_post(pid)["status"] == "awaiting_approval"   # nothing published
    assert fake.answers() == [tg.CHANGED_HE]
    fresh = [p for p in fake.sent() if p.get("reply_markup")][-1]
    assert "מצב אימון" not in fresh["text"]                         # the true state now
    assert notifier._cards()[str(aid)]["live"] == {"youtube": True}


def test_sync_refreshes_open_cards_when_a_switch_changes(bot, ctx, tiny_video, monkeypatch):
    notifier, fake = bot
    notifier.handle_update(start_msg(OLD_CHAT, notifier.new_pairing_code()))
    aid, _pid = publish_approval(ctx, tiny_video)
    assert notifier.sync()["cards"] == 1
    monkeypatch.setattr(TelegramNotifier, "_platform_live", lambda self, p: True)
    fake.calls.clear()
    notifier.sync()
    assert any(m == "editMessageText" and "המצב השתנה" in p["text"] for m, p in fake.calls)
    assert notifier._cards()[str(aid)]["live"] == {"youtube": True}


def test_imported_studio_posts_are_not_announced(bot, ctx, tiny_video):
    notifier, fake = bot
    notifier.handle_update(start_msg(OLD_CHAT, notifier.new_pairing_code()))
    vid = ctx.db.add_video("studio:a1", "EP2")
    plan = PostPlan(platform="youtube_shorts", variant_path="", title="EP2",
                    extra={"source": "studio"})
    pid = ctx.db.add_post(vid, plan, status="planned")
    ctx.db.update_post(pid, status="published", result=PostResult(
        platform="youtube_shorts", ok=True, external_id="abc",
        url="https://www.youtube.com/shorts/abc"))
    fake.calls.clear()
    assert notifier.sync()["published"] == 0 and notifier.send_published(pid) is False


# ============================================================================== phone PIN
class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(auth, "_now", c)
    monkeypatch.setattr(auth, "SCRYPT_N", 2 ** 4)
    return c


def _guess(db, n, ip_base="192.168.1."):
    out = []
    for i in range(n):
        out.append(auth.login(db, "000000", f"{ip_base}{10 + i // 5}"))
    return out


def test_global_lockouts_escalate_and_the_owner_is_told(db, clock):
    auth.set_pin(db, "123456")
    _guess(db, auth.GLOBAL_MAX_FAILURES)
    first = auth.verify_pin(db, "123456", "192.168.1.200")
    assert first.reason == auth.REASON_LOCKED and first.retry_after_s == auth.LOCKOUT_S
    assert any("לנחש את קוד הכניסה" in e["message"] for e in db.list_events())
    assert auth.security_alert(db)["text_he"]
    clock.t += auth.LOCKOUT_S
    _guess(db, auth.GLOBAL_MAX_FAILURES, "10.0.0.")
    second = auth.verify_pin(db, "123456", "192.168.1.200")
    assert second.reason == auth.REASON_LOCKED and second.retry_after_s == 2 * auth.LOCKOUT_S
    assert auth.global_lockout_s(30) == auth.GLOBAL_LOCKOUT_MAX_S


def test_too_many_wrong_pins_delete_the_pin(db, clock):
    auth.set_pin(db, "123456")
    done = 0
    while done < auth.MAX_TOTAL_FAILURES:  # slow guessing that never trips the global lock
        clock.t += auth.LOCKOUT_S + 1
        batch = min(5, auth.MAX_TOTAL_FAILURES - done)
        for _ in range(batch):
            auth.login(db, "000000", f"172.16.0.{done // 5}")
        done += batch
    assert not auth.pin_is_set(db)
    assert auth.login(db, "123456", "192.168.1.9").body["reason"] == auth.REASON_NO_PIN
    alert = auth.security_alert(db)
    assert alert and alert["pin_deleted"] is True
    auth.set_pin(db, "654321")  # a new PIN made on the PC starts clean
    assert auth.security_alert(db) is None
    assert auth.login(db, "654321", "192.168.1.9").status == 200


def test_legacy_lan_key_with_non_ascii_is_401_not_500(ctx, monkeypatch):
    ctx.settings.raw["dashboard"]["host"] = "0.0.0.0"
    ctx.settings.raw["dashboard"]["remote_access"] = False
    monkeypatch.setattr("agency.dashboard.app.machine_hosts", lambda: {"192.168.1.5"})
    app = create_app(ctx)
    with TestClient(app, base_url="http://192.168.1.5:8765", client=("192.168.1.50", 5000),
                    raise_server_exceptions=False) as c:
        assert c.get("/?key=%C3%A9").status_code == 401
        assert c.get("/api/home", headers={"x-agency-key": "é".encode()}).status_code == 401


def test_phone_sessions_die_when_access_was_switched_off_by_hand(ctx):
    auth.set_pin(ctx.db, "123456")
    auth.create_session(ctx.db)
    ctx.settings.raw["dashboard"]["remote_access"] = False  # e.g. edited in config.yaml
    create_app(ctx)
    assert auth.session_count(ctx.db) == 0 and not auth.pin_is_set(ctx.db)


# ============================================================================== secrets
HOOK = "https://discord.com/api/webhooks/123456789/SECRETTOKEN_abc-XYZ_0123456789"


def test_discord_network_error_never_carries_the_webhook(settings, monkeypatch, tiny_video):
    import requests

    from agency.platforms.discord import DiscordAdapter

    settings.raw["dry_run"] = False
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", HOOK)

    def boom(url, **kwargs):
        raise requests.ConnectionError(
            "HTTPSConnectionPool(host='discord.com', port=443): Max retries exceeded with url: "
            "/api/webhooks/123456789/SECRETTOKEN_abc-XYZ_0123456789?wait=true")

    monkeypatch.setattr(requests, "post", boom)
    result = DiscordAdapter(settings).upload(PostPlan(platform="discord",
                                                      variant_path=str(tiny_video), title="t"))
    assert not result.ok and "SECRETTOKEN" not in (result.error or "")
    # and the scrubber alone also catches the host-less path form
    text = DiscordAdapter(settings).scrub("url: /api/webhooks/123456789/SECRETTOKEN_abc-XYZ_01"
                                          "23456789?wait=true and /api/v10/webhooks/1/other_tok")
    assert "SECRETTOKEN" not in text and "other_tok" not in text


@pytest.mark.parametrize("bad", ["pw\fHTTPS_PROXY=http://evil.example:8080",
                                 "pw AGENCY_KEYRING=off", "pw\x0bX", "pw\x85X", "a\x1eb"])
def test_env_values_with_line_breaking_characters_are_refused(project_dir, bad):
    with pytest.raises(ss.SettingsError):
        ss.env_set(project_dir, {"REDDIT_PASSWORD": bad})


def test_env_writer_splits_lines_like_dotenv(project_dir):
    from dotenv import dotenv_values

    env = project_dir / ".env"
    # a value already in the file with a form feed in it (written by an older version)
    env.write_text("REDDIT_PASSWORD='pw\fAGENCY_KEYRING=off\fHTTPS_PROXY=http://evil:1\f#'\n",
                   encoding="utf-8")
    ss.env_set(project_dir, {"ZERNIO_API_KEY": "zk_1"})
    ss.env_set(project_dir, {"REDDIT_PASSWORD": "newpw"})
    values = dotenv_values(env)
    assert "HTTPS_PROXY" not in values and "AGENCY_KEYRING" not in values
    assert values["REDDIT_PASSWORD"] == "newpw" and values["ZERNIO_API_KEY"] == "zk_1"
    import os
    os.environ.pop("ZERNIO_API_KEY", None)
    os.environ.pop("REDDIT_PASSWORD", None)


# ============================================================================== connections
def test_live_switch_also_enables_the_platform(settings, monkeypatch, db):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", HOOK)
    assert settings.get("publishing.platforms.discord.enabled") is False
    tile = connect.platform_state("discord", settings, db, check=False)
    ss.update_config(settings, {"publishing": {"platforms": {"discord": {"live": True}}}})
    assert connect.platform_state("discord", settings, db, check=False)["live"] is False
    assert tile["live"] is False  # switched live by hand but never planned: not "live"
    ss.set_platform_live(settings, "discord", True)
    assert settings.get("publishing.platforms.discord.enabled") is True
    assert connect.platform_state("discord", settings, db, check=False)["live"] is True
    from agency.home import live_platforms
    from agency.staff.base import build_context
    assert "discord" in live_platforms(build_context(settings))


def test_connect_invalidate_drops_the_zernio_account_cache(settings, db):
    from agency.platforms.zernio import ACCOUNTS_KV

    db.kv_set(ACCOUNTS_KV, {"ts": 9e12, "map": {"youtube": {"id": "old"}}})
    connect.invalidate(settings, "tiktok", db)
    assert db.kv_get(ACCOUNTS_KV) is None


# ============================================================================== Zernio
from test_meta import TOKEN as META_TOKEN  # noqa: E402  (shared fakes of the other suites)
from test_meta import FakeGraph  # noqa: E402
from test_zernio import KEY as ZKEY  # noqa: E402
from test_zernio import FakeZernio  # noqa: E402

from agency.platforms.zernio import ACCOUNTS_KV, ZernioAdapter  # noqa: E402


@pytest.fixture
def zlive(settings, monkeypatch):
    settings.raw["dry_run"] = False
    monkeypatch.setenv("ZERNIO_API_KEY", ZKEY)
    monkeypatch.setattr(ZernioAdapter, "poll_interval_s", 0)
    monkeypatch.setattr(ZernioAdapter, "backoff_s", (0, 0))
    return settings


def zplan(key, path, **extra):
    return PostPlan(platform=key, variant_path=str(path), title="Deagle 3K", description="d",
                    hashtags=["CS2"], extra=dict(extra))


def test_reconnected_account_is_refreshed_not_refused(zlive, tiny_video, monkeypatch):
    adapter = ZernioAdapter(zlive, "youtube")
    adapter.db.kv_set(ACCOUNTS_KV, {"ts": 9e12, "map": {"youtube": {
        "id": "acc_yt", "username": "x", "needs_reconnection": True}}})
    fake = FakeZernio(monkeypatch)  # Zernio now says the account is fine
    assert adapter.upload(zplan("youtube", tiny_video)).ok
    assert any(c["url"].endswith("/v1/accounts") for c in fake.calls)


def test_retry_never_posts_twice_after_the_idempotency_window(zlive, tiny_video, monkeypatch):
    zlive.raw["publishing"]["platforms"]["youtube"]["poll_max_s"] = 0
    FakeZernio(monkeypatch, post_status="publishing", entry_status="processing",
               polls=[("publishing", "processing")] * 3)
    first = ZernioAdapter(zlive, "youtube").upload(zplan("youtube", tiny_video))
    assert not first.ok and first.external_id == "post_1"
    # a day later Zernio published it; the owner presses "נסה שוב"
    fake = FakeZernio(monkeypatch)
    again = ZernioAdapter(zlive, "youtube").upload(zplan("youtube", tiny_video))
    assert again.ok and again.external_id == "dQw4w9WgXcQ"
    assert fake.bodies() == []  # no second POST /v1/posts


def test_cloud_scheduled_post_stays_scheduled_and_is_reconciled(ctx, tiny_video, monkeypatch):
    from agency import scheduler
    from agency.home import is_real_post
    from agency.staff.publisher import Publisher

    ctx.settings.raw["dry_run"] = False
    monkeypatch.setenv("ZERNIO_API_KEY", ZKEY)
    monkeypatch.setattr(ZernioAdapter, "poll_interval_s", 0)
    when = (datetime.now(timezone.utc) + timedelta(hours=3)).replace(microsecond=0)
    vid = ctx.db.add_video(str(tiny_video), "Deagle")
    plan = zplan("youtube", tiny_video)
    plan.scheduled_at = when.isoformat()
    pid = ctx.db.add_post(vid, plan, status="failed")  # a retry before its slot
    FakeZernio(monkeypatch, post_status="scheduled", entry_status="pending")
    result = Publisher(ctx).retry(pid)
    post = ctx.db.get_post(pid)
    assert result.ok and post["status"] == "scheduled" and not is_real_post(post)
    assert post["plan"]["extra"]["remote"]["post_id"] == "post_1"
    assert scheduler.due_posts(ctx, when + timedelta(hours=1)) == []  # never uploaded again
    assert Publisher(ctx).publish(pid).ok is False                     # nor by hand
    # after its slot the scheduler asks Zernio and records the truth
    fake = FakeZernio(monkeypatch)  # GET /v1/posts/post_1 -> published
    out = scheduler.reconcile_remote(ctx, when + timedelta(minutes=5))
    post = ctx.db.get_post(pid)
    assert out["published"] == [pid] and post["status"] == "published"
    assert post["external_id"] == "dQw4w9WgXcQ" and post["url"].startswith("https://")
    assert is_real_post(post) and "remote" not in post["plan"]["extra"]
    assert not [c for c in fake.calls if c["method"] == "POST"]


def test_still_processing_failure_is_reconciled(ctx, tiny_video, monkeypatch):
    from agency import scheduler

    ctx.settings.raw["dry_run"] = False
    monkeypatch.setenv("ZERNIO_API_KEY", ZKEY)
    vid = ctx.db.add_video(str(tiny_video), "Deagle")
    pid = ctx.db.add_post(vid, zplan("youtube_shorts", tiny_video), status="planned")
    ctx.db.update_post(pid, status="failed", result=PostResult(
        platform="youtube_shorts", ok=False, external_id="post_9",
        error="still processing, post post_9"))
    FakeZernio(monkeypatch, polls=[("failed", "failed")])
    out = scheduler.reconcile_remote(ctx)
    post = ctx.db.get_post(pid)
    assert out["failed"] == [pid] and post["status"] == "failed" and post["external_id"] is None
    assert "quota" in post["error"] or "מכסה" in post["error"]
    assert scheduler.reconcile_remote(ctx) == {"published": [], "failed": [], "checked": 0}


def test_tiktok_consent_follows_the_owners_approval_not_the_mode(ctx, tiny_video):
    from agency.pipeline import execute_approval

    aid, pid = publish_approval(ctx, tiny_video, "tiktok")
    ctx.db.decide_approval(aid, True)
    execute_approval(ctx, aid)
    assert ctx.db.get_post(pid)["plan"]["extra"]["owner_approved"] is True
    adapter = ZernioAdapter(ctx.settings, "tiktok")
    ctx.settings.raw["publishing"]["mode"] = "approve"
    assert adapter._owner_consented(zplan("tiktok", tiny_video)) is False  # auto-scheduled
    ctx.settings.raw["publishing"]["mode"] = "auto"
    assert adapter._owner_consented(PostPlan.from_dict(ctx.db.get_post(pid)["plan"]))


def test_shorts_only_live_switch_still_gets_its_stats(settings, monkeypatch):
    from agency.platforms.youtube import YouTubeShortsAdapter

    monkeypatch.setenv("ZERNIO_API_KEY", ZKEY)
    settings.raw["publishing"]["platforms"]["youtube_shorts"]["live"] = True
    seen = []
    monkeypatch.setattr(YouTubeShortsAdapter, "fetch_stats",
                        lambda self, ext: seen.append(self.key) or {"views": 7})
    assert ZernioAdapter(settings, "youtube_shorts").fetch_stats("abc") == {"views": 7}
    assert seen == ["youtube_shorts"]


# ============================================================================== one channel
def test_same_file_never_goes_to_youtube_twice():
    from agency.staff.strategist import one_youtube_upload

    same = [Variant(platform="youtube", path="/v/clip.mp4", aspect="16:9", duration_s=None),
            Variant(platform="youtube_shorts", path="/v/clip.mp4", aspect="16:9")]
    assert one_youtube_upload(["youtube", "youtube_shorts", "tiktok"], same) == [
        "youtube", "tiktok"]
    vertical = [Variant(platform="youtube", path="/v/c.mp4", aspect="9:16", duration_s=None),
                Variant(platform="youtube_shorts", path="/v/c.mp4", aspect="9:16")]
    assert one_youtube_upload(["youtube", "youtube_shorts"], vertical) == ["youtube_shorts"]


def test_publisher_refuses_the_same_file_on_the_other_youtube_key(ctx, tiny_video):
    from agency.staff.publisher import SAME_CHANNEL_HE, Publisher

    vid = ctx.db.add_video(str(tiny_video), "x")
    first = ctx.db.add_post(vid, zplan("youtube", tiny_video), status="planned")
    ctx.db.update_post(first, status="published", result=PostResult(
        platform="youtube", ok=True, dry_run=False, external_id="yt1", url="https://y/1"))
    second = ctx.db.add_post(vid, zplan("youtube_shorts", tiny_video), status="scheduled")
    refused = Publisher(ctx).same_channel_check("youtube_shorts", zplan("youtube_shorts",
                                                                        tiny_video), vid, second)
    assert refused is not None and refused.error == SAME_CHANNEL_HE


# ============================================================================== editor
def test_finished_vertical_short_is_not_trimmed(ctx, tiny_video, monkeypatch):
    from agency import media
    from agency.media import ffmpeg
    from agency.staff.editor import VideoEditor

    info = {"duration": 40.0, "width": 1080, "height": 1920, "fps": 30, "has_audio": False}
    monkeypatch.setattr(media, "have_ffmpeg", lambda: True)
    monkeypatch.setattr(media, "probe", lambda p: dict(info))
    monkeypatch.setattr(media, "thumbnail", lambda src, dst, t: Path(dst))
    cuts = []

    def fake_encode(src, dst, start=None, end=None, **kw):
        cuts.append((Path(dst).name, start, end))
        return Path(dst)

    monkeypatch.setattr(ffmpeg, "encode_vertical", fake_encode)
    monkeypatch.setattr(ffmpeg, "encode_youtube_hq", lambda src, dst, **kw: cuts.append(
        (Path(dst).name, kw.get("start"), kw.get("end"))) or Path(dst))
    monkeypatch.setattr(ffmpeg, "long_master", lambda src, dst, **kw: Path(dst))
    video = ctx.db.get_video(ctx.db.add_video(str(tiny_video), "Deagle"))
    analysis = {"best_short_window": {"start": 2.0, "end": 37.0}}
    variants = VideoEditor(ctx).make_variants(video, analysis)
    assert all((s, e) == (0.0, 40.0) for _n, s, e in cuts if _n != "youtube_1440.mp4")
    assert {v.duration_s for v in variants if v.aspect == "9:16"} == {40.0}
    assert VideoEditor._finished_short({"width": 1920, "height": 1080, "duration": 40}) is False
    assert VideoEditor._finished_short({"width": 1080, "height": 1920, "duration": 120}) is False


# ============================================================================== inbox
@pytest.fixture
def clips(ctx, tmp_path, monkeypatch):
    from agency.staff.manager import Manager

    folder = tmp_path / "CS2_CLIPS"
    folder.mkdir()
    ss.update_config(ctx.settings, {"inbox": {"enabled": False, "folders": [str(folder)],
                                              "stable_seconds": 5}})
    calls = []

    def fake(self, path, title_hint=""):
        calls.append(Path(path).name)
        return {"ok": True, "video_id": self.db.add_video(str(path), title_hint),
                "approval_id": 1}

    monkeypatch.setattr(Manager, "handle_new_video", fake)
    return folder, calls


def _two_scans(ctx):
    inbox.scan_once(ctx, now=1000)
    return inbox.scan_once(ctx, now=1100)


def test_renamed_or_copied_baseline_clip_is_not_processed(ctx, clips):
    import shutil

    folder, calls = clips
    (folder / "EP3.mp4").write_bytes(b"ep3" * 1000)
    inbox.set_enabled(ctx, True)
    (folder / "EP3.mp4").rename(folder / "EP3 final.mp4")
    shutil.copy2(folder / "EP3 final.mp4", folder / "EP3 final - Copy.mp4")
    actions = {Path(a["path"]).name: a["action"] for a in _two_scans(ctx)}
    assert calls == []
    assert set(actions.values()) <= {"baseline", "duplicate", "waiting"}
    names = [c["name"] for c in inbox.inbox_status(ctx)["baseline"]]
    assert len(names) == 1 and names[0].startswith("EP3 final")  # still one previous clip


def test_clips_dropped_while_off_are_baseline_when_switched_back_on(ctx, clips):
    folder, calls = clips
    inbox.set_enabled(ctx, True)
    inbox.set_enabled(ctx, False)
    (folder / "while_off.mp4").write_bytes(b"x" * 500)
    inbox.set_enabled(ctx, True)
    _two_scans(ctx)
    assert calls == [] and [c["name"] for c in inbox.inbox_status(ctx)["baseline"]] == [
        "while_off.mp4"]


def test_prepare_answers_at_once_when_it_cannot_run(ctx, clips):
    folder, _calls = clips
    (folder / "gone.mp4").write_bytes(b"x" * 500)
    inbox.set_enabled(ctx, True)
    clip = inbox.inbox_status(ctx)["baseline"][0]
    (folder / "gone.mp4").unlink()
    assert inbox.check_prepare(ctx, clip["path"]) == "הקובץ כבר לא נמצא בתיקייה"


# ============================================================================== Meta
@pytest.fixture
def meta(settings, monkeypatch):
    from agency.platforms.facebook import FacebookAdapter
    from agency.platforms.instagram import InstagramReelsAdapter

    settings.raw["dry_run"] = False
    monkeypatch.setenv("META_ACCESS_TOKEN", META_TOKEN)
    settings.raw["meta"] = {"ig_user_id": "17841400000000001", "page_id": "100000000000001"}
    monkeypatch.setattr(InstagramReelsAdapter, "poll_interval_s", 0)
    monkeypatch.setattr(FacebookAdapter, "poll_interval_s", 0)
    return settings


def test_instagram_published_container_is_adopted_not_published_again(meta, tiny_video,
                                                                      monkeypatch):
    from agency.platforms._common import build_caption
    from agency.platforms.instagram import CAPTION_LIMIT, MAX_HASHTAGS, InstagramReelsAdapter

    adapter = InstagramReelsAdapter(meta)
    plan = zplan("instagram_reels", tiny_video)
    graph = FakeGraph(monkeypatch)
    real = graph.request

    def lost_answer(method, url, **kw):
        if method == "POST" and url.endswith("/media_publish"):
            graph.calls.append((method, url, kw))
            import requests
            raise requests.ReadTimeout("read timed out")
        return real(method, url, **kw)

    monkeypatch.setattr("requests.request", lost_answer)
    assert not adapter.upload(plan).ok  # published at Meta, but the answer was lost
    caption = build_caption(plan, CAPTION_LIMIT, max_hashtags=MAX_HASHTAGS)

    def after(method, url, **kw):
        tail = url.split("/v24.0/", 1)[1]
        graph.calls.append((method, url, kw))
        if method == "GET" and tail == "c_1":
            return _resp({"status_code": "PUBLISHED"})
        if method == "GET" and tail.endswith("/media"):
            return _resp({"data": [{"id": "m_9", "caption": caption,
                                    "permalink": "https://www.instagram.com/reel/Z/",
                                    "timestamp": datetime.now(timezone.utc).isoformat()}]})
        raise AssertionError(f"unexpected {method} {url}")

    monkeypatch.setattr("requests.request", after)
    retry = adapter.upload(plan)
    assert retry.ok and retry.external_id == "m_9"
    assert len([c for c in graph.calls if c[1].endswith("/media_publish")]) == 1
    assert len([c for c in graph.calls if c[0] == "POST" and c[1].endswith("/media")]) == 1


def _resp(payload, status=200):
    from test_meta import FakeResponse
    return FakeResponse(status, payload)


def test_instagram_upload_that_never_arrived_is_sent_again(meta, tiny_video, monkeypatch):
    import requests

    from agency.platforms.instagram import InstagramReelsAdapter

    graph = FakeGraph(monkeypatch, container_codes=["IN_PROGRESS", "FINISHED", "FINISHED"])
    ok_post = graph.post
    monkeypatch.setattr(requests, "post", lambda *a, **k: (_ for _ in ()).throw(
        requests.ConnectionError("reset")))
    adapter = InstagramReelsAdapter(meta)
    assert not adapter.upload(zplan("instagram_reels", tiny_video)).ok
    monkeypatch.setattr(requests, "post", ok_post)
    retry = adapter.upload(zplan("instagram_reels", tiny_video))
    assert retry.ok, retry.error
    assert len(graph.made("RUPLOAD", "c_1")) == 1  # the bytes went again, to the same one
    assert len(graph.made("POST", "/media")) == 1


def test_instagram_waits_for_a_youtube_post_zernio_still_holds(db, tiny_video):
    from agency.platforms.instagram import meta_delay_status

    vid = db.add_video(str(tiny_video), "x")
    yt = db.add_post(vid, zplan("youtube_shorts", tiny_video), status="planned")
    db.update_post(yt, status="failed", result=PostResult(
        platform="youtube_shorts", ok=False, dry_run=False, external_id="post_1",
        error="still processing"))
    assert meta_delay_status(db, vid) == (False, None)
    db.update_post(yt, result=PostResult(platform="youtube_shorts", ok=False, dry_run=False,
                                         external_id=None, error="confirmed failure"))
    assert meta_delay_status(db, vid) == (True, None)


def test_facebook_lost_finish_answer_never_makes_a_second_reel(meta, tiny_video, monkeypatch):
    from agency.platforms.facebook import FacebookAdapter

    meta.raw["publishing"]["platforms"]["facebook"] = {"format": "reels"}
    graph = FakeGraph(monkeypatch)
    real = graph.request

    def lost_finish(method, url, **kw):
        if method == "POST" and url.endswith("/video_reels") \
                and kw["data"]["upload_phase"] == "finish":
            graph.calls.append((method, url, kw))
            import requests
            raise requests.ReadTimeout("read timed out")
        return real(method, url, **kw)

    monkeypatch.setattr("requests.request", lost_finish)
    adapter = FacebookAdapter(meta)
    assert not adapter.upload(zplan("facebook", tiny_video)).ok
    graph.reel_status = [{"video_status": "ready", "publishing_phase": {
        "status": "complete", "publish_status": "published"}}] * 3
    monkeypatch.setattr("requests.request", real)
    retry = adapter.upload(zplan("facebook", tiny_video))
    assert retry.ok and retry.external_id == "v_1"
    phases = [c[2]["data"]["upload_phase"] for c in graph.made("POST", "/video_reels")]
    assert phases.count("start") == 1


# ============================================================================== home / pipeline
def test_published_time_is_the_real_one(ctx, tiny_video):
    from agency.home import published_at
    from agency.staff.publisher import Publisher

    vid = ctx.db.add_video(str(tiny_video), "x")
    plan = zplan("youtube", tiny_video)
    plan.scheduled_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    pid = ctx.db.add_post(vid, plan, status="scheduled")
    Publisher(ctx).publish(pid, publish_now=True)  # "agency approve N --now"
    ctx.db.update_post(pid, stats={"views": 900, "fetched_at": "later"})  # a stats refresh
    when = published_at(ctx.db.get_post(pid))
    assert abs((when - datetime.now(timezone.utc)).total_seconds()) < 60
    # an old post without the record: the adapters' table wins over updated_at
    old = {"platform": "reddit", "external_id": "r1", "status": "published",
           "plan": {}, "stats": {"fetched_at": "x"}, "scheduled_at": None,
           "created_at": "2026-10-13T10:00:00+00:00", "updated_at": "2026-10-18T16:00:00+00:00"}
    assert published_at(old).day == 13
    assert published_at(old, {"reddit:r1": "2026-10-12T09:00:00+00:00"}).day == 12


def test_dead_preparation_is_marked_failed_and_can_be_retried(ctx, tiny_video):
    from agency import scheduler
    from agency.home import home_sentence, processing_view

    vid = ctx.db.add_video(str(tiny_video), "Deagle 3K")
    ctx.db.update_video(vid, status=VideoStatus.ANALYZED, meta={"job_pid": 4242})
    assert scheduler.recover_stalled_videos(ctx, dead_pids=(4242,)) == [vid]
    video = ctx.db.get_video(vid)
    assert video.status == VideoStatus.FAILED and video.meta["failed_step"] == "editor"
    item = processing_view(ctx)[0]
    assert item["failed"] and item["can_retry"] and "נתקע" in home_sentence(ctx)["sentence"]
    app = create_app(ctx)
    app.state.inline_jobs = True
    with TestClient(app, base_url="http://127.0.0.1:8765", client=PC, headers=HEADERS) as c:
        res = c.post(f"/api/videos/{vid}/retry").json()
        assert res["ok"] and res["approval_id"]
        assert c.post(f"/api/videos/{vid}/retry").status_code == 409  # not failed any more
    assert ctx.db.get_video(vid).status == VideoStatus.AWAITING_APPROVAL


def test_a_running_preparation_is_never_touched(ctx, tiny_video, monkeypatch):
    from agency import pipeline, scheduler

    vid = ctx.db.add_video(str(tiny_video), "x")
    ctx.db.update_video(vid, status=VideoStatus.EDITED, meta={"job_pid": 1})
    monkeypatch.setattr(pipeline, "running_videos", lambda: {vid})
    assert scheduler.recover_stalled_videos(ctx, dead_pids=(1,)) == []
    monkeypatch.setattr(pipeline, "running_videos", lambda: set())
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    assert scheduler.recover_stalled_videos(ctx, now=later) == []  # alive elsewhere, recent
    assert scheduler.recover_stalled_videos(ctx, now=later + timedelta(hours=2)) == [vid]


def test_failed_reply_comes_back_with_a_retry(ctx):
    from agency.dashboard.views import needs_you

    aid = ctx.db.add_approval(Approval(id=None, kind="reply", title="t", requested_by="c",
                                       payload={"text": "תודה"}))
    app = create_app(ctx)
    app.state.inline_jobs = True
    with TestClient(app, base_url="http://127.0.0.1:8765", client=PC, headers=HEADERS) as c:
        res = c.post(f"/api/approvals/{aid}/approve", json={"note": ""}).json()
        assert res["ok"] is False  # the reply was not sent (training / no connection)
    later = datetime.now(timezone.utc) + timedelta(minutes=1)
    cards = needs_you(ctx, later)
    assert [c["id"] for c in cards] == [aid] and cards[0]["retry"] is True
    assert cards[0]["retry_he"]


# ============================================================================== studio / CLI
def test_studio_db_is_read_without_creating_files_next_to_it(studio_dir):
    from agency import importer

    state = studio_dir / "state"
    con = sqlite3.connect(state / "studio.db")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE IF NOT EXISTS t (x)")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    con.close()
    before = sorted(p.name for p in state.iterdir())
    with importer.open_studio_db(importer.studio_db_path(studio_dir)) as conn:
        assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO t VALUES (2)")
    assert sorted(p.name for p in state.iterdir()) == before


def test_english_console_never_prints_hebrew_events(capsys, monkeypatch):
    from agency import cli
    from agency.events import emit

    monkeypatch.setenv("AGENCY_CLI_LANG", "en")
    logger = logging.getLogger("agency")
    old = list(logger.handlers)
    try:
        cli._console_logging()
        emit(None, "owner", "הבעלים דחה את הקליפ", "warning")
        err = capsys.readouterr().err
        assert "[owner] warning: details are in the dashboard" in err
        assert not any("֐" <= ch <= "׿" for ch in err)
    finally:
        logger.handlers[:] = old
