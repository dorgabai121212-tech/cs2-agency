"""Inbox folder watcher (SPEC_V2 §11.12): a finished clip dropped in a folder becomes a video.

OFF until the owner enables it (``inbox.enabled``). Folders: ``inbox.folders``, else the
studio's clip folder ``<home>\\Desktop\\CS2_CLIPS`` when it exists, else ``data/inbox``.

- When the watcher is enabled (first time, and again after it was switched off), the clips
  already in the folder are recorded as a BASELINE and not processed; the dashboard lists them
  ("קליפים קודמים") and the owner can pick one with "תכין לפרסום" (:func:`prepare_clip`).
  A baseline clip that is renamed or copied inside the folder is still that baseline clip
  (same size + modification time, or the same SHA-256): it is never processed on its own.
- A new clip is processed only after its size and mtime stayed the same for
  ``inbox.stable_seconds`` (still being written / copied otherwise).
- Clips already known by SHA-256 are ignored (same clip renamed / copied twice).
- Processing copies the clip into ``data/videos`` (the owner's file is never moved or changed)
  and runs the normal pipeline, which ends with an approval — nothing is published here.

State lives in the DB kv key ``inbox`` (paths, sizes, hashes; no secrets).
"""
from __future__ import annotations

import logging
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("agency.inbox")

KV_KEY = "inbox"
VIDEO_EXTENSIONS = (".mp4", ".mov", ".mkv", ".webm", ".avi")
PARTIAL_SUFFIXES = (".part", ".tmp", ".crdownload", ".download", ".partial")
DEFAULT_STABLE_S = 10.0
DEFAULT_INTERVAL_S = 15.0
MAX_NOTE_BYTES = 4096

_lock = threading.Lock()


# ----------------------------------------------------------------------------- folders
def studio_clips_folder() -> Path:
    """Where the owner's finished clips land today (``Desktop\\CS2_CLIPS``)."""
    return Path.home() / "Desktop" / "CS2_CLIPS"


def inbox_folders(settings) -> list[Path]:
    """Watched folders: config ``inbox.folders`` → the studio clip folder → ``data/inbox``."""
    configured = [Path(str(p)).expanduser() for p in (settings.get("inbox.folders") or [])
                  if str(p or "").strip()]
    if configured:
        return configured
    clips = studio_clips_folder()
    try:
        if clips.is_dir():
            return [clips]
    except OSError:
        pass
    path = settings.data_dir / "inbox"
    path.mkdir(parents=True, exist_ok=True)
    return [path]


def is_enabled(settings) -> bool:
    return settings.get("inbox.enabled", False) is True


def _number(settings, dotted: str, default: float) -> float:
    try:
        return max(0.0, float(settings.get(dotted, default)))
    except (TypeError, ValueError):
        return default


def _is_clip(path: Path) -> bool:
    name = path.name.lower()
    return (name.endswith(VIDEO_EXTENSIONS) and not name.startswith((".", "~"))
            and not name.endswith(PARTIAL_SUFFIXES))


def _list_clips(folder: Path) -> list[Path]:
    try:
        return sorted(p for p in folder.iterdir() if p.is_file() and _is_clip(p))
    except OSError:
        return []


