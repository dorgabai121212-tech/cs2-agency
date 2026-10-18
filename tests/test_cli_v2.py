"""CLI v2: Windows console language rule, `agency` = dashboard, import / connect / live."""
from __future__ import annotations

import json
import re

import pytest
import requests
from typer.testing import CliRunner

from agency import cli
from agency.cli import app

runner = CliRunner()
HEBREW = re.compile(r"[֐-׿]")
HOOK = "https://discord.com/api/webhooks/42/SECRET_hook_value_77"


@pytest.fixture(autouse=True)
def _no_default_scan_roots(monkeypatch):
    """`agency import` must not scan the real Documents/Desktop folders from tests."""
    from agency import importer
    monkeypatch.setattr(importer, "default_roots", lambda settings=None: [])


def run(*args: str, input: str | None = None):
    return runner.invoke(app, list(args), input=input, catch_exceptions=False)


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(cli, "IS_WINDOWS", True)


@pytest.mark.parametrize("args", [("platforms",), ("status",), ("approvals",), ("budget",),
                                  ("tick",), ("report",), ("live", "discord", "on", "--yes"),
                                  ("live", "myspace", "on")])
def test_windows_console_is_english(project_dir, windows, args):
    result = run(*args)
    assert not HEBREW.search(result.output), result.output


def test_windows_hebrew_when_asked(project_dir, windows, monkeypatch):
    monkeypatch.setenv("AGENCY_CLI_LANG", "he")
    assert HEBREW.search(run("approvals").output)


def test_linux_default_is_hebrew(project_dir):
    assert cli.cli_lang() == "he"
    assert "אין אישורים" in run("approvals").output


def test_no_subcommand_starts_dashboard(project_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_run_dashboard", lambda *a: calls.append(a))
    result = run()
    assert result.exit_code == 0 and calls == [(None, None, False, 60.0)]
    assert run("--help").exit_code == 0 and calls == [(None, None, False, 60.0)]


def test_live_on_off(project_dir, settings, monkeypatch):
    refused = run("live", "discord", "on", "--yes")
    assert refused.exit_code == 1  # not connected
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", HOOK)
    cancelled = run("live", "discord", "on", input="n\n")
    assert cancelled.exit_code == 0 and "באמת" in cancelled.output
    from agency.config import load_settings
    assert load_settings(project_dir / "config.yaml").platform_live("discord") is False
    assert run("live", "discord", "on", input="y\n").exit_code == 0
    assert load_settings(project_dir / "config.yaml").platform_live("discord") is True
    assert run("live", "discord", "off").exit_code == 0
    assert load_settings(project_dir / "config.yaml").platform_live("discord") is False
    assert HOOK not in json.dumps([refused.output, cancelled.output])


def test_live_shorts_alias(project_dir, monkeypatch):
    monkeypatch.setenv("ZERNIO_API_KEY", "zk-SECRET_1")
    result = run("live", "shorts", "on", "--yes")
    assert result.exit_code == 0, result.output
    from agency.config import load_settings
    assert load_settings(project_dir / "config.yaml").platform_live("youtube_shorts")


class Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._p = payload
        self.content = json.dumps(payload).encode()

    def json(self):
        return self._p


def test_connect_hidden_input_never_echoed(project_dir, fake_keyring, monkeypatch):
    monkeypatch.setattr(requests, "request",
                        lambda m, url, **k: Resp(200, {"name": "CS2 Clips"}))
    result = run("connect", "discord", input=HOOK + "\n")
    assert result.exit_code == 0, result.output
    assert HOOK not in result.output and "SECRET_hook" not in result.output
    assert "CS2 Clips" in result.output
    assert fake_keyring.store[("cs2-agency", "DISCORD_WEBHOOK_URL")] == HOOK


def test_connect_bad_value_refused(project_dir, fake_keyring):
    result = runner.invoke(app, ["connect", "discord"], input="https://x\x00y\n")
    assert result.exit_code == 1
    assert "https://x" not in result.output


def test_import_studio_cli(project_dir, studio_dir, fake_keyring, windows, capsys):
    fake_keyring.store[("cs2studio", "zernio")] = "zernio-SECRET_studio"
    result = run("import", str(studio_dir), "--yes")
    assert result.exit_code == 0, result.output
    assert "SECRET_studio" not in result.output
    assert not HEBREW.search(result.output), result.output
    assert "linked" in result.output
    assert "agency connect facebook" in result.output  # Meta token still needed
    assert "agency connect youtube" not in result.output  # Zernio came from the studio
    from agency.config import load_settings
    s = load_settings(project_dir / "config.yaml")
    assert s.get("studio.dir") == str(studio_dir)
    assert s.get("meta.ig_user_id") == "17841400000000001"


def test_import_scan_cli_names_only(project_dir, tmp_path):
    other = tmp_path / "old"
    other.mkdir()
    (other / ".env").write_text("TELEGRAM_BOT_TOKEN=777:SECRET_tg_value\n")
    result = run("import", str(other), "--yes")
    assert result.exit_code == 0, result.output
    assert "TELEGRAM_BOT_TOKEN" in result.output
    assert "SECRET_tg_value" not in result.output
    assert (project_dir / ".env").read_text(encoding="utf-8").count("SECRET_tg_value") == 1


def test_platforms_shows_routes(project_dir):
    result = run("platforms")
    assert result.exit_code == 0
    assert "zernio" in result.output and "direct" in result.output
