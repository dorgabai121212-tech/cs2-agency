"""Platform adapters: dry-run never touches the network, honest failures, mocked happy paths."""
from __future__ import annotations

import json

import pytest

from agency.models import PostPlan
from agency.platforms import registry
from agency.platforms._common import build_caption, truncate

SECRETS = {
    "youtube": ["YOUTUBE_CLIENT_SECRETS_FILE"],
    "youtube_shorts": ["YOUTUBE_CLIENT_SECRETS_FILE"],
    "tiktok": ["TIKTOK_ACCESS_TOKEN"],
    "instagram_reels": ["IG_USER_ID", "META_ACCESS_TOKEN"],
    "facebook": ["FB_PAGE_ID", "META_ACCESS_TOKEN"],
    "x": ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET"],
    "reddit": ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USERNAME", "REDDIT_PASSWORD"],
    "discord": ["DISCORD_WEBHOOK_URL"],
}


def make_plan(key: str, path: str) -> PostPlan:
    return PostPlan(platform=key, variant_path=path, title="אייס מטורף ב-Mirage",
                    description="קליפ קצר " * 400, tags=["cs2", "counter strike 2", "ace"],
                    hashtags=["cs2", "#ace", "mirage"], scheduled_at=None, extra={})


class FakeResponse:
    def __init__(self, status: int = 200, payload: dict | None = None):
        self.status_code = status
        self._payload = payload or {}
        self.content = json.dumps(self._payload).encode()

    def json(self) -> dict:
        return self._payload


def test_registry_resolves_all_keys(settings):
    assert set(registry.ADAPTERS) == set(registry.ALL_KEYS)
    for key in registry.ALL_KEYS:
        adapter = registry.get_adapter(key, settings)
        assert adapter.key == key
        assert adapter.required_secrets == SECRETS[key]
        assert adapter.display_he
    with pytest.raises(KeyError):
        registry.get_adapter("myspace", settings)


@pytest.mark.parametrize("key", registry.ALL_KEYS)
def test_dry_run_makes_no_network_calls(key, settings, tiny_video, no_network, monkeypatch):
    for name in SECRETS[key]:  # even fully configured, dry run must not touch the network
        monkeypatch.setenv(name, "https://example.invalid/secret-value-1234")
    adapter = registry.get_adapter(key, settings)
    assert settings.dry_run is True
    result = adapter.upload(make_plan(key, str(tiny_video)))
    assert result.ok and result.dry_run
    assert result.external_id.startswith(f"dryrun-{key}-")
    assert result.url is None
    assert adapter.fetch_stats(result.external_id) == {}
    assert adapter.fetch_comments(result.external_id) == []
    assert adapter.reply_comment("c1", "תודה!") is False


@pytest.mark.parametrize("key", registry.ALL_KEYS)
def test_unconfigured_real_mode_fails_honestly(key, settings, tiny_video, no_network):
    settings.raw["dry_run"] = False
    adapter = registry.get_adapter(key, settings)
    assert not adapter.configured()
    result = adapter.upload(make_plan(key, str(tiny_video)))
    assert result.ok is False and result.dry_run is False
    assert result.error.startswith("חסרים פרטי התחברות: ")
    for name in SECRETS[key]:
        assert name in result.error


def test_discord_happy_path_and_no_token_leak(settings, tiny_video, monkeypatch):
    settings.raw["dry_run"] = False
    webhook = "https://discord.com/api/webhooks/123/SECRET-TOKEN-abc"
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", webhook)
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        assert kwargs.get("timeout")
        return FakeResponse(200, {"id": "999", "channel_id": "1"})

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    plan = make_plan("discord", str(tiny_video))
    plan.extra["link"] = "https://youtu.be/abc"
    result = registry.get_adapter("discord", settings).upload(plan)
    assert result.ok and not result.dry_run and result.external_id == "999"
    url, kwargs = calls[0]
    assert url == webhook and kwargs["params"] == {"wait": "true"}
    payload = json.loads(kwargs["data"]["payload_json"])  # small file -> attached
    assert "files[0]" in kwargs["files"]
    assert len(payload["content"]) <= 2000
    assert "https://youtu.be/abc" in payload["content"]
    assert payload["allowed_mentions"] == {"parse": []}

    def boom(url, **kwargs):
        raise requests.ConnectionError(f"cannot reach {url}")

    monkeypatch.setattr(requests, "post", boom)
    failed = registry.get_adapter("discord", settings).upload(plan)
    assert not failed.ok and "SECRET-TOKEN-abc" not in failed.error


