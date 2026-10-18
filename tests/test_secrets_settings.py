"""Secrets backend (env → keyring cs2-agency → read-only cs2studio alias) and the settings
writer (config.yaml deep-merge/validation/atomic/.bak, .env writer)."""
from __future__ import annotations

import os

import pytest
import yaml

from agency import config as cfg
from agency import settings_store as ss

SECRET = "zr_live_SUPERSECRET_value_123456"


# ----------------------------------------------------------------- secret()
def test_keyring_disabled_in_tests_and_missing_backend_is_none(project_dir):
    assert os.environ["AGENCY_KEYRING"] == "off"
    assert cfg.keyring_usable() is False
    assert cfg.secret("ZERNIO_API_KEY") is None
    assert cfg.secret_source("ZERNIO_API_KEY") is None


def test_resolution_order_env_then_keyring_then_studio(project_dir, fake_keyring, monkeypatch):
    fake_keyring.store[("cs2studio", "zernio")] = "from-studio"
    assert cfg.secret("ZERNIO_API_KEY") == "from-studio"
    assert cfg.secret_source("ZERNIO_API_KEY") == "studio"

    fake_keyring.store[("cs2-agency", "ZERNIO_API_KEY")] = "from-agency"
    cfg.clear_secret_cache()
    assert cfg.secret("ZERNIO_API_KEY") == "from-agency"
    assert cfg.secret_source("ZERNIO_API_KEY") == "keyring"

    monkeypatch.setenv("ZERNIO_API_KEY", "from-env")
    assert cfg.secret("ZERNIO_API_KEY") == "from-env"
    assert cfg.secret_source("ZERNIO_API_KEY") == "env"


@pytest.mark.parametrize("name,alias", sorted(cfg.STUDIO_ALIASES.items()))
def test_studio_alias_map(project_dir, fake_keyring, name, alias):
    fake_keyring.store[("cs2studio", alias)] = f"v-{alias}"
    assert cfg.secret(name) == f"v-{alias}"
    assert cfg.secret_source(name) == "studio"


def test_unaliased_names_never_read_from_studio(project_dir, fake_keyring):
    fake_keyring.store[("cs2studio", "claude_token")] = "nope"
    fake_keyring.store[("cs2studio", "ANTHROPIC_API_KEY")] = "nope"
    assert cfg.secret("ANTHROPIC_API_KEY") is None


