"""Zernio adapter (YouTube / Shorts / TikTok): honest results, error mapping, no network in
training mode, the API key never leaks. All HTTP is mocked."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from agency.models import PostPlan
from agency.platforms import registry
from agency.platforms.zernio import (ACCOUNTS_KV, API_BASE, UPLOADS_KV, ZernioAdapter,
                                     file_sha256)

KEY = "zk_live_SECRET-zernio-key-123456"


class FakeResponse:
    def __init__(self, status: int = 200, payload: dict | None = None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.content = json.dumps(self._payload).encode()
        self.text = self.content.decode()

    def json(self) -> dict:
        return self._payload


class FakeZernio:
    """Routes ``requests.request`` / ``requests.put`` to canned Zernio answers."""

    def __init__(self, monkeypatch, *, platform="youtube", post_status="published",
                 entry_status="published", polls=None, create_status=201, create_body=None,
                 accounts=None, creator_info=None):
        import requests

        self.calls: list[dict] = []
        self.puts: list[dict] = []
        self.platform = platform
        self.post_status, self.entry_status = post_status, entry_status
        self.polls = list(polls or [])
        self.create_status, self.create_body = create_status, create_body
        self.accounts = accounts if accounts is not None else [
            {"_id": "acc_yt", "platform": "youtube", "username": "imaginationcs2",
             "isActive": True, "needsReconnection": False},
            {"_id": "acc_tt", "platform": "tiktok", "username": "imaginationcs2",
             "isActive": True, "needsReconnection": False}]
        self.creator_info = creator_info if creator_info is not None else {
            "creator": {"nickname": "Imagination", "canPostMore": True},
            "privacyLevels": [{"value": "PUBLIC_TO_EVERYONE"}, {"value": "SELF_ONLY"}]}
        monkeypatch.setattr(requests, "request", self.request)
        monkeypatch.setattr(requests, "put", self.put)

    def entry(self, status):
        native = "dQw4w9WgXcQ" if self.platform == "youtube" else "7400000000000000001"
        e = {"platform": self.platform, "accountId": "acc", "status": status}
        if status == "published":
            e["platformPostId"] = native
            e["platformPostUrl"] = (f"https://www.youtube.com/watch?v={native}"
                                    if self.platform == "youtube"
                                    else f"https://www.tiktok.com/@imaginationcs2/video/{native}")
        if status == "failed":
            e.update(errorCategory="quota_exhausted", errorMessage="daily limit reached")
        return e

    def request(self, method, url, **kwargs):
        assert kwargs.get("timeout"), f"no timeout on {method} {url}"
        assert kwargs["headers"]["Authorization"] == f"Bearer {KEY}"
        self.calls.append({"method": method, "url": url, **kwargs})
        path = url[len(API_BASE):]
        if method == "GET" and path == "/v1/accounts":
            return FakeResponse(200, {"accounts": self.accounts, "hasAnalyticsAccess": False})
        if method == "GET" and path.endswith("/tiktok/creator-info"):
            return FakeResponse(200, self.creator_info)
        if method == "POST" and path == "/v1/media/presign":
            return FakeResponse(200, {"uploadUrl": "https://storage.example/put?sig=abc",
                                      "publicUrl": "https://media.zernio.example/v.mp4",
                                      "key": "k", "expiresIn": 3600})
        if method == "POST" and path == "/v1/posts":
            if self.create_body is not None:
                return FakeResponse(self.create_status, self.create_body)
            return FakeResponse(self.create_status, {"message": "ok", "post": {
                "_id": "post_1", "status": self.post_status,
                "platforms": [self.entry(self.entry_status)]}})
        if method == "GET" and path.startswith("/v1/posts/"):
            status, entry_status = self.polls.pop(0) if self.polls else ("published",
                                                                        "published")
            return FakeResponse(200, {"post": {"_id": path.rsplit("/", 1)[1], "status": status,
                                               "platforms": [self.entry(entry_status)]}})
        if method == "DELETE" and path.startswith("/v1/posts/"):
            return FakeResponse(200, {"success": True})
        raise AssertionError(f"unexpected {method} {url}")

    def put(self, url, data=None, headers=None, timeout=None, **kwargs):
        assert timeout, "no timeout on PUT"
        assert "Authorization" not in (headers or {})  # presigned URL: no key sent there
        self.puts.append({"url": url, "headers": headers})
        return FakeResponse(200, {})

    def bodies(self, path="/v1/posts", method="POST"):
        return [c.get("json") for c in self.calls
                if c["method"] == method and c["url"] == API_BASE + path]


@pytest.fixture
def live(settings, monkeypatch):
    """Real path (global dry_run false), Zernio key present, instant polling."""
    settings.raw["dry_run"] = False
    monkeypatch.setenv("ZERNIO_API_KEY", KEY)
    monkeypatch.setenv("AGENCY_KEYRING", "off")
    monkeypatch.setattr(ZernioAdapter, "poll_interval_s", 0)
    monkeypatch.setattr(ZernioAdapter, "backoff_s", (0, 0))
    return settings


def plan_for(key, path, **extra) -> PostPlan:
    return PostPlan(platform=key, variant_path=str(path), title="דיגל 3K בסגנון סרט",
                    description="רגע אגדי מהדמו.", tags=["cs2", "deagle"],
                    hashtags=["CS2", "CounterStrike2", "CS2Edit"], scheduled_at=None,
                    extra=dict(extra))


# ------------------------------------------------------------------------------ routing
def test_registry_routes_youtube_shorts_tiktok_through_zernio(settings):
    for key in ("youtube", "youtube_shorts", "tiktok"):
        adapter = registry.get_adapter(key, settings)
        assert isinstance(adapter, ZernioAdapter)
        assert adapter.key == key and adapter.required_secrets == ["ZERNIO_API_KEY"]
    settings.raw["publishing"]["platforms"]["youtube"]["via"] = "direct"
    assert not isinstance(registry.get_adapter("youtube", settings), ZernioAdapter)
    with pytest.raises(ValueError):
        ZernioAdapter(settings, "instagram_reels")


# ------------------------------------------------------------------------------ happy path
def test_youtube_happy_path_publish_now(live, tiny_video, monkeypatch, caplog):
    fake = FakeZernio(monkeypatch)
    live.raw["brand"] = {"ai_label": True}
    caplog.set_level(logging.DEBUG)
    adapter = ZernioAdapter(live, "youtube")
    result = adapter.upload(plan_for("youtube", tiny_video))
    assert result.ok and not result.dry_run, result.error
    assert result.external_id == "dQw4w9WgXcQ"
    assert result.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    body = fake.bodies()[0]
    assert body["publishNow"] is True and "scheduledFor" not in body
    entry = body["platforms"][0]
    assert entry["platform"] == "youtube" and entry["accountId"] == "acc_yt"
    psd = entry["platformSpecificData"]
    assert psd["title"].startswith("דיגל 3K") and len(psd["title"]) <= 100
    assert psd["visibility"] == "public" and psd["madeForKids"] is False
    assert psd["containsSyntheticMedia"] is True  # brand.ai_label: true
    assert "#CS2" in body["content"] and body["mediaItems"] == [
        {"type": "video", "url": "https://media.zernio.example/v.mp4"}]
    post_call = next(c for c in fake.calls if c["url"].endswith("/v1/posts"))
    sha = file_sha256(tiny_video)
    assert post_call["headers"]["Idempotency-Key"] == f"{sha[:24]}:youtube"
    assert fake.puts and fake.puts[0]["headers"]["Content-Type"] == "video/mp4"
    assert KEY not in caplog.text

    # accounts + the uploaded file are cached: a second (Shorts) post re-uses both
    db = adapter.db
    assert db.kv_get(ACCOUNTS_KV)["map"]["youtube"]["id"] == "acc_yt"
    assert sha in db.kv_get(UPLOADS_KV)
    fake.calls.clear()
    fake.puts.clear()
    shorts = ZernioAdapter(live, "youtube_shorts").upload(plan_for("youtube_shorts", tiny_video))
    assert shorts.ok and shorts.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"  # Zernio's
    paths = [c["url"][len(API_BASE):] for c in fake.calls]
    assert "/v1/accounts" not in paths and "/v1/media/presign" not in paths and not fake.puts
    assert next(c for c in fake.calls if c["url"].endswith("/v1/posts"))["headers"][
        "Idempotency-Key"] == f"{sha[:24]}:youtube_shorts"


def test_per_platform_live_switch_uses_real_path(settings, tiny_video, monkeypatch):
    """Global dry_run stays true; the owner switched only YouTube live."""
    monkeypatch.setenv("ZERNIO_API_KEY", KEY)
    monkeypatch.setattr(ZernioAdapter, "poll_interval_s", 0)
    settings.raw["publishing"]["platforms"]["youtube"]["live"] = True
    fake = FakeZernio(monkeypatch)
    assert settings.dry_run is True
    result = registry.get_adapter("youtube", settings).upload(plan_for("youtube", tiny_video))
    assert result.ok and not result.dry_run and fake.bodies()
    tiktok = registry.get_adapter("tiktok", settings).upload(plan_for("tiktok", tiny_video))
    assert tiktok.ok and tiktok.dry_run  # TikTok still in training mode


def test_poll_until_published(live, tiny_video, monkeypatch):
    fake = FakeZernio(monkeypatch, post_status="publishing", entry_status="processing",
                      polls=[("publishing", "uploading"), ("published", "published")])
    result = ZernioAdapter(live, "youtube").upload(plan_for("youtube", tiny_video))
    assert result.ok and result.external_id == "dQw4w9WgXcQ"
    gets = [c for c in fake.calls if c["method"] == "GET" and "/v1/posts/" in c["url"]]
    assert len(gets) == 2


def test_scheduled_for_future_plan(live, tiny_video, monkeypatch):
    fake = FakeZernio(monkeypatch, post_status="scheduled", entry_status="pending")
    when = (datetime.now(timezone.utc) + timedelta(hours=3)).replace(microsecond=0)
    plan = plan_for("youtube", tiny_video)
    plan.scheduled_at = when.isoformat()
    result = ZernioAdapter(live, "youtube").upload(plan)
    assert result.ok and result.url is None and result.external_id == "post_1"
    body = fake.bodies()[0]
    assert "publishNow" not in body
    assert datetime.fromisoformat(body["scheduledFor"]) == when
    assert not [c for c in fake.calls if c["method"] == "GET" and "/v1/posts/" in c["url"]]
    # "publish now" from the owner ignores the future time: the post Zernio holds for later
    # is cancelled first (it must not go out a second time), then a new one goes out now
    plan.extra["publish_now"] = True
    fake2 = FakeZernio(monkeypatch, polls=[("scheduled", "pending")])
    assert ZernioAdapter(live, "youtube").upload(plan).ok
    assert [c["url"] for c in fake2.calls if c["method"] == "DELETE"] == [
        f"{API_BASE}/v1/posts/post_1"]
    assert fake2.bodies()[0]["publishNow"] is True


# ------------------------------------------------------------------------------ honesty
def test_still_pending_after_wait_is_not_success(live, tiny_video, monkeypatch):
    FakeZernio(monkeypatch, post_status="publishing", entry_status="processing",
               polls=[("publishing", "processing")] * 50)
    live.raw["publishing"]["platforms"]["youtube"]["poll_max_s"] = 0
    result = ZernioAdapter(live, "youtube").upload(plan_for("youtube", tiny_video))
    assert result.ok is False and result.dry_run is False
    assert "still processing" in result.error and "עדיין מעבד" in result.error
    assert result.external_id == "post_1"


def test_platform_failure_maps_category_and_retry_gets_fresh_key(live, tiny_video,
                                                                 monkeypatch):
    fake = FakeZernio(monkeypatch, post_status="failed", entry_status="failed")
    adapter = ZernioAdapter(live, "youtube")
    result = adapter.upload(plan_for("youtube", tiny_video))
    assert not result.ok and "נגמרה המכסה היומית" in result.error
    assert "daily limit reached" in result.error and "quota_exhausted" in result.error
    first = next(c for c in fake.calls if c["url"].endswith("/v1/posts"))
    fake2 = FakeZernio(monkeypatch)
    assert ZernioAdapter(live, "youtube").upload(plan_for("youtube", tiny_video)).ok
    second = next(c for c in fake2.calls if c["url"].endswith("/v1/posts"))
    assert second["headers"]["Idempotency-Key"] == \
        first["headers"]["Idempotency-Key"] + ":r1"  # the old key would replay the failure


@pytest.mark.parametrize("status,needle", [
    (401, "מפתח Zernio לא תקין"), (403, "מפתח Zernio לא תקין"), (429, "יותר מדי בקשות"),
    (500, "Zernio לא זמין כרגע"), (400, "Zernio דחה את הבקשה")])
def test_http_error_mapping_never_leaks_key(live, tiny_video, monkeypatch, status, needle):
    import requests

    def answer(method, url, **kwargs):
        return FakeResponse(status, {"error": f"bad request with {KEY}"})

    monkeypatch.setattr(requests, "request", answer)
    result = ZernioAdapter(live, "youtube").upload(plan_for("youtube", tiny_video))
    assert not result.ok and needle in result.error and f"HTTP {status}" in result.error
    assert KEY not in result.error


def test_network_error_message_has_no_url_or_key(live, tiny_video, monkeypatch):
    import requests

    def boom(method, url, **kwargs):
        raise requests.ConnectionError(f"cannot reach {url} with {kwargs['headers']}")

    monkeypatch.setattr(requests, "request", boom)
    result = ZernioAdapter(live, "youtube").upload(plan_for("youtube", tiny_video))
    assert not result.ok and "אין חיבור ל-Zernio" in result.error
    assert KEY not in result.error and "zernio.com" not in result.error


def test_conflict_with_existing_post_is_adopted(live, tiny_video, monkeypatch):
    fake = FakeZernio(monkeypatch, create_status=409, create_body={
        "error": "duplicate", "details": {"existingPostId": "post_old"}})
    result = ZernioAdapter(live, "youtube").upload(plan_for("youtube", tiny_video))
    assert result.ok and result.external_id == "dQw4w9WgXcQ"
    assert any(c["url"].endswith("/v1/posts/post_old") for c in fake.calls)


def test_missing_account_and_reconnection(live, tiny_video, monkeypatch):
    FakeZernio(monkeypatch, accounts=[])
    result = ZernioAdapter(live, "youtube").upload(plan_for("youtube", tiny_video))
    assert not result.ok and "אין חשבון יוטיוב מחובר ב-Zernio" in result.error
    FakeZernio(monkeypatch, accounts=[{"_id": "acc_yt", "platform": "youtube",
                                       "needsReconnection": True}])
    adapter = ZernioAdapter(live, "youtube")
    adapter.db.kv_set(ACCOUNTS_KV, None)
    result = adapter.upload(plan_for("youtube", tiny_video))
    assert not result.ok and "צריך חיבור מחדש" in result.error


# ------------------------------------------------------------------------------ TikTok
def test_tiktok_fields_and_creator_info(live, tiny_video, monkeypatch):
    fake = FakeZernio(monkeypatch, platform="tiktok")
    plan = plan_for("tiktok", tiny_video, ai_label=True, owner_approved=True)
    result = ZernioAdapter(live, "tiktok").upload(plan)
    assert result.ok and result.external_id == "7400000000000000001"
    assert result.url.startswith("https://www.tiktok.com/@imaginationcs2/video/")
    psd = fake.bodies()[0]["platforms"][0]["platformSpecificData"]
    assert psd == {"privacyLevel": "PUBLIC_TO_EVERYONE", "allowComment": True,
                   "allowDuet": False, "allowStitch": False, "commercialContentType": "none",
                   "contentPreviewConfirmed": True, "expressConsentGiven": True,
                   "videoMadeWithAi": True, "videoCoverTimestampMs": 1000}
    assert len(fake.bodies()[0]["content"]) <= 2200
    assert any(c["url"].endswith("/v1/accounts/acc_tt/tiktok/creator-info") for c in fake.calls)


def test_tiktok_refusals(live, tiny_video, monkeypatch):
    FakeZernio(monkeypatch, platform="tiktok", creator_info={
        "creator": {"canPostMore": False}, "privacyLevels": [{"value": "PUBLIC_TO_EVERYONE"}]})
    approved = plan_for("tiktok", tiny_video, owner_approved=True)
    result = ZernioAdapter(live, "tiktok").upload(approved)
    assert not result.ok and "canPostMore" in result.error

    live.raw["publishing"]["platforms"]["tiktok"]["privacy_level"] = "FOLLOWER_OF_CREATOR"
    FakeZernio(monkeypatch, platform="tiktok")
    result = ZernioAdapter(live, "tiktok").upload(approved)
    assert not result.ok and "FOLLOWER_OF_CREATOR" in result.error

    # consent flags only for content the owner approved himself — whatever the mode is now
    live.raw["publishing"]["platforms"]["tiktok"].pop("privacy_level")
    for mode in ("auto", "approve"):
        live.raw["publishing"]["mode"] = mode
        fake = FakeZernio(monkeypatch, platform="tiktok")
        result = ZernioAdapter(live, "tiktok").upload(plan_for("tiktok", tiny_video))
        assert not result.ok and "TikTok consent" in result.error and not fake.bodies()
    # and an approved post keeps its consent when the mode is switched to auto later
    live.raw["publishing"]["mode"] = "auto"
    fake = FakeZernio(monkeypatch, platform="tiktok")
    assert ZernioAdapter(live, "tiktok").upload(approved).ok and fake.bodies()


# ------------------------------------------------------------------------------ training
def test_training_mode_makes_zero_network_calls(settings, tiny_video, no_network,
                                                monkeypatch):
    monkeypatch.setenv("ZERNIO_API_KEY", KEY)
    for key in ("youtube", "youtube_shorts", "tiktok"):
        adapter = ZernioAdapter(settings, key)
        assert adapter.dry_run
        result = adapter.upload(plan_for(key, tiny_video))
        assert result.ok and result.dry_run and result.external_id.startswith(f"dryrun-{key}-")
        assert adapter.fetch_stats(result.external_id) == {}
        assert adapter.fetch_stats("dQw4w9WgXcQ") == {}
        assert adapter.fetch_comments("dQw4w9WgXcQ") == []
        assert adapter.reply_comment("c1", "תודה") is False
        assert adapter.post_status("post_1") == {}
        assert adapter.unpublish("post_1")["ok"] is False


def test_live_without_key_fails_without_network(settings, tiny_video, no_network,
                                                monkeypatch):
    monkeypatch.setenv("AGENCY_KEYRING", "off")
    settings.raw["dry_run"] = False
    result = ZernioAdapter(settings, "youtube").upload(plan_for("youtube", tiny_video))
    assert not result.ok and "ZERNIO_API_KEY" in result.error


# ------------------------------------------------------------------------------ unpublish
def test_unpublish_never_hard_deletes(live, tiny_video, monkeypatch):
    from agency.platforms.youtube import YouTubeAdapter

    fake = FakeZernio(monkeypatch)
    adapter = ZernioAdapter(live, "youtube")
    assert adapter.upload(plan_for("youtube", tiny_video)).ok
    privatized = []
    monkeypatch.setattr(YouTubeAdapter, "make_private",
                        lambda self, vid: privatized.append(vid) or True)
    fake.calls.clear()
    out = adapter.unpublish("dQw4w9WgXcQ")  # native id -> its Zernio post is found
    assert out == {"ok": True, "action": "made_private", "error": None}
    assert privatized == ["dQw4w9WgXcQ"]
    assert not [c for c in fake.calls if c["method"] == "DELETE" or "unpublish" in c["url"]]

    fake.polls = [("scheduled", "pending")]
    out = adapter.unpublish("post_2")
    assert out["action"] == "cancelled"
    assert [c["url"] for c in fake.calls if c["method"] == "DELETE"] == [
        f"{API_BASE}/v1/posts/post_2"]

    tiktok = ZernioAdapter(live, "tiktok")
    fake_tt = FakeZernio(monkeypatch, platform="tiktok")
    out = tiktok.unpublish("post_3")
    assert out["ok"] is False and "באפליקציה" in out["error"]
    assert not [c for c in fake_tt.calls if c["method"] == "DELETE"]


def test_verify_reports_account_name_only(live, monkeypatch):
    FakeZernio(monkeypatch)
    out = ZernioAdapter(live, "tiktok").verify()
    assert out == {"ok": True, "account_name": "@imaginationcs2", "problem": None}
    assert KEY not in json.dumps(out)
