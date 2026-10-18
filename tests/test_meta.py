"""Meta (Instagram Reels + Facebook Reels/videos, Graph v24), the YouTube-first rule, and the
direct YouTube adapter's credentials / make_private. All HTTP is mocked."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from agency.models import PostPlan, PostResult
from agency.platforms import instagram as ig_mod
from agency.platforms.facebook import FacebookAdapter
from agency.platforms.instagram import (GRAPH_API_VERSION, InstagramReelsAdapter,
                                        meta_delay_status, meta_min_delay_ok,
                                        record_published_at)

TOKEN = "EAAG-SECRET-page-token-9876543210"


class FakeResponse:
    def __init__(self, status: int = 200, payload: dict | None = None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.content = json.dumps(self._payload).encode()
        self.text = self.content.decode()

    def json(self) -> dict:
        return self._payload


def plan_for(key, path, **extra) -> PostPlan:
    return PostPlan(platform=key, variant_path=str(path), title="דיגל 3K",
                    description="רגע אגדי.", tags=[], hashtags=["CS2", "CS2Edit"],
                    scheduled_at=None, extra=dict(extra))


@pytest.fixture
def meta_live(settings, monkeypatch):
    settings.raw["dry_run"] = False
    monkeypatch.setenv("AGENCY_KEYRING", "off")
    monkeypatch.setenv("META_ACCESS_TOKEN", TOKEN)
    settings.raw["meta"] = {"ig_user_id": "17841400000000001", "page_id": "100000000000001"}
    monkeypatch.setattr(InstagramReelsAdapter, "poll_interval_s", 0)
    monkeypatch.setattr(FacebookAdapter, "poll_interval_s", 0)
    return settings


class FakeGraph:
    def __init__(self, monkeypatch, *, container_codes=None, reel_status=None):
        import requests

        self.calls: list[tuple] = []
        self.container_codes = list(container_codes or ["IN_PROGRESS", "FINISHED"])
        self.reel_status = list(reel_status or [
            {"video_status": "processing", "publishing_phase": {"status": "not_started"}},
            {"video_status": "ready", "publishing_phase": {"status": "complete",
                                                            "publish_status": "published"}}])
        monkeypatch.setattr(requests, "request", self.request)
        monkeypatch.setattr(requests, "post", self.post)

    def request(self, method, url, **kwargs):
        assert kwargs.get("timeout")
        assert kwargs["headers"]["Authorization"] == f"OAuth {TOKEN}"
        assert "access_token" not in json.dumps(kwargs.get("params") or {})
        self.calls.append((method, url, kwargs))
        assert f"/{GRAPH_API_VERSION}/" in url
        tail = url.split(f"/{GRAPH_API_VERSION}/", 1)[1]
        fields = (kwargs.get("params") or {}).get("fields", "")
        if method == "POST" and tail.endswith("/media"):
            return FakeResponse(200, {"id": "c_1", "uri": "https://rupload.facebook.com/x/c_1"})
        if method == "GET" and tail == "c_1":
            code = self.container_codes.pop(0) if self.container_codes else "FINISHED"
            return FakeResponse(200, {"status_code": code, "status": code})
        if method == "POST" and tail.endswith("/media_publish"):
            return FakeResponse(200, {"id": "m_1"})
        if method == "GET" and tail == "m_1":
            return FakeResponse(200, {"permalink": "https://www.instagram.com/reel/ABC/"})
        if method == "POST" and tail.endswith("/video_reels"):
            phase = kwargs["data"]["upload_phase"]
            if phase == "start":
                return FakeResponse(200, {"video_id": "v_1",
                                          "upload_url": "https://rupload.facebook.com/v/v_1"})
            return FakeResponse(200, {"success": True})
        if method == "GET" and tail == "v_1" and fields == "status":
            st = self.reel_status.pop(0) if self.reel_status else self.reel_status_last
            self.reel_status_last = st
            return FakeResponse(200, {"status": st})
        if method == "GET" and tail == "v_1":
            return FakeResponse(200, {"permalink_url": "/reel/v_1"})
        if method == "POST" and tail.endswith("/videos"):
            return FakeResponse(200, {"id": "fbv_1"})
        raise AssertionError(f"unexpected {method} {url}")

    def post(self, url, data=None, headers=None, timeout=None, **kwargs):
        assert timeout and headers["Authorization"] == f"OAuth {TOKEN}"
        assert headers["offset"] == "0" and int(headers["file_size"]) > 0
        self.calls.append(("RUPLOAD", url, {}))
        return FakeResponse(200, {"success": True})

    def made(self, method, suffix):
        return [c for c in self.calls if c[0] == method and c[1].endswith(suffix)]


def test_graph_version_is_v24():
    from agency.platforms import facebook
    assert GRAPH_API_VERSION == "v24.0"
    assert "/v24.0" in facebook.GRAPH_BASE and "/v24.0" in facebook.RUPLOAD_BASE


# ------------------------------------------------------------------------------ Instagram
def test_instagram_ids_from_config(settings, monkeypatch):
    monkeypatch.setenv("AGENCY_KEYRING", "off")
    monkeypatch.setenv("META_ACCESS_TOKEN", TOKEN)
    adapter = InstagramReelsAdapter(settings)
    assert adapter.missing_secrets() == ["IG_USER_ID"]
    settings.raw["publishing"]["platforms"]["instagram_reels"]["ig_user_id"] = "1784"
    assert adapter.configured() and adapter.ig_user_id() == "1784"
    settings.raw["meta"] = {"ig_user_id": "999"}  # the importer's key wins
    assert adapter.ig_user_id() == "999"
    monkeypatch.setenv("IG_USER_ID", "555")  # env wins over config
    assert adapter.ig_user_id() == "555"


def test_instagram_happy_path(meta_live, tiny_video, monkeypatch):
    graph = FakeGraph(monkeypatch)
    result = InstagramReelsAdapter(meta_live).upload(plan_for("instagram_reels", tiny_video))
    assert result.ok, result.error
    assert result.external_id == "m_1" and result.url == "https://www.instagram.com/reel/ABC/"
    create = graph.made("POST", "/17841400000000001/media")[0][2]["data"]
    assert create["media_type"] == "REELS" and create["upload_type"] == "resumable"
    assert "#CS2" in create["caption"] and len(create["caption"]) <= 2200
    assert graph.made("RUPLOAD", "/x/c_1")
    assert graph.made("POST", "/media_publish")[0][2]["data"] == {"creation_id": "c_1"}
    assert TOKEN not in json.dumps([c[1] for c in graph.calls])


def test_instagram_still_processing_is_honest_and_retry_does_not_reupload(
        meta_live, tiny_video, monkeypatch):
    graph = FakeGraph(monkeypatch, container_codes=["IN_PROGRESS"] * 5)
    meta_live.raw["publishing"]["platforms"]["instagram_reels"]["poll_max_s"] = 0
    adapter = InstagramReelsAdapter(meta_live)
    result = adapter.upload(plan_for("instagram_reels", tiny_video))
    assert not result.ok and "still processing" in result.error
    assert result.external_id == "c_1"
    assert len(graph.made("POST", "/media")) == 1
    graph.container_codes = ["FINISHED", "FINISHED"]
    retry = adapter.upload(plan_for("instagram_reels", tiny_video))
    assert retry.ok and retry.external_id == "m_1"
    assert len(graph.made("POST", "/media")) == 1 and len(graph.made("RUPLOAD", "c_1")) == 1
    again = adapter.upload(plan_for("instagram_reels", tiny_video))  # never a second Reel
    assert again.ok and len(graph.made("POST", "/media_publish")) == 1


def test_instagram_container_error(meta_live, tiny_video, monkeypatch):
    FakeGraph(monkeypatch, container_codes=["ERROR"])
    result = InstagramReelsAdapter(meta_live).upload(plan_for("instagram_reels", tiny_video))
    assert not result.ok and "ERROR" in result.error and TOKEN not in result.error


# ------------------------------------------------------------------------------ YouTube first
def _video_with_posts(db, tiny_video, yt_status="published", dry=False):
    vid = db.add_video(str(tiny_video), "test")
    yt = db.add_post(vid, PostPlan(platform="youtube_shorts", variant_path=str(tiny_video),
                                   title="t", description="d"), status="planned")
    if yt_status == "published":
        db.update_post(yt, status="published", result=PostResult(
            platform="youtube_shorts", ok=True, dry_run=dry, external_id="yt1", url="u"))
    else:
        db.update_post(yt, status=yt_status)
    ig = db.add_post(vid, PostPlan(platform="instagram_reels", variant_path=str(tiny_video),
                                   title="t", description="d"), status="scheduled")
    return vid, yt, ig


def test_meta_min_delay_rules(db, tiny_video):
    now = datetime.now(timezone.utc)
    vid = db.add_video(str(tiny_video), "no youtube")
    assert meta_min_delay_ok(db, vid) is True

    vid, _yt, _ig = _video_with_posts(db, tiny_video, "scheduled")
    assert meta_delay_status(db, vid) == (False, None)  # YouTube goes first

    vid, _yt, _ig = _video_with_posts(db, tiny_video, "published")
    record_published_at(db, "youtube_shorts", "yt1", (now - timedelta(minutes=10)).isoformat())
    ok, not_before = meta_delay_status(db, vid, now=now)
    assert ok is False and abs((not_before - (now + timedelta(minutes=20))).total_seconds()) < 2
    assert meta_min_delay_ok(db, vid, now=now + timedelta(minutes=21)) is True

    vid, _yt, _ig = _video_with_posts(db, tiny_video, "failed")
    assert meta_min_delay_ok(db, vid) is True
    vid, _yt, _ig = _video_with_posts(db, tiny_video, "published", dry=True)
    assert meta_min_delay_ok(db, vid) is True  # a training-mode post is not on YouTube


def test_instagram_adapter_enforces_youtube_first_before_network(meta_live, tiny_video, db,
                                                                 no_network):
    vid, _yt, ig = _video_with_posts(db, tiny_video, "published")
    record_published_at(db, "youtube_shorts", "yt1")  # YouTube went live just now
    db.update_post(ig, status="publishing")  # the publisher claimed it
    result = InstagramReelsAdapter(meta_live).upload(plan_for("instagram_reels", tiny_video))
    assert not result.ok and "דקות אחרי יוטיוב" in result.error
    result = InstagramReelsAdapter(meta_live).upload(
        plan_for("instagram_reels", tiny_video, video_id=vid))
    assert not result.ok and "YouTube first" in result.error


def test_instagram_training_mode_no_network(settings, tiny_video, no_network, monkeypatch):
    monkeypatch.setenv("META_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("IG_USER_ID", "1784")
    result = InstagramReelsAdapter(settings).upload(plan_for("instagram_reels", tiny_video))
    assert result.ok and result.dry_run


# ------------------------------------------------------------------------------ Facebook
@pytest.fixture
def vertical_video(project_dir):
    import shutil
    import subprocess
    path = project_dir / "vert.mp4"
    if shutil.which("ffmpeg"):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        "testsrc=duration=2:size=360x640:rate=30", "-pix_fmt", "yuv420p",
                        "-c:v", "libx264", "-preset", "ultrafast", str(path)],
                       check=True, capture_output=True, timeout=60)
    else:
        path.write_bytes(b"\x00" * 2048)
    return path


def test_facebook_reel_flow(meta_live, vertical_video, monkeypatch):
    graph = FakeGraph(monkeypatch)
    plan = plan_for("facebook", vertical_video, aspect="9:16")
    result = FacebookAdapter(meta_live).upload(plan)
    assert result.ok, result.error
    assert result.external_id == "v_1" and result.url == "https://www.facebook.com/reel/v_1"
    phases = [c[2]["data"]["upload_phase"] for c in graph.made("POST", "/video_reels")]
    assert phases == ["start", "finish"]
    finish = graph.made("POST", "/video_reels")[1][2]["data"]
    assert finish["video_state"] == "PUBLISHED" and "#CS2" in finish["description"]
    assert graph.made("RUPLOAD", "/v/v_1")


def test_facebook_reel_scheduled_and_processing(meta_live, vertical_video, monkeypatch):
    graph = FakeGraph(monkeypatch)
    plan = plan_for("facebook", vertical_video, aspect="9:16")
    plan.scheduled_at = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    result = FacebookAdapter(meta_live).upload(plan)
    assert result.ok and result.url is None
    finish = graph.made("POST", "/video_reels")[1][2]["data"]
    assert finish["video_state"] == "SCHEDULED" and finish["scheduled_publish_time"].isdigit()

    FakeGraph(monkeypatch, reel_status=[{"video_status": "processing"}] * 5)
    meta_live.raw["publishing"]["platforms"]["facebook"] = {"poll_max_s": 0, "format": "reels"}
    other = plan_for("facebook", vertical_video)
    other.title = "another"
    adapter = FacebookAdapter(meta_live)
    adapter.db.kv_set(ig_mod.PENDING_KV, {})
    result = adapter.upload(other)
    assert not result.ok and "still processing" in result.error and result.external_id == "v_1"


def test_facebook_horizontal_goes_to_videos(meta_live, tiny_video, monkeypatch):
    graph = FakeGraph(monkeypatch)
    result = FacebookAdapter(meta_live).upload(plan_for("facebook", tiny_video, aspect="16:9"))
    assert result.ok and result.external_id == "fbv_1"
    assert graph.made("POST", "/100000000000001/videos") and not graph.made("POST", "/video_reels")


def test_facebook_reel_state_parsing():
    state = FacebookAdapter.reel_state
    assert state({"video_status": "processing"})[0] == "processing"
    assert state({"publishing_phase": {"status": "complete"}})[0] == "published"
    assert state({"video_status": "ready"})[0] == "published"
    assert state({"processing_phase": {"status": "error", "errors": ["bad"]}})[0] == "error"


# ------------------------------------------------------------------------------ YouTube direct
def test_youtube_token_migrates_to_secrets(settings):
    from agency.platforms.youtube import YouTubeAdapter

    old = settings.data_dir / "youtube_token.json"
    old.write_text(json.dumps({"refresh_token": "r", "client_id": "c", "client_secret": "s"}))
    adapter = YouTubeAdapter(settings)
    assert adapter.has_google_credentials()
    assert adapter.token_path == settings.data_dir / "secrets" / "youtube_token.json"
    assert adapter.token_path.is_file() and not old.exists()


def test_youtube_credentials_from_studio_oauth_secret(settings, monkeypatch):
    from agency.platforms.youtube import YouTubeAdapter

    monkeypatch.setenv("AGENCY_KEYRING", "off")
    adapter = YouTubeAdapter(settings)
    assert not adapter.configured() and adapter.missing_secrets() == [
        "YOUTUBE_CLIENT_SECRETS_FILE"]
    monkeypatch.setenv("YOUTUBE_OAUTH_JSON", json.dumps(
        {"client_id": "c", "client_secret": "s", "refresh_token": "1//r"}))
    assert adapter.configured() and adapter.missing_secrets() == []


class _Exec:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class FakeYouTubeService:
    def __init__(self, status):
        self.status = status
        self.updated = None

    def videos(self):
        return self

    def list(self, part, id):
        return _Exec({"items": [{"id": id, "status": dict(self.status)}]})

    def update(self, part, body):
        self.updated = body
        return _Exec(body)


def test_make_private_instead_of_delete(settings, monkeypatch):
    from agency.platforms.youtube import YouTubeAdapter

    settings.raw["dry_run"] = False
    monkeypatch.setenv("YOUTUBE_OAUTH_JSON", json.dumps(
        {"client_id": "c", "client_secret": "s", "refresh_token": "1//r"}))
    service = FakeYouTubeService({"privacyStatus": "public", "publishAt": "2099-01-01T00:00:00Z",
                                  "selfDeclaredMadeForKids": False})
    monkeypatch.setattr(YouTubeAdapter, "_service", lambda self, interactive=False: service)
    adapter = YouTubeAdapter(settings)
    assert adapter.make_private("dQw4w9WgXcQ") is True
    assert service.updated["status"]["privacyStatus"] == "private"
    assert "publishAt" not in service.updated["status"]
    assert adapter.make_private("dryrun-youtube-abc") is False
    assert not hasattr(adapter, "delete_video")


def test_make_private_training_mode_no_network(settings, no_network, monkeypatch):
    from agency.platforms.youtube import YouTubeAdapter

    monkeypatch.setenv("YOUTUBE_OAUTH_JSON", json.dumps(
        {"client_id": "c", "client_secret": "s", "refresh_token": "1//r"}))
    assert YouTubeAdapter(settings).make_private("dQw4w9WgXcQ") is False