def test_broken_keyring_never_crashes(project_dir, fake_keyring, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("dbus is on fire")

    monkeypatch.setattr(fake_keyring, "get_password", boom)
    cfg.clear_secret_cache()
    assert cfg.secret("ZERNIO_API_KEY") is None
    assert cfg.secret_source("TELEGRAM_BOT_TOKEN") is None


def test_keyring_import_failure_is_none(project_dir, monkeypatch):
    import builtins

    monkeypatch.delenv("AGENCY_KEYRING")
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "keyring":
            raise ImportError("no keyring")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    cfg.clear_secret_cache()
    assert cfg.keyring_usable() is False
    assert cfg.secret("ZERNIO_API_KEY") is None


# ----------------------------------------------------------------- store / delete
def test_store_secret_uses_keyring_and_clears_stale_env(project_dir, fake_keyring):
    ss.env_set(project_dir, {"ZERNIO_API_KEY": "old-value-in-env"})
    assert os.environ["ZERNIO_API_KEY"] == "old-value-in-env"
    where = cfg.store_secret("ZERNIO_API_KEY", SECRET, project_dir)
    assert where == "keyring"
    assert fake_keyring.store[("cs2-agency", "ZERNIO_API_KEY")] == SECRET
    assert ("cs2studio", "zernio") not in fake_keyring.store  # never writes the studio
    assert "ZERNIO_API_KEY" not in os.environ
    assert "old-value-in-env" not in (project_dir / ".env").read_text(encoding="utf-8")
    assert cfg.secret("ZERNIO_API_KEY") == SECRET
    assert cfg.secret_source("ZERNIO_API_KEY") == "keyring"


def test_store_secret_falls_back_to_env_file(project_dir):
    assert cfg.store_secret("TELEGRAM_BOT_TOKEN", "123:abc-def", project_dir) == "env"
    assert "TELEGRAM_BOT_TOKEN='123:abc-def'" in (project_dir / ".env").read_text("utf-8")
    assert cfg.secret_source("TELEGRAM_BOT_TOKEN") == "env"


def test_store_secret_rejects_unknown_names_and_newlines(project_dir, fake_keyring):
    with pytest.raises(ss.SettingsError):
        cfg.store_secret("PATH", "x", project_dir)
    with pytest.raises(ss.SettingsError) as exc:
        cfg.store_secret("ZERNIO_API_KEY", "abc\ndef", project_dir)
    assert "abc" not in str(exc.value)
    with pytest.raises(ss.SettingsError):
        cfg.store_secret("ZERNIO_API_KEY", "abc\x00def", project_dir)
    assert fake_keyring.store == {}


def test_delete_secret_only_ours(project_dir, fake_keyring, monkeypatch):
    fake_keyring.store[("cs2studio", "zernio")] = "studio-key"
    fake_keyring.store[("cs2-agency", "ZERNIO_API_KEY")] = "agency-key"
    ss.env_set(project_dir, {"ZERNIO_API_KEY": "env-key"})
    removed = cfg.delete_secret("ZERNIO_API_KEY", project_dir)
    assert set(removed) == {"keyring", "env"}
    assert fake_keyring.store == {("cs2studio", "zernio"): "studio-key"}
    assert cfg.secret_source("ZERNIO_API_KEY") == "studio"


# ----------------------------------------------------------------- settings writer
def test_update_config_deep_merge_atomic_backup(settings, project_dir):
    before = (project_dir / "config.yaml").read_text(encoding="utf-8")
    ss.update_config(settings, {"channel": {"name": "Imagination"},
                                "publishing": {"platforms": {"tiktok": {"live": True,
                                                                        "via": "zernio"}}}})
    data = yaml.safe_load((project_dir / "config.yaml").read_text(encoding="utf-8"))
    assert data["channel"]["name"] == "Imagination"
    assert data["channel"]["language"] == "he"  # untouched siblings survive
    assert data["publishing"]["platforms"]["tiktok"] == {"enabled": True, "live": True,
                                                         "via": "zernio"}
    assert (project_dir / "config.yaml.bak").read_text(encoding="utf-8") == before
    assert settings.get("channel.name") == "Imagination"  # same object updated in place
    assert settings.platform_live("tiktok") is True
    assert not list(project_dir.glob(".config.yaml.*.tmp"))


@pytest.mark.parametrize("patch", [
    {"dry_run": "false"},                                   # wrong type
    {"finance": {"monthly_budget_ils": -5}},                 # negative
    {"publishing": {"platforms": {"tiktok": {"via": "carrier-pigeon"}}}},
    {"publishing": {"platforms": {"myspace": {"live": True}}}},
    {"nonsense": {"key": 1}},                                # unknown key
    {"zernio": {"api_key": "abc"}},                          # secret-looking key name
    {"channel": {"name": "https://discord.com/api/webhooks/1/abc"}},
    {"meta": {"page_id": "12ab"}},
    {"publishing": {"default_times": {"tiktok": "25:99"}}},
])
def test_update_config_rejects_bad_patches(settings, project_dir, patch):
    before = (project_dir / "config.yaml").read_text(encoding="utf-8")
    with pytest.raises(ss.SettingsError):
        ss.update_config(settings, patch)
    assert (project_dir / "config.yaml").read_text(encoding="utf-8") == before


def test_update_config_refuses_known_secret_values(settings, monkeypatch):
    monkeypatch.setenv("ZERNIO_API_KEY", SECRET)
    with pytest.raises(ss.SettingsError) as exc:
        ss.update_config(settings, {"brand": {"bio": f"my key {SECRET}"}})
    assert SECRET not in str(exc.value)


def test_update_config_accepts_v2_keys(settings):
    ss.update_config(settings, {
        "meta": {"page_id": "102030405060708", "ig_user_id": "17841400000000001"},
        "studio": {"dir": "C:\\CS2_STUDIO", "double_post_guard": True},
        "inbox": {"enabled": False, "folders": ["C:\\Users\\dorga\\Desktop\\CS2_CLIPS"]},
        "brand": {"hashtags": ["#CS2"], "blocked_words": ["aimbot"], "ai_label": True,
                  "style_notes": "funny\ntrolling"},
        "notify": {"telegram": {"enabled": False, "chat_id": -1001234}},
        "publishing": {"default_times": {"youtube_shorts": "17:00", "tiktok": "20:40"}},
    })
    assert settings.get("meta.ig_user_id") == "17841400000000001"
    assert settings.get("notify.telegram.chat_id") == "-1001234"
    assert settings.get("publishing.default_times.tiktok") == "20:40"


def test_meta_id_equal_to_env_id_is_not_treated_as_secret(settings, monkeypatch):
    monkeypatch.setenv("IG_USER_ID", "17841400000000001")
    ss.update_config(settings, {"meta": {"ig_user_id": "17841400000000001"}})


def test_allowed_env_names(project_dir):
    names = ss.allowed_env_names()
    for name in ("ANTHROPIC_API_KEY", "ZERNIO_API_KEY", "TELEGRAM_BOT_TOKEN",
                 "META_ACCESS_TOKEN", "DISCORD_WEBHOOK_URL", "X_API_KEY"):
        assert name in names
    assert "PATH" not in names


# ----------------------------------------------------------------- .env writer
def test_env_set_preserves_comments_order_and_quotes(project_dir):
    env = project_dir / ".env"
    env.write_text("# my comment\nANTHROPIC_API_KEY=\n# keep me too\nOTHER_THING=1\n"
                   "DISCORD_WEBHOOK_URL=old\n", encoding="utf-8")
    tricky = "it's a \\ value with 'quotes' and $HOME"
    ss.env_set(project_dir, {"ANTHROPIC_API_KEY": tricky, "ZERNIO_API_KEY": "zz"})
    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# my comment" and lines[2] == "# keep me too"
    assert lines[3] == "OTHER_THING=1" and lines[4] == "DISCORD_WEBHOOK_URL=old"
    assert lines[-1].startswith("ZERNIO_API_KEY=")
    from dotenv import dotenv_values
    values = dotenv_values(env)
    assert values["ANTHROPIC_API_KEY"] == tricky
    assert values["ZERNIO_API_KEY"] == "zz"
    assert os.environ["ANTHROPIC_API_KEY"] == tricky


def test_env_set_rejects_unknown_and_bad_values(project_dir):
    for mapping in ({"PATH": "x"}, {"ZERNIO_API_KEY": "a\nb"}, {"ZERNIO_API_KEY": "a\x00"},
                    {"ZERNIO_API_KEY": 5}, {"ZERNIO_API_KEY": "  "}):
        with pytest.raises(ss.SettingsError) as exc:
            ss.env_set(project_dir, mapping)
        assert "a\nb" not in str(exc.value)
    assert not (project_dir / ".env").exists()


def test_env_unset_blanks_and_updates_environ(project_dir):
    ss.env_set(project_dir, {"TELEGRAM_BOT_TOKEN": "123:abc"})
    assert ss.env_file_has(project_dir, "TELEGRAM_BOT_TOKEN")
    ss.env_unset(project_dir, ["TELEGRAM_BOT_TOKEN"])
    assert not ss.env_file_has(project_dir, "TELEGRAM_BOT_TOKEN")
    assert "TELEGRAM_BOT_TOKEN" not in os.environ
    assert "TELEGRAM_BOT_TOKEN=" in (project_dir / ".env").read_text(encoding="utf-8")


def test_secrets_dir_private(settings):
    path = ss.secrets_dir(settings)
    assert path == settings.data_dir / "secrets" and path.is_dir()
    if os.name != "nt":
        assert (path.stat().st_mode & 0o777) == 0o700


def test_set_platform_live_requires_connection(settings, monkeypatch):
    with pytest.raises(ss.SettingsError):
        ss.set_platform_live(settings, "discord", True)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/x")
    ss.set_platform_live(settings, "discord", True)
    assert settings.platform_live("discord") is True
    ss.set_platform_live(settings, "discord", False)
    assert settings.platform_live("discord") is False
    with pytest.raises(ss.SettingsError):
        ss.set_platform_live(settings, "myspace", True)
