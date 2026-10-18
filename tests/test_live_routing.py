"""Per-platform live mode (SPEC_V2 §2), Zernio/direct routing (§11.2) and the double-posting
guard against the old studio (§11.10)."""
from __future__ import annotations

import shutil

import pytest

from agency.models import PostPlan, PostResult
from agency.platforms import registry
from agency.platforms.base import PlatformAdapter
from agency.staff.publisher import Publisher


def _cfg(settings, key: str, **values) -> None:
    node = settings.raw.setdefault("publishing", {}).setdefault("platforms", {})
    node.setdefault(key, {}).update(values)


def make_post(ctx, platform: str, path: str, status: str = "scheduled") -> int:
    vid = ctx.db.add_video(path, "clip")
    plan = PostPlan(platform=platform, variant_path=path, title="t", description="d",
                    tags=[], hashtags=[], scheduled_at=None, extra={})
    return ctx.db.add_post(vid, plan, status=status)


# ----------------------------------------------------------------- live semantics
def test_platform_live_rules(settings):
    assert settings.dry_run is True
    assert not any(settings.platform_live(k) for k in registry.ALL_KEYS)
    _cfg(settings, "tiktok", live=True)
    assert settings.platform_live("tiktok") and not settings.platform_live("discord")
    _cfg(settings, "tiktok", live="yes please")  # anything unclear = training
    assert settings.platform_live("tiktok") is False
    settings.raw["dry_run"] = False
    assert all(settings.platform_live(k) for k in registry.ALL_KEYS)


def test_shorts_shares_youtube_switch_unless_set(settings):
    _cfg(settings, "youtube", live=True)
    assert settings.platform_live("youtube_shorts") is True
    _cfg(settings, "youtube_shorts", live=False)
    assert settings.platform_live("youtube_shorts") is False
    assert settings.platform_live("youtube") is True


def test_via_defaults_and_shorts_follow_youtube(settings):
    assert [settings.platform_via(k) for k in ("youtube", "youtube_shorts", "tiktok")] == \
        ["zernio"] * 3
    assert settings.platform_via("instagram_reels") == "direct"
    _cfg(settings, "youtube", via="direct")
    assert settings.platform_via("youtube_shorts") == "direct"
    assert type(registry.get_adapter("youtube_shorts", settings)).__name__ == \
        "YouTubeShortsAdapter"
    _cfg(settings, "youtube_shorts", via="zernio")
    assert registry.route("youtube_shorts", settings) == "zernio"


def test_registry_routes_zernio_by_default(settings):
    for key in ("youtube", "youtube_shorts", "tiktok"):
        adapter = registry.get_adapter(key, settings)
        assert adapter.key == key
        assert "ZERNIO_API_KEY" in adapter.required_secrets
    _cfg(settings, "tiktok", via="direct")
    assert registry.get_adapter("tiktok", settings).required_secrets == ["TIKTOK_ACCESS_TOKEN"]


def test_missing_zernio_module_fails_closed(settings, monkeypatch, tiny_video, no_network):
    real_import = registry.importlib.import_module

    def fake_import(name, *a, **k):
        if name.endswith(".zernio"):
            raise ImportError("gone")
        return real_import(name, *a, **k)

    monkeypatch.setattr(registry.importlib, "import_module", fake_import)
    monkeypatch.setenv("ZERNIO_API_KEY", "zk")
    adapter = registry.get_adapter("youtube", settings)
    assert isinstance(adapter, registry.UnavailableAdapter) and not adapter.configured()
    plan = PostPlan(platform="youtube", variant_path=str(tiny_video), title="t",
                    description="", tags=[], hashtags=[], scheduled_at=None, extra={})
    assert adapter.upload(plan).dry_run is True  # training still works, no network
    settings.raw["dry_run"] = False
    result = adapter.upload(plan)
    assert result.ok is False and not result.dry_run


def test_config_fallback_ids_and_secret_file(settings, monkeypatch):
    adapter = registry.get_adapter("instagram_reels", settings)
    assert adapter.missing_secrets() == ["IG_USER_ID", "META_ACCESS_TOKEN"]
    settings.raw["meta"] = {"ig_user_id": "17841400000000001", "page_id": ""}
    monkeypatch.setenv("META_ACCESS_TOKEN", "EAAtoken")
    assert adapter.configured()
    _cfg(settings, "youtube", via="direct")
    yt = registry.get_adapter("youtube", settings)
    assert yt.missing_secrets() == ["YOUTUBE_CLIENT_SECRETS_FILE"]
    (settings.data_dir / "secrets").mkdir(parents=True, exist_ok=True)
    client = settings.data_dir / "secrets" / "youtube_client_secret.json"
    client.write_text("{}")
    assert yt.credential("YOUTUBE_CLIENT_SECRETS_FILE") == str(client)


