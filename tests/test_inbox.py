"""Inbox folder watcher: OFF by default, baseline, stable-size check, sha256 dedupe."""
from __future__ import annotations

import threading

import pytest

from agency import inbox


@pytest.fixture
def folder(ctx, tmp_path):
    from agency.settings_store import update_config

    path = tmp_path / "CS2_CLIPS"
    path.mkdir()
    update_config(ctx.settings, {"inbox": {"enabled": False, "folders": [str(path)],
                                           "stable_seconds": 5, "interval_s": 15}})
    return path


@pytest.fixture
def handled(monkeypatch):
    """Replace the real pipeline (tested elsewhere) with a recorder."""
    from agency.staff.manager import Manager

    calls: list[tuple[str, str]] = []

    def fake(self, path, title_hint=""):
        calls.append((str(path), title_hint))
        vid = self.db.add_video(str(path), title_hint)
        return {"ok": True, "video_id": vid, "approval_id": 42}

    monkeypatch.setattr(Manager, "handle_new_video", fake)
    return calls


def test_off_by_default(ctx, folder, handled):
    assert ctx.settings.get("inbox.enabled") is False
    (folder / "a.mp4").write_bytes(b"x" * 100)
    assert inbox.scan_once(ctx, now=0) == [] and inbox.scan_once(ctx, now=100) == []
    assert handled == []


def test_default_folder_is_data_inbox(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(inbox, "studio_clips_folder", lambda: tmp_path / "nope")
    assert inbox.inbox_folders(settings) == [settings.data_dir / "inbox"]
    clips = tmp_path / "Desktop" / "CS2_CLIPS"
    clips.mkdir(parents=True)
    monkeypatch.setattr(inbox, "studio_clips_folder", lambda: clips)
    assert inbox.inbox_folders(settings) == [clips]


def test_baseline_then_new_clip_after_stable_size(ctx, folder, handled):
    (folder / "old_clip.mp4").write_bytes(b"old" * 100)
    status = inbox.set_enabled(ctx, True)
    assert status["enabled"] and [b["name"] for b in status["baseline"]] == ["old_clip.mp4"]

    new = folder / "SHORT_EP4_ACE.mp4"
    new.write_bytes(b"new" * 100)
    (folder / "SHORT_EP4_ACE.txt").write_text("ace on mirage with the deagle")
    (folder / "partial.mp4.part").write_bytes(b"x")
    assert inbox.scan_once(ctx, now=1000)[0]["action"] == "waiting"
    assert inbox.scan_once(ctx, now=1002)[0]["action"] == "waiting"  # not stable long enough
    with new.open("ab") as fh:  # still being written: size changed → wait again
        fh.write(b"more")
    assert inbox.scan_once(ctx, now=1010)[0]["action"] == "waiting"
    done = inbox.scan_once(ctx, now=1020)
    assert [a["action"] for a in done] == ["processed"]
    assert len(handled) == 1
    dest, title = handled[0]
    assert dest.startswith(str(ctx.settings.data_dir / "videos")) and title.startswith("ace on")
    assert new.exists()  # the owner's file is never moved
    video = ctx.db.get_video(done[0]["video_id"])
    assert video.meta["source"] == "inbox" and len(video.meta["sha256"]) == 64
    assert inbox.scan_once(ctx, now=2000) == []  # nothing processed twice


def test_duplicate_sha_is_ignored(ctx, folder, handled):
    inbox.set_enabled(ctx, True)
    (folder / "one.mp4").write_bytes(b"same-bytes" * 50)
    inbox.scan_once(ctx, now=0)
    inbox.scan_once(ctx, now=10)
    (folder / "one_copy.mp4").write_bytes(b"same-bytes" * 50)
    inbox.scan_once(ctx, now=20)
    assert [a["action"] for a in inbox.scan_once(ctx, now=30)] == ["duplicate"]
    assert len(handled) == 1


def test_prepare_baseline_clip_only(ctx, folder, handled, tmp_path):
    old = folder / "before.mp4"
    old.write_bytes(b"b" * 64)
    inbox.set_enabled(ctx, True)
    other = tmp_path / "secret_place.mp4"
    other.write_bytes(b"o")
    assert inbox.prepare_clip(ctx, other)["ok"] is False  # not in the baseline
    result = inbox.prepare_clip(ctx, old)
    assert result["ok"] and result["video_id"]
    assert inbox.prepare_clip(ctx, old)["ok"] is False  # already prepared
    status = inbox.inbox_status(ctx)
    assert status["baseline"] == [] and status["done"][0]["action"] == "processed"


def test_watcher_thread_idempotent_and_stops(ctx, folder):
    stop = threading.Event()
    t1 = inbox.start_inbox_watcher(ctx, stop)
    t2 = inbox.start_inbox_watcher(ctx, stop)
    assert t1 is t2 and t1.is_alive()
    stop.set()
    t1.join(timeout=5)
    assert not t1.is_alive()
