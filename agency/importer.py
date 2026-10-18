"""Import existing connections (SPEC_V2 §5 + §11.4): the old studio first, then a generic scan.

The owner already runs a live studio (``C:\\CS2_STUDIO``). Its secrets stay where they are — the
Windows keyring service ``cs2studio`` — and :func:`agency.config.secret` reads them read-only,
so linking needs no copying. From the studio we import only NON-secret things: the folder
path, the Meta page / Instagram ids (``state\\studio.db`` table ``kv`` key ``meta_ids``), brand
rules, posting times and (optionally) the published-post history.

Everything here is read-only towards the studio: its DB is opened with ``mode=ro``, its
keyring service is never written, its scheduled tasks are only queried (never changed).
Secret VALUES are never printed, logged, returned or sent to the LLM — only names and paths.
Pickled tokens are never unpickled (that would run code); they are only reported.
"""
from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import (DEFAULT_STUDIO_DIR, STUDIO_ALIASES, STUDIO_KEYRING_NAMES,
                     STUDIO_KEYRING_SERVICE, Settings, _keyring_get, secret)

log = logging.getLogger("agency.importer")

# ----------------------------------------------------------------------------- constants
MAX_FILE_BYTES = 1024 * 1024
MAX_JSON_BYTES = 256 * 1024
MAX_DEPTH = 4
MAX_ENTRIES = 20_000  # directory entries visited per scan root
SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "env", "__pycache__", "site-packages",
             "appdata", ".cache", "$recycle.bin", "system volume information", ".tox",
             ".mypy_cache", ".pytest_cache", ".ruff_cache", "windows", "program files",
             "program files (x86)", "programdata", "perflogs", "recovery", "msocache",
             "$windows.~bt", "$windows.~ws", "onedrivetemp", "dist", "build"}
UPLOAD_SCOPES = ("https://www.googleapis.com/auth/youtube.upload",
                 "https://www.googleapis.com/auth/youtube",
                 "https://www.googleapis.com/auth/youtube.force-ssl")
DAEMON_TASKS = ("CS2Studio Daemon", "CS2Studio Watchdog")
DAEMON_LOG_RECENT_S = 10 * 60
IS_WINDOWS = os.name == "nt"
DAEMON_QUESTION_HE = "הסטודיו הישן עדיין פעיל — הסוכנות החדשה תחליף אותו?"
RELOGIN_HE = "צריך התחברות מחדש"

# Studio platform names -> our platform keys.
STUDIO_PLATFORM_MAP = {"youtube": "youtube", "youtube_shorts": "youtube_shorts",
                       "tiktok": "tiktok", "instagram": "instagram_reels",
                       "facebook": "facebook", "facebook_reels": "facebook"}
# Any name (ours or the studio's) -> the account it lands on (for "same platform" checks).
_BASE_PLATFORM = {"youtube_shorts": "youtube", "facebook_reels": "facebook",
                  "instagram_reels": "instagram"}
_DEAD_STATUSES = {"failed", "cancelled", "canceled", "superseded", "rejected", "expired",
                  "dry", "draft"}


def base_platform(name: str) -> str:
    """``youtube_shorts`` -> ``youtube``, ``instagram_reels`` -> ``instagram`` etc."""
    name = str(name or "").strip().lower()
    return _BASE_PLATFORM.get(name, name)


# ----------------------------------------------------------------------------- studio dir
def _is_studio_dir(path: Path) -> bool:
    try:
        return path.is_dir() and ((path / "state" / "studio.db").is_file()
                                  or (path / "studio").is_dir()
                                  or (path / "prompts" / "copywriter.md").is_file())
    except OSError:
        return False


def find_studio_dir(path: Path | str | None = None, settings: Settings | None = None
                    ) -> Path | None:
    """The studio folder: ``path`` if it is one, else the configured/default one, else None."""
    candidates: list[Path] = []
    if path:
        candidates.append(Path(path).expanduser())
    if settings is not None and settings.studio_dir is not None:
        candidates.append(settings.studio_dir)
    if os.name == "nt":
        candidates.append(Path(DEFAULT_STUDIO_DIR))
    for cand in candidates:
        if _is_studio_dir(cand):
            return cand
    return None


def studio_db_path(studio_dir: Path | str) -> Path:
    return Path(studio_dir) / "state" / "studio.db"


