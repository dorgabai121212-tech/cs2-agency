"""Shared fixtures: isolated project dir, mock LLM, dry-run settings, DB, Context, tiny video."""
from __future__ import annotations

import shutil
import socket
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

_SECRET_VARS = [
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "YOUTUBE_CLIENT_SECRETS_FILE",
    "TIKTOK_ACCESS_TOKEN", "META_ACCESS_TOKEN", "IG_USER_ID", "FB_PAGE_ID", "X_API_KEY",
    "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET", "REDDIT_CLIENT_ID",
    "REDDIT_CLIENT_SECRET", "REDDIT_USERNAME", "REDDIT_PASSWORD", "DISCORD_WEBHOOK_URL",
    "ZERNIO_API_KEY", "TELEGRAM_BOT_TOKEN", "YOUTUBE_OAUTH_JSON",
]


@pytest.fixture(autouse=True)
def _hermetic_secrets(monkeypatch: pytest.MonkeyPatch):
    """Never read the real OS keyring or the owner's real C:\\CS2_STUDIO from tests.

    Tests that need a keyring install a fake backend and delete AGENCY_KEYRING themselves.
    """
    monkeypatch.setenv("AGENCY_KEYRING", "off")
    monkeypatch.setenv("AGENCY_STUDIO_DIR", "off")
    monkeypatch.delenv("AGENCY_CLI_LANG", raising=False)
    try:
        from agency.config import clear_secret_cache
        clear_secret_cache()
    except ImportError:  # pragma: no cover
        pass
    yield


