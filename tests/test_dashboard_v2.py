"""Dashboard v2 API (SPEC_V2 §1-§6, §11): home, thumbnails, plan editing, settings,
connections + wizards, import, Telegram pairing, inbox, phone access, CSRF, secret hygiene and
the background services. Offline: every HTTP call is faked, mock LLM, training mode."""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
import yaml
from fastapi.testclient import TestClient

from agency import settings_store as ss
from agency.dashboard import auth, views
from agency.dashboard.app import create_app, start_services, stop_services
from agency.models import Approval, PostPlan, VideoStatus

HEADERS = {"X-Agency-Request": "1"}
PC = ("127.0.0.1", 50000)
PHONE = ("192.168.1.50", 5000)
LAN_IP = "192.168.1.5"
PORT = 8765
HOOK = "https://discord.com/api/webhooks/123/SECRET_hook_value_0004"
TG_TOKEN = "777000:SECRET_telegram_token_0003"
ZKEY = "zr_SECRET_zernio_key_0001"


# --------------------------------------------------------------------------- fakes / fixtures
class Resp:
    def __init__(self, status: int = 200, payload=None):
        self.status_code = status
        self._payload = {} if payload is None else payload
        self.content = json.dumps(self._payload).encode()

    def json(self):
        return self._payload


class FakeHTTP:
    """``requests.request`` replacement routed by (method, url prefix); ``default`` answers
    everything else (or the call fails the test)."""

    def __init__(self, routes: dict, default: Resp | None = None):
        self.routes = routes
        self.default = default
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url))
        for (m, prefix), resp in self.routes.items():
            if m == method and url.startswith(prefix):
                return resp
        if self.default is not None:
            return self.default
        raise AssertionError(f"unexpected request {method} {url[:40]}")


@pytest.fixture
def http(monkeypatch):
    def install(routes: dict, default: Resp | None = None) -> FakeHTTP:
        fake = FakeHTTP(routes, default)
        monkeypatch.setattr(requests, "request", fake)
        return fake
    return install


@pytest.fixture
def lan(monkeypatch):
    """This PC's LAN address for the Host allow-list / QR (no real network lookups)."""
    monkeypatch.setattr(auth, "lan_addresses", lambda: [LAN_IP])


@pytest.fixture
def fast_scrypt(monkeypatch):
    monkeypatch.setattr(auth, "SCRYPT_N", 2 ** 8)


