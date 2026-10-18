"""Dashboard API via FastAPI TestClient (mock LLM, dry run, no network)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agency.dashboard.app import create_app, sanitize_filename

HEADERS = {"X-Agency-Request": "1"}
PC = ("127.0.0.1", 50000)  # TestClient's default peer "testclient" is not loopback (v2)


@pytest.fixture
def app(ctx, no_network):
    application = create_app(ctx)
    application.state.inline_jobs = True  # run pipeline / publishing synchronously in tests
    return application


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://127.0.0.1:8765", client=PC, headers=HEADERS) as c:
        yield c


def _upload(client, path: Path, name: str | None = None, title: str = "אייס במיראז'"):
    with open(path, "rb") as fh:
        return client.post("/api/upload", data={"title": title},
                           files={"file": (name or path.name, fh, "video/mp4")})


def _videos_dir(ctx) -> Path:
    return ctx.settings.data_dir / "videos"


# --------------------------------------------------------------------------- page + guards
def test_index_page_is_hebrew_rtl(client):
    res = client.get("/")
    assert res.status_code == 200
    assert 'dir="rtl"' in res.text and 'lang="he"' in res.text
    assert "/static/app.js" in res.text
    # v2: friendly wording ("מצב אימון" = training mode) instead of the old DRY-RUN banner
    assert "מצב אימון" in res.text and "DRY-RUN" not in res.text
    assert "Imagination" in res.text
    assert "cdn" not in res.text.lower()
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_post_without_csrf_header_is_refused(app):
    with TestClient(app, base_url="http://127.0.0.1:8765", client=PC) as c:
        res = c.post("/api/finance/expense", json={"amount": 5, "category": "ads"})
    assert res.status_code == 403


def test_foreign_host_is_refused(app):
    with TestClient(app, base_url="http://evil.example.com", client=PC, headers=HEADERS) as c:
        assert c.get("/api/status").status_code == 403


# --------------------------------------------------------------------------- read API
def test_status(client):
    data = client.get("/api/status").json()
    assert data["dry_run"] is True
    assert data["llm_mock"] is True
    assert data["staff"][0]["name"] == "manager" and data["staff"][0]["is_manager"]
    names = {m["name"] for m in data["staff"]}
    assert {"analyst", "editor", "copywriter", "finance"} <= names
    assert data["counts"]["videos"] == 0
    assert "remaining" in data["budget"]


def test_platforms_panel(client):
    data = client.get("/api/platforms").json()
    assert data["dry_run"] is True
    keys = [p["key"] for p in data["platforms"]]
    assert keys[0] == "youtube" and "discord" in keys and len(keys) == 8
    for p in data["platforms"]:
        assert p["live"] is False
        assert p["configured"] is False  # conftest clears every secret


def test_list_endpoints_empty(client):
    assert client.get("/api/videos").json() == []
    assert client.get("/api/posts").json() == []
    assert client.get("/api/approvals").json() == []
    assert isinstance(client.get("/api/events").json(), list)


# --------------------------------------------------------------------------- upload
def test_upload_creates_video_and_runs_pipeline(client, ctx, tiny_video):
    res = _upload(client, tiny_video)
    assert res.status_code == 200, res.text
    body = res.json()
    video = ctx.db.get_video(body["video_id"])
    assert video is not None
    assert Path(video.path).parent == _videos_dir(ctx)
    assert Path(video.path).read_bytes() == tiny_video.read_bytes()
    assert video.title_hint == "אייס במיראז'"
    assert not list(_videos_dir(ctx).glob("*.part"))
    # pipeline ran inline: a publish approval is waiting with per-platform plans
    approvals = client.get("/api/approvals").json()
    publish = [a for a in approvals if a["kind"] == "publish"]
    assert publish, approvals
    assert publish[0]["plans"] and publish[0]["plans"][0]["platform"]
    assert {"title", "description", "scheduled_at", "why"} <= set(publish[0]["plans"][0])
    vids = client.get("/api/videos").json()
    assert vids[0]["id"] == body["video_id"] and vids[0]["posts"]


@pytest.mark.parametrize("bad_name", ["evil.sh", "noext", "clip.mp4.exe", ".mp4"])
def test_upload_rejects_bad_extension(client, ctx, tiny_video, bad_name):
    res = _upload(client, tiny_video, name=bad_name)
    assert res.status_code == 400
    assert ctx.db.list_videos() == []
    assert not any(_videos_dir(ctx).iterdir())


@pytest.mark.parametrize("name", ["../../../etc/evil.mp4", "..\\..\\Windows\\evil.mp4",
                                  "/abs/path/evil.mp4", "ev<il>|:*?.mp4"])
def test_upload_path_traversal_is_sanitized(client, ctx, project_dir, tiny_video, name):
    res = _upload(client, tiny_video, name=name)
    assert res.status_code == 200, res.text
    video = ctx.db.get_video(res.json()["video_id"])
    saved = Path(video.path)
    assert saved.parent.resolve() == _videos_dir(ctx).resolve()
    assert ".." not in saved.name and "/" not in saved.name and "\\" not in saved.name
    assert saved.name.endswith(".mp4")
    assert not (project_dir.parent / "etc").exists()


def test_upload_size_limit(client, ctx, project_dir):
    ctx.settings.raw.setdefault("dashboard", {})["max_upload_gb"] = 2 / 1024 ** 2  # 2 KB
    big = project_dir / "big.mp4"
    big.write_bytes(b"\x00" * 10_000)
    res = _upload(client, big)
    assert res.status_code == 413
    assert ctx.db.list_videos() == []
    assert not any(_videos_dir(ctx).iterdir())  # partial file removed


def test_upload_without_file(client):
    res = client.post("/api/upload", data={"title": "x"}, files={"other": ("a.txt", b"hi")})
    assert res.status_code == 400


def test_sanitize_filename():
    assert sanitize_filename("../../a b.MP4") == "a b.mp4"
    assert sanitize_filename("C:\\Users\\me\\קליפ אייס.mov") == "קליפ אייס.mov"
    assert sanitize_filename("a.b.c.webm") == "a_b_c.webm"
    for bad in ("x.txt", "", "..", "a/b/.mkv"):
        with pytest.raises(ValueError):
            sanitize_filename(bad)


# --------------------------------------------------------------------------- approvals
def _publish_approval(client, tiny_video) -> dict:
    assert _upload(client, tiny_video).status_code == 200
    return [a for a in client.get("/api/approvals").json() if a["kind"] == "publish"][0]


def test_approve_publishes_in_dry_run(client, ctx, tiny_video):
    a = _publish_approval(client, tiny_video)
    res = client.post(f"/api/approvals/{a['id']}/approve", json={"note": "יאללה"})
    assert res.status_code == 200, res.text
    assert ctx.db.get_approval(a["id"]).status in ("approved", "executed")
    posts = client.get("/api/posts").json()
    assert posts and all(p["status"] in ("published", "scheduled") for p in posts)
    for p in posts:
        if p["status"] == "published":
            assert p["dry_run"] is True and p["external_id"].startswith("dryrun-")
    # cannot decide twice
    assert client.post(f"/api/approvals/{a['id']}/approve", json={}).status_code == 409
    assert client.post(f"/api/approvals/{a['id']}/reject", json={}).status_code == 409


def test_reject_cancels_posts(client, ctx, tiny_video):
    a = _publish_approval(client, tiny_video)
    res = client.post(f"/api/approvals/{a['id']}/reject", json={"note": "לא עכשיו"})
    assert res.status_code == 200, res.text
    assert ctx.db.get_approval(a["id"]).status == "rejected"
    assert ctx.db.get_approval(a["id"]).note == "לא עכשיו"
    posts = client.get("/api/posts").json()
    assert posts and all(p["status"] == "cancelled" for p in posts)
    assert all(p["external_id"] is None for p in posts)


def test_unknown_approval_404(client):
    assert client.post("/api/approvals/999/approve", json={}).status_code == 404
    assert client.post("/api/approvals/999/reject").status_code == 404


def test_approved_spend_over_cap_is_refused(client, ctx):
    from agency.models import Approval

    aid = ctx.db.add_approval(Approval(id=None, kind="spend", title="קידום בטיקטוק",
                                       cost_ils=500, requested_by="promoter",
                                       payload={"platform": "tiktok"}))
    res = client.post(f"/api/approvals/{aid}/approve", json={})
    assert res.status_code == 200
    assert res.json()["result"]["ok"] is False  # budget is 0 → refused even though approved
    assert ctx.db.list_ledger() == []


# --------------------------------------------------------------------------- finance
def test_finance_expense_and_income(client):
    assert client.post("/api/finance/expense", json={"amount": 25, "category": "software",
                                                     "description": "OBS plugin"}).status_code == 200
    assert client.post("/api/finance/income", json={"amount": 100, "source": "adsense"}
                       ).status_code == 200
    data = client.get("/api/finance").json()
    assert data["budget"]["spent"] == 25
    assert data["budget"]["income"] == 100
    assert data["budget"]["net"] == 75
    kinds = sorted(e["kind"] for e in data["entries"])
    assert kinds == ["expense", "income"]


@pytest.mark.parametrize("amount", [0, -5, "abc"])
def test_finance_rejects_bad_amount(client, amount):
    res = client.post("/api/finance/expense", json={"amount": amount, "category": "ads"})
    assert res.status_code == 422


# --------------------------------------------------------------------------- chat
def test_chat_mock_mode(client):
    res = client.post("/api/chat", json={"message": "מה המצב?"})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["mock"] is True
    assert isinstance(data["reply"], str) and data["reply"].strip()


def test_chat_rejects_empty(client):
    assert client.post("/api/chat", json={"message": ""}).status_code == 422
