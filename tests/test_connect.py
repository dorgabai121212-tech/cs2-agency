"""Connections: read-only verification (mocked network), state tiles, cache, keys, disconnect,
YouTube direct OAuth helpers. No secret value may ever appear in a result."""
from __future__ import annotations

import json
import os
import sys
import types

import pytest
import requests

from agency import connect
from agency.platforms import registry

ZKEY = "zr_SECRET_zernio_key_0001"
META = "EAAG_SECRET_meta_page_token_0002"
TG = "777000:SECRET_telegram_token_0003"
HOOK = "https://discord.com/api/webhooks/123/SECRET_hook_0004"


class Resp:
    def __init__(self, status: int = 200, payload=None):
        self.status_code = status
        self._payload = {} if payload is None else payload
        self.content = json.dumps(self._payload).encode()

    def json(self):
        return self._payload


class FakeHTTP:
    """requests.request replacement: routes by (method, url prefix)."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        for (m, prefix), resp in self.routes.items():
            if m == method and url.startswith(prefix):
                return resp(url, kwargs) if callable(resp) else resp
        raise AssertionError(f"unexpected request {method} {url[:40]}")


@pytest.fixture
def http(monkeypatch):
    def install(routes: dict) -> FakeHTTP:
        fake = FakeHTTP(routes)
        monkeypatch.setattr(requests, "request", fake)
        return fake
    return install


ACCOUNTS = {"accounts": [
    {"_id": "acc-yt", "platform": "youtube", "username": "imaginationcs2",
     "displayName": "Imagination", "isActive": True, "needsReconnection": False},
    {"_id": "acc-tt", "platform": "tiktok", "username": "imaginationcs2",
     "isActive": True, "needsReconnection": True},
], "hasAnalyticsAccess": False}


def assert_no_secrets(obj) -> None:
    text = json.dumps(obj, ensure_ascii=False, default=str)
    for value in (ZKEY, META, TG, HOOK, "SECRET_", "SECRET-"):
        assert value not in text


# ----------------------------------------------------------------- verify
@pytest.mark.parametrize("key", connect.CONNECT_KEYS)
def test_not_configured_means_no_network(settings, no_network, key):
    result = connect.verify(key, settings)
    assert result == {"connected": False, "account_name": None, "problem": None}


def test_zernio_verify_and_header(settings, http, monkeypatch):
    monkeypatch.setenv("ZERNIO_API_KEY", ZKEY)
    fake = http({("GET", "https://zernio.com/api/v1/accounts"): Resp(200, ACCOUNTS)})
    yt = connect.verify("youtube", settings)
    assert yt == {"connected": True, "account_name": "Imagination", "problem": None}
    method, url, kwargs = fake.calls[0]
    assert kwargs["headers"]["Authorization"] == f"Bearer {ZKEY}"
    assert kwargs["timeout"] == 15
    tt = connect.verify("tiktok", settings)
    assert tt["connected"] is False and "מחדש" in tt["problem"]
    assert_no_secrets([yt, tt])


def test_zernio_bad_key_and_missing_account(settings, http, monkeypatch):
    monkeypatch.setenv("ZERNIO_API_KEY", ZKEY)
    http({("GET", "https://zernio.com/api/v1/accounts"): Resp(401, {"error": "bad"})})
    assert connect.verify("youtube", settings)["problem"] == connect.BAD_KEY_HE
    connect.invalidate(settings)
    http({("GET", "https://zernio.com/api/v1/accounts"): Resp(200, {"accounts": []})})
    result = connect.verify("youtube", settings)
    assert result["connected"] is False and "Zernio" in result["problem"]


def test_network_error_is_plain_hebrew(settings, monkeypatch):
    monkeypatch.setenv("ZERNIO_API_KEY", ZKEY)

    def boom(*a, **k):
        raise requests.ConnectionError(f"failed https://zernio.com?key={ZKEY}")

    monkeypatch.setattr(requests, "request", boom)
    result = connect.verify("youtube", settings)
    assert result["problem"] == connect.NO_NET_HE
    assert_no_secrets(result)


def test_verify_cache_and_invalidate(settings, http, monkeypatch, db):
    monkeypatch.setenv("ZERNIO_API_KEY", ZKEY)
    fake = http({("GET", "https://zernio.com/api/v1/accounts"): Resp(200, ACCOUNTS)})
    connect.verify("youtube", settings, db=db)
    connect.verify("youtube", settings, db=db)
    assert len(fake.calls) == 1
    assert_no_secrets(db.kv_get(connect.CACHE_KEY))
    monkeypatch.setenv("ZERNIO_API_KEY", ZKEY + "-new")  # changed value → new fingerprint
    connect.verify("youtube", settings, db=db)
    assert len(fake.calls) == 2
    connect.invalidate(settings, "youtube", db=db)
    connect.verify("youtube_shorts", settings, db=db)
    assert len(fake.calls) == 3


def test_meta_graph_v24_token_in_header_only(settings, http, monkeypatch):
    monkeypatch.setenv("META_ACCESS_TOKEN", META)
    settings.raw["meta"] = {"ig_user_id": "17841400000000001", "page_id": "102030405060708"}
    fake = http({
        ("GET", "https://graph.facebook.com/v24.0/17841400000000001"):
            Resp(200, {"username": "imaginationcs2", "id": "17841400000000001"}),
        ("GET", "https://graph.facebook.com/v24.0/102030405060708"):
            Resp(400, {"error": {"code": 190, "message": "Invalid OAuth access token"}}),
    })
    ig = connect.verify("instagram_reels", settings)
    assert ig == {"connected": True, "account_name": "imaginationcs2", "problem": None}
    fb = connect.verify("facebook", settings)
    assert fb["connected"] is False and fb["problem"] == connect.BAD_KEY_HE
    for _m, url, kwargs in fake.calls:
        assert META not in url and META not in json.dumps(kwargs.get("params"))
        assert kwargs["headers"]["Authorization"] == f"OAuth {META}"
        assert kwargs["timeout"] == 15


def test_telegram_getme(settings, http, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TG)
    http({("GET", "https://api.telegram.org/bot"): Resp(200, {
        "ok": True, "result": {"username": "imagination_control_bot"}})})
    result = connect.verify("telegram", settings)
    assert result == {"connected": True, "account_name": "@imagination_control_bot",
                      "problem": None}
    connect.invalidate(settings)
    http({("GET", "https://api.telegram.org/bot"): Resp(401, {"ok": False})})
    assert connect.verify("telegram", settings)["problem"] == connect.BAD_KEY_HE


def test_discord_webhook(settings, http, monkeypatch, no_network):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://evil.example/api/webhooks/1/x")
    assert "דיסקורד" in connect.verify("discord", settings)["problem"]  # no request made
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", HOOK)
    http({("GET", "https://discord.com/api/webhooks/"): Resp(200, {"name": "CS2 Clips",
                                                                    "channel_id": "9"})})
    result = connect.verify("discord", settings)
    assert result == {"connected": True, "account_name": "CS2 Clips", "problem": None}


def test_x_users_me_oauth1(settings, http, monkeypatch):
    for name in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET"):
        monkeypatch.setenv(name, f"x-SECRET-{name}")
    fake = http({("GET", "https://api.x.com/2/users/me"): Resp(200, {"data": {
        "id": "1", "username": "imaginationcs2"}})})
    assert connect.verify("x", settings)["account_name"] == "@imaginationcs2"
    from requests_oauthlib import OAuth1
    assert isinstance(fake.calls[0][2]["auth"], OAuth1)


def test_reddit_user_me(settings, monkeypatch):
    for name in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USERNAME",
                 "REDDIT_PASSWORD"):
        monkeypatch.setenv(name, f"r-SECRET-{name}")
    seen = {}

    class FakeReddit:
        def __init__(self, **kwargs):
            seen.update(kwargs)
            self.user = types.SimpleNamespace(me=lambda: types.SimpleNamespace(name="dorga"))

    monkeypatch.setitem(sys.modules, "praw", types.SimpleNamespace(Reddit=FakeReddit))
    result = connect.verify("reddit", settings)
    assert result == {"connected": True, "account_name": "u/dorga", "problem": None}
    assert seen["timeout"] == 15


def test_youtube_direct_channels_mine(settings, http):
    settings.raw["publishing"]["platforms"]["youtube"]["via"] = "direct"
    secrets = settings.data_dir / "secrets"
    secrets.mkdir(parents=True, exist_ok=True)
    assert connect.verify("youtube", settings)["problem"] is None  # nothing set: not connected
    (secrets / "youtube_token.json").write_text(json.dumps({
        "refresh_token": "1//SECRET-refresh", "client_id": "cid.apps.googleusercontent.com",
        "client_secret": "GOCSPX-SECRET", "scopes": ["https://www.googleapis.com/auth/youtube"]}))
    fake = http({
        ("POST", "https://oauth2.googleapis.com/token"): Resp(200, {"access_token": "ya29.A"}),
        ("GET", "https://www.googleapis.com/youtube/v3/channels"): Resp(200, {"items": [
            {"snippet": {"title": "Imagination", "thumbnails": {"default": {
                "url": "https://yt3.example/a.jpg"}}}}]}),
    })
    result = connect.verify("youtube", settings)
    assert result["connected"] and result["account_name"] == "Imagination"
    assert result["thumbnail"] == "https://yt3.example/a.jpg"
    assert fake.calls[1][2]["params"]["mine"] == "true"
    tile = connect.platform_state("youtube", settings)
    assert tile["via"] == "direct" and tile["source"] == "agency"
    assert_no_secrets(tile)


# ----------------------------------------------------------------- state
def test_state_tiles_shape_no_values(settings, http, monkeypatch, fake_keyring):
    fake_keyring.store[("cs2studio", "zernio")] = ZKEY
    fake_keyring.store[("cs2studio", "meta_page_token")] = META
    settings.raw["meta"] = {"ig_user_id": "17841400000000001", "page_id": ""}
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", HOOK)
    fake = http({
        ("GET", "https://zernio.com/api/v1/accounts"): Resp(200, ACCOUNTS),
        ("GET", "https://graph.facebook.com/v24.0/"): Resp(200, {"username": "imaginationcs2"}),
        ("GET", "https://discord.com/api/webhooks/"): Resp(200, {"name": "hook"}),
    })
    tiles = connect.state(settings)
    assert [t["key"] for t in tiles] == registry.ALL_KEYS
    by = {t["key"]: t for t in tiles}
    expected = {"key", "name_he", "connected", "account_name", "live", "live_switch", "via",
                "source", "shares", "required", "missing", "problem"}
    assert all(expected <= set(t) for t in tiles)
    assert by["youtube"]["connected"] and by["youtube"]["source"] == "studio"
    assert by["youtube_shorts"]["shares"] == "youtube" and by["youtube_shorts"]["connected"]
    assert by["tiktok"]["connected"] is False and by["tiktok"]["problem"]
    assert by["instagram_reels"]["connected"] and by["instagram_reels"]["source"] == "studio"
    assert by["facebook"]["missing"] == ["FB_PAGE_ID"]
    assert by["discord"]["source"] == "agency" and by["discord"]["via"] == "direct"
    assert by["x"] == {**by["x"], "connected": False, "source": None, "problem": None}
    assert not any(t["live"] for t in tiles)
    zernio_calls = [c for c in fake.calls if "zernio" in c[1]]
    assert len(zernio_calls) == 1  # one accounts call shared by YouTube/Shorts/TikTok
    assert_no_secrets(tiles)
    settings.raw["publishing"]["platforms"]["youtube"]["live"] = True
    tile = connect.platform_state("youtube_shorts", settings, check=False)
    assert tile["live"] and tile["live_switch"]


# ----------------------------------------------------------------- keys / disconnect
def test_save_keys_keyring_and_config(settings, http, fake_keyring, db):
    with pytest.raises(connect.ConnectError):
        connect.save_keys("instagram_reels", {"PATH": "x"}, settings, db)
    http({("GET", "https://graph.facebook.com/v24.0/178"): Resp(200, {"username": "imag"})})
    tile = connect.save_keys("instagram_reels", {"IG_USER_ID": "17841400000000001",
                                                 "META_ACCESS_TOKEN": META}, settings, db)
    assert tile["connected"] and tile["account_name"] == "imag"
    assert tile["saved"] == ["IG_USER_ID", "META_ACCESS_TOKEN"]
    assert fake_keyring.store[("cs2-agency", "META_ACCESS_TOKEN")] == META
    assert settings.get("meta.ig_user_id") == "17841400000000001"
    assert "IG_USER_ID" not in os.environ
    assert_no_secrets(tile)


def test_save_keys_rejects_newlines(settings, fake_keyring):
    with pytest.raises(connect.ConnectError) as exc:
        connect.save_keys("discord", {"DISCORD_WEBHOOK_URL": "https://a\nb"}, settings)
    assert "https://a" not in str(exc.value)
    assert fake_keyring.store == {}


def test_disconnect_only_ours(settings, fake_keyring, monkeypatch, db):
    from agency import settings_store as ss

    fake_keyring.store[("cs2studio", "meta_page_token")] = "studio-meta"
    fake_keyring.store[("cs2-agency", "DISCORD_WEBHOOK_URL")] = HOOK
    ss.update_config(settings, {"publishing": {"platforms": {"discord": {"live": True}}}})
    out = connect.disconnect("discord", settings, db)
    assert out["removed"] == ["DISCORD_WEBHOOK_URL"]
    assert ("cs2-agency", "DISCORD_WEBHOOK_URL") not in fake_keyring.store
    assert settings.platform_live("discord") is False and not out["state"]["connected"]

    # Instagram rides on the studio's Meta token: we never delete it; the shared token
    # stays because Facebook (connected) still uses it.
    settings.raw["meta"] = {"ig_user_id": "17841400000000001", "page_id": "102030405060708"}
    out = connect.disconnect("instagram_reels", settings, db)
    assert "IG_USER_ID" in out["removed"] and out["kept_shared"] == ["META_ACCESS_TOKEN"]
    assert fake_keyring.store[("cs2studio", "meta_page_token")] == "studio-meta"
    assert settings.get("meta.ig_user_id") == ""
    out = connect.disconnect("facebook", settings, db)
    assert out["studio"] == ["META_ACCESS_TOKEN"]
    assert fake_keyring.store[("cs2studio", "meta_page_token")] == "studio-meta"


# ----------------------------------------------------------------- youtube oauth helpers
def test_save_client_secret(settings):
    with pytest.raises(connect.ConnectError):
        connect.save_client_secret(settings, b"not json")
    with pytest.raises(connect.ConnectError):
        connect.save_client_secret(settings, {"installed": {"client_id": "x"}})
    out = connect.save_client_secret(settings, json.dumps(
        {"installed": {"client_id": "cid", "client_secret": "GOCSPX-x"}}).encode())
    assert out == {"ok": True, "kind": "installed"}
    path = settings.data_dir / "secrets" / "youtube_client_secret.json"
    assert json.loads(path.read_text())["installed"]["client_id"] == "cid"
    if os.name != "nt":
        assert (path.stat().st_mode & 0o777) == 0o600


def test_youtube_oauth_flow(settings, http, monkeypatch):
    connect.save_client_secret(settings, {"installed": {"client_id": "cid",
                                                        "client_secret": "GOCSPX-x"}})
    seen = {}

    class Creds:
        def to_json(self):
            return json.dumps({"token": "ya29.T", "expiry": "2999-01-01T00:00:00Z",
                               "refresh_token": "1//r", "client_id": "cid",
                               "client_secret": "GOCSPX-x"})

    class Flow:
        @classmethod
        def from_client_secrets_file(cls, path, scopes):
            seen["scopes"] = scopes
            return cls()

        def run_local_server(self, **kwargs):
            seen.update(kwargs)
            return Creds()

    flow_mod = types.ModuleType("google_auth_oauthlib.flow")
    flow_mod.InstalledAppFlow = Flow
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib.flow", flow_mod)
    http({("GET", "https://www.googleapis.com/youtube/v3/channels"): Resp(200, {"items": [
        {"snippet": {"title": "Imagination"}}]})})
    monkeypatch.setitem(connect._oauth_state, "state", "idle")
    state = connect.start_youtube_oauth(settings, background=False)
    assert state["state"] == "done" and state["channel"] == "Imagination"
    assert seen["port"] == 0 and seen["open_browser"] is True
    assert set(seen["scopes"]) == {
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube.readonly",
        "https://www.googleapis.com/auth/youtube.force-ssl"}
    assert (settings.data_dir / "secrets" / "youtube_token.json").is_file()
    assert connect.youtube_oauth_state()["state"] == "done"
