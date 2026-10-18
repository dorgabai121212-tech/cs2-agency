"""Importer: the old studio (read-only DB, keyring names, ids, brand, times, history, daemon)
and the generic scan (Google files, tokens, pickles, .env). Values are never printed."""
from __future__ import annotations

import json
import os
import pickle
import sqlite3
import time

import pytest

from agency import importer

SECRET = "SECRET_value_from_an_old_env_9999"


# ----------------------------------------------------------------- studio
def test_find_studio_and_read_only_db(settings, studio_dir):
    assert importer.find_studio_dir(studio_dir) == studio_dir
    assert importer.find_studio_dir(studio_dir / "logs") is None
    with importer.open_studio_db(importer.studio_db_path(studio_dir)) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO kv VALUES ('x', 'y')")


def test_meta_ids_brand_times(studio_dir, capfd):
    assert importer.read_meta_ids(studio_dir) == {"page_id": "102030405060708",
                                                  "ig_user_id": "17841400000000001"}
    brand = importer.read_brand(studio_dir)
    assert brand["name"] == "Imagination"
    assert brand["bio"] == "Legendary CS2 moments. Real demo. Shot like a movie."
    assert brand["hashtags"] == ["#CS2", "#CounterStrike2", "#CS2Edit"]
    for word in ("case opening", "free skins", "aimbot", "skins gambling", "betting"):
        assert word in brand["blocked_words"]
    assert "should-never-run" not in capfd.readouterr().out  # parsed with ast, never run
    assert importer.read_posting_times(studio_dir) == {
        "youtube": "16:00", "facebook": "17:40", "youtube_shorts": "17:00",
        "instagram_reels": "20:00", "tiktok": "20:40"}


def test_keyring_entry_names_only(project_dir, fake_keyring):
    fake_keyring.store[("cs2studio", "zernio")] = "zz"
    fake_keyring.store[("cs2studio", "telegram_bot")] = "tt"
    assert importer.studio_keyring_entries() == ["zernio", "telegram_bot"]


def test_import_studio_links_config_and_history(settings, studio_dir, fake_keyring, db):
    fake_keyring.store[("cs2studio", "zernio")] = "zernio-secret-value"
    fake_keyring.store[("cs2studio", "meta_page_token")] = "meta-secret-value"
    db_file = importer.studio_db_path(studio_dir)
    before = db_file.read_bytes()
    report = importer.import_studio(settings, studio_dir, db=db)
    assert db_file.read_bytes() == before  # read-only
    assert settings.get("studio.dir") == str(studio_dir)
    assert settings.get("meta.page_id") == "102030405060708"
    assert settings.get("meta.ig_user_id") == "17841400000000001"
    assert settings.get("brand.name") == "Imagination"
    assert "#CS2Edit" in settings.get("brand.hashtags")
    assert settings.get("publishing.default_times.tiktok") == "20:40"
    assert set(report["linked"]) >= {"youtube", "youtube_shorts", "tiktok", "instagram_reels",
                                     "facebook"}
    assert report["keyring"] == ["zernio", "meta_page_token"]
    assert report["history"] == {"imported": 1, "skipped": 0}
    # no secret was copied anywhere
    assert set(fake_keyring.store) == {("cs2studio", "zernio"), ("cs2studio", "meta_page_token")}
    text = (settings.root / "config.yaml").read_text(encoding="utf-8")
    assert "secret-value" not in text and "secret-value" not in json.dumps(report)
    posts = db.list_posts()
    assert len(posts) == 1 and posts[0]["status"] == "published"
    assert posts[0]["plan"]["extra"]["source"] == "studio"
    assert posts[0]["url"] == "https://www.youtube.com/shorts/w-rEUe6qMN8"
    assert posts[0]["created_at"].startswith("2025-10-09")  # the studio's real time
    video = db.get_video(posts[0]["video_id"])
    assert video.meta["source"] == "studio" and video.title_hint == "SHORT_EP3_MOZART_PHONK"
    again = importer.import_studio(settings, studio_dir, db=db)
    assert again["history"] == {"imported": 0, "skipped": 1}