@pytest.fixture(autouse=True)
def _restore_environ():
    """Keys saved through the API land in os.environ (keyring off): undo that per test."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture(autouse=True)
def _fresh_view_caches():
    views.clear_studio_cache()
    yield
    views.clear_studio_cache()


@pytest.fixture
def app(ctx, no_network, lan):
    application = create_app(ctx)
    application.state.inline_jobs = True
    return application


@pytest.fixture
def pc(app):
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", client=PC, headers=HEADERS,
                    follow_redirects=False) as c:
        yield c


@pytest.fixture
def remote_app(ctx, no_network, lan, fast_scrypt):
    ctx.settings.raw.setdefault("dashboard", {})["remote_access"] = True
    application = create_app(ctx)
    application.state.inline_jobs = True
    return application


def phone(app, **kwargs) -> TestClient:
    return TestClient(app, base_url=f"http://{LAN_IP}:{PORT}", client=PHONE,
                      follow_redirects=False, **kwargs)


def make_publish(ctx, *, platforms=("youtube", "instagram_reels"), when=None,
                 hint="Deagle 3K on Mirage", thumb: Path | None = None, extra=None):
    """A waiting publish approval (posts awaiting approval), as the pipeline makes it."""
    videos = ctx.settings.data_dir / "videos"
    videos.mkdir(parents=True, exist_ok=True)
    clip = videos / "clip.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    vid = ctx.db.add_video(str(clip), hint)
    if thumb is not None:
        ctx.db.update_video(vid, meta={"thumbnail": str(thumb)})
    plans = [PostPlan(platform=p, variant_path=str(clip), title=f"Deagle 3K on Mirage {i}",
                      description="Real demo, shot like a movie.",
                      tags=["CS2"], hashtags=["#CS2", "#CounterStrike2", "#CS2Edit"],
                      scheduled_at=(when[i] if when else None),
                      extra={"why": "השעה המוכחת", **(extra or {})})
             for i, p in enumerate(platforms)]
    post_ids = [ctx.db.add_post(vid, plan, status="awaiting_approval") for plan in plans]
    aid = ctx.db.add_approval(Approval(
        id=None, kind="publish", requested_by="manager",
        title=f"פרסום \"Deagle\" ב-{len(plans)} פלטפורמות",
        payload={"video_id": vid, "post_ids": post_ids, "plans": [p.to_dict() for p in plans],
                 "publish_now": False}))
    ctx.db.update_video(vid, status=VideoStatus.AWAITING_APPROVAL)
    return aid, post_ids, vid


def config_on_disk(ctx) -> dict:
    return yaml.safe_load((ctx.settings.root / "config.yaml").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- health / home
def test_health(pc):
    assert pc.get("/api/health").json() == {"ok": True, "app": "cs2-agency"}


def test_home_empty_is_calm_and_plain(pc):
    data = pc.get("/api/home").json()
    for key in ("greeting", "sentence", "mode", "live_platforms", "needs_you", "results",
                "money", "team", "processing", "studio", "previous_clips"):
        assert key in data
    assert data["mode"] == "training" and data["live_platforms"] == []
    assert data["needs_you"] == [] and data["previous_clips"] == []
    assert data["sentence"] == "זרוק את הקליפ הראשון ואני על זה."
    assert data["money"]["sentence"] == "החודש: הוצאות 0 ₪ · הכנסות 0 ₪"
    assert data["money"]["pct"] == 0
    assert data["studio"] == {"found": False, "daemon_active": False, "imported": False,
                              "question_he": "", "disable_commands": [], "keys_missing": False,
                              "message_he": ""}
    assert data["team"][0]["name"] == "manager"
    assert data["results"]["has_data"] is False
    text = json.dumps({k: data[k] for k in ("greeting", "sentence", "team")}, ensure_ascii=False)
    assert not re.search(r"dry.?run|api|token|#\d", text, re.I)


def test_home_publish_card(pc, ctx):
    thumb = ctx.settings.data_dir / "videos" / "thumbnail.jpg"
    thumb.parent.mkdir(parents=True, exist_ok=True)
    thumb.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
    later = (datetime.now(timezone.utc) + timedelta(days=2, hours=1)).replace(microsecond=0)
    when = [later.isoformat(), (later + timedelta(hours=3)).isoformat()]
    aid, _posts, vid = make_publish(ctx, when=when, thumb=thumb)
    data = pc.get("/api/home").json()
    assert data["mood"] == "attention" and "מחכה לך" in data["sentence"]
    [card] = data["needs_you"]
    assert card["id"] == aid and card["kind"] == "publish"
    assert card["title"] == "Deagle 3K on Mirage" and card["video_id"] == vid
    assert card["thumbnail_url"] == f"/api/videos/{vid}/thumbnail"
    assert card["platforms"] == [{"key": "youtube", "name_he": "יוטיוב"},
                                 {"key": "instagram_reels", "name_he": "אינסטגרם"}]
    assert card["main_title"] == "Deagle 3K on Mirage 0"
    assert card["when_he"] == views.when_he(ctx.settings, when[0])
    assert re.fullmatch(r"(יום \S+|מחר|היום|\d\d\.\d\d) \d\d:\d\d", card["when_he"])
    plan = card["plans"][1]
    assert plan["platform"] == "instagram_reels" and plan["editable"] is True
    assert plan["when_he"] == views.when_he(ctx.settings, when[1])
    assert plan["why"] == "השעה המוכחת" and plan["hashtags"][0] == "#CS2"
    assert card["chat_prefill"] == "לגבי Deagle 3K on Mirage: "


def test_home_spend_card_shows_amount_and_cfo_reason(pc, ctx):
    vid = ctx.db.add_video("x.mp4", "Ace on Inferno")
    ctx.db.add_approval(Approval(id=None, kind="spend", title="קידום ממומן ב-youtube: 50.00 ₪",
                                 cost_ils=50, requested_by="promoter",
                                 payload={"action": "paid_promo", "platform": "youtube",
                                          "video_id": vid, "days": 3, "why": "הקליפ חזק"}))
    [card] = pc.get("/api/home").json()["needs_you"]
    assert card["kind"] == "spend" and card["cost_ils"] == 50 and card["cost_he"] == "50 ₪"
    assert card["title"] == "קידום ממומן ביוטיוב"
    assert card["cfo_ok"] is False  # the budget is 0: the CFO says no, in plain Hebrew
    assert card["cfo_reason"] and "finance." not in card["cfo_reason"]
    assert card["why"] == "הקליפ חזק" and card["video_title"] == "Ace on Inferno"


def test_home_reports_studio(pc, ctx, studio_dir):
    ctx.settings.raw["studio"] = {"dir": str(studio_dir)}
    studio = pc.get("/api/home").json()["studio"]
    assert studio["found"] is True and studio["imported"] is True
    assert studio["daemon_active"] is False  # daemon.log is old / missing
    assert studio["keys_missing"] is True and "Zernio" in studio["message_he"]


def test_when_he_human_times(settings):
    now = datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc)  # Sunday 13:00 in Israel
    assert views.when_he(settings, "2026-10-18T15:00:00+00:00", now) == "היום 18:00"
    assert views.when_he(settings, "2026-10-19T11:30:00+00:00", now) == "מחר 14:30"
    assert views.when_he(settings, "2026-10-23T17:00:00+00:00", now) == "יום שישי 20:00"
    assert views.when_he(settings, "2026-11-05T18:00:00+00:00", now) == "05.11 20:00"
    assert views.when_he(settings, "2026-10-18T09:00:00+00:00", now) == views.ASAP_HE
    assert views.when_he(settings, None, now) == views.ASAP_HE


# --------------------------------------------------------------------------- thumbnail
def test_thumbnail_is_served_only_from_the_data_folder(pc, ctx, project_dir):
    data = ctx.settings.data_dir
    good = data / "videos" / "thumb.jpg"
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    vid = ctx.db.add_video("a.mp4", "x")
    ctx.db.update_video(vid, meta={"thumbnail": str(good)})
    res = pc.get(f"/api/videos/{vid}/thumbnail")
    assert res.status_code == 200 and res.headers["content-type"] == "image/jpeg"
    assert res.content == good.read_bytes()

    outside = project_dir / "outside.jpg"
    outside.write_bytes(b"\xff\xd8secret")
    link = data / "videos" / "link.jpg"
    link.symlink_to(outside)
    (data / "secrets").mkdir(parents=True, exist_ok=True)
    (data / "secrets" / "youtube_token.json").write_text("{}")
    bad_values = [str(outside), str(link), "../outside.jpg", "videos/../../outside.jpg",
                  str(data / "agency.db"), str(data / "secrets" / "youtube_token.json"),
                  str(data / "videos" / "missing.jpg"), "", "a\x00b.jpg"]
    for value in bad_values:
        ctx.db.update_video(vid, meta={"thumbnail": value})
        assert pc.get(f"/api/videos/{vid}/thumbnail").status_code == 404, value
    assert pc.get("/api/videos/999/thumbnail").status_code == 404
    assert pc.get("/api/videos/abc/thumbnail").status_code == 422


# --------------------------------------------------------------------------- plan editing
def test_plan_edit_while_pending_enforces_brand_and_limits(pc, ctx):
    aid, post_ids, _vid = make_publish(ctx, extra={"ai_label": True})
    long_title = "Deagle 3K on Mirage " + "x" * 200
    res = pc.patch(f"/api/approvals/{aid}/plans/youtube",
                   json={"title": long_title, "description": "New text. Use promo code CS2 now"})
    assert res.status_code == 200, res.text
    plan = res.json()["plan"]
    assert len(plan["title"]) <= 100 and plan["edited"] is True and plan["editable"] is True
    assert "promo code" not in plan["description"].lower()
    assert "AI" in plan["description"]  # the AI label always survives an edit
    stored = ctx.db.get_post(post_ids[0])["plan"]
    assert stored["title"] == plan["title"] and stored["extra"]["edited_by_owner"] is True
    assert stored["hashtags"][:3] == ["#CS2", "#CounterStrike2", "#CS2Edit"]
    snapshot = ctx.db.get_approval(aid).payload["plans"][0]
    assert snapshot["title"] == plan["title"] and snapshot["description"] == stored["description"]
    other = ctx.db.get_post(post_ids[1])["plan"]
    assert other["title"] == "Deagle 3K on Mirage 1"  # the other platform is untouched
    card = pc.get("/api/home").json()["needs_you"][0]
    assert card["plans"][0]["title"] == plan["title"] and card["main_title"] == plan["title"]


def test_plan_edit_refuses_blocked_title_and_bad_requests(pc, ctx):
    aid, post_ids, _vid = make_publish(ctx)
    res = pc.patch(f"/api/approvals/{aid}/plans/youtube", json={"title": "Free skins inside"})
    assert res.status_code == 400 and "free skins" in res.json()["detail"].lower()
    assert ctx.db.get_post(post_ids[0])["plan"]["title"] == "Deagle 3K on Mirage 0"
    assert pc.patch(f"/api/approvals/{aid}/plans/youtube", json={}).status_code == 400
    assert pc.patch(f"/api/approvals/{aid}/plans/youtube", json={"title": "  "}).status_code \
        == 400
    assert pc.patch(f"/api/approvals/{aid}/plans/tiktok", json={"title": "x"}).status_code \
        == 404
    assert pc.patch("/api/approvals/999/plans/youtube", json={"title": "x"}).status_code == 404
    spend = ctx.db.add_approval(Approval(id=None, kind="spend", title="s", cost_ils=5))
    assert pc.patch(f"/api/approvals/{spend}/plans/youtube", json={"title": "x"}).status_code \
        == 409


def test_plan_edit_only_while_pending(pc, ctx):
    aid, post_ids, _vid = make_publish(ctx)
    ctx.db.decide_approval(aid, True)  # the owner approved (execution may still be running)
    res = pc.patch(f"/api/approvals/{aid}/plans/youtube", json={"title": "Too late"})
    assert res.status_code == 409
    assert ctx.db.get_post(post_ids[0])["plan"]["title"] == "Deagle 3K on Mirage 0"
    rejected, _p, _v = make_publish(ctx)
    ctx.db.decide_approval(rejected, False)
    assert pc.patch(f"/api/approvals/{rejected}/plans/youtube",
                    json={"description": "x"}).status_code == 409


# --------------------------------------------------------------------------- settings
def test_settings_get_and_put(pc, ctx):
    data = pc.get("/api/settings").json()
    assert data["publishing"]["mode"] == "approve"
    assert data["brand"]["name"] == "Imagination" and data["brand"]["hashtags"][0] == "#CS2"
    assert data["dashboard"]["remote_access"] is False
    assert data["notify"]["telegram"]["enabled"] is False
    assert data["info"]["training"] is True
    res = pc.put("/api/settings", json={
        "channel": {"name": "Imagination", "handle": "@imaginationcs2"},
        "publishing": {"mode": "auto", "quiet_hours": {"enabled": True, "start": "01:00",
                                                       "end": "08:00"}},
        "finance": {"monthly_budget_ils": 100, "max_single_spend_ils": 30},
        "brand": {"bio": "Legendary CS2 moments."}})
    assert res.status_code == 200, res.text
    assert "publishing.mode" in res.json()["saved"]
    disk = config_on_disk(ctx)
    assert disk["publishing"]["mode"] == "auto" and disk["finance"]["monthly_budget_ils"] == 100
    assert disk["publishing"]["quiet_hours"] == {"enabled": True, "start": "01:00",
                                                 "end": "08:00"}
    assert (ctx.settings.root / "config.yaml.bak").exists()
    assert ctx.settings.get("publishing.mode") == "auto"  # the running app sees it at once
    again = pc.get("/api/settings").json()
    assert again["finance"]["max_single_spend_ils"] == 30
    assert again["brand"]["bio"] == "Legendary CS2 moments."
    # what GET returns can be PUT back as-is
    assert pc.put("/api/settings", json=again).status_code == 200


@pytest.mark.parametrize("body", [
    {"dry_run": False},                                    # not an owner setting here
    {"publishing": {"platforms": {"tiktok": {"live": True}}}},  # live has its own confirm
    {"llm": {"model": "x"}},
    {"brand": {"api_key": "abc"}},
    {"finance": {"monthly_budget_ils": "a lot"}},
    {"publishing": {"mode": "yolo"}},
    {"publishing": {"quiet_hours": {"start": "25:99"}}},
    {"inbox": {"enabled": "yes"}},
    {},
])
def test_settings_put_refuses(pc, ctx, body):
    before = (ctx.settings.root / "config.yaml").read_text(encoding="utf-8")
    res = pc.put("/api/settings", json=body)
    assert res.status_code == 400, res.text
    assert (ctx.settings.root / "config.yaml").read_text(encoding="utf-8") == before


def test_settings_never_store_a_secret_value(pc, ctx, monkeypatch):
    monkeypatch.setenv("ZERNIO_API_KEY", ZKEY)
    res = pc.put("/api/settings", json={"brand": {"bio": f"my key {ZKEY}"}})
    assert res.status_code == 400 and ZKEY not in res.text
    assert ZKEY not in (ctx.settings.root / "config.yaml").read_text(encoding="utf-8")


def test_settings_inbox_on_records_baseline(pc, ctx, project_dir):
    folder = project_dir / "CS2_CLIPS"
    folder.mkdir()
    (folder / "SHORT_DEAGLE_MOVIE.mp4").write_bytes(b"\x00" * 2048)
    res = pc.put("/api/settings", json={"inbox": {"enabled": True, "folders": [str(folder)]}})
    assert res.status_code == 200, res.text
    assert config_on_disk(ctx)["inbox"]["enabled"] is True
    clips = pc.get("/api/home").json()["previous_clips"]
    assert [c["name"] for c in clips] == ["SHORT_DEAGLE_MOVIE.mp4"]
    assert re.fullmatch(r"[0-9a-f]{12}", clips[0]["id"]) and "path" not in clips[0]
    assert ctx.db.list_videos() == []  # baseline clips are NOT processed by themselves
    inbox = pc.get("/api/inbox").json()
    assert inbox["enabled"] is True and inbox["previous_clips"] == clips


# --------------------------------------------------------------------------- inbox prepare
def test_inbox_prepare_previous_clip(pc, ctx, project_dir, tiny_video):
    folder = project_dir / "clips"
    folder.mkdir()
    clip = folder / "SHORT_EP3.mp4"
    clip.write_bytes(tiny_video.read_bytes())
    assert pc.put("/api/settings", json={"inbox": {"enabled": True, "folders": [str(folder)]}}
                  ).status_code == 200
    [prev] = pc.get("/api/home").json()["previous_clips"]
    assert pc.post("/api/inbox/prepare", json={"clip_id": "0" * 12}).status_code == 404
    res = pc.post("/api/inbox/prepare", json={"clip_id": prev["id"]})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True and body["video_id"] and body["name"] == "SHORT_EP3.mp4"
    assert [a.kind for a in ctx.db.list_approvals("pending")].count("publish") == 1
    assert clip.exists()  # the owner's file is never moved
    home = pc.get("/api/home").json()
    assert home["previous_clips"] == [] and home["needs_you"][0]["kind"] == "publish"
    assert pc.post("/api/inbox/prepare", json={"clip_id": prev["id"]}).status_code == 404


# --------------------------------------------------------------------------- connections
def test_connections_tiles(pc):
    data = pc.get("/api/connections?check=0").json()
    keys = [t["key"] for t in data["tiles"]]
    assert keys == ["youtube", "youtube_shorts", "tiktok", "instagram_reels", "facebook", "x",
                    "reddit", "discord", "telegram"]
    for tile in data["tiles"]:
        assert tile["state"] == "not_connected" and tile["status_he"] == "לא מחובר"
        assert tile["connected"] is False and tile["live"] is False
        assert isinstance(tile["missing"], list)
    youtube = data["tiles"][0]
    assert youtube["via"] == "zernio" and youtube["required"] == ["ZERNIO_API_KEY"]
    assert data["tiles"][1]["shares"] == "youtube"
    assert data["tiles"][-1]["telegram"]["state"] == "off"
    assert data["keys_saved_in"] == "file" and data["training"] is True
    # a real check with nothing configured makes no network call at all (no_network is on)
    checked = pc.get("/api/connections").json()["tiles"]
    assert {t["state"] for t in checked} == {"not_connected"}


def test_connections_studio_source_and_direct_warning(pc, ctx, monkeypatch, http):
    from agency import config

    ctx.settings.raw["publishing"]["platforms"]["youtube"]["via"] = "direct"
    monkeypatch.setattr(config, "_resolve_secret",
                        lambda name: (ZKEY, "studio") if name == "ZERNIO_API_KEY"
                        else (None, None))
    http({("GET", "https://zernio.com/api/v1/accounts"): Resp(200, {"accounts": [
        {"platform": "tiktok", "displayName": "Imagination", "isActive": True}]})})
    tiles = {t["key"]: t for t in pc.get("/api/connections").json()["tiles"]}
    assert tiles["youtube"]["warning_he"] and tiles["tiktok"]["warning_he"] == ""
    assert tiles["tiktok"]["connected"] is True
    assert tiles["tiktok"]["status_he"] == "מחובר ✓ Imagination"
    assert tiles["tiktok"]["source_he"] == "מחובר דרך הסטודיו הקיים"


def test_connect_keys_saved_verified_and_never_echoed(pc, ctx, http):
    fake = http({("GET", "https://discord.com/api/webhooks/"): Resp(200, {"name": "Imagination"})})
    res = pc.post("/api/connect/discord/keys", json={"values": {"DISCORD_WEBHOOK_URL": HOOK}})
    assert res.status_code == 200, res.text
    body = res.json()
    assert HOOK not in res.text and "SECRET_hook" not in res.text
    assert body["ok"] is True and body["saved"] == ["DISCORD_WEBHOOK_URL"]
    assert body["tile"]["connected"] is True and body["tile"]["account_name"] == "Imagination"
    assert fake.calls and fake.calls[0][0] == "GET"
    assert HOOK in (ctx.settings.root / ".env").read_text(encoding="utf-8")  # keyring is off
    assert os.environ["DISCORD_WEBHOOK_URL"] == HOOK
    events = json.dumps(pc.get("/api/events").json(), ensure_ascii=False)
    assert HOOK not in events and "SECRET_hook" not in events


@pytest.mark.parametrize("payload, status", [
    ({"values": {"X_API_KEY": HOOK}}, 400),               # not a discord name
    ({"values": {"DISCORD_WEBHOOK_URL": [HOOK]}}, 400),   # not text
    ({"values": {"discord url": HOOK}}, 400),             # not a key name
    ({"values": {}}, 400),
    ({"nothing": HOOK}, 400),
])
def test_connect_keys_refusals_never_echo(pc, ctx, payload, status):
    res = pc.post("/api/connect/discord/keys", json=payload)
    assert res.status_code == status
    assert "SECRET_hook" not in res.text
    assert not (ctx.settings.root / ".env").exists() or \
        HOOK not in (ctx.settings.root / ".env").read_text(encoding="utf-8")


def test_connect_keys_bad_json_and_unknown_platform(pc):
    res = pc.post("/api/connect/discord/keys", content=b"{not json SECRET_hook",
                  headers={"content-type": "application/json"})
    assert res.status_code == 400 and "SECRET_hook" not in res.text
    assert pc.post("/api/connect/myspace/keys", json={"values": {"A_B": "x"}}).status_code \
        == 404
    assert pc.post("/api/connect/myspace/verify").status_code == 404


def test_connect_verify_and_disconnect(pc, ctx, monkeypatch, http):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", HOOK)  # (restored after the test)
    ss.env_set(ctx.settings.root, {"DISCORD_WEBHOOK_URL": HOOK})  # saved in our .env
    http({("GET", "https://discord.com/api/webhooks/"): Resp(401, {"message": "Unknown"})})
    res = pc.post("/api/connect/discord/verify")
    assert res.status_code == 200 and res.json()["ok"] is False
    tile = res.json()["tile"]
    assert tile["state"] == "needs_fix" and tile["problem"] and HOOK not in res.text
    out = pc.post("/api/connect/discord/disconnect")
    assert out.status_code == 200 and out.json()["removed"] == ["DISCORD_WEBHOOK_URL"]
    assert out.json()["tile"]["state"] == "not_connected"
    assert "DISCORD_WEBHOOK_URL" not in os.environ


def test_disconnect_removes_the_one_zernio_key(pc, ctx, monkeypatch):
    # YouTube and TikTok ride on the same Zernio key and need nothing else: "נתק" really
    # removes it (it used to be kept forever), and says TikTok went with it.
    monkeypatch.setenv("ZERNIO_API_KEY", ZKEY)  # (restored after the test)
    ss.env_set(ctx.settings.root, {"ZERNIO_API_KEY": ZKEY})
    ss.update_config(ctx.settings, {"publishing": {"platforms": {"tiktok": {"live": True}}}})
    out = pc.post("/api/connect/youtube/disconnect").json()
    assert out["removed"] == ["ZERNIO_API_KEY"] and out["kept_shared"] == []
    assert out["also_disconnected"] == ["tiktok"] and "ZERNIO_API_KEY" not in os.environ
    assert ctx.settings.platform_live("tiktok") is False  # never live again by surprise


def test_disconnect_keeps_a_key_another_platform_needs(pc, ctx, monkeypatch):
    monkeypatch.setenv("META_ACCESS_TOKEN", "EAA_SECRET_meta_token_0009")
    ss.env_set(ctx.settings.root, {"META_ACCESS_TOKEN": "EAA_SECRET_meta_token_0009"})
    ss.update_config(ctx.settings, {"meta": {"ig_user_id": "17841400000000001",
                                             "page_id": "102030405060708"}})
    out = pc.post("/api/connect/instagram_reels/disconnect").json()
    assert out["kept_shared"] == ["META_ACCESS_TOKEN"] and out["removed"] == ["IG_USER_ID"]
    assert out["also_disconnected"] == []


def test_disconnect_reports_a_key_set_in_the_system(pc, monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", HOOK)  # set outside our .env: not ours
    out = pc.post("/api/connect/discord/disconnect").json()
    assert out["system"] == ["DISCORD_WEBHOOK_URL"] and out["removed"] == []
    assert os.environ["DISCORD_WEBHOOK_URL"] == HOOK


def test_live_switch_needs_confirm_and_a_connection(pc, ctx, monkeypatch):
    res = pc.post("/api/connect/discord/live", json={"live": True})
    assert res.status_code == 400 and res.json()["reason"] == "confirm_required"
    assert "באמת" in res.json()["confirm_he"]
    assert pc.post("/api/connect/discord/live", json={"live": True, "confirm": True}
                   ).status_code == 409  # not connected -> never live
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", HOOK)
    ctx.settings.raw["publishing"]["platforms"]["discord"]["enabled"] = True
    res = pc.post("/api/connect/discord/live", json={"live": True, "confirm": True})
    assert res.status_code == 200, res.text
    assert res.json()["tile"]["live"] is True
    assert config_on_disk(ctx)["publishing"]["platforms"]["discord"]["live"] is True
    assert ctx.settings.dry_run is True  # the global safety stays on
    platforms = {p["key"]: p for p in pc.get("/api/platforms").json()["platforms"]}
    assert platforms["discord"]["live"] is True and platforms["x"]["live"] is False
    home = pc.get("/api/home").json()
    assert home["mode"] == "live" and home["live_platforms"] == [
        {"key": "discord", "name_he": "דיסקורד"}]
    assert pc.post("/api/connect/discord/live", json={"live": False}).json()["tile"]["live"] \
        is False
    assert pc.post("/api/connect/telegram/live", json={"live": False}).status_code == 404


# --------------------------------------------------------------------------- youtube direct
CLIENT_SECRET = {"installed": {"client_id": "123-abc.apps.googleusercontent.com",
                               "client_secret": "GOCSPX-SECRET_client_0005",
                               "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                               "token_uri": "https://oauth2.googleapis.com/token"}}


def test_youtube_client_secret_upload(pc, ctx):
    target = ctx.settings.data_dir / "secrets" / "youtube_client_secret.json"
    res = pc.post("/api/connect/youtube/client-secret", content=json.dumps(CLIENT_SECRET),
                  headers={"content-type": "application/json"})
    assert res.status_code == 200, res.text
    assert res.json()["ok"] is True and res.json()["kind"] == "installed"
    assert "SECRET_client" not in res.text
    assert json.loads(target.read_text())["installed"]["client_secret"].endswith("0005")
    if os.name != "nt":
        assert target.stat().st_mode & 0o077 == 0
    target.unlink()
    res = pc.post("/api/connect/youtube/client-secret",
                  files={"file": ("client_secret_123.json", json.dumps(CLIENT_SECRET),
                                  "application/json")})
    assert res.status_code == 200, res.text
    assert target.exists()
    bad = pc.post("/api/connect/youtube/client-secret", content=b'{"hello": "SECRET_x"}',
                  headers={"content-type": "application/json"})
    assert bad.status_code == 400 and "SECRET_x" not in bad.text
    assert pc.post("/api/connect/youtube/client-secret", content=b"").status_code == 400
    huge = pc.post("/api/connect/youtube/client-secret", content=b"x" * (1024 * 1024 + 10))
    assert huge.status_code == 413


def test_youtube_oauth_start_and_state(pc, monkeypatch):
    from agency import connect

    seen = []

    def fake_run(settings, open_browser):
        seen.append(open_browser)
        connect._set_oauth(state="done", channel="Imagination", thumbnail="https://t/x.jpg",
                           problem=None)

    monkeypatch.setattr(connect, "_run_oauth", fake_run)
    monkeypatch.setattr(connect, "_oauth_state", {"state": "idle", "channel": None,
                                                  "thumbnail": None, "problem": None})
    state = pc.post("/api/connect/youtube/start").json()
    assert seen == [True] and state["state"] == "done"
    assert pc.get("/api/connect/youtube/state").json() == {
        "state": "done", "channel": "Imagination", "thumbnail": "https://t/x.jpg",
        "problem": None}


# --------------------------------------------------------------------------- import
@pytest.fixture
def found_dir(project_dir) -> Path:
    folder = project_dir / "old_project"
    folder.mkdir()
    (folder / ".env").write_text(f"DISCORD_WEBHOOK_URL={HOOK}\nUNRELATED=1\n", encoding="utf-8")
    (folder / "client_secret_123.json").write_text(json.dumps(CLIENT_SECRET), encoding="utf-8")
    (folder / "token.pickle").write_bytes(b"\x80\x04not-opened")
    return folder


@pytest.fixture
def no_default_roots(monkeypatch):
    from agency import importer
    monkeypatch.setattr(importer, "default_roots", lambda settings=None: [])


def test_import_scan_and_apply(pc, ctx, found_dir, no_default_roots, http):
    assert pc.post("/api/connect/import/apply", json={"ids": ["abc"]}).status_code == 409
    res = pc.post("/api/connect/import/scan", json={"extra_path": str(found_dir)})
    assert res.status_code == 200, res.text
    assert "SECRET_hook" not in res.text and "SECRET_client" not in res.text
    items = {i["kind"]: i for i in res.json()["items"]}
    assert set(items) == {"env_file", "google_client_secret", "token_pickle"}
    assert items["env_file"]["names"] == ["DISCORD_WEBHOOK_URL"]
    assert items["token_pickle"]["ok"] is False and items["token_pickle"]["selected"] is False
    assert res.json()["studio"] == {"found": False, "dir": str(found_dir)}

    http({("GET", "https://discord.com/api/webhooks/"): Resp(200, {"name": "Imagination"})})
    ids = [items["env_file"]["id"], items["google_client_secret"]["id"], "unknown0000"]
    out = pc.post("/api/connect/import/apply", json={"ids": ids})
    assert out.status_code == 200, out.text
    assert "SECRET_hook" not in out.text and "SECRET_client" not in out.text
    results = {r["id"]: r for r in out.json()["results"]}
    assert results[items["env_file"]["id"]]["imported"] == ["DISCORD_WEBHOOK_URL"]
    assert results[items["env_file"]["id"]]["verify"]["discord"]["connected"] is True
    assert results[items["google_client_secret"]["id"]]["imported"] == [
        "youtube_client_secret.json"]
    assert results["unknown0000"]["problem"]
    assert os.environ["DISCORD_WEBHOOK_URL"] == HOOK
    assert (ctx.settings.data_dir / "secrets" / "youtube_client_secret.json").is_file()
    tiles = {t["key"]: t for t in out.json()["tiles"]}
    assert tiles["discord"]["connected"] is True
    assert pc.post("/api/connect/import/apply", json={"ids": ["nope"]}).status_code == 400


def test_import_scan_finds_the_studio(pc, ctx, studio_dir, no_default_roots):
    res = pc.post("/api/connect/import/scan", json={"extra_path": str(studio_dir)})
    assert res.status_code == 200, res.text
    studio = res.json()["studio"]
    assert studio["found"] is True and studio["meta_ids"]["page_id"] == "102030405060708"
    assert studio["next_connect"][0]["name"] == "ZERNIO_API_KEY"
    [item] = [i for i in res.json()["items"] if i["kind"] == "studio"]
    out = pc.post("/api/connect/import/apply", json={"ids": [item["id"]]})
    assert out.status_code == 200, out.text
    assert ctx.settings.get("meta.ig_user_id") == "17841400000000001"
    assert config_on_disk(ctx)["studio"]["dir"] == str(studio_dir)
    assert pc.get("/api/home").json()["studio"]["imported"] is True


def test_import_scan_bad_path(pc, project_dir, no_default_roots):
    assert pc.post("/api/connect/import/scan", json={"extra_path": str(project_dir / "nope")}
                   ).status_code == 400
    assert pc.post("/api/connect/import/scan", json={}).status_code == 200


# --------------------------------------------------------------------------- telegram
def test_telegram_pair_and_state(pc, ctx, monkeypatch):
    assert pc.post("/api/telegram/pair").status_code == 409  # no bot token yet
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TG_TOKEN)
    res = pc.post("/api/telegram/pair")
    assert res.status_code == 200, res.text
    body = res.json()
    assert re.fullmatch(r"\d{6}", body["code"]) and body["code"] in body["instructions_he"]
    assert body["enabled_now"] is True and TG_TOKEN not in res.text
    assert config_on_disk(ctx)["notify"]["telegram"]["enabled"] is True
    state = pc.get("/api/telegram/state")
    assert state.json()["enabled"] is True and state.json()["paired"] is False
    assert body["code"] not in state.text and TG_TOKEN not in state.text


# --------------------------------------------------------------------------- phone access
def test_remote_client_refused_while_phone_access_is_off(app):
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", client=PHONE,
                    follow_redirects=False) as c:
        for path in ("/", "/api/home", "/login", "/api/health", "/static/app.js"):
            res = c.get(path)
            assert res.status_code == 403, path
        assert c.get("/api/home").json()["reason"] == "remote_disabled"
        assert c.post("/api/auth/login", json={"pin": "123456"}, headers=HEADERS
                      ).status_code == 403
    with phone(app) as c:  # the LAN address is not even on the Host allow-list
        assert c.get("/api/home").status_code == 403


def test_phone_login_flow(remote_app, ctx):
    pin = auth.rotate_pin(ctx.db)
    aid, _p, _v = make_publish(ctx)
    with phone(remote_app) as c:
        first = c.get("/")
        assert first.status_code == 303 and first.headers["location"] == "/login"
        api = c.get("/api/home")
        assert api.status_code == 401 and api.json()["reason"] == "login_required"
        assert c.get("/", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 303
        assert c.get("/api/health").status_code == 200  # public
        page = c.get("/login")
        assert page.status_code == 200 and "default-src 'none'" in page.headers[
            "content-security-policy"] and 'dir="rtl"' in page.text
        assert c.post("/api/auth/login", json={"pin": pin}).status_code == 403  # no CSRF
        wrong = c.post("/api/auth/login", json={"pin": "000000" if pin != "000000"
                                                else "999999"}, headers=HEADERS)
        assert wrong.status_code == 401 and wrong.json()["attempts_left"] == 4
        ok = c.post("/api/auth/login", json={"pin": pin}, headers=HEADERS)
        assert ok.status_code == 200 and ok.json() == {"ok": True} and pin not in ok.text
        cookie = ok.headers["set-cookie"]
        assert "HttpOnly" in cookie and re.search(r"(?i)samesite=strict", cookie)
        assert c.get("/api/home").status_code == 200
        assert c.get("/").status_code == 200
        assert c.get("/").headers["content-security-policy"] == \
            "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; " \
            "style-src 'self'; script-src 'self'; connect-src 'self'; " \
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        # the owner approves from the couch
        assert c.post(f"/api/approvals/{aid}/approve", json={}, headers=HEADERS
                      ).status_code == 200
        assert c.post("/api/auth/logout", headers=HEADERS).json()["revoked"] is True
        assert c.get("/api/home").status_code == 401
    assert ctx.db.get_approval(aid).status in ("approved", "executed")


def test_pc_only_endpoints_refuse_a_logged_in_phone(remote_app, ctx):
    token = auth.create_session(ctx.db, label="phone")
    with phone(remote_app, headers=HEADERS) as c:
        c.cookies.set(auth.COOKIE_NAME, token)
        assert c.get("/api/home").status_code == 200
        refused = [
            ("get", "/api/remote", None), ("post", "/api/remote/pin", None),
            ("post", "/api/auth/revoke-all", None), ("post", "/api/telegram/pair", None),
            ("post", "/api/connect/import/scan", {}),
            ("post", "/api/connect/import/apply", {"ids": ["x"]}),
            ("post", "/api/connect/discord/keys", {"values": {"DISCORD_WEBHOOK_URL": HOOK}}),
            ("post", "/api/connect/youtube/client-secret", CLIENT_SECRET),
            ("post", "/api/connect/youtube/start", None),
            ("post", "/api/connect/discord/disconnect", None),
            ("put", "/api/settings", {"dashboard": {"remote_access": False}}),
            ("put", "/api/settings", {"inbox": {"folders": ["/etc"]}}),
        ]
        for method, path, body in refused:
            res = getattr(c, method)(path, **({"json": body} if body is not None else {}))
            assert res.status_code == 403, (method, path)
            assert res.json()["reason"] == "local_only", (method, path)
            assert "SECRET" not in res.text
        # ordinary owner settings may change from the phone
        assert c.put("/api/settings", json={"publishing": {"mode": "approve"},
                                            "dashboard": {"remote_access": True}}
                     ).status_code == 200
    assert auth.session_count(ctx.db) == 1


def test_remote_screen_shows_the_pin_once(remote_app, ctx):
    with TestClient(remote_app, base_url=f"http://127.0.0.1:{PORT}", client=PC,
                    headers=HEADERS) as pc_client:
        # GET is read-only (a forged cross-site GET cannot rotate the PIN): no PIN yet
        before = pc_client.get("/api/remote").json()
        assert before["pin"] is None and before["pin_set"] is False and not auth.pin_is_set(ctx.db)
        first = pc_client.post("/api/remote/pin").json()
        assert re.fullmatch(r"\d{6}", first["pin"]) and first["pin_set"] is True
        assert first["url"] == f"http://{LAN_IP}:{PORT}/" and first["qr_svg"].startswith("<svg")
        assert first["restart_needed"] is True  # bound to 127.0.0.1 until the next start
        assert auth.pin_matches(first["pin"], ctx.db.kv_get(auth.PIN_KEY))
        second = pc_client.get("/api/remote").json()
        assert second["pin"] is None and second["pin_set"] is True and second["qr_svg"]
        new = pc_client.post("/api/remote/pin").json()
        assert re.fullmatch(r"\d{6}", new["pin"])
        assert auth.pin_matches(new["pin"], ctx.db.kv_get(auth.PIN_KEY))
        auth.create_session(ctx.db)
        assert pc_client.post("/api/auth/revoke-all").json()["revoked"] == 1
        # switching phone access off logs every phone out and forgets the PIN
        auth.create_session(ctx.db)
        assert pc_client.put("/api/settings", json={"dashboard": {"remote_access": False}}
                             ).status_code == 200
        assert auth.session_count(ctx.db) == 0 and not auth.pin_is_set(ctx.db)
        off = pc_client.get("/api/remote").json()
        assert off["remote_enabled"] is False and off["pin"] is None
        assert pc_client.post("/api/remote/pin").status_code == 409
    with phone(remote_app) as c:  # now refused at once (no restart needed)
        assert c.get("/api/home").json()["reason"] == "remote_disabled"


def test_wildcard_bind_with_phone_access_uses_the_pin_not_the_key(ctx, no_network, lan,
                                                                  fast_scrypt):
    ctx.settings.raw["dashboard"].update(host="0.0.0.0", remote_access=True)
    application = create_app(ctx)
    assert application.state.access_key is None
    assert LAN_IP in application.state.allowed_hosts
    with phone(application) as c:
        assert c.get("/api/home").status_code == 401
    with TestClient(application, base_url="http://evil.example:8765", client=PC) as c:
        assert c.get("/api/home").status_code == 403  # DNS rebinding


# --------------------------------------------------------------------------- CSRF
NEW_WRITES = [
    ("patch", "/api/approvals/1/plans/youtube", {"title": "x"}),
    ("put", "/api/settings", {"publishing": {"mode": "approve"}}),
    ("post", "/api/connect/discord/keys", {"values": {"DISCORD_WEBHOOK_URL": HOOK}}),
    ("post", "/api/connect/discord/verify", None),
    ("post", "/api/connect/discord/disconnect", None),
    ("post", "/api/connect/discord/live", {"live": False}),
    ("post", "/api/connect/youtube/client-secret", CLIENT_SECRET),
    ("post", "/api/connect/youtube/start", None),
    ("post", "/api/connect/import/scan", {}),
    ("post", "/api/connect/import/apply", {"ids": []}),
    ("post", "/api/telegram/pair", None),
    ("post", "/api/inbox/prepare", {"clip_id": "abcdef123456"}),
    ("post", "/api/auth/login", {"pin": "123456"}),
    ("post", "/api/auth/logout", None),
    ("post", "/api/auth/revoke-all", None),
    ("post", "/api/remote/pin", None),
]


@pytest.mark.parametrize("method, path, body", NEW_WRITES)
def test_csrf_header_required_on_every_new_write(app, ctx, method, path, body):
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", client=PC) as c:
        res = getattr(c, method)(path, **({"json": body} if body is not None else {}))
        assert res.status_code == 403 and "X-Agency-Request" in res.json()["detail"]
        foreign = getattr(c, method)(path, headers={**HEADERS, "Origin": "http://evil.example"},
                                     **({"json": body} if body is not None else {}))
        assert foreign.status_code == 403
    assert not (ctx.settings.root / ".env").exists()


def test_oversized_json_body_refused(pc):
    res = pc.put("/api/settings", content=b"{" + b" " * (3 * 1024 * 1024) + b"}",
                 headers={"content-type": "application/json"})
    assert res.status_code == 413


def test_validation_errors_do_not_echo_input(pc, ctx):
    aid, _p, _v = make_publish(ctx)
    res = pc.patch(f"/api/approvals/{aid}/plans/youtube", json={"title": ["SECRET_echo_1"]})
    assert res.status_code == 422 and "SECRET_echo_1" not in res.text
    res = pc.post("/api/connect/discord/live", json={"live": "SECRET_echo_2"})
    assert res.status_code == 422 and "SECRET_echo_2" not in res.text
    res = pc.put("/api/settings", json=["SECRET_echo_3"])
    assert res.status_code == 422 and "SECRET_echo_3" not in res.text


# --------------------------------------------------------------------------- secret hygiene
def test_no_response_ever_contains_a_secret_value(app, ctx, monkeypatch, http):
    from agency.settings_store import NOT_SECRET_NAMES, allowed_env_names

    planted = {n: f"PLANTED_{n}_v4lu3_9z" for n in allowed_env_names()
               if n not in NOT_SECRET_NAMES}
    planted["DISCORD_WEBHOOK_URL"] = HOOK
    for name, value in planted.items():
        monkeypatch.setenv(name, value)
    secrets_dir = ctx.settings.data_dir / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    token = {"token": "ya29.PLANTED_access_token_value", "refresh_token": "1//PLANTED_refresh",
             "client_id": "cid.apps.googleusercontent.com",
             "client_secret": "PLANTED_client_secret_x", "scopes": [
                 "https://www.googleapis.com/auth/youtube.upload"]}
    (secrets_dir / "youtube_token.json").write_text(json.dumps(token))
    (secrets_dir / "youtube_client_secret.json").write_text(json.dumps(CLIENT_SECRET))
    values = [*planted.values(), *token.values(), CLIENT_SECRET["installed"]["client_secret"]]
    values = [v for v in values if isinstance(v, str) and ("PLANTED" in v or "SECRET" in v)]
    fake = http({}, default=Resp(401, {"error": {"code": 190, "message": "bad"}}))
    make_publish(ctx)
    texts: list[str] = []
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", client=PC, headers=HEADERS) as c:
        for path in ("/api/health", "/api/home", "/api/status", "/api/platforms",
                     "/api/connections?check=0", "/api/connections?check=1", "/api/settings",
                     "/api/approvals?status=all", "/api/videos", "/api/posts", "/api/finance",
                     "/api/telegram/state", "/api/remote", "/api/inbox",
                     "/api/connect/youtube/state"):
            res = c.get(path)
            assert res.status_code == 200, (path, res.text)
            texts.append(res.text)
        for key in ("youtube", "tiktok", "instagram_reels", "facebook", "x", "discord",
                    "telegram"):
            res = c.post(f"/api/connect/{key}/verify")
            assert res.status_code == 200, (key, res.text)
            texts.append(res.text)
        texts.append(c.post("/api/connect/telegram/keys",
                            json={"values": {"TELEGRAM_BOT_TOKEN": TG_TOKEN}}).text)
        texts.append(c.post("/api/telegram/pair").text)
        texts.append(c.put("/api/settings", json={"brand": {"bio": "x"}}).text)
        texts.append(c.get("/api/events?limit=500").text)
    joined = "\n".join(texts)
    # the real verification paths ran (Zernio, X, Discord, Telegram...), all answered "401"
    called = {url.split("/")[2] for _m, url in fake.calls}
    assert {"zernio.com", "api.x.com", "discord.com", "api.telegram.org"} <= called
    assert "המפתח לא נכון" in joined
    for value in [*values, TG_TOKEN]:
        assert value not in joined, value[:12]
    assert "PLANTED" not in joined and "SECRET_" not in joined


# --------------------------------------------------------------------------- services
SERVICE_THREADS = {"scheduler", "inbox-watcher", "services-stop", "telegram-bot"}


def _threads(before: set[threading.Thread]) -> list[threading.Thread]:
    """Service threads started since ``before`` (not the web server's worker threads)."""
    return [t for t in threading.enumerate() if t not in before and t.name in SERVICE_THREADS]


def test_services_share_one_stop_event(ctx, no_network, monkeypatch):
    from agency.notify import telegram

    calls: list[str] = []
    monkeypatch.setattr(telegram, "start_telegram", lambda c: calls.append("start") or object())
    monkeypatch.setattr(telegram, "stop_telegram", lambda: calls.append("stop"))

    application = create_app(ctx)
    before = set(threading.enumerate())
    stop = threading.Event()
    assert start_services(application, stop, interval_s=3600) == ["scheduler"]
    assert calls == []  # inbox and Telegram are off -> not started
    started = _threads(before)
    assert {"scheduler", "services-stop"} <= {t.name for t in started}
    stop.set()
    for t in started:
        t.join(timeout=5)
        assert not t.is_alive(), t.name
    assert calls == ["stop"]

    calls.clear()
    ctx.settings.raw["inbox"]["enabled"] = True
    ctx.settings.raw["notify"]["telegram"]["enabled"] = True
    application = create_app(ctx)
    before = set(threading.enumerate())
    stop = threading.Event()
    assert start_services(application, stop, interval_s=3600) == [
        "scheduler", "inbox", "telegram"]
    started = _threads(before)
    assert {"scheduler", "inbox-watcher", "services-stop"} <= {t.name for t in started}
    stop_services(application)
    assert stop.is_set()
    for t in started:
        t.join(timeout=5)
        assert not t.is_alive(), t.name
    assert calls[0] == "start" and "stop" in calls


def test_settings_toggle_starts_inbox_and_telegram_while_running(ctx, no_network, lan,
                                                                 monkeypatch, project_dir):
    from agency.notify import telegram

    calls: list[str] = []
    monkeypatch.setattr(telegram, "start_telegram", lambda c: calls.append("start") or object())
    monkeypatch.setattr(telegram, "stop_telegram", lambda: calls.append("stop"))
    application = create_app(ctx)
    before = set(threading.enumerate())
    stop = threading.Event()
    start_services(application, stop, interval_s=3600)
    folder = project_dir / "clips"
    folder.mkdir()
    with TestClient(application, base_url=f"http://127.0.0.1:{PORT}", client=PC,
                    headers=HEADERS) as c:
        assert c.put("/api/settings", json={"inbox": {"enabled": True,
                                                      "folders": [str(folder)]},
                                            "notify": {"telegram": {"enabled": True}}}
                     ).status_code == 200
        assert calls == ["start"]
        assert any(t.name == "inbox-watcher" and t.is_alive() for t in _threads(before))
        assert c.put("/api/settings", json={"notify": {"telegram": {"enabled": False}}}
                     ).status_code == 200
        assert calls == ["start", "stop"]
    stop.set()
    for t in _threads(before):
        t.join(timeout=5)
        assert not t.is_alive(), t.name


def test_cli_dashboard_binds_lan_for_phone_access_and_stops_services(project_dir, monkeypatch,
                                                                     lan, no_network):
    import uvicorn
    from typer.testing import CliRunner

    from agency import cli

    config = yaml.safe_load((project_dir / "config.yaml").read_text(encoding="utf-8"))
    config["dashboard"]["remote_access"] = True
    (project_dir / "config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True),
                                             encoding="utf-8")
    seen: dict = {}

    def fake_run(app, **kwargs):
        seen.update(kwargs, app=app, services=dict(app.state.services))
        assert not app.state.services["stop"].is_set()

    monkeypatch.setattr(uvicorn, "run", fake_run)
    before = set(threading.enumerate())
    result = CliRunner().invoke(cli.app, ["dashboard", "--no-browser", "--interval", "3600"],
                                catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert seen["host"] == "0.0.0.0" and seen["port"] == 8765
    assert seen["proxy_headers"] is False
    assert seen["app"].state.access_key is None  # the PIN login, not the legacy key
    assert seen["services"]["stop"].is_set()     # everything stopped when uvicorn returned
    for t in _threads(before):
        t.join(timeout=5)
        assert not t.is_alive(), t.name
    assert LAN_IP in result.output