@pytest.fixture
def project_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Temp project with config.yaml (dry_run: true), cwd set to it, mock LLM, no secrets."""
    example = REPO_ROOT / "config.example.yaml"
    config = yaml.safe_load(example.read_text(encoding="utf-8")) if example.exists() else {}
    config["dry_run"] = True
    if not isinstance(config.get("media"), dict):
        config["media"] = {}
    config["media"]["preset_speed"] = "ultrafast"  # same presets, fast x264 (keeps tests quick)
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENCY_MOCK_LLM", "1")
    monkeypatch.setenv("AGENCY_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENCY_DATA_DIR", raising=False)
    for name in _SECRET_VARS:
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.fixture
def settings(project_dir: Path):
    from agency.config import load_settings
    return load_settings(project_dir / "config.yaml")


@pytest.fixture
def db(settings):
    from agency.db import DB
    return DB(settings.db_path)


@pytest.fixture
def ctx(settings):
    from agency.staff.base import build_context
    return build_context(settings)


@pytest.fixture
def tiny_video(project_dir: Path) -> Path:
    """3s 1280x720 test mp4 (ffmpeg lavfi testsrc + sine); dummy bytes if ffmpeg missing."""
    path = project_dir / "tiny.mp4"
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        try:
            subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error",
                 "-f", "lavfi", "-i", "testsrc=duration=3:size=1280x720:rate=30",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                 "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-shortest", str(path)],
                check=True, capture_output=True, timeout=120)
            return path
        except (subprocess.SubprocessError, OSError):
            pass
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 1024)
    return path


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch):
    """Make any HTTP request / socket connection fail loudly."""
    def _blocked(*args, **kwargs):
        raise AssertionError(f"network access attempted: {args[:2]!r}")

    try:
        import requests
        monkeypatch.setattr(requests.Session, "request", _blocked)
    except ImportError:  # pragma: no cover
        pass
    monkeypatch.setattr(socket, "create_connection", _blocked)
    yield


# ----------------------------------------------------------------- core: keyring + studio
class FakeKeyring:
    """In-memory keyring backend (installed with keyring.set_keyring)."""

    def __new__(cls):
        from keyring.backend import KeyringBackend
        from keyring.errors import PasswordDeleteError

        class _Fake(KeyringBackend):
            priority = 1

            def __init__(self):
                super().__init__()
                self.store: dict[tuple[str, str], str] = {}

            def get_password(self, service, username):
                return self.store.get((service, username))

            def set_password(self, service, username, password):
                self.store[(service, username)] = password

            def delete_password(self, service, username):
                if (service, username) not in self.store:
                    raise PasswordDeleteError("not found")
                del self.store[(service, username)]

        return _Fake()


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch):
    """A working OS keyring (in memory). ``.store`` maps (service, name) -> value."""
    import keyring

    from agency.config import clear_secret_cache

    fake = FakeKeyring()
    previous = keyring.get_keyring()
    keyring.set_keyring(fake)
    monkeypatch.delenv("AGENCY_KEYRING", raising=False)
    clear_secret_cache()
    yield fake
    keyring.set_keyring(previous)
    clear_secret_cache()


STUDIO_CLIP_BYTES = b"\x00\x00\x00\x18ftypmp42studio-short-ep3" + b"\x01" * 2048


@pytest.fixture
def studio_dir(tmp_path: Path) -> Path:
    """A reconstructed C:\\CS2_STUDIO: state/studio.db (kv meta_ids, assets, packages,
    executions, posts), studio/config.py, studio/package.py, prompts/copywriter.md, a clip."""
    import hashlib
    import json
    import sqlite3

    root = tmp_path / "CS2_STUDIO"
    for sub in ("state", "studio", "prompts", "logs", "assets"):
        (root / sub).mkdir(parents=True)
    clip = root / "assets" / "ep3_vert.mp4"
    clip.write_bytes(STUDIO_CLIP_BYTES)
    sha = hashlib.sha256(STUDIO_CLIP_BYTES).hexdigest()
    con = sqlite3.connect(root / "state" / "studio.db")
    con.executescript("""
        PRAGMA journal_mode = WAL;
        CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE assets (id TEXT PRIMARY KEY, name TEXT, status TEXT, created REAL);
        CREATE TABLE packages (id TEXT PRIMARY KEY, asset_id TEXT, created REAL, body TEXT,
                               sha256 TEXT, status TEXT);
        CREATE TABLE executions (id INTEGER PRIMARY KEY, package_id TEXT, platform TEXT,
                                 adapter TEXT, created REAL, status TEXT, due REAL,
                                 vendor_ref TEXT, url TEXT, error TEXT, checked REAL,
                                 UNIQUE(package_id, platform));
        CREATE TABLE posts (id INTEGER PRIMARY KEY, platform TEXT, remote_id TEXT,
                            asset_id TEXT, url TEXT, published REAL, kind TEXT);
    """)
    con.execute("INSERT INTO kv VALUES (?, ?)", ("meta_ids", json.dumps(
        {"page_id": "102030405060708", "ig_user_id": "17841400000000001"})))
    con.execute("INSERT INTO kv VALUES (?, ?)", ("live_platforms", json.dumps(["youtube"])))
    con.execute("INSERT INTO assets VALUES (?,?,?,?)",
                ("a1", "SHORT_EP3_MOZART_PHONK", "published", 1760000000.0))
    targets = [{"platform": p, "file": str(clip), "file_sha256": sha, "adapter": a}
               for p, a in (("youtube_shorts", "zernio"), ("tiktok", "zernio"),
                            ("instagram", "dry"), ("facebook_reels", "meta"))]
    con.execute("INSERT INTO packages VALUES (?,?,?,?,?,?)",
                ("p1", "a1", 1760000000.0, json.dumps({"v": 1, "asset_id": "a1",
                                                      "targets": targets}), "x" * 64, "done"))
    for plat, adapter, status in (("youtube_shorts", "zernio", "published"),
                                  ("tiktok", "zernio", "failed"),
                                  ("instagram", "dry", "published"),
                                  ("facebook_reels", "meta", "cancelled")):
        con.execute("INSERT INTO executions (package_id, platform, adapter, created, status) "
                    "VALUES (?,?,?,?,?)", ("p1", plat, adapter, 1760000000.0, status))
    con.execute("INSERT INTO posts (platform, remote_id, asset_id, url, published, kind) "
                "VALUES (?,?,?,?,?,?)", ("youtube_shorts", "w-rEUe6qMN8", "a1",
                                         "https://www.youtube.com/shorts/w-rEUe6qMN8",
                                         1760000000.0, "short"))
    con.commit()
    con.close()
    (root / "studio" / "config.py").write_text(
        'BRAND = "Imagination"\n'
        'HASHTAGS_BASE = ["#CS2", "#CounterStrike2", "#CS2Edit"]\n'
        'RED_LIST = ["case opening", "free skins", "aimbot"]\n'
        'import os\nos.system("echo should-never-run")\n', encoding="utf-8")
    (root / "studio" / "package.py").write_text(
        'SLOT = {"youtube_shorts": (17, 0), "facebook_reels": (17, 40), "instagram": (20, 0),\n'
        '        "tiktok": (20, 40), "youtube": (16, 0), "facebook": (16, 30)}\n',
        encoding="utf-8")
    (root / "prompts" / "copywriter.md").write_text(
        'You are the Copywriter of "Imagination".\n'
        'Tagline: "Legendary CS2 moments. Real demo. Shot like a movie."\n'
        "## Hard rules\n"
        "- Never mention skins gambling, case opening, betting, cheats, \"free skins\", promo "
        "codes or giveaways.\n"
        "- YouTube hashtags are exactly: #CS2 #CounterStrike2 #CS2Edit\n", encoding="utf-8")
    return root