# ----------------------------------------------------------------- publisher + network
class RecordingAdapter(PlatformAdapter):
    """Real base-class logic (training / credentials), records live uploads."""

    key = "discord"
    display_he = "דיסקורד"
    required_secrets = ["DISCORD_WEBHOOK_URL"]
    calls: list[str] = []

    def _upload(self, plan):
        RecordingAdapter.calls.append(plan.platform)
        return PostResult(platform=self.key, ok=True, dry_run=False, external_id="real-1",
                          url="https://discord.example/1")


@pytest.fixture
def recording(monkeypatch):
    RecordingAdapter.calls = []
    real = registry.get_adapter

    def fake(key, settings):
        if key == "discord":
            return RecordingAdapter(settings)
        return real(key, settings)

    monkeypatch.setattr(registry, "get_adapter", fake)
    return RecordingAdapter


def test_global_dry_run_without_live_flag_is_zero_network(ctx, tiny_video, no_network,
                                                          monkeypatch):
    for name in ("ZERNIO_API_KEY", "META_ACCESS_TOKEN", "IG_USER_ID", "FB_PAGE_ID",
                 "DISCORD_WEBHOOK_URL", "X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN",
                 "X_ACCESS_SECRET", "REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET",
                 "REDDIT_USERNAME", "REDDIT_PASSWORD"):
        monkeypatch.setenv(name, "configured-value-123")
    publisher = Publisher(ctx)
    for key in registry.ALL_KEYS:
        result = publisher.publish(make_post(ctx, key, str(tiny_video)))
        assert result.ok and result.dry_run, (key, result.error)