def _key(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


# ----------------------------------------------------------------------------- state
def _empty_state() -> dict:
    return {"baseline": {}, "known": {}, "pending": {}, "done": {}, "folders": []}


def _load(db) -> dict:
    data = db.kv_get(KV_KEY, None)
    state = _empty_state()
    if isinstance(data, dict):
        for k in state:
            if isinstance(data.get(k), type(state[k])):
                state[k] = data[k]
    return state


def _save(db, state: dict) -> None:
    db.kv_set(KV_KEY, state)


def _stat(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


def record_baseline(ctx, folders: list[Path] | None = None, *, again: bool = False) -> int:
    """Record the clips already present as the baseline (not processed). Returns how many.

    ``again``: also for folders seen before (the watcher is switched back on: whatever was
    dropped while it was off is a previous clip too, not new work)."""
    folders = folders or inbox_folders(ctx.settings)
    added = 0
    with _lock:
        state = _load(ctx.db)
        for folder in folders:
            fkey = _key(folder)
            if fkey in state["folders"] and not again:
                continue
            if fkey not in state["folders"]:
                state["folders"].append(fkey)
            for clip in _list_clips(folder):
                st = _stat(clip)
                ck = _key(clip)
                if st and ck not in state["baseline"] and ck not in state["done"]:
                    state["baseline"][ck] = {"name": clip.name, "size": st[0],
                                             "mtime_ns": st[1], "folder": fkey}
                    added += 1
        _save(ctx.db, state)
    return added


def set_enabled(ctx, on: bool) -> dict:
    """Owner switch. Turning ON records the baseline first, so old clips are not processed."""
    from .settings_store import update_config

    if on:
        record_baseline(ctx, again=not is_enabled(ctx.settings))
        with _lock:  # clips that were settling when it was switched off are baseline now
            state = _load(ctx.db)
            state["pending"] = {}
            _save(ctx.db, state)
    update_config(ctx.settings, {"inbox": {"enabled": bool(on)}})
    return inbox_status(ctx)


def _known_sha(state: dict, sha: str) -> bool:
    return sha in state["known"]


def _baseline_twin(state: dict, sha: str, st: tuple[int, int] | None) -> str | None:
    """The baseline entry this clip is a renamed / copied twin of (same size + mtime, or a
    recorded SHA-256), else None."""
    for key, entry in state["baseline"].items():
        if not isinstance(entry, dict):
            continue
        if entry.get("sha256") == sha:
            return key
        if st is not None and entry.get("size") == st[0] and entry.get("mtime_ns") == st[1]:
            return key
    return None


def _hash_same_size_baseline(state: dict, size: int) -> dict[str, str]:
    """SHA-256 of baseline clips that still exist and have this size (the rare copy that
    got a new modification time); computed outside the lock by the caller."""
    from .importer import file_sha256

    out: dict[str, str] = {}
    for key, entry in list(state["baseline"].items()):
        if isinstance(entry, dict) and not entry.get("sha256") and entry.get("size") == size:
            sha = file_sha256(Path(key))
            if sha:
                out[key] = sha
    return out


def _title_hint(clip: Path) -> str:
    """A sidecar ``<name>.txt`` note (studio habit) or the file name made readable."""
    note = clip.with_suffix(".txt")
    try:
        if note.is_file() and note.stat().st_size <= MAX_NOTE_BYTES:
            text = note.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                return text[:500]
    except OSError:
        pass
    return re.sub(r"[_\-.]+", " ", clip.stem).strip()[:200]


def _safe_name(name: str) -> str:
    try:
        from .dashboard.app import sanitize_filename
        return sanitize_filename(name)
    except Exception:
        stem = re.sub(r"[^\w\-]+", "_", Path(name).stem).strip("_")[:120] or "clip"
        return f"{stem}{Path(name).suffix.lower() or '.mp4'}"


def _process(ctx, clip: Path, sha: str) -> dict:
    """Copy into data/videos and run the pipeline (ends with an owner approval)."""
    from .models import VideoStatus
    from .staff.manager import Manager

    dest_dir = ctx.settings.data_dir / "videos"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{_safe_name(clip.name)}"
    try:
        shutil.copy2(clip, dest)
    except OSError as exc:  # disk full / file vanished: report, never crash the watcher
        log.warning("inbox copy failed: %s", type(exc).__name__)
        return {"ok": False, "error": "לא הצלחתי להעתיק את הקליפ", "video_id": None}
    title = _title_hint(clip)
    try:
        result = Manager(ctx).handle_new_video(dest, title)
    except Exception as exc:  # the manager already guards steps; belt and braces
        log.exception("inbox pipeline crashed")
        return {"ok": False, "error": type(exc).__name__, "video_id": None}
    vid = result.get("video_id")
    if vid:
        try:
            ctx.db.update_video(vid, meta={"source": "inbox", "inbox_path": str(clip),
                                           "sha256": sha})
            if not result.get("ok"):
                ctx.db.update_video(vid, status=VideoStatus.FAILED)
        except Exception:  # pragma: no cover
            pass
    return {"ok": bool(result.get("ok")), "video_id": vid,
            "approval_id": result.get("approval_id"), "error": result.get("error")}


def scan_once(ctx, now: float | None = None) -> list[dict]:
    """One pass over the watched folders. Returns what happened per clip (no secrets)::

        [{"path", "action": "baseline"|"waiting"|"duplicate"|"processed"|"failed", ...}]

    Does nothing while the watcher is disabled.
    """
    from .events import emit
    from .importer import file_sha256

    if not is_enabled(ctx.settings):
        return []
    now = time.time() if now is None else now
    stable_s = _number(ctx.settings, "inbox.stable_seconds", DEFAULT_STABLE_S)
    folders = inbox_folders(ctx.settings)
    with _lock:
        state = _load(ctx.db)
        new_folders = [f for f in folders if _key(f) not in state["folders"]]
    if new_folders:
        record_baseline(ctx, new_folders)
    actions: list[dict] = []
    ready: list[tuple[Path, str]] = []
    with _lock:
        state = _load(ctx.db)
        present: set[str] = set()
        for folder in folders:
            for clip in _list_clips(folder):
                ck = _key(clip)
                present.add(ck)
                if ck in state["baseline"] or ck in state["done"]:
                    continue
                st = _stat(clip)
                if st is None:
                    continue
                seen = state["pending"].get(ck)
                if not seen or seen.get("size") != st[0] or seen.get("mtime_ns") != st[1]:
                    state["pending"][ck] = {"size": st[0], "mtime_ns": st[1], "since": now,
                                            "name": clip.name}
                    actions.append({"path": ck, "action": "waiting"})
                elif now - float(seen.get("since", now)) >= stable_s:
                    ready.append((clip, ck))
                else:
                    actions.append({"path": ck, "action": "waiting"})
        for ck in [k for k in state["pending"] if k not in present]:
            state["pending"].pop(ck, None)  # deleted / moved away before it settled
        _save(ctx.db, state)

    for clip, ck in ready:
        sha = file_sha256(clip)
        if sha is None:
            continue
        st = _stat(clip)
        with _lock:
            state = _load(ctx.db)
        hashed = _hash_same_size_baseline(state, st[0]) if st else {}
        with _lock:
            state = _load(ctx.db)
            for key, digest in hashed.items():
                if isinstance(state["baseline"].get(key), dict):
                    state["baseline"][key]["sha256"] = digest
            state["pending"].pop(ck, None)
            twin = _baseline_twin(state, sha, st)
            if twin is not None:
                entry = {**state["baseline"][twin], "sha256": sha}
                if twin != ck and Path(twin).is_file():
                    # a copy of a previous clip: same clip, never processed on its own
                    state["done"][ck] = {"name": clip.name, "sha256": sha,
                                         "action": "duplicate", "video_id": None, "ts": now}
                    actions.append({"path": ck, "action": "duplicate"})
                else:  # renamed: it stays a previous clip, under its new name
                    state["baseline"].pop(twin, None)
                    state["baseline"][ck] = {**entry, "name": clip.name, "size": st[0] if st
                                             else entry.get("size"),
                                             "mtime_ns": st[1] if st else entry.get("mtime_ns")}
                    actions.append({"path": ck, "action": "baseline"})
                _save(ctx.db, state)
                continue
            if _known_sha(state, sha):
                state["done"][ck] = {"name": clip.name, "sha256": sha, "action": "duplicate",
                                     "video_id": state["known"][sha].get("video_id"),
                                     "ts": now}
                _save(ctx.db, state)
                actions.append({"path": ck, "action": "duplicate"})
                continue
            state["known"][sha] = {"name": clip.name, "video_id": None, "ts": now}
            state["done"][ck] = {"name": clip.name, "sha256": sha, "action": "processing",
                                 "video_id": None, "ts": now}
            _save(ctx.db, state)
        emit(ctx.db, "manager", f"קליפ חדש בתיקייה: {clip.name} — הצוות מתחיל לעבוד עליו")
        result = _process(ctx, clip, sha)
        with _lock:
            state = _load(ctx.db)
            action = "processed" if result["ok"] else "failed"
            state["known"].setdefault(sha, {})["video_id"] = result["video_id"]
            state["done"][ck] = {"name": clip.name, "sha256": sha, "action": action,
                                 "video_id": result["video_id"], "ts": now}
            _save(ctx.db, state)
        actions.append({"path": ck, "action": action, "video_id": result["video_id"],
                        "approval_id": result.get("approval_id")})
    return actions


def check_prepare(ctx, path: str | Path) -> str | None:
    """Why "תכין לפרסום" cannot run for this clip right now (Hebrew), or None when it can.
    The same early checks as :func:`prepare_clip`, so the dashboard can answer at once."""
    from .importer import file_sha256

    ck = _key(Path(path))
    with _lock:
        state = _load(ctx.db)
    if ck not in state["baseline"]:
        return "הקליפ הזה לא ברשימת הקליפים הקודמים"
    clip = Path(ck)
    if not clip.is_file():
        return "הקובץ כבר לא נמצא בתיקייה"
    sha = file_sha256(clip)
    if sha is None:
        return "אי אפשר לקרוא את הקובץ"
    if _known_sha(state, sha) and state["known"][sha].get("video_id"):
        return "הקליפ הזה כבר הוכן לפרסום"
    return None


def prepare_clip(ctx, path: str | Path) -> dict:
    """"תכין לפרסום" for a baseline clip. Only paths recorded in the baseline are accepted
    (the dashboard cannot make us read arbitrary files). Returns the pipeline summary."""
    from .importer import file_sha256

    ck = _key(Path(path))
    with _lock:
        state = _load(ctx.db)
        if ck not in state["baseline"]:
            return {"ok": False, "error": "הקליפ הזה לא ברשימת הקליפים הקודמים"}
    clip = Path(ck)
    if not clip.is_file():
        return {"ok": False, "error": "הקובץ כבר לא נמצא בתיקייה"}
    sha = file_sha256(clip)
    if sha is None:
        return {"ok": False, "error": "אי אפשר לקרוא את הקובץ"}
    with _lock:
        state = _load(ctx.db)
        if _known_sha(state, sha) and state["known"][sha].get("video_id"):
            return {"ok": False, "error": "הקליפ הזה כבר הוכן לפרסום",
                    "video_id": state["known"][sha]["video_id"]}
        state["baseline"].pop(ck, None)
        state["known"][sha] = {"name": clip.name, "video_id": None, "ts": time.time()}
        state["done"][ck] = {"name": clip.name, "sha256": sha, "action": "processing",
                             "video_id": None, "ts": time.time()}
        _save(ctx.db, state)
    result = _process(ctx, clip, sha)
    with _lock:
        state = _load(ctx.db)
        state["known"].setdefault(sha, {})["video_id"] = result["video_id"]
        state["done"][ck] = {"name": clip.name, "sha256": sha,
                             "action": "processed" if result["ok"] else "failed",
                             "video_id": result["video_id"], "ts": time.time()}
        _save(ctx.db, state)
    return result


def inbox_status(ctx) -> dict:
    """For the dashboard: ``{enabled, folders, baseline:[{name, path, size}], waiting:[...],
    done:[{name, path, action, video_id}]}``."""
    with _lock:
        state = _load(ctx.db)
    present_baseline = [{"name": v.get("name"), "path": k, "size": v.get("size")}
                        for k, v in state["baseline"].items() if Path(k).is_file()]
    return {"enabled": is_enabled(ctx.settings),
            "folders": [str(f) for f in inbox_folders(ctx.settings)],
            "baseline": sorted(present_baseline, key=lambda x: str(x["name"]).lower()),
            "waiting": [{"name": v.get("name"), "path": k} for k, v in state["pending"].items()],
            "done": [{"name": v.get("name"), "path": k, "action": v.get("action"),
                      "video_id": v.get("video_id")}
                     for k, v in sorted(state["done"].items(),
                                        key=lambda kv: -float(kv[1].get("ts", 0)))][:50]}


def run_forever(ctx, stop_event: threading.Event) -> None:
    """Loop: scan while enabled (re-read every pass, so the dashboard switch acts live)."""
    while not stop_event.is_set():
        try:
            if is_enabled(ctx.settings):
                scan_once(ctx)
        except Exception:  # never kill the thread
            log.exception("inbox scan failed")
        stop_event.wait(max(2.0, _number(ctx.settings, "inbox.interval_s",
                                         DEFAULT_INTERVAL_S)))


_watcher: dict[str, threading.Thread | None] = {"thread": None}


def start_inbox_watcher(ctx, stop_event: threading.Event) -> threading.Thread:
    """Scheduler/dashboard hook: start the watcher thread (it idles while disabled).

    Idempotent per process: a second call while one is running returns the running thread.
    """
    with _lock:
        running = _watcher["thread"]
        if running is not None and running.is_alive():
            return running
        thread = threading.Thread(target=run_forever, args=(ctx, stop_event), daemon=True,
                                  name="inbox-watcher")
        _watcher["thread"] = thread
        thread.start()
    return thread


__all__ = ["check_prepare", "inbox_folders", "inbox_status", "prepare_clip", "record_baseline",
           "run_forever", "scan_once", "set_enabled", "start_inbox_watcher"]