def test_import_studio_keeps_owner_brand_text(settings, studio_dir, db):
    from agency.settings_store import update_config

    update_config(settings, {"brand": {"bio": "my own bio", "hashtags": ["#Mine"]}})
    importer.import_studio(settings, studio_dir, history=False, db=db)
    assert settings.get("brand.bio") == "my own bio"
    assert settings.get("brand.hashtags")[0] == "#Mine"
    assert "#CS2" in settings.get("brand.hashtags")


def test_import_studio_empty_keyring_is_positive_and_leads_to_wizards(settings, studio_dir,
                                                                      db, fake_keyring):
    # HANDOFF: on the new PC the cs2studio keyring is empty; the accounts are still connected
    # on the platforms' side, so the card says so and opens Zernio → Meta → Telegram.
    report = importer.import_studio(settings, studio_dir, history=False, db=db)
    assert report["keyring"] == [] and report["problems"] == []
    assert report["message_he"] == importer.STUDIO_KEYS_MISSING_HE
    assert [s["name"] for s in report["next_connect"]] == [
        "ZERNIO_API_KEY", "META_ACCESS_TOKEN", "TELEGRAM_BOT_TOKEN"]
    assert report["next_connect"][0]["key"] == "youtube"
    fake_keyring.store[("cs2-agency", "ZERNIO_API_KEY")] = "zk"
    from agency.config import clear_secret_cache
    clear_secret_cache()
    again = importer.studio_report(settings, studio_dir)
    assert [s["name"] for s in again["next_connect"]] == ["META_ACCESS_TOKEN",
                                                          "TELEGRAM_BOT_TOKEN"]


def test_daemon_activity_from_log(studio_dir):
    log = studio_dir / "logs" / "daemon.log"
    assert importer.daemon_activity(studio_dir)["active"] is False
    log.write_text("tick\n")
    act = importer.daemon_activity(studio_dir)
    assert act["active"] and act["log_recent"] and act["question_he"]
    assert any("CS2Studio Daemon" in c and "/DISABLE" in c for c in act["disable_commands"])
    old = time.time() - 3600
    os.utime(log, (old, old))
    assert importer.daemon_activity(studio_dir)["active"] is False


def test_daemon_task_query_windows(monkeypatch, tmp_path):
    import subprocess

    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        xml = "<Task><Settings><Enabled>true</Enabled></Settings></Task>".encode("utf-16")
        return subprocess.CompletedProcess(args, 0, stdout=xml, stderr=b"")

    monkeypatch.setattr(importer, "IS_WINDOWS", True)
    monkeypatch.setattr(importer.subprocess, "run", fake_run)
    act = importer.daemon_activity(tmp_path)
    assert act["active"] and act["task_found"] and act["task_enabled"]
    assert calls[0][:3] == ["schtasks", "/Query", "/TN"] and "/XML" in calls[0]


# ----------------------------------------------------------------- generic scan
@pytest.fixture
def scan_tree(tmp_path):
    root = tmp_path / "old-project"
    (root / "deep" / "er").mkdir(parents=True)
    (root / "node_modules" / "x").mkdir(parents=True)
    (root / "client_secret_123.json").write_text(json.dumps(
        {"installed": {"client_id": "cid.apps", "client_secret": "GOCSPX-abc"}}))
    (root / "deep" / "token.json").write_text(json.dumps({
        "refresh_token": "1//refresh", "client_id": "cid.apps", "client_secret": "GOCSPX-abc",
        "scopes": ["https://www.googleapis.com/auth/youtube.upload"]}))
    (root / "deep" / "er" / "readonly_token.json").write_text(json.dumps({
        "refresh_token": "1//r2", "client_id": "cid", "client_secret": "s",
        "scopes": ["https://www.googleapis.com/auth/youtube.readonly"]}))
    marker = tmp_path / "PWNED"

    class Evil:
        def __reduce__(self):
            return (open, (str(marker), "w"))

    (root / "token.pickle").write_bytes(pickle.dumps(Evil()))
    (root / "deep" / ".env").write_text(
        f"# old\nDISCORD_WEBHOOK_URL={SECRET}\nUNRELATED=1\nZERNIO_API_KEY=\n")
    (root / "node_modules" / "x" / "client_secret.json").write_text(json.dumps(
        {"installed": {"client_id": "n", "client_secret": "n"}}))
    return root, marker


