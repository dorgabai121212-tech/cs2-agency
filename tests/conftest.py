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
]


@pytest.fixture
def project_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Temp project with config.yaml (dry_run: true), cwd set to it, mock LLM, no secrets."""
    example = REPO_ROOT / "config.example.yaml"
    config = yaml.safe_load(example.read_text(encoding="utf-8")) if example.exists() else {}
    config["dry_run"] = True
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