def test_live_flag_with_global_dry_run_uses_real_path(ctx, tiny_video, recording,
                                                      monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/x")
    _cfg(ctx.settings, "discord", live=True)
    assert ctx.settings.dry_run is True
    result = Publisher(ctx).publish(make_post(ctx, "discord", str(tiny_video)))
    assert result.ok and not result.dry_run and result.external_id == "real-1"
    assert recording.calls == ["discord"]


def test_not_connected_is_never_live(ctx, tiny_video, recording, no_network):
    _cfg(ctx.settings, "discord", live=True)
    adapter = registry.get_adapter("discord", ctx.settings)
    assert adapter.is_live() is False
    result = Publisher(ctx).publish(make_post(ctx, "discord", str(tiny_video)))
    assert result.ok is False and recording.calls == []


# ----------------------------------------------------------------- double posting guard
@pytest.fixture
def studio_clip(studio_dir, project_dir):
    """Our copy of the very file the studio already published (same bytes → same sha)."""
    dst = project_dir / "data" / "videos" / "ep3.mp4"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(studio_dir / "assets" / "ep3_vert.mp4", dst)
    return dst


def test_studio_conflict_matching(settings, studio_dir, studio_clip, tiny_video):
    from agency.importer import studio_conflict

    settings.raw["studio"] = {"dir": str(studio_dir), "double_post_guard": True}
    hit = studio_conflict(settings, "youtube_shorts", [studio_clip])
    assert hit and hit["studio_platform"] == "youtube_shorts" and hit["where"] == "executions"
    assert studio_conflict(settings, "youtube", [studio_clip])  # same YouTube account
    assert studio_conflict(settings, "tiktok", [studio_clip]) is None  # studio run failed
    assert studio_conflict(settings, "instagram_reels", [studio_clip]) is None  # dry run
    assert studio_conflict(settings, "facebook", [studio_clip]) is None  # cancelled
    assert studio_conflict(settings, "youtube_shorts", [tiny_video]) is None  # other file
    settings.raw["studio"]["double_post_guard"] = False
    assert studio_conflict(settings, "youtube_shorts", [studio_clip]) is None
    settings.raw["studio"] = {"dir": "off"}
    assert studio_conflict(settings, "youtube_shorts", [studio_clip]) is None


def test_studio_db_untouched_by_guard(settings, studio_dir, studio_clip):
    from agency.importer import studio_conflict

    db_file = studio_dir / "state" / "studio.db"
    before = db_file.read_bytes()
    settings.raw["studio"] = {"dir": str(studio_dir)}
    assert studio_conflict(settings, "youtube_shorts", [studio_clip])
    assert db_file.read_bytes() == before


def test_generic_schema_with_sha_and_platform_columns(settings, tmp_path, studio_clip):
    import hashlib
    import sqlite3

    from agency.importer import studio_conflict

    root = tmp_path / "OtherStudio"
    (root / "state").mkdir(parents=True)
    sha = hashlib.sha256(studio_clip.read_bytes()).hexdigest()
    con = sqlite3.connect(root / "state" / "studio.db")
    con.execute("CREATE TABLE uploads (file_sha256 TEXT, platform TEXT, status TEXT)")
    con.execute("INSERT INTO uploads VALUES (?, 'tiktok', 'published')", (sha,))
    con.commit()
    con.close()
    settings.raw["studio"] = {"dir": str(root)}
    assert studio_conflict(settings, "tiktok", [studio_clip])["where"] == "uploads"
    assert studio_conflict(settings, "youtube", [studio_clip]) is None


def test_unreadable_studio_db_fails_closed(settings, tmp_path, studio_clip):
    from agency.importer import studio_conflict

    root = tmp_path / "Broken"
    (root / "state").mkdir(parents=True)
    (root / "state" / "studio.db").write_bytes(b"this is not sqlite" * 100)
    settings.raw["studio"] = {"dir": str(root)}
    # we cannot tell whether the studio already posted it: refuse the live upload (iron rule
    # 7); the owner can switch the guard off in config (studio.double_post_guard: false)
    assert studio_conflict(settings, "youtube", [studio_clip]) == {"platform": "youtube",
                                                                   "unreadable": True}
    settings.raw["studio"]["double_post_guard"] = False
    assert studio_conflict(settings, "youtube", [studio_clip]) is None


def test_publisher_refuses_double_post_live(ctx, studio_dir, studio_clip, monkeypatch):
    calls = []

    class LiveZernio(PlatformAdapter):
        required_secrets = ["ZERNIO_API_KEY"]
        display_he = "יוטיוב Shorts"

        def __init__(self, settings, key):
            super().__init__(settings)
            self.key = key

        def _upload(self, plan):
            calls.append(plan.platform)
            return PostResult(platform=self.key, ok=True, dry_run=False, external_id="v1")

    monkeypatch.setattr(registry, "get_adapter", lambda key, s: LiveZernio(s, key))
    monkeypatch.setenv("ZERNIO_API_KEY", "zk")
    ctx.settings.raw["studio"] = {"dir": str(studio_dir)}
    _cfg(ctx.settings, "youtube", live=True)
    _cfg(ctx.settings, "tiktok", live=True)

    pid = make_post(ctx, "youtube_shorts", str(studio_clip))
    result = Publisher(ctx).publish(pid)
    assert result.ok is False and "הסטודיו הישן" in result.error
    assert ctx.db.get_post(pid)["status"] == "failed" and calls == []

    ok = Publisher(ctx).publish(make_post(ctx, "tiktok", str(studio_clip)))
    assert ok.ok and calls == ["tiktok"]  # the studio's TikTok attempt failed: allowed


def test_publisher_training_mode_skips_guard(ctx, studio_dir, studio_clip, monkeypatch):
    import agency.importer as importer

    monkeypatch.setattr(importer, "studio_conflict",
                        lambda *a, **k: pytest.fail("guard must not run in training mode"))
    ctx.settings.raw["studio"] = {"dir": str(studio_dir)}
    result = Publisher(ctx).publish(make_post(ctx, "youtube_shorts", str(studio_clip)))
    assert result.ok and result.dry_run



def test_same_original_reencoded_by_studio_is_caught(settings, tmp_path, studio_clip):
    """The studio publishes re-encodes (vert / yt_vert); its ``versions`` table keeps the
    untouched master (profile ``long``). Our original == that master → same clip."""
    import hashlib
    import json as _json
    import sqlite3

    from agency.importer import studio_conflict

    root = tmp_path / "S2"
    (root / "state").mkdir(parents=True)
    master = hashlib.sha256(studio_clip.read_bytes()).hexdigest()
    con = sqlite3.connect(root / "state" / "studio.db")
    con.executescript("""
        CREATE TABLE versions (asset_id TEXT, profile TEXT, path TEXT, sha256 TEXT);
        CREATE TABLE packages (id TEXT, asset_id TEXT, body TEXT, sha256 TEXT, status TEXT);
        CREATE TABLE executions (package_id TEXT, platform TEXT, adapter TEXT, status TEXT);
        CREATE TABLE posts (platform TEXT, remote_id TEXT, asset_id TEXT, url TEXT);
    """)
    con.execute("INSERT INTO versions VALUES ('a9', 'long', 'x/long.mp4', ?)", (master,))
    con.execute("INSERT INTO versions VALUES ('a9', 'vert', 'x/vert.mp4', ?)", ("b" * 64,))
    body = {"targets": [{"platform": "youtube_shorts", "file_sha256": "c" * 64},
                        {"platform": "tiktok", "file_sha256": "b" * 64}]}
    con.execute("INSERT INTO packages VALUES ('p9', 'a9', ?, ?, 'done')",
                (_json.dumps(body), "d" * 64))
    con.execute("INSERT INTO executions VALUES ('p9', 'tiktok', 'zernio', 'scheduled')")
    con.execute("INSERT INTO posts VALUES ('youtube_shorts', 'vid9', 'a9', 'u')")
    con.commit()
    con.close()
    settings.raw["studio"] = {"dir": str(root)}
    assert studio_conflict(settings, "youtube_shorts", [studio_clip])["where"] == "posts"
    assert studio_conflict(settings, "tiktok", [studio_clip])["where"] == "executions"
    assert studio_conflict(settings, "instagram_reels", [studio_clip]) is None