def test_tiktok_happy_path(settings, tiny_video, monkeypatch):
    settings.raw["dry_run"] = False
    monkeypatch.setenv("TIKTOK_ACCESS_TOKEN", "act.tiktok-secret")
    size = tiny_video.stat().st_size
    posts, puts = [], []
    statuses = iter([{"status": "PROCESSING_UPLOAD"},
                     {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [7300000001]}])

    def fake_post(url, json=None, headers=None, timeout=None, **kwargs):
        assert timeout and headers["Authorization"] == "Bearer act.tiktok-secret"
        posts.append((url, json))
        ok = {"code": "ok", "message": ""}
        if url.endswith("/creator_info/query/"):
            return FakeResponse(200, {"data": {"privacy_level_options":
                                               ["SELF_ONLY", "PUBLIC_TO_EVERYONE"]}, "error": ok})
        if url.endswith("/video/init/"):
            return FakeResponse(200, {"data": {"publish_id": "v_pub_1",
                                               "upload_url": "https://upload.example/u"},
                                      "error": ok})
        if url.endswith("/status/fetch/"):
            return FakeResponse(200, {"data": next(statuses), "error": ok})
        raise AssertionError(url)

    def fake_put(url, data=None, headers=None, timeout=None, **kwargs):
        assert timeout
        puts.append((url, headers, len(data)))
        return FakeResponse(201, {})

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "put", fake_put)
    from agency.platforms.tiktok import TikTokAdapter
    monkeypatch.setattr(TikTokAdapter, "poll_interval_s", 0)

    result = registry.get_adapter("tiktok", settings).upload(make_plan("tiktok", str(tiny_video)))
    assert result.ok, result.error
    assert result.external_id == "7300000001"
    assert result.url == "https://www.tiktok.com/video/7300000001"
    init_body = next(body for url, body in posts if url.endswith("/video/init/"))
    assert init_body["source_info"] == {"source": "FILE_UPLOAD", "video_size": size,
                                        "chunk_size": size, "total_chunk_count": 1}
    assert init_body["post_info"]["privacy_level"] == "SELF_ONLY"
    assert len(init_body["post_info"]["title"]) <= 2200
    assert puts == [("https://upload.example/u",
                     {"Content-Type": "video/mp4", "Content-Length": str(size),
                      "Content-Range": f"bytes 0-{size - 1}/{size}"}, size)]


def test_tiktok_chunk_plan():
    from agency.platforms.tiktok import chunk_plan

    mb = 1024 * 1024
    assert chunk_plan(3 * mb) == (3 * mb, 1)
    assert chunk_plan(64 * mb) == (64 * mb, 1)
    size, count = chunk_plan(105 * mb)
    assert size == 10 * mb and count == 10  # last chunk carries the 5MB remainder


def test_caption_limits():
    plan = make_plan("x", "v.mp4")
    plan.hashtags = [f"tag{i}" for i in range(50)]
    caption = build_caption(plan, 2200, max_hashtags=30)
    assert len(caption) <= 2200 and caption.count("#") <= 30
    assert len(truncate("א" * 500, 100)) <= 100

    from agency.platforms.x_twitter import build_tweet, weighted_length
    plan.extra["link"] = "https://youtu.be/abcdefghijk"
    assert weighted_length(build_tweet(plan)) <= 280

    from agency.platforms.youtube import TAGS_TOTAL_LIMIT, clean_tags
    assert sum(len(t) for t in clean_tags([f"tag number {i}" for i in range(200)])) \
        <= TAGS_TOTAL_LIMIT


def test_youtube_body_limits_and_schedule(settings):
    from agency.platforms.youtube import YouTubeShortsAdapter

    plan = make_plan("youtube_shorts", "v.mp4")
    plan.title = "x" * 300
    plan.scheduled_at = "2099-01-01T10:00:00+00:00"
    body = YouTubeShortsAdapter(settings).build_body(plan)
    assert len(body["snippet"]["title"]) <= 100
    assert len(body["snippet"]["description"]) <= 5000
    assert "#Shorts" in body["snippet"]["description"]
    assert body["status"] == {"privacyStatus": "private", "selfDeclaredMadeForKids": False,
                              "embeddable": True, "publishAt": "2099-01-01T10:00:00Z"}


def test_reddit_only_configured_subreddits(settings, tiny_video, no_network, monkeypatch):
    settings.raw["dry_run"] = False
    for name in SECRETS["reddit"]:
        monkeypatch.setenv(name, "value-1234")
    settings.raw["publishing"]["platforms"]["reddit"] = {"enabled": True,
                                                         "subreddits": ["GlobalOffensive"]}
    adapter = registry.get_adapter("reddit", settings)
    plan = make_plan("reddit", str(tiny_video))
    assert adapter.targets(plan) == (["GlobalOffensive"], [])
    plan.extra["subreddits"] = ["GlobalOffensive", "pics"]
    result = adapter.upload(plan)
    assert not result.ok and "pics" in result.error

    settings.raw["publishing"]["platforms"]["reddit"]["subreddits"] = []
    plan.extra.clear()
    assert not adapter.upload(plan).ok