@contextmanager
def open_studio_db(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    """Read the studio's SQLite DB without touching its folder.

    Even a ``mode=ro`` connection to a WAL database creates ``-wal`` / ``-shm`` files next to
    it, and ``immutable=1`` would miss rows still in the WAL. So the DB and its ``-wal`` are
    COPIED into a private temporary folder, the copy is brought up to date there and opened
    read-only (``mode=ro``); the copy is deleted afterwards. Raises ``sqlite3.Error`` when it
    cannot be read; callers decide what that means (the double-post guard fails closed)."""
    import shutil
    import tempfile

    db_path = Path(db_path)
    if not db_path.is_file():
        raise sqlite3.OperationalError("studio db not found")
    tmp = Path(tempfile.mkdtemp(prefix="agency-studio-"))
    try:
        copy = tmp / "studio.db"
        try:
            shutil.copyfile(db_path, copy)
            wal = db_path.with_name(db_path.name + "-wal")
            if wal.is_file():
                shutil.copyfile(wal, tmp / "studio.db-wal")
        except OSError as exc:
            raise sqlite3.OperationalError(f"studio db not readable ({type(exc).__name__})") \
                from None
        fold = sqlite3.connect(str(copy), timeout=5)  # our private copy: apply its WAL
        try:
            fold.execute("PRAGMA journal_mode=DELETE").fetchall()
        finally:
            fold.close()
        conn = sqlite3.connect(copy.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
            conn.row_factory = sqlite3.Row
            yield conn
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _tables(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """{table: [columns]} via introspection (names come from sqlite_master, then quoted)."""
    out: dict[str, list[str]] = {}
    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        name = row[0]
        if not isinstance(name, str) or name.startswith("sqlite_"):
            continue
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{_q(name)}")').fetchall()]
        out[name] = cols
    return out


def _q(identifier: str) -> str:
    return str(identifier).replace('"', '""')


def _json(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def studio_kv(studio_dir: Path | str, key: str) -> Any:
    """One value from the studio's ``kv`` table (JSON-decoded), or None. Read-only."""
    try:
        with open_studio_db(studio_db_path(studio_dir)) as conn:
            tables = _tables(conn)
            cols = tables.get("kv") or []
            if "key" not in cols or "value" not in cols:
                return None
            row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    return _json(row[0]) if row is not None else None


_ID_RE = re.compile(r"^\d{5,40}$")


def read_meta_ids(studio_dir: Path | str) -> dict[str, str]:
    """``{"page_id": ..., "ig_user_id": ...}`` (digits only, ids are not secrets)."""
    data = studio_kv(studio_dir, "meta_ids")
    out: dict[str, str] = {}
    if not isinstance(data, dict):
        return out
    for ours, keys in (("page_id", ("page_id", "fb_page_id", "facebook_page_id")),
                       ("ig_user_id", ("ig_user_id", "ig_id", "instagram_user_id",
                                       "instagram_id"))):
        for k in keys:
            value = str(data.get(k) or "").strip()
            if _ID_RE.match(value):
                out[ours] = value
                break
    return out


def studio_keyring_entries() -> list[str]:
    """Names of entries that exist under the studio's keyring service (never values)."""
    return [name for name in STUDIO_KEYRING_NAMES
            if _keyring_get(STUDIO_KEYRING_SERVICE, name)]


# ----------------------------------------------------------------------------- brand & times
def _literal_assignments(path: Path, names: Iterable[str]) -> dict[str, Any]:
    """Top-level ``NAME = <literal>`` values of a Python file, parsed with ``ast`` (never run)."""
    wanted = set(names)
    out: dict[str, Any] = {}
    try:
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return out
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return out
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id in wanted:
            try:
                out[node.targets[0].id] = ast.literal_eval(node.value)
            except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                continue
    return out


def _clean_words(items: Iterable[Any], limit: int = 100) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in items:
        word = str(raw or "").strip().strip("\"'“”`").strip()
        if not word or len(word) > 60 or word.lower() in seen:
            continue
        seen.add(word.lower())
        out.append(word)
        if len(out) >= limit:
            break
    return out


def _hashtags(text: str) -> list[str]:
    return _clean_words(re.findall(r"#\w+", text), 30)


def read_brand(studio_dir: Path | str) -> dict:
    """Brand rules from the studio, parsed conservatively (no LLM, nothing executed).

    Sources: ``studio\\config.py`` (``BRAND``, ``HASHTAGS_BASE``, ``RED_LIST`` via ``ast``) and
    ``prompts\\copywriter.md`` (``Tagline: "..."``, the "hashtags are exactly" line, the
    "Never mention ..." list, and bullets of a "## Style"/"## Voice"/"## Tone" section).
    Returns only the keys it found: name, bio, hashtags, blocked_words, style_notes.
    """
    studio_dir = Path(studio_dir)
    brand: dict[str, Any] = {}
    consts = _literal_assignments(studio_dir / "studio" / "config.py",
                                  ("BRAND", "HASHTAGS_BASE", "RED_LIST"))
    if isinstance(consts.get("BRAND"), str) and consts["BRAND"].strip():
        brand["name"] = consts["BRAND"].strip()[:100]
    if isinstance(consts.get("HASHTAGS_BASE"), (list, tuple)):
        tags = _hashtags(" ".join(str(t) for t in consts["HASHTAGS_BASE"]))
        if tags:
            brand["hashtags"] = tags
    blocked: list[str] = []
    if isinstance(consts.get("RED_LIST"), (list, tuple)):
        blocked += [str(w) for w in consts["RED_LIST"] if isinstance(w, str)]

    prompt = studio_dir / "prompts" / "copywriter.md"
    try:
        text = prompt.read_text(encoding="utf-8", errors="replace") \
            if prompt.is_file() and prompt.stat().st_size <= MAX_FILE_BYTES else ""
    except OSError:
        text = ""
    if text:
        m = re.search(r"Tagline:\s*[\"“]([^\"”\n]{3,200})[\"”]", text)
        if m:
            brand["bio"] = m.group(1).strip()
        for line in text.splitlines():
            if re.search(r"hashtags\s+are\s+exactly", line, re.I):
                tags = _hashtags(line)
                if tags:
                    brand["hashtags"] = _clean_words([*brand.get("hashtags", []), *tags], 30)
        for m in re.finditer(r"Never mention ([^.\n]{3,400})", text, re.I):
            parts = re.split(r",|\bor\b|\band\b", m.group(1))
            blocked += [p for p in (x.strip() for x in parts) if 1 < len(p) <= 40]
        style = re.search(r"^##\s*(?:Style|Voice|Tone)\b[^\n]*\n(.*?)(?=^##\s|\Z)", text,
                          re.I | re.M | re.S)
        if style:
            bullets = [ln.strip()[2:].strip() for ln in style.group(1).splitlines()
                       if ln.strip().startswith(("- ", "* "))]
            notes = "\n".join(b[:300] for b in bullets[:10] if b)
            if notes:
                brand["style_notes"] = notes[:2000]
    if blocked:
        brand["blocked_words"] = _clean_words(blocked, 100)
    return brand


def read_posting_times(studio_dir: Path | str) -> dict[str, str]:
    """``{our_platform: "HH:MM"}`` from the studio's ``SLOT`` table (``studio\\package.py``)."""
    consts = _literal_assignments(Path(studio_dir) / "studio" / "package.py", ("SLOT",))
    slots = consts.get("SLOT")
    out: dict[str, str] = {}
    if not isinstance(slots, dict):
        return out
    # The studio's Reels slot is what our "facebook" posts (we publish Reels-style videos).
    order = ["youtube", "facebook", "youtube_shorts", "facebook_reels", "instagram", "tiktok"]
    for studio_key in order:
        value = slots.get(studio_key)
        ours = STUDIO_PLATFORM_MAP.get(studio_key)
        if not ours or not isinstance(value, (list, tuple)) or len(value) != 2:
            continue
        h, m = value
        if isinstance(h, int) and isinstance(m, int) and 0 <= h < 24 and 0 <= m < 60:
            out[ours] = f"{h:02d}:{m:02d}"
    return out


# ----------------------------------------------------------------------------- history
def studio_post_history(studio_dir: Path | str, limit: int = 1000) -> list[dict]:
    """Published posts recorded by the studio (read-only), mapped to our platform keys.

    Each: ``{platform, studio_platform, remote_id, url, published_at, asset_id, title}``.
    """
    try:
        with open_studio_db(studio_db_path(studio_dir)) as conn:
            tables = _tables(conn)
            cols = set(tables.get("posts") or [])
            if "platform" not in cols or not ({"url", "remote_id"} & cols):
                return []
            rows = [dict(r) for r in conn.execute(
                f"SELECT * FROM posts LIMIT {int(limit)}").fetchall()]
            names: dict[Any, str] = {}
            acols = set(tables.get("assets") or [])
            if "id" in acols and ({"name", "title"} & acols):
                col = "title" if "title" in acols else "name"
                for r in conn.execute(f'SELECT id, "{col}" FROM assets').fetchall():
                    names[r[0]] = str(r[1] or "")
    except sqlite3.Error:
        return []
    out = []
    for r in rows:
        ours = STUDIO_PLATFORM_MAP.get(str(r.get("platform") or "").lower())
        if not ours:
            continue
        published = r.get("published") or r.get("published_at") or r.get("created")
        out.append({"platform": ours, "studio_platform": r.get("platform"),
                    "remote_id": str(r.get("remote_id") or "") or None,
                    "url": str(r.get("url") or "") or None,
                    "published_at": _to_iso(published),
                    "asset_id": r.get("asset_id"),
                    "title": names.get(r.get("asset_id"), "") or str(r.get("asset_id") or "")})
    return out


def _to_iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(
            timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        text = str(value)
        try:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
            return text
        except ValueError:
            return None


def import_post_history(db, studio_dir: Path | str) -> dict:
    """Copy the studio's published posts into our ``posts`` table (marked ``source: studio``
    in the plan/video meta), once. Returns ``{"imported": n, "skipped": m}``."""
    from .models import PostPlan, PostResult, VideoStatus

    history = studio_post_history(studio_dir)
    known = {(p["platform"], p.get("external_id") or p.get("url"))
             for p in db.list_posts()}
    videos: dict[Any, int] = {}
    imported = skipped = 0
    for item in history:
        ident = item["remote_id"] or item["url"]
        if not ident or (item["platform"], ident) in known:
            skipped += 1
            continue
        asset = item["asset_id"]
        if asset not in videos:
            title = item["title"] or "סרטון מהסטודיו"
            vid = db.add_video(f"studio:{asset}", title[:200])
            db.update_video(vid, status=VideoStatus.PUBLISHED,
                            meta={"source": "studio", "studio_asset_id": asset})
            videos[asset] = vid
            if item["published_at"]:
                _backdate(db, "videos", vid, item["published_at"])
        plan = PostPlan(platform=item["platform"], variant_path="", title=item["title"][:300],
                        description="", tags=[], hashtags=[], scheduled_at=None,
                        extra={"source": "studio", "studio_asset_id": asset})
        pid = db.add_post(videos[asset], plan, status="published")
        db.update_post(pid, result=PostResult(platform=item["platform"], ok=True, dry_run=False,
                                              external_id=item["remote_id"] or item["url"],
                                              url=item["url"], error=None))
        if item["published_at"]:
            _backdate(db, "posts", pid, item["published_at"])
        known.add((item["platform"], ident))
        imported += 1
    return {"imported": imported, "skipped": skipped}


def _backdate(db, table: str, row_id: int, iso: str) -> None:
    """Keep the studio's real publish time so "uploaded today" stays truthful."""
    assert table in ("videos", "posts")
    with db.immediate() as conn:
        if table == "posts":
            conn.execute("UPDATE posts SET created_at=?, updated_at=? WHERE id=?",
                         (iso, iso, row_id))
        else:
            conn.execute("UPDATE videos SET created_at=? WHERE id=?", (iso, row_id))


# ----------------------------------------------------------------------------- double posting
_sha_cache: dict[tuple[str, int, int], str] = {}
_sha_lock = threading.Lock()


def file_sha256(path: Path | str) -> str | None:
    """SHA-256 of a file (cached by path+size+mtime), or None when it cannot be read."""
    path = Path(path)
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path.resolve()), st.st_size, st.st_mtime_ns)
    with _sha_lock:
        if key in _sha_cache:
            return _sha_cache[key]
    h = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    digest = h.hexdigest()
    with _sha_lock:
        _sha_cache[key] = digest
    return digest


def studio_conflict(settings: Settings, platform: str, files: Iterable[Path | str]
                    ) -> dict | None:
    """Did the old studio already publish (or schedule live) one of ``files`` to ``platform``?

    Read-only check of ``studio.db`` (SPEC_V2 §11.10). Matches by file SHA-256 and the account
    the platform lands on. Returns ``{"platform", "studio_platform", "sha256", "where"}`` or
    None (also None when there is no studio, no DB, or the guard is switched off). A studio
    DB that exists but cannot be read fails CLOSED: ``{"platform", "unreadable": True}``.
    """
    if settings.get("studio.double_post_guard", True) is False:
        return None
    studio = settings.studio_dir
    if studio is None:
        return None
    db_path = studio_db_path(studio)
    if not db_path.is_file():
        return None
    hashes = {h for h in (file_sha256(f) for f in files if f) if h}
    if not hashes:
        return None
    target = base_platform(platform)
    try:
        with open_studio_db(db_path) as conn:
            return _find_conflict(conn, _tables(conn), hashes, target)
    except sqlite3.Error as exc:
        log.warning("studio double-post check could not read the studio DB: %s",
                    type(exc).__name__)
        return {"platform": platform, "unreadable": True}


def _live_row(row: dict) -> bool:
    status = str(row.get("status") or "").lower()
    adapter = str(row.get("adapter") or "").lower()
    return status not in _DEAD_STATUSES and adapter not in ("dry", "-")


def _find_conflict(conn: sqlite3.Connection, tables: dict[str, list[str]], hashes: set[str],
                   target: str) -> dict | None:
    """Look for the same file (or the same studio asset) already live on ``target``.

    1. ``packages.body.targets[]`` with our ``file_sha256`` + a live ``executions`` row.
    2. Any table with a ``platform`` and a ``*sha256*`` column (generic schemas).
    3. Asset level: the studio re-encodes clips (vert / yt_vert), so a hash match on ANY of
       an asset's files (``versions`` rows, package targets, ``assets`` columns — the
       ``long`` version is the untouched master) links us to that asset; its ``posts`` or
       live executions on the same platform count as "already published".
    """
    def hit(where: str, studio_platform: str, sha: str) -> dict:
        return {"platform": target, "studio_platform": studio_platform, "sha256": sha,
                "where": where}

    ecols = set(tables.get("executions") or [])
    postcols = set(tables.get("posts") or [])
    assets: dict[Any, str] = {}  # studio asset id -> the hash that linked it

    # 1) packages.body.targets[] (+ executions)
    pcols = set(tables.get("packages") or [])
    if {"id", "body"} <= pcols:
        has_asset = "asset_id" in pcols
        sel = "id, body" + (", asset_id" if has_asset else "")
        for prow in conn.execute(f"SELECT {sel} FROM packages").fetchall():
            body = _json(prow["body"])
            targets = body.get("targets") if isinstance(body, dict) else None
            for t in targets or []:
                if not isinstance(t, dict) or t.get("file_sha256") not in hashes:
                    continue
                if has_asset and prow["asset_id"] is not None:
                    assets.setdefault(prow["asset_id"], t["file_sha256"])
                plat = str(t.get("platform") or "")
                if base_platform(plat) != target or not {"package_id", "platform"} <= ecols:
                    continue
                for erow in conn.execute(
                        "SELECT * FROM executions WHERE package_id=? AND platform=?",
                        (prow["id"], plat)).fetchall():
                    if _live_row(dict(erow)):
                        return hit("executions", plat, t["file_sha256"])

    # 2) generic: a platform column next to a *sha256* column
    marks = ",".join("?" * len(hashes))
    for table, cols in tables.items():
        sha_cols = [c for c in cols if "sha" in c.lower()]
        for col in sha_cols:
            if "platform" not in cols and "asset_id" not in cols:
                continue
            rows = conn.execute(f'SELECT * FROM "{_q(table)}" WHERE "{_q(col)}" IN ({marks})',
                                tuple(hashes)).fetchall()
            for row in rows:
                d = dict(row)
                if "platform" in d and base_platform(d.get("platform")) == target \
                        and _live_row(d):
                    return hit(table, str(d.get("platform")), str(d.get(col)))
                if d.get("asset_id") is not None and table != "packages":
                    assets.setdefault(d["asset_id"], str(d.get(col)))

    # 3a) assets whose own columns (hash / JSON text) mention one of our hashes
    acols = tables.get("assets") or []
    if "id" in acols:
        text_cols = [c for c in acols if c != "id"]
        if text_cols:
            query = "SELECT id, " + ", ".join(f'"{_q(c)}"' for c in text_cols) + " FROM assets"
            for arow in conn.execute(query).fetchall():
                blob = " ".join(str(arow[c]) for c in text_cols if arow[c] is not None)
                sha = next((h for h in hashes if h in blob), None)
                if sha:
                    assets.setdefault(arow["id"], sha)

    # 3b) is a linked asset already on this platform?
    for asset_id, sha in assets.items():
        if {"platform", "asset_id"} <= postcols:
            for row in conn.execute("SELECT * FROM posts WHERE asset_id=?",
                                    (asset_id,)).fetchall():
                d = dict(row)
                if base_platform(d.get("platform")) == target and _live_row(d):
                    return hit("posts", str(d.get("platform")), sha)
        if {"id", "asset_id"} <= pcols and {"package_id", "platform"} <= ecols:
            for erow in conn.execute(
                    "SELECT e.* FROM executions e JOIN packages p ON p.id = e.package_id "
                    "WHERE p.asset_id=?", (asset_id,)).fetchall():
                d = dict(erow)
                if base_platform(d.get("platform")) == target and _live_row(d):
                    return hit("executions", str(d.get("platform")), sha)
    return None


# ----------------------------------------------------------------------------- daemon
def _query_task(name: str) -> dict:
    """``schtasks /Query /TN <name> /XML`` (language-independent XML). Windows only."""
    if not IS_WINDOWS:
        return {"found": None, "enabled": None}
    try:
        proc = subprocess.run(["schtasks", "/Query", "/TN", name, "/XML"], capture_output=True,
                              timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return {"found": None, "enabled": None}
    if proc.returncode != 0:
        return {"found": False, "enabled": False}
    raw = proc.stdout or b""
    text = raw.decode("utf-16", "replace") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") \
        else raw.decode("utf-8", "replace")
    settings_block = re.search(r"<Settings>(.*?)</Settings>", text, re.S)
    block = settings_block.group(1) if settings_block else text
    disabled = re.search(r"<Enabled>\s*false\s*</Enabled>", block, re.I) is not None
    return {"found": True, "enabled": not disabled}


def daemon_activity(studio_dir: Path | str | None, now: float | None = None) -> dict:
    """Is the old studio still publishing? Scheduled task "CS2Studio Daemon" enabled (Windows)
    or ``logs\\daemon.log`` written in the last 10 minutes. We never disable it ourselves.

    Returns ``{active, task_found, task_enabled, log_recent, log_age_min, question_he,
    disable_commands}``.
    """
    now = time.time() if now is None else now
    task = _query_task(DAEMON_TASKS[0])
    log_age = None
    if studio_dir:
        try:
            log_age = (now - (Path(studio_dir) / "logs" / "daemon.log").stat().st_mtime) / 60
        except OSError:
            log_age = None
    log_recent = log_age is not None and log_age * 60 <= DAEMON_LOG_RECENT_S
    active = bool(log_recent or (task["found"] and task["enabled"]))
    return {"active": active, "task_found": task["found"], "task_enabled": task["enabled"],
            "log_recent": log_recent,
            "log_age_min": round(log_age, 1) if log_age is not None else None,
            "question_he": DAEMON_QUESTION_HE if active else "",
            # /DISABLE keeps it from starting again; /End stops the copy running right now
            "disable_commands": [f'schtasks /Change /TN "{t}" /DISABLE' for t in DAEMON_TASKS]
            + [f'schtasks /End /TN "{t}"' for t in DAEMON_TASKS]}


# ----------------------------------------------------------------------------- studio import
# Keys to paste, in order, when the studio's keyring did not survive a PC move (HANDOFF.md):
# the accounts themselves are still connected on the platforms' side.
NEXT_CONNECT = (
    {"key": "youtube", "name": "ZERNIO_API_KEY",
     "unlocks": ["youtube", "youtube_shorts", "tiktok"],
     "title_he": "מפתח Zernio — מחזיר את יוטיוב וטיקטוק",
     "title_en": "Zernio key — brings back YouTube and TikTok"},
    {"key": "facebook", "name": "META_ACCESS_TOKEN", "unlocks": ["facebook", "instagram_reels"],
     "title_he": "מפתח העמוד של מטא — מחזיר את פייסבוק ואינסטגרם",
     "title_en": "Meta Page token — brings back Facebook and Instagram"},
    {"key": "telegram", "name": "TELEGRAM_BOT_TOKEN", "unlocks": ["telegram"],
     "title_he": "המפתח של בוט הטלגרם — אישורים בלחיצה אחת",
     "title_en": "Telegram bot token — one-tap approvals"},
)
STUDIO_KEYS_MISSING_HE = ("מצאתי את הסטודיו. החיבורים עצמם שמורים אצל הפלטפורמות — צריך רק "
                          "להדביק מפתח אחד של Zernio כדי להחזיר את יוטיוב וטיקטוק")
STUDIO_KEYS_MISSING_EN = ("Found the studio. The connections themselves are kept by the "
                          "platforms — just paste one Zernio key to bring back YouTube and TikTok")
STUDIO_LINKED_HE = ("מצאתי את הסטודיו. החיבורים השמורים בו עובדים גם כאן — בלי להעתיק "
                    "מפתחות")
STUDIO_LINKED_EN = "Found the studio. Its saved connections work here too — no keys copied"


def next_connect_steps() -> list[dict]:
    """The key wizards still needed (Zernio → Meta → Telegram), names only."""
    return [dict(step) for step in NEXT_CONNECT if not secret(step["name"])]


def studio_report(settings: Settings, path: Path | str | None = None) -> dict:
    """What the old studio has, without changing anything (names / ids / counts only).

    ``message_he`` / ``message_en`` is the one friendly line for the import card and
    ``next_connect`` lists the key wizards to open next, in order (Zernio, Meta, Telegram).
    """
    studio = find_studio_dir(path, settings)
    if studio is None:
        return {"found": False, "dir": str(path) if path else None}
    has_db = studio_db_path(studio).is_file()
    keyring_names = studio_keyring_entries()
    steps = next_connect_steps()
    missing_keys = not keyring_names or any(s["name"] == "ZERNIO_API_KEY" for s in steps)
    return {
        "found": True,
        "dir": str(studio),
        "db": has_db,
        "keyring": keyring_names,
        "meta_ids": read_meta_ids(studio) if has_db else {},
        "brand": read_brand(studio),
        "posting_times": read_posting_times(studio),
        "history_count": len(studio_post_history(studio)) if has_db else 0,
        "daemon": daemon_activity(studio),
        "message_he": STUDIO_KEYS_MISSING_HE if missing_keys else STUDIO_LINKED_HE,
        "message_en": STUDIO_KEYS_MISSING_EN if missing_keys else STUDIO_LINKED_EN,
        "next_connect": steps,
    }


def _merge_list(current: Any, new: list[str]) -> list[str]:
    return _clean_words([*(current or []), *new], 100)


def import_studio(settings: Settings, path: Path | str | None = None, *, history: bool = True,
                  db=None) -> dict:
    """Link the agency to the old studio (SPEC_V2 §11.4). Writes only non-secret config.

    - ``studio.dir`` (so the read-only fallback, double-post guard and inbox know it)
    - ``meta.page_id`` / ``meta.ig_user_id`` from ``studio.db`` kv ``meta_ids``
    - ``brand.*`` (lists are merged; text fields only filled when empty)
    - ``publishing.default_times.*`` from the studio's posting slots
    - optionally the published-post history (``source: studio``)
    Secrets are NOT copied: the ``cs2studio`` keyring entries are read in place.
    """
    from .platforms import registry
    from .settings_store import update_config

    report = studio_report(settings, path)
    if not report["found"]:
        return {**report, "config_updated": [], "linked": [], "history": None,
                "problems": ["לא נמצאה תיקיית הסטודיו הקיים"]}
    studio = Path(report["dir"])
    patch: dict[str, Any] = {"studio": {"dir": str(studio)}}
    if report["meta_ids"]:
        patch["meta"] = dict(report["meta_ids"])
    brand_now = settings.get("brand", {}) or {}
    brand_patch: dict[str, Any] = {}
    for k, v in report["brand"].items():
        if isinstance(v, list):
            merged = _merge_list(brand_now.get(k), v)
            if merged != list(brand_now.get(k) or []):
                brand_patch[k] = merged
        elif not str(brand_now.get(k) or "").strip():
            brand_patch[k] = v
    if brand_patch:
        patch["brand"] = brand_patch
    if report["posting_times"]:
        patch["publishing"] = {"default_times": dict(report["posting_times"])}
    problems: list[str] = []
    updated: list[str] = []
    try:
        update_config(settings, patch)
        updated = _dotted_keys(patch)
    except Exception as exc:  # a bad value must not hide the rest of the report
        problems.append(f"לא הצלחתי לשמור הגדרות מהסטודיו: {exc}")
    linked = []
    for key in registry.ALL_KEYS:
        try:
            adapter = registry.get_adapter(key, settings)
            if adapter.configured() and any(
                    _source(name) == "studio" for name in adapter.required_secrets):
                linked.append(key)
        except Exception:
            continue
    hist = None
    if history and report["db"]:
        if db is None:
            from .db import DB
            db = DB(settings.db_path)
        try:
            hist = import_post_history(db, studio)
        except Exception as exc:
            problems.append(f"לא הצלחתי לייבא את היסטוריית הפרסומים ({type(exc).__name__})")
    return {**report, "config_updated": updated, "linked": linked, "history": hist,
            "problems": problems}


def _source(name: str) -> str | None:
    from .config import secret_source
    return secret_source(name)


def _dotted_keys(patch: dict, prefix: str = "") -> list[str]:
    out: list[str] = []
    for k, v in patch.items():
        if isinstance(v, dict) and v:
            out += _dotted_keys(v, f"{prefix}{k}.")
        else:
            out.append(f"{prefix}{k}")
    return out


# ----------------------------------------------------------------------------- generic scan
@dataclass
class Found:
    """One importable thing found on disk. Holds names and paths only — never values."""

    kind: str  # google_client_secret | google_token | token_pickle | env_file | studio
    path: str
    label_he: str
    names: list[str] = field(default_factory=list)
    ok: bool = True
    problem: str = ""
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = hashlib.sha1(f"{self.kind}|{self.path}".encode("utf-8")).hexdigest()[:12]

    def to_dict(self) -> dict:
        return asdict(self)


def default_roots(settings: Settings | None = None) -> list[Path]:
    """Where to look by default: the agency folder's parent, the studio, the user's
    Documents / Desktop / OneDrive / Projects folders, and ``importer.extra_roots``."""
    roots: list[Path] = []
    home = Path.home()
    if settings is not None:
        parent = settings.root.parent
        if parent != Path(parent.anchor):  # never crawl a whole drive (C:\)
            roots.append(parent)
        if settings.studio_dir is not None:
            roots.append(settings.studio_dir)
        for extra in settings.get("importer.extra_roots", []) or []:
            roots.append(Path(str(extra)).expanduser())
    elif os.name == "nt":
        roots.append(Path(DEFAULT_STUDIO_DIR))
    for name in ("Documents", "Desktop", "OneDrive", "Projects", "projects", "source"):
        roots.append(home / name)
    out: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            key = str(root.resolve()).lower()
        except OSError:
            continue
        if key not in seen and root.is_dir():
            seen.add(key)
            out.append(root)
    return out


def _load_json(path: Path) -> Any:
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            return None
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _token_scopes(data: dict) -> list[str]:
    scopes = data.get("scopes") or data.get("scope") or []
    if isinstance(scopes, str):
        scopes = scopes.split()
    return [str(s) for s in scopes if isinstance(s, str)]


def is_client_secret(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    inner = data.get("installed") or data.get("web")
    return isinstance(inner, dict) and bool(inner.get("client_id")) \
        and bool(inner.get("client_secret"))


def is_authorized_user(data: Any) -> bool:
    return isinstance(data, dict) and bool(data.get("refresh_token")) \
        and bool(data.get("client_id"))


def token_can_upload(data: dict) -> bool | None:
    """True/False from the token's scopes; None when the file does not list them."""
    scopes = _token_scopes(data)
    if not scopes:
        return None
    return any(s in UPLOAD_SCOPES for s in scopes)


def inspect_file(path: Path | str, settings: Settings | None = None) -> Found | None:
    """Classify one file (no secrets kept). None when it is nothing we import."""
    from .settings_store import allowed_env_names

    path = Path(path)
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
            return None
    except OSError:
        return None
    name = path.name.lower()
    if name.endswith((".pickle", ".pkl")) and "token" in name:
        return Found("token_pickle", str(path), "נמצא קובץ חיבור ישן (pickle)", ok=False,
                     problem=f"{RELOGIN_HE} — מטעמי אבטחה הקובץ הזה לא נפתח")
    if name.endswith(".json"):
        data = _load_json(path)
        if is_client_secret(data):
            return Found("google_client_secret", str(path),
                         "נמצא קובץ התחברות של גוגל (ליוטיוב)")
        if is_authorized_user(data):
            can = token_can_upload(data)
            if can is False:
                return Found("google_token", str(path), "נמצא חיבור ליוטיוב", ok=False,
                             problem=RELOGIN_HE)
            return Found("google_token", str(path), "נמצא חיבור ליוטיוב")
        return None
    if name == ".env" or name.startswith(".env.") or name.endswith(".env"):
        if name.endswith((".example", ".sample", ".template")):
            return None
        if settings is not None:
            try:
                if path.resolve() == (settings.root / ".env").resolve():
                    return None  # our own .env
            except OSError:
                pass
        allowed = set(allowed_env_names())
        names = [n for n, v in _env_values(path).items() if n in allowed and v]
        if names:
            return Found("env_file", str(path), "נמצאו מפתחות: " + ", ".join(names),
                         names=names)
    return None


def _env_values(path: Path) -> dict[str, str]:
    """Parse a .env file without interpolation. The values stay inside this module."""
    try:
        from dotenv import dotenv_values
        return {k: v for k, v in dotenv_values(path, interpolate=False).items()
                if isinstance(k, str) and isinstance(v, str)}
    except Exception:
        return {}


def _dirs_last(entry: os.DirEntry) -> tuple[bool, str]:
    """Files before folders, so a folder's own files are seen before the budget runs out."""
    try:
        is_dir = entry.is_dir(follow_symlinks=False)
    except OSError:
        is_dir = True
    return is_dir, entry.name.lower()


def scan(paths: Iterable[Path | str] | None = None, settings: Settings | None = None, *,
         include_defaults: bool = True, max_depth: int = MAX_DEPTH) -> list[Found]:
    """Look for importable connections (non-recursive beyond ``max_depth``, files < 1MB).

    Finds Google client secrets, Google authorized-user tokens (scope-checked), pickled tokens
    (reported only, never opened) and ``.env`` files with known key names. A studio folder
    among the roots is reported as ``kind="studio"``.
    """
    roots: list[Path] = [Path(p).expanduser() for p in (paths or [])]
    if include_defaults:
        roots += default_roots(settings)
    skip_dirs = set(SKIP_DIRS)
    own_data = None
    if settings is not None:
        try:
            own_data = settings.data_dir.resolve()
        except OSError:
            own_data = None
    found: list[Found] = []
    seen_files: set[str] = set()
    seen_dirs: set[str] = set()
    budget = [MAX_ENTRIES]

    def add(item: Found | None) -> None:
        if item is not None and item.path not in seen_files:
            seen_files.add(item.path)
            found.append(item)

    def walk(directory: Path, depth: int) -> None:
        try:
            real = str(directory.resolve()).lower()
        except OSError:
            return
        if real in seen_dirs or (own_data is not None and real == str(own_data).lower()):
            return
        seen_dirs.add(real)
        if _is_studio_dir(directory):
            add(Found("studio", str(directory), "נמצא הסטודיו הקיים"))
        try:
            entries = sorted(os.scandir(directory), key=_dirs_last)
        except OSError:
            return
        for entry in entries:
            budget[0] -= 1
            if budget[0] <= 0:
                return
            try:
                if entry.is_symlink() or getattr(entry, "is_junction", lambda: False)():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if depth < max_depth and entry.name.lower() not in skip_dirs \
                            and not entry.name.startswith("."):
                        walk(Path(entry.path), depth + 1)
                elif entry.is_file(follow_symlinks=False):
                    lname = entry.name.lower()
                    if lname.endswith((".json", ".pickle", ".pkl")) or ".env" in lname:
                        add(inspect_file(Path(entry.path), settings))
            except OSError:
                continue

    for root in roots:
        budget[0] = MAX_ENTRIES  # per root: one huge folder must not hide the others
        try:
            if root.is_file():
                add(inspect_file(root, settings))
            elif root.is_dir():
                walk(root, 0)
        except OSError:
            continue
    return found


def import_found(items: Iterable[Found | dict], settings: Settings, *, overwrite: bool = False,
                 verify: bool = False) -> list[dict]:
    """Import selected items. Each file is re-inspected from disk (never trust the caller's
    "kind"). Google files go to ``data/secrets`` (owner-only); recognised ``.env`` keys are
    stored with :func:`agency.config.store_secret` (keyring, else .env) without overwriting
    existing values unless ``overwrite``. Ids go to config. Returns per item::

        {"id", "kind", "path", "imported": [...], "skipped": [...], "problem", "verify": {...}}
    """
    from .config import store_secret
    from .settings_store import secrets_dir, update_config, write_private_file

    results: list[dict] = []
    for raw in items:
        path = raw.path if isinstance(raw, Found) else str((raw or {}).get("path") or "")
        wanted = raw.kind if isinstance(raw, Found) else str((raw or {}).get("kind") or "")
        out: dict[str, Any] = {"id": getattr(raw, "id", None) or (raw or {}).get("id"),
                               "kind": wanted, "path": path, "imported": [], "skipped": [],
                               "problem": "", "verify": {}}
        results.append(out)
        if wanted == "studio":
            rep = import_studio(settings, path)
            out["imported"] = rep.get("config_updated", [])
            out["problem"] = "; ".join(rep.get("problems") or [])
            out["linked"] = rep.get("linked", [])
            out["history"] = rep.get("history")
            continue
        item = inspect_file(path, settings) if path else None
        if item is None or (wanted and item.kind != wanted):
            out["problem"] = "הקובץ השתנה או לא נמצא — סרוק שוב"
            continue
        out["id"] = out["id"] or item.id
        out["kind"] = item.kind
        if not item.ok:
            out["problem"] = item.problem
            continue
        try:
            if item.kind in ("google_client_secret", "google_token"):
                target = secrets_dir(settings) / ("youtube_client_secret.json"
                                                  if item.kind == "google_client_secret"
                                                  else "youtube_token.json")
                if target.exists() and not overwrite:
                    out["skipped"].append(target.name)
                else:
                    data = _load_json(Path(path))
                    ok = is_client_secret(data) if item.kind == "google_client_secret" \
                        else is_authorized_user(data)
                    if not ok:
                        out["problem"] = "הקובץ לא תקין"
                        continue
                    write_private_file(target, json.dumps(data, ensure_ascii=False, indent=2))
                    out["imported"].append(target.name)
                if verify:
                    from . import connect
                    out["verify"]["youtube"] = connect.verify_youtube_direct(settings)
            elif item.kind == "env_file":
                values = _env_values(Path(path))
                ids = {"IG_USER_ID": "ig_user_id", "FB_PAGE_ID": "page_id"}
                meta_patch: dict[str, str] = {}
                for name in item.names:
                    value = (values.get(name) or "").strip()
                    if not value:
                        continue
                    exists = bool(secret(name)) or (
                        name in ids and str(settings.get(f"meta.{ids[name]}") or ""))
                    if exists and not overwrite:
                        out["skipped"].append(name)
                        continue
                    if name in ids:
                        meta_patch[ids[name]] = value
                    else:
                        store_secret(name, value, settings.root)
                    out["imported"].append(name)
                if meta_patch:
                    update_config(settings, {"meta": meta_patch})
                if verify and out["imported"]:
                    from . import connect
                    for key in connect.platforms_using(item.names, settings):
                        connect.invalidate(settings, key)
                        out["verify"][key] = connect.verify(key, settings)
        except Exception as exc:  # report the NAME of the problem only, never values
            log.warning("import of %s failed: %s", item.kind, type(exc).__name__)
            out["problem"] = f"הייבוא נכשל ({type(exc).__name__})"
    return results


__all__ = ["Found", "STUDIO_ALIASES", "base_platform", "daemon_activity", "default_roots",
           "file_sha256", "find_studio_dir", "import_found", "import_post_history",
           "import_studio", "inspect_file", "open_studio_db", "read_brand", "read_meta_ids",
           "read_posting_times", "scan", "studio_conflict", "studio_db_path", "studio_kv",
           "studio_keyring_entries", "studio_post_history", "studio_report"]