def test_scan_finds_and_validates(settings, scan_tree):
    root, marker = scan_tree
    found = importer.scan([root], settings, include_defaults=False)
    kinds = {(f.kind, os.path.basename(f.path)): f for f in found}
    assert ("google_client_secret", "client_secret_123.json") in kinds
    good = kinds[("google_token", "token.json")]
    assert good.ok
    bad = kinds[("google_token", "readonly_token.json")]
    assert not bad.ok and bad.problem == importer.RELOGIN_HE
    pick = kinds[("token_pickle", "token.pickle")]
    assert not pick.ok and importer.RELOGIN_HE in pick.problem
    env = kinds[("env_file", ".env")]
    assert env.names == ["DISCORD_WEBHOOK_URL"]
    assert not any("node_modules" in f.path for f in found)
    assert not marker.exists()  # the pickle was never unpickled
    assert SECRET not in json.dumps([f.to_dict() for f in found])


def test_scan_depth_limit(settings, tmp_path):
    deep = tmp_path / "a" / "b" / "c" / "d" / "e" / "f"
    deep.mkdir(parents=True)
    (deep / "client_secret.json").write_text(json.dumps(
        {"installed": {"client_id": "x", "client_secret": "y"}}))
    assert importer.scan([tmp_path / "a"], settings, include_defaults=False) == []


def test_import_found_copies_and_merges(settings, scan_tree, capsys):
    root, _marker = scan_tree
    found = [f for f in importer.scan([root], settings, include_defaults=False) if f.ok]
    results = importer.import_found(found, settings)
    secrets = settings.data_dir / "secrets"
    assert json.loads((secrets / "youtube_client_secret.json").read_text())["installed"]
    assert json.loads((secrets / "youtube_token.json").read_text())["refresh_token"]
    if os.name != "nt":
        assert (secrets / "youtube_token.json").stat().st_mode & 0o777 == 0o600
    env = next(r for r in results if r["kind"] == "env_file")
    assert env["imported"] == ["DISCORD_WEBHOOK_URL"]
    assert os.environ["DISCORD_WEBHOOK_URL"] == SECRET
    # a second import never overwrites existing values / files
    again = importer.import_found(found, settings)
    assert all(not r["imported"] for r in again)
    assert {n for r in again for n in r["skipped"]} >= {"DISCORD_WEBHOOK_URL",
                                                        "youtube_token.json"}
    out = capsys.readouterr()
    assert SECRET not in out.out + out.err
    assert SECRET not in json.dumps(results + again)


def test_import_found_revalidates_from_disk(settings, scan_tree):
    root, _ = scan_tree
    forged = {"kind": "google_client_secret", "path": str(root / "deep" / ".env")}
    result = importer.import_found([forged], settings)[0]
    assert result["problem"] and not result["imported"]
    assert not (settings.data_dir / "secrets" / "youtube_client_secret.json").exists()


def test_import_env_ids_go_to_config(settings, tmp_path):
    env = tmp_path / "proj" / ".env"
    env.parent.mkdir()
    env.write_text("IG_USER_ID=17841400000000009\nMETA_ACCESS_TOKEN=EAAsecret\n")
    found = importer.scan([env.parent], settings, include_defaults=False)
    importer.import_found(found, settings)
    assert settings.get("meta.ig_user_id") == "17841400000000009"
    assert "IG_USER_ID" not in os.environ
    assert os.environ["META_ACCESS_TOKEN"] == "EAAsecret"


def test_scan_reports_studio(settings, studio_dir):
    found = importer.scan([studio_dir.parent], settings, include_defaults=False)
    assert any(f.kind == "studio" and f.path == str(studio_dir) for f in found)
