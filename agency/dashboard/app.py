"""FastAPI dashboard: JSON API + a single Hebrew RTL page.

Staff / pipeline / scheduler modules are imported lazily inside handlers so this module
imports even while those parts are missing or broken.

Access (SPEC_V2 §6): the PC itself (a loopback TCP peer) needs no PIN. Other devices are
refused unless ``dashboard.remote_access`` is on; then they log in with the 6-digit PIN and
get a session cookie (:mod:`agency.dashboard.auth`). Every state-changing request needs the
``X-Agency-Request: 1`` header (CSRF) and a Host header on the allow-list (DNS rebinding).
Actions that touch secrets, the PC's disk or the PIN are loopback-only.

Background services (:func:`start_services`): the scheduler, the inbox watcher (when on) and
the Telegram bot (when on), all ending with one shared stop event.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import secrets
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field

from ..events import emit
from ..models import Approval, PostPlan, VideoStatus, now_iso
from ..staff.base import Context, build_context
from . import auth, views

log = logging.getLogger("agency.dashboard")

HERE = Path(__file__).resolve().parent
TEMPLATES_DIR = HERE / "templates"
STATIC_DIR = HERE / "static"

ALLOWED_EXTENSIONS: frozenset[str] = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi"})
DEFAULT_MAX_UPLOAD_BYTES = 4 * 1024 ** 3  # 4 GB
MAX_TITLE_LEN = 200
MAX_CHAT_LEN = 4000
MAX_NOTE_LEN = 1000
MAX_AMOUNT_ILS = 10_000_000
MAX_BODY_BYTES = 2 * 1024 * 1024          # any request but the video upload
MAX_KEYS_BODY_BYTES = 64 * 1024
MAX_CLIENT_SECRET_BYTES = 1024 * 1024
MAX_PLAN_TITLE_LEN = 1000
MAX_PLAN_DESCRIPTION_LEN = 20_000
IMPORT_SCAN_TTL_S = 30 * 60
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

# Header every state-changing request must carry. Browsers cannot add a custom header to a
# cross-site request without a CORS preflight (which we never allow), so this blocks CSRF.
CSRF_HEADER = "x-agency-request"

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
WILDCARD_HOSTS = {"0.0.0.0", "::", "[::]", ""}

# Legacy (advanced): an explicit non-loopback bind WITHOUT phone access needs this per-launch
# key (printed URL -> HttpOnly cookie). With phone access on, the PIN login replaces it.
ACCESS_COOKIE = "agency_key"
ACCESS_HEADER = "x-agency-key"

DEFAULT_CSP = ("default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
               "style-src 'self'; script-src 'self'; connect-src 'self'; "
               "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")

STAFF_TITLES: dict[str, str] = {
    "manager": 'מנכ"ל',
    "analyst": "אנליסט וידאו",
    "editor": "עורך וידאו",
    "copywriter": "קופירייטר",
    "strategist": "אסטרטג תוכן",
    "publisher": "מפרסם",
    "promoter": "מקדם",
    "community": "מנהל קהילה",
    "analytics": "אנליסט נתונים",
    "finance": "מנהל כספים",
}

PLATFORM_TITLES: dict[str, str] = {
    "youtube": "יוטיוב",
    "youtube_shorts": "יוטיוב שורטס",
    "tiktok": "טיקטוק",
    "instagram_reels": "אינסטגרם רילס",
    "facebook": "פייסבוק",
    "x": "X (טוויטר)",
    "reddit": "רדיט",
    "discord": "דיסקורד",
}


# --------------------------------------------------------------------------- helpers
class UploadRejected(Exception):
    """Upload refused; carries an HTTP status and a Hebrew message."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Denied(Exception):
    """Access refused with an :mod:`auth` reason code (answered via :func:`auth.denial`)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


_UNSAFE_CHARS = re.compile(r"[^\w.\- ]+", re.UNICODE)


def sanitize_filename(name: str) -> str:
    """Return a safe basename for an uploaded video, or raise ValueError.

    Strips any directory part (both ``/`` and ``\\``), control and special characters,
    leading dots and repeated separators; keeps Hebrew/Latin letters, digits, ``-_. ``.
    The extension must be one of ``ALLOWED_EXTENSIONS``.
    """
    name = unicodedata.normalize("NFC", name or "")
    name = name.replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C")
    name = _UNSAFE_CHARS.sub("_", name)
    name = re.sub(r"[ _]{2,}", "_", name).strip(" ._-")
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        raise ValueError("שם קובץ לא תקין או בלי סיומת")
    ext = "." + ext.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"סוג קובץ לא נתמך ({ext}). מותר: {', '.join(sorted(ALLOWED_EXTENSIONS))}")
    stem = stem.replace(".", "_")[:120].strip(" ._-") or "video"
    return f"{stem}{ext}"


def clean_text(value: str | None, limit: int) -> str:
    """Trim, drop control characters and cap the length of free text from the owner."""
    value = "".join(ch for ch in (value or "") if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    return value.strip()[:limit]


def machine_hosts() -> set[str]:
    """This machine's host name(s) and interface IPs (for a LAN bind's Host allow-list)."""
    import socket

    names: set[str] = set()
    try:
        hostname = socket.gethostname()
        names |= {hostname, socket.getfqdn()}
        for info in socket.getaddrinfo(hostname, None):
            names.add(str(info[4][0]))
    except OSError:
        pass
    try:  # the address used for outbound traffic (no packet is sent for UDP connect)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))
            names.add(sock.getsockname()[0])
    except OSError:
        pass
    out = set()
    for name in names:
        name = name.lower().split("%", 1)[0]
        out.add(f"[{name}]" if ":" in name else name)
    return {n for n in out if n}


def split_host(host: str) -> str:
    """Host header -> hostname without port (keeps ``[v6]`` brackets)."""
    host = (host or "").strip().lower()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def dashboard_port(settings) -> int:
    """The port the dashboard serves on (config ``dashboard.port``, CLI overrides it)."""
    try:
        port = int(float(settings.get("dashboard.port", 8765)))
    except (TypeError, ValueError):
        return 8765
    return port if 0 < port < 65536 else 8765


def budget_status(ctx: Context) -> dict:
    """CFO budget status; falls back to raw DB totals if the finance module is unavailable."""
    try:
        from ..staff.finance import CFO
        return CFO(ctx).budget_status()
    except ImportError:
        month = current_month()
        totals = ctx.db.month_totals(month)
        budget = float(ctx.settings.get("finance.monthly_budget_ils", 0) or 0)
        return {"month": month, "currency": "ILS", "budget": budget, "spent": totals["expense"],
                "remaining": round(max(0.0, budget - totals["expense"]), 2),
                "income": totals["income"], "net": totals["net"],
                "max_single": float(ctx.settings.get("finance.max_single_spend_ils", 0) or 0)}


def staff_snapshot(ctx: Context) -> list[dict]:
    """Manager first, then every enabled staff member, merged with their live DB status."""
    live = {row["name"]: row for row in ctx.db.list_staff_status()}
    enabled = [n for n in (ctx.settings.get("staff.enabled", []) or []) if n != "manager"]
    out = []
    for name in ["manager", *enabled]:
        row = live.get(name, {})
        out.append({"name": name, "title_he": STAFF_TITLES.get(name, name),
                    "status": row.get("status", "idle"), "detail": row.get("detail", ""),
                    "updated_at": row.get("updated_at"), "is_manager": name == "manager"})
    return out


def status_snapshot(ctx: Context) -> dict:
    """Everything the header / org chart needs in one call."""
    videos = ctx.db.list_videos(limit=10_000)
    posts = ctx.db.list_posts()
    by_video: dict[str, int] = {}
    for v in videos:
        by_video[v.status.value] = by_video.get(v.status.value, 0) + 1
    by_post: dict[str, int] = {}
    for p in posts:
        by_post[p["status"]] = by_post.get(p["status"], 0) + 1
    return {
        "dry_run": ctx.settings.dry_run,
        "mode": ctx.settings.get("publishing.mode", "approve"),
        "llm_mock": bool(getattr(ctx.llm, "mock", True)),
        "channel": ctx.settings.get("channel.name", "") or "",
        "staff": staff_snapshot(ctx),
        "counts": {"videos": len(videos), "videos_by_status": by_video, "posts": len(posts),
                   "posts_by_status": by_post,
                   "pending_approvals": len(ctx.db.list_approvals("pending"))},
        "budget": budget_status(ctx),
        "server_time": _now_iso(),
    }


def platforms_snapshot(settings) -> list[dict]:
    """Per platform (advanced view): enabled in config, credentials present (names of missing
    secrets only), upload route and the effective per-platform live state (SPEC_V2 §2)."""
    from ..platforms import registry

    rows = []
    for key in registry.ALL_KEYS:
        cfg = settings.get(f"publishing.platforms.{key}", {}) or {}
        row: dict[str, Any] = {"key": key, "display_he": PLATFORM_TITLES.get(key, key),
                               "enabled": bool(cfg.get("enabled", False)),
                               "dry_run": settings.dry_run, "configured": False,
                               "live_switch": settings.platform_live(key), "via": None,
                               "missing_secrets": [], "error": None}
        live = False
        try:
            row["via"] = registry.route(key, settings)
            adapter = registry.get_adapter(key, settings)
            row["display_he"] = getattr(adapter, "display_he", row["display_he"])
            row["configured"] = bool(adapter.configured())
            missing = getattr(adapter, "missing_secrets", None)
            row["missing_secrets"] = list(missing()) if callable(missing) else []
            is_live = getattr(adapter, "is_live", None)
            live = bool(is_live() if callable(is_live) else is_live)
        except Exception as exc:  # adapter module missing/broken: show it, do not crash
            row["error"] = f"{type(exc).__name__}: {exc}"
        if key == "reddit":
            row["subreddits"] = list(cfg.get("subreddits") or [])
        row["live"] = row["enabled"] and row["configured"] and live
        rows.append(row)
    return rows


def _plan_view(plan: dict, post: dict | None = None) -> dict:
    extra = plan.get("extra") or {}
    view = {"platform": plan.get("platform") or (post or {}).get("platform", ""),
            "title": plan.get("title", ""), "description": plan.get("description", ""),
            "tags": plan.get("tags") or [], "hashtags": plan.get("hashtags") or [],
            "scheduled_at": plan.get("scheduled_at"), "why": extra.get("why", ""),
            "variant": Path(plan.get("variant_path") or "").name}
    if post:
        view.update(post_id=post.get("id"), post_status=post.get("status"))
    return view


def approval_view(ctx: Context, a: Approval) -> dict:
    """Approval as dict plus a normalised ``plans`` list for publish approvals."""
    data = a.to_dict()
    payload = a.payload or {}
    plans: list[dict] = []
    if a.kind == "publish":
        posts_by_id = {}
        video_id = payload.get("video_id")
        if video_id is not None:
            try:
                posts_by_id = {p["id"]: p for p in ctx.db.list_posts(int(video_id))}
            except (TypeError, ValueError):
                posts_by_id = {}
        wanted = payload.get("post_ids")
        raw_plans = payload.get("plans")
        if isinstance(raw_plans, list) and raw_plans:
            for i, plan in enumerate(raw_plans):
                if not isinstance(plan, dict):
                    continue
                pid = plan.get("post_id")
                if pid is None and isinstance(wanted, list) and i < len(wanted):
                    pid = wanted[i]
                inner = plan["plan"] if isinstance(plan.get("plan"), dict) else plan
                plans.append(_plan_view(inner, posts_by_id.get(pid)))
        else:
            ids = wanted if isinstance(wanted, list) else list(posts_by_id)
            for pid in ids:
                post = posts_by_id.get(pid) or ctx.db.get_post(int(pid))
                if post:
                    plans.append(_plan_view(post.get("plan") or {}, post))
        if video_id is not None:
            video = ctx.db.get_video(int(video_id)) if str(video_id).isdigit() else None
            if video:
                data["video"] = {"id": video.id, "title_hint": video.title_hint,
                                 "file_name": Path(video.path).name,
                                 "summary_he": (video.analysis or {}).get("summary_he", ""),
                                 "clip_type": (video.analysis or {}).get("clip_type", "")}
    data["plans"] = plans
    return data


def video_view(ctx: Context, v) -> dict:
    d = v.to_dict()
    d["file_name"] = Path(v.path).name
    d["posts"] = [{"id": p["id"], "platform": p["platform"], "status": p["status"],
                   "url": p.get("url"), "scheduled_at": p.get("scheduled_at")}
                  for p in ctx.db.list_posts(v.id)]
    return d


def post_view(p: dict) -> dict:
    plan = p.get("plan") or {}
    result = p.get("result") or {}
    return {"id": p["id"], "video_id": p["video_id"], "platform": p["platform"],
            "status": p["status"], "scheduled_at": p.get("scheduled_at"),
            "title": plan.get("title", ""), "external_id": p.get("external_id"),
            "url": p.get("url"), "error": p.get("error"),
            "dry_run": bool(result.get("dry_run")) if result else None,
            "stats": p.get("stats") or {}, "why": (plan.get("extra") or {}).get("why", ""),
            "updated_at": p.get("updated_at")}


def run_pipeline_safely(ctx: Context, video_id: int) -> None:
    """Run the pipeline for one video; never raises (marks the video FAILED instead)."""
    try:
        from ..pipeline import run_pipeline

        ctx.db.set_staff_status("manager", "working", f"מטפל בסרטון חדש #{video_id}")
        result = run_pipeline(ctx, video_id)
        if result.get("ok"):
            msg = (f"סרטון #{video_id} מוכן — מחכה לאישור #{result['approval_id']}"
                   if result.get("approval_id") else f"סרטון #{video_id} תוזמן לפרסום")
        else:
            msg = f"סרטון #{video_id} נכשל: {result.get('error')}"
        ctx.db.set_staff_status("manager", "idle", msg[:300])
    except Exception as exc:
        ctx.db.set_staff_status("manager", "error", f"{type(exc).__name__}: {exc}"[:300])
        emit(ctx.db, "manager", f"הצינור נכשל לסרטון #{video_id}: {type(exc).__name__}: {exc}",
             "error", video_id)
        try:
            ctx.db.update_video(video_id, status=VideoStatus.FAILED,
                                meta={"error": f"{type(exc).__name__}: {exc}"})
        except Exception:  # pragma: no cover
            pass


def execute_approval_safely(ctx: Context, approval_id: int) -> dict:
    """Execute an approved approval via the pipeline; errors become ``{"ok": False}``."""
    try:
        from ..pipeline import execute_approval
        result = execute_approval(ctx, approval_id)
        return result if isinstance(result, dict) else {"ok": True, "result": result}
    except Exception as exc:
        emit(ctx.db, "manager", f"ביצוע אישור #{approval_id} נכשל: {type(exc).__name__}: {exc}",
             "error")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def retry_post_safely(ctx: Context, post_id: int) -> dict:
    """Retry a failed post via the publisher; errors become ``{"ok": False}``."""
    try:
        from ..staff.publisher import Publisher
        return Publisher(ctx).retry(post_id).to_dict()
    except Exception as exc:
        emit(ctx.db, "publisher", f"ניסיון חוזר לפוסט #{post_id} נכשל: {type(exc).__name__}: {exc}",
             "error")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def retry_video_safely(ctx: Context, video_id: int) -> dict:
    """"נסה שוב" for a clip whose preparation failed; never raises."""
    try:
        from ..pipeline import retry_video
        return retry_video(ctx, video_id)
    except Exception as exc:
        emit(ctx.db, "manager", f"ניסיון חוזר להכנת סרטון #{video_id} נכשל: "
             f"{type(exc).__name__}: {exc}", "error", video_id)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def prepare_clip_safely(ctx: Context, path: str) -> dict:
    """"תכין לפרסום" for a previous inbox clip; never raises."""
    try:
        from ..inbox import prepare_clip
        return prepare_clip(ctx, path)
    except Exception as exc:
        log.exception("preparing an inbox clip failed")
        return {"ok": False, "error": f"ההכנה נכשלה ({type(exc).__name__})", "video_id": None}


# --------------------------------------------------------------------------- plan editing
def edit_pending_plan(ctx: Context, aid: int, platform: str, title: str | None,
                      description: str | None) -> dict:
    """The owner edits one platform's title / description of a WAITING publish approval.

    Brand rules and platform limits are enforced exactly like the copywriter's output (a
    title with a blocked word is refused; the AI label / music credit are kept). The post (what
    really gets published) and the approval's snapshot change together, in one transaction
    that re-checks the approval is still pending. Raises HTTPException (Hebrew detail).
    """
    from ..brand import detect_ai, detect_music, load_brand
    from ..staff.copywriter import finalize_copy

    approval = ctx.db.get_approval(aid)
    if approval is None:
        raise HTTPException(404, "האישור לא נמצא")
    if approval.kind != "publish":
        raise HTTPException(409, "אפשר לערוך רק תוכנית פרסום")
    if approval.status != "pending":
        raise HTTPException(409, "אפשר לשנות טקסטים רק לפני שאישרת")
    if title is None and description is None:
        raise HTTPException(400, "לא נשלח שום שינוי")
    post = None
    for pid in (approval.payload or {}).get("post_ids") or []:
        try:
            candidate = ctx.db.get_post(int(pid))
        except (TypeError, ValueError):
            continue
        if candidate and candidate["platform"] == platform:
            post = candidate
            break
    if post is None:
        raise HTTPException(404, "אין בתוכנית הזאת פרסום לפלטפורמה הזאת")
    if post["status"] != "awaiting_approval":
        raise HTTPException(409, "אפשר לשנות טקסטים רק לפני שאישרת")

    brand = load_brand(ctx.settings)
    plan = PostPlan.from_dict(post.get("plan") or {})
    new_title = clean_text(title, MAX_PLAN_TITLE_LEN) if title is not None else plan.title
    if title is not None:
        if not new_title:
            raise HTTPException(400, "הכותרת לא יכולה להיות ריקה")
        hits = brand.blocked_in(new_title)
        if hits:
            raise HTTPException(400, "בכותרת יש מילה שאסור לפרסם: " + ", ".join(hits[:3]))
    new_description = clean_text(description, MAX_PLAN_DESCRIPTION_LEN) \
        if description is not None else plan.description
    video = ctx.db.get_video(int(post["video_id"]))
    analysis = (video.analysis if video else {}) or {}
    meta = (video.meta if video else {}) or {}
    hint = video.title_hint if video else ""
    has_ai = bool(plan.extra.get("ai_label")) or bool(analysis.get("ai_shots")) or detect_ai(hint)
    music = (str(analysis.get("music") or "") or str(meta.get("music") or "")
             or detect_music(hint))
    out = finalize_copy(platform, {"title": new_title, "description": new_description,
                                   "tags": plan.tags, "hashtags": plan.hashtags},
                        brand, has_ai=has_ai, music=music, fallback_title=plan.title)
    plan.title, plan.description = out["title"], out["description"]
    plan.tags, plan.hashtags = out["tags"], out["hashtags"]
    plan.extra = {**(plan.extra or {}), "edited_by_owner": True}
    plan_dict = plan.to_dict()

    changed = ("title", "description", "tags", "hashtags", "extra")
    with ctx.db.immediate() as conn:  # rolls back on any exception below
        row = conn.execute("SELECT status, payload FROM approvals WHERE id=?", (aid,)).fetchone()
        if row is None or row["status"] != "pending":
            raise HTTPException(409, "אפשר לשנות טקסטים רק לפני שאישרת")
        cur = conn.execute("UPDATE posts SET plan=?, updated_at=? WHERE id=? AND "
                           "status='awaiting_approval'",
                           (json.dumps(plan_dict, ensure_ascii=False, default=str), now_iso(),
                            post["id"]))
        if cur.rowcount != 1:
            raise HTTPException(409, "אפשר לשנות טקסטים רק לפני שאישרת")
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        snapshot = payload.get("plans") if isinstance(payload, dict) else None
        if isinstance(snapshot, list):
            for item in snapshot:
                inner = item.get("plan") if isinstance(item, dict) and isinstance(
                    item.get("plan"), dict) else item
                if isinstance(inner, dict) and inner.get("platform") == platform:
                    inner.update({k: plan_dict[k] for k in changed})
            conn.execute("UPDATE approvals SET payload=? WHERE id=?",
                         (json.dumps(payload, ensure_ascii=False, default=str), aid))
    emit(ctx.db, "owner", f"הבעלים ערך את הטקסט ל{views.platform_chip(platform)['name_he']} "
                          "לפני האישור", "info", post["video_id"])
    fresh = ctx.db.get_post(post["id"]) or post
    return {"ok": True, "plan": views.plan_card(ctx.settings, fresh.get("plan") or plan_dict,
                                                fresh, editable=True)}


# --------------------------------------------------------------------------- upload streaming
class _MultipartUpload:
    """Streams a multipart/form-data body straight to disk (no temp spool, size-capped)."""

    def __init__(self, boundary: bytes, dest_dir: Path, max_bytes: int):
        from python_multipart.multipart import MultipartParser

        self.dest_dir = dest_dir
        self.max_bytes = max_bytes
        self.fields: dict[str, str] = {}
        self.file_path: Path | None = None
        self.file_name: str | None = None
        self.size = 0
        self._tmp: Path | None = None
        self._fh = None
        self._headers: dict[str, bytes] = {}
        self._hname = b""
        self._hvalue = b""
        self._part: str | None = None  # "file" | field name | None (ignored)
        self._buf = bytearray()
        self.parser = MultipartParser(boundary, {
            "on_part_begin": self._part_begin, "on_header_field": self._header_field,
            "on_header_value": self._header_value, "on_header_end": self._header_end,
            "on_headers_finished": self._headers_done, "on_part_data": self._data,
            "on_part_end": self._part_end})

    # parser callbacks ---------------------------------------------------------
    def _part_begin(self) -> None:
        self._headers, self._hname, self._hvalue, self._part = {}, b"", b"", None
        self._buf = bytearray()

    def _header_field(self, data: bytes, start: int, end: int) -> None:
        self._hname += data[start:end]

    def _header_value(self, data: bytes, start: int, end: int) -> None:
        self._hvalue += data[start:end]

    def _header_end(self) -> None:
        self._headers[self._hname.decode("latin-1").lower()] = self._hvalue
        self._hname, self._hvalue = b"", b""

    def _headers_done(self) -> None:
        from python_multipart.multipart import parse_options_header

        _, opts = parse_options_header(self._headers.get("content-disposition", b""))
        name = opts.get(b"name", b"").decode("utf-8", "replace")
        if b"filename" in opts:
            if name != "file" or self._fh is not None:
                return  # ignore unexpected / extra files
            raw = opts[b"filename"].decode("utf-8", "replace")
            try:
                safe = sanitize_filename(raw)
            except ValueError as exc:
                raise UploadRejected(400, str(exc)) from None
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            final = self.dest_dir / f"{stamp}-{secrets.token_hex(3)}-{safe}"
            if final.resolve().parent != self.dest_dir.resolve():  # belt and braces
                raise UploadRejected(400, "נתיב קובץ לא חוקי")
            self.file_name, self.file_path = safe, final
            self._tmp = final.with_name(final.name + ".part")
            self._fh = open(self._tmp, "xb")
            self._part = "file"
        elif name in ("title",):
            self._part = name

    def _data(self, data: bytes, start: int, end: int) -> None:
        if self._part == "file":
            self.size += end - start
            if self.size > self.max_bytes:
                raise UploadRejected(413, "הקובץ גדול מדי")
            self._fh.write(data[start:end])
        elif self._part:
            self._buf += data[start:end]
            if len(self._buf) > 16 * 1024:
                raise UploadRejected(400, "שדה טקסט ארוך מדי")

    def _part_end(self) -> None:
        if self._part and self._part != "file":
            self.fields[self._part] = self._buf.decode("utf-8", "replace")
        self._part = None

    # lifecycle ---------------------------------------------------------------
    def write(self, chunk: bytes) -> None:
        self.parser.write(chunk)

    def finish(self) -> Path:
        self.parser.finalize()
        if self._fh is None or self.file_path is None or self._tmp is None:
            raise UploadRejected(400, "לא נשלח קובץ וידאו (שדה file)")
        self._fh.close()
        if self.size == 0:
            raise UploadRejected(400, "הקובץ ריק")
        self._tmp.replace(self.file_path)
        self._tmp = None
        return self.file_path

    def abort(self) -> None:
        if self._fh is not None and not self._fh.closed:
            self._fh.close()
        if self._tmp is not None:
            self._tmp.unlink(missing_ok=True)


def multipart_file(body: bytes, boundary: bytes, field: str = "file") -> bytes | None:
    """The bytes of the uploaded file in a small, already-buffered multipart body (the
    ``field`` part, else the first part with a filename). None when there is no file."""
    from python_multipart.multipart import MultipartParser, parse_options_header

    parts: list[tuple[dict[str, bytes], bytes]] = []
    cur: dict[str, Any] = {}

    def begin() -> None:
        cur.update(headers={}, name=b"", value=b"", data=bytearray())

    def header_field(data: bytes, start: int, end: int) -> None:
        cur["name"] += data[start:end]

    def header_value(data: bytes, start: int, end: int) -> None:
        cur["value"] += data[start:end]

    def header_end() -> None:
        cur["headers"][cur["name"].decode("latin-1").lower()] = cur["value"]
        cur["name"], cur["value"] = b"", b""

    def part_data(data: bytes, start: int, end: int) -> None:
        cur["data"] += data[start:end]

    def part_end() -> None:
        parts.append((cur["headers"], bytes(cur["data"])))

    parser = MultipartParser(boundary, {
        "on_part_begin": begin, "on_header_field": header_field,
        "on_header_value": header_value, "on_header_end": header_end,
        "on_part_data": part_data, "on_part_end": part_end})
    parser.write(body)
    parser.finalize()
    files = []
    for headers, data in parts:
        _, opts = parse_options_header(headers.get("content-disposition", b""))
        if b"filename" in opts:
            files.append((opts.get(b"name", b"").decode("utf-8", "replace"), data))
    for name, data in files:
        if name == field:
            return data
    return files[0][1] if files else None


async def read_body(request: Request, limit: int) -> bytes:
    """The request body, refused with 413 once it grows beyond ``limit`` bytes."""
    try:
        declared = int(request.headers.get("content-length", "0") or 0)
    except ValueError:
        declared = 0
    if declared > limit:
        raise HTTPException(413, "הקובץ או הבקשה גדולים מדי")
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > limit:
            raise HTTPException(413, "הקובץ או הבקשה גדולים מדי")
    return bytes(buf)


# --------------------------------------------------------------------------- request bodies
class NoteBody(BaseModel):
    note: str = Field(default="", max_length=MAX_NOTE_LEN)


class MoneyBody(BaseModel):
    amount: float
    category: str = Field(default="", max_length=100)
    source: str = Field(default="", max_length=100)
    description: str = Field(default="", max_length=500)
    platform: str | None = Field(default=None, max_length=40)
    video_id: int | None = None


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_CHAT_LEN)


class PlanEditBody(BaseModel):
    title: str | None = Field(default=None, max_length=MAX_PLAN_TITLE_LEN)
    description: str | None = Field(default=None, max_length=MAX_PLAN_DESCRIPTION_LEN)


class LiveBody(BaseModel):
    live: bool
    confirm: bool = False


class ScanBody(BaseModel):
    extra_path: str | None = Field(default=None, max_length=1024)


class ApplyBody(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=200)
    overwrite: bool = False


class PrepareBody(BaseModel):
    clip_id: str = Field(min_length=6, max_length=64)


# --------------------------------------------------------------------------- services
DASHBOARD_PID_KV = "dashboard.pid"   # the process that runs the dashboard (stalled-clip recovery)


def start_services(app: FastAPI, stop: threading.Event, *, interval_s: float = 60.0
                   ) -> list[str]:
    """Background work of a running dashboard, all ending when ``stop`` is set:

    - the scheduler (always) — publishes posts whose time has come;
    - the inbox watcher, when ``inbox.enabled`` (turning it on later starts it too);
    - the Telegram bot, when ``notify.telegram.enabled`` and a bot token exists.

    Returns the names of what was started.
    """
    import os

    from ..scheduler import recover_stalled_videos, run_forever

    ctx: Context = app.state.ctx
    app.state.services = {"stop": stop, "interval_s": float(interval_s)}
    # A clip that was being prepared when the previous dashboard stopped (update, restart,
    # sleep) will never finish by itself: mark it failed so the home offers "נסה שוב".
    try:
        previous = ctx.db.kv_get(DASHBOARD_PID_KV)
        ctx.db.kv_set(DASHBOARD_PID_KV, os.getpid())
        dead = (int(previous),) if str(previous or "").isdigit() else ()
        recover_stalled_videos(ctx, dead_pids=dead)
    except Exception:  # pragma: no cover - recovery is best-effort
        log.warning("recovering interrupted clips failed", exc_info=True)
    threading.Thread(target=run_forever, args=(ctx,),
                     kwargs={"interval_s": float(interval_s), "stop": stop},
                     daemon=True, name="scheduler").start()
    started = ["scheduler"]
    if ensure_inbox(app):
        started.append("inbox")
    if ctx.settings.get("notify.telegram.enabled", False) is True and ensure_telegram(app):
        started.append("telegram")
    threading.Thread(target=_stop_on, args=(stop,), daemon=True, name="services-stop").start()
    return started


def _stop_on(stop: threading.Event) -> None:
    """Waits for the shared stop event, then stops what has its own loop (Telegram)."""
    stop.wait()
    _stop_telegram()


def _stop_telegram() -> None:
    try:
        from ..notify.telegram import stop_telegram
        stop_telegram()
    except Exception:  # pragma: no cover - shutting down: never raise
        log.warning("stopping the Telegram bot failed", exc_info=True)


def stop_services(app: FastAPI) -> None:
    """Set the shared stop event and stop the Telegram bot now (on dashboard exit)."""
    services = getattr(app.state, "services", None)
    if services:
        services["stop"].set()
    _stop_telegram()


def _services(app: FastAPI) -> dict | None:
    services = getattr(app.state, "services", None)
    if not services or services["stop"].is_set():
        return None
    return services


def ensure_inbox(app: FastAPI) -> bool:
    """Start the inbox watcher if the services run and the owner turned the inbox on."""
    from ..inbox import is_enabled, start_inbox_watcher

    services = _services(app)
    if services is None or not is_enabled(app.state.ctx.settings):
        return False
    start_inbox_watcher(app.state.ctx, services["stop"])  # idempotent
    return True


def ensure_telegram(app: FastAPI) -> bool:
    """Start / stop the Telegram bot to match ``notify.telegram.enabled`` (when the services
    run). True when the bot runs afterwards."""
    services = _services(app)
    if services is None:
        return False
    ctx: Context = app.state.ctx
    try:
        from ..notify import telegram
        if ctx.settings.get("notify.telegram.enabled", False) is True:
            return telegram.start_telegram(ctx) is not None
        telegram.stop_telegram()
    except Exception:
        log.warning("Telegram bot could not be started", exc_info=True)
    return False


def _same_key(given: str, expected: str) -> bool:
    """Constant-time key check that never raises (non-ASCII input is simply wrong)."""
    import hmac

    return hmac.compare_digest(given.encode("utf-8", "surrogatepass"),
                               expected.encode("utf-8", "surrogatepass"))


# --------------------------------------------------------------------------- app factory
def create_app(ctx: Context | None = None) -> FastAPI:
    """Build the dashboard app. ``app.state.inline_jobs = True`` runs background work inline."""
    from ..settings_store import SettingsError

    ctx = ctx or build_context()
    app = FastAPI(title="CS2 Agency", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx
    app.state.inline_jobs = False
    app.state.jobs = []  # live background threads (list[threading.Thread])
    app.state.services = None
    app.state.import_scan = None
    jinja = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)),
                        autoescape=select_autoescape(["html"]))
    bind_host = str(ctx.settings.get("dashboard.host", "127.0.0.1") or "").strip().lower()
    port = dashboard_port(ctx.settings)
    remote_at_start = auth.remote_enabled(ctx.settings)
    extra_hosts = {str(h).lower() for h in (ctx.settings.get("dashboard.allowed_hosts", []) or [])}
    # The Host allow-list is ALWAYS on (DNS-rebinding defence), wildcard binds included:
    # there it also admits this machine's own names / interface IPs.
    allowed_hosts = LOCAL_HOSTS | extra_hosts
    if bind_host in WILDCARD_HOSTS:
        allowed_hosts |= machine_hosts()
    else:
        allowed_hosts.add(f"[{bind_host}]" if ":" in bind_host and not
                          bind_host.startswith("[") else bind_host)
    if remote_at_start or bind_host in WILDCARD_HOSTS:  # phones on the home Wi-Fi
        allowed_hosts |= {split_host(h) for h in auth.allowed_hosts(port, True)}
    loopback_only = bind_host in LOCAL_HOSTS
    # Legacy advanced mode: reachable from other devices WITHOUT phone access -> every remote
    # request needs the per-launch secret key. With phone access on, the PIN login is used.
    access_key = None if loopback_only or remote_at_start else secrets.token_urlsafe(24)
    app.state.access_key = access_key
    app.state.allowed_hosts = allowed_hosts
    app.state.bind_host = bind_host
    app.state.port = port
    if not remote_at_start:
        # Phone access is off (maybe switched off in config.yaml by hand): no phone session
        # or old PIN may survive until it is switched on again (ADVANCED.md §11).
        try:
            auth.revoke_all(ctx.db)
            auth.clear_pin(ctx.db)
        except Exception:  # pragma: no cover - a broken DB shows up everywhere else
            log.warning("could not clear phone sessions", exc_info=True)

    def run_job(fn: Callable[..., Any], *args: Any) -> None:
        if app.state.inline_jobs:
            fn(*args)
            return
        t = threading.Thread(target=fn, args=args, daemon=True, name=f"job-{fn.__name__}")
        app.state.jobs = [j for j in app.state.jobs if j.is_alive()] + [t]
        t.start()

    def secure(response, path: str):
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        # The login page brings its own (nonce) policy; everything else gets the default.
        response.headers.setdefault("Content-Security-Policy", DEFAULT_CSP)
        return response

    def deny(reason: str, path: str):
        status, body, redirect = auth.denial(reason, path)
        if redirect:
            return RedirectResponse(redirect, status_code=status)
        return JSONResponse(body, status_code=status)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        """Host check (DNS rebinding), who may come in (PC / logged-in phone / legacy key),
        CSRF header + Origin on writes, body size, security headers, no caching of the API."""
        host = request.headers.get("host") or ""
        path = request.url.path
        if split_host(host) not in allowed_hosts:
            return secure(JSONResponse({"detail": "host not allowed"}, status_code=403), path)
        set_cookie = False
        if not auth.is_loopback(auth.client_host(request)):
            if auth.remote_enabled(ctx.settings):
                reason = auth.require_auth(request, ctx)
                if reason:
                    return secure(deny(reason, path), path)
            elif access_key is not None:
                given = (request.cookies.get(ACCESS_COOKIE) or request.headers.get(ACCESS_HEADER)
                         or "")
                url_key = request.query_params.get("key") or ""
                if url_key and path == "/" and _same_key(url_key, access_key):
                    set_cookie = True
                elif not (given and _same_key(given, access_key)):
                    return secure(JSONResponse(
                        {"detail": "נדרש מפתח גישה: פתח את הקישור עם ?key= שהודפס כשהפעלת את "
                                   "לוח הבקרה"}, status_code=401), path)
            else:
                return secure(deny(auth.REASON_REMOTE_DISABLED, path), path)
        if request.method not in SAFE_METHODS:
            if request.headers.get(CSRF_HEADER) != "1":
                return secure(JSONResponse({"detail": "missing X-Agency-Request header"},
                                           status_code=403), path)
            origin = request.headers.get("origin") or ""
            if origin and origin != "null":
                from urllib.parse import urlsplit
                if urlsplit(origin).netloc.lower() != host.lower():
                    return secure(JSONResponse({"detail": "origin not allowed"},
                                               status_code=403), path)
            if path != "/api/upload":
                try:
                    declared = int(request.headers.get("content-length", "0") or 0)
                except ValueError:
                    declared = 0
                if declared > MAX_BODY_BYTES:
                    return secure(JSONResponse({"detail": "הבקשה גדולה מדי"},
                                               status_code=413), path)
        response = await call_next(request)
        if set_cookie:
            response.set_cookie(ACCESS_COOKIE, access_key, httponly=True, samesite="strict",
                                path="/")
        return secure(response, path)

    @app.exception_handler(Denied)
    async def _denied(request: Request, exc: Denied):
        return deny(exc.reason, request.url.path)

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError):
        # FastAPI's default echoes the submitted values ("input"): never send them back.
        errors = [{"loc": [str(p) for p in (e.get("loc") or [])], "msg": str(e.get("msg", "")),
                   "type": str(e.get("type", ""))} for e in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    def local_only(request: Request) -> None:
        """Dependency: PC-only actions (secrets, PIN, the PC's disk, the PC's browser)."""
        reason = auth.require_local(request)
        if reason:
            raise Denied(reason)

    pc_only = [Depends(local_only)]

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ---------------------------------------------------------------- pages
    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        html = jinja.get_template("index.html").render(
            channel=ctx.settings.get("channel.name", "") or "", dry_run=ctx.settings.dry_run,
            max_upload_gb=round(_max_upload_bytes() / 1024 ** 3, 2),
            extensions=" ".join(sorted(ALLOWED_EXTENSIONS)))
        return HTMLResponse(html)

    @app.get("/login", response_class=HTMLResponse)
    def login_page() -> HTMLResponse:
        nonce = auth.new_nonce()
        return HTMLResponse(auth.render_login(nonce),
                            headers={"Content-Security-Policy": auth.login_csp(nonce),
                                     "Cache-Control": "no-store"})

    def _max_upload_bytes() -> int:
        gb = ctx.settings.get("dashboard.max_upload_gb")
        try:
            return int(float(gb) * 1024 ** 3) if gb else DEFAULT_MAX_UPLOAD_BYTES
        except (TypeError, ValueError):
            return DEFAULT_MAX_UPLOAD_BYTES

    # ---------------------------------------------------------------- health / home
    @app.get("/api/health")
    def api_health() -> dict:
        return {"ok": True, "app": "cs2-agency"}

    @app.get("/api/home")
    def api_home() -> dict:
        """The home screen in one call (SPEC_V2 §1). Rule-based and truthful."""
        from .. import home
        from ..brand import load_brand

        now = datetime.now(timezone.utc)
        manager = home.home_sentence(ctx, now)
        try:
            from ..inbox import inbox_status
            inbox = inbox_status(ctx)
        except Exception:
            inbox = {"enabled": False, "baseline": []}
        return {
            "greeting": manager["greeting"], "sentence": manager["sentence"],
            "text": manager["text"], "mood": manager["mood"], "counts": manager["counts"],
            "mode": "training" if manager["training"] else "live",
            "live_platforms": [views.platform_chip(k) for k in manager["live_platforms"]],
            "channel": load_brand(ctx.settings).name or views.CHANNEL_DEFAULT,
            "needs_you": views.needs_you(ctx, now),
            "results": home.results_summary(ctx, 7, now),
            "money": views.money_line(budget_status(ctx)),
            "team": home.team_view(ctx, now),
            "processing": home.processing_view(ctx, now),
            "studio": views.studio_summary(ctx),
            "security": auth.security_alert(ctx.db),
            "inbox": {"enabled": bool(inbox.get("enabled"))},
            "previous_clips": views.previous_clips(ctx, inbox),
            "server_time": _now_iso(),
        }

    @app.get("/api/videos/{vid}/thumbnail")
    def api_thumbnail(vid: int) -> FileResponse:
        path = views.thumbnail_path(ctx, ctx.db.get_video(vid))
        if path is None:
            raise HTTPException(404, "אין תמונה לסרטון הזה")
        return FileResponse(path, media_type=views.THUMB_TYPES[path.suffix.lower()])

    # ---------------------------------------------------------------- read API
    @app.get("/api/status")
    def api_status() -> dict:
        return status_snapshot(ctx)

    @app.get("/api/videos")
    def api_videos(limit: int = Query(100, ge=1, le=1000)) -> list[dict]:
        return [video_view(ctx, v) for v in ctx.db.list_videos(limit=limit)]

    @app.get("/api/posts")
    def api_posts(video_id: int | None = None) -> list[dict]:
        return [post_view(p) for p in ctx.db.list_posts(video_id)]

    @app.get("/api/approvals")
    def api_approvals(status: str = Query(
            "pending", pattern="^(pending|approved|executing|rejected|executed|all|open)$")
                      ) -> list[dict]:
        if status == "open":  # waiting for the owner + approved-but-interrupted (resumable)
            rows = ctx.db.list_approvals("pending") + ctx.db.list_approvals("approved")
        else:
            rows = ctx.db.list_approvals(None if status == "all" else status)
        return [approval_view(ctx, a) for a in rows]

    @app.get("/api/finance")
    def api_finance(month: str | None = Query(None, pattern=r"^\d{4}-\d{2}$")) -> dict:
        month = month or current_month()
        status = budget_status(ctx) if month == current_month() else None
        if status is None:
            try:
                from ..staff.finance import CFO
                status = CFO(ctx).budget_status(month)
            except ImportError:
                status = budget_status(ctx)
        entries = [e.to_dict() for e in ctx.db.list_ledger(month)]
        pending = [approval_view(ctx, a) for a in ctx.db.list_approvals("pending")
                   if a.kind == "spend"]
        return {"month": month, "budget": status, "entries": entries, "pending_spend": pending}

    @app.get("/api/events")
    def api_events(limit: int = Query(100, ge=1, le=500)) -> list[dict]:
        return ctx.db.list_events(limit=limit)

    @app.get("/api/platforms")
    def api_platforms() -> dict:
        return {"dry_run": ctx.settings.dry_run,
                "mode": ctx.settings.get("publishing.mode", "approve"),
                "platforms": platforms_snapshot(ctx.settings)}

    # ---------------------------------------------------------------- approvals
    @app.post("/api/approvals/{aid}/approve")
    def api_approve(aid: int, body: NoteBody | None = None) -> dict:
        note = clean_text(body.note if body else "", MAX_NOTE_LEN)
        try:
            approval = ctx.db.decide_approval(aid, True, note)
        except KeyError:
            raise HTTPException(404, "האישור לא נמצא") from None
        except ValueError as exc:
            current = ctx.db.get_approval(aid)
            if current is None or current.status != "approved":
                raise HTTPException(409, f"אי אפשר לאשר: {exc}") from None
            approval = current  # approved earlier, execution interrupted -> resume it
            emit(ctx.db, "owner", f"הבעלים המשיך ביצוע של #{aid}: {approval.title}")
        else:
            emit(ctx.db, "owner", f"הבעלים אישר את #{aid}: {approval.title}", "success")
        if approval.kind == "publish" and not app.state.inline_jobs:
            run_job(execute_approval_safely, ctx, aid)
            return {"ok": True, "approval": approval_view(ctx, approval), "executing": True}
        result = execute_approval_safely(ctx, aid)
        fresh = ctx.db.get_approval(aid) or approval
        return {"ok": bool(result.get("ok", True)), "approval": approval_view(ctx, fresh),
                "result": result, "executing": False}

    @app.post("/api/approvals/{aid}/reject")
    def api_reject(aid: int, body: NoteBody | None = None) -> dict:
        note = clean_text(body.note if body else "", MAX_NOTE_LEN)
        try:
            approval = ctx.db.decide_approval(aid, False, note)
        except KeyError:
            raise HTTPException(404, "האישור לא נמצא") from None
        except ValueError as exc:
            raise HTTPException(409, f"אי אפשר לדחות: {exc}") from None
        emit(ctx.db, "owner", f"הבעלים דחה את #{aid}: {approval.title}", "warning")
        # execute_approval on a *rejected* approval only cleans up (cancels its posts).
        cleanup = execute_approval_safely(ctx, aid)
        return {"ok": True, "approval": approval_view(ctx, approval),
                "cancelled_post_ids": cleanup.get("cancelled_post_ids", [])}

    @app.patch("/api/approvals/{aid}/plans/{platform}")
    def api_edit_plan(aid: int, platform: str, body: PlanEditBody) -> dict:
        """"פרטים" → edit one platform's title/description, only while still pending."""
        return edit_pending_plan(ctx, aid, platform, body.title, body.description)

    # ---------------------------------------------------------------- videos
    @app.post("/api/videos/{vid}/retry")
    def api_retry_video(vid: int) -> dict:
        """"נסה שוב" on a clip whose preparation got stuck: the team prepares it again (it
        ends at a new approval card, nothing is published)."""
        from ..pipeline import running_videos

        video = ctx.db.get_video(vid)
        if video is None:
            raise HTTPException(404, "הסרטון לא נמצא")
        if video.status != VideoStatus.FAILED or not (video.meta or {}).get("failed_step") \
                or any(p["status"] != "cancelled" for p in ctx.db.list_posts(vid)):
            raise HTTPException(409, "אפשר לנסות שוב רק קליפ שההכנה שלו נתקעה")
        if vid in running_videos():
            raise HTTPException(409, "הצוות כבר עובד על הקליפ הזה")
        emit(ctx.db, "owner", "הבעלים ביקש לנסות שוב להכין את הקליפ", "info", vid)
        if not app.state.inline_jobs:
            run_job(retry_video_safely, ctx, vid)
            return {"ok": True, "executing": True}
        result = retry_video_safely(ctx, vid)
        return {"ok": bool(result.get("ok")), "executing": False,
                "approval_id": result.get("approval_id"), "error": result.get("error")}

    # ---------------------------------------------------------------- posts
    @app.post("/api/posts/{post_id}/retry")
    def api_retry_post(post_id: int) -> dict:
        """Owner retries a failed post (it was approved already; no new approval)."""
        post = ctx.db.get_post(post_id)
        if post is None:
            raise HTTPException(404, "הפוסט לא נמצא")
        if post["status"] != "failed":
            raise HTTPException(409, "אפשר לנסות שוב רק פוסט שנכשל")
        emit(ctx.db, "owner", f"הבעלים ביקש ניסיון חוזר לפוסט #{post_id}", "info",
             post["video_id"])
        if not app.state.inline_jobs:
            run_job(retry_post_safely, ctx, post_id)
            return {"ok": True, "executing": True}
        result = retry_post_safely(ctx, post_id)
        return {"ok": bool(result.get("ok")), "result": result, "executing": False}

    # ---------------------------------------------------------------- finance
    def _amount(value: float) -> float:
        if not math.isfinite(value) or value <= 0 or value > MAX_AMOUNT_ILS:
            raise HTTPException(422, "סכום חייב להיות מספר חיובי")
        return round(float(value), 2)

    @app.post("/api/finance/expense")
    def api_expense(body: MoneyBody) -> dict:
        from ..staff.finance import CFO

        amount = _amount(body.amount)
        category = clean_text(body.category or body.source, 100) or "כללי"
        eid = CFO(ctx).record_expense(amount, category, clean_text(body.description, 500),
                                      platform=body.platform, video_id=body.video_id)
        return {"ok": True, "id": eid, "budget": budget_status(ctx)}

    @app.post("/api/finance/income")
    def api_income(body: MoneyBody) -> dict:
        from ..staff.finance import CFO

        amount = _amount(body.amount)
        source = clean_text(body.source or body.category, 100) or "כללי"
        iid = CFO(ctx).record_income(amount, source, clean_text(body.description, 500),
                                     platform=body.platform, video_id=body.video_id)
        return {"ok": True, "id": iid, "budget": budget_status(ctx)}

    # ---------------------------------------------------------------- chat
    @app.post("/api/chat")
    def api_chat(body: ChatBody) -> dict:
        message = clean_text(body.message, MAX_CHAT_LEN)
        if not message:
            raise HTTPException(422, "הודעה ריקה")
        from ..llm import LLMError
        from ..staff.manager import Manager

        emit(ctx.db, "owner", f"הודעה למנכ\"ל: {message[:200]}")
        try:
            reply = Manager(ctx).chat(message)
        except LLMError as exc:
            raise HTTPException(502, f"המנכ\"ל לא זמין כרגע: {exc}") from None
        return {"reply": str(reply), "mock": bool(getattr(ctx.llm, "mock", False))}

    # ---------------------------------------------------------------- settings (§3)
    def _training() -> bool:
        from ..home import live_platforms
        return not live_platforms(ctx)

    def settings_payload() -> dict:
        out = views.settings_view(ctx)
        remote = auth.status_summary(ctx.db, ctx.settings)
        bound = app.state.bind_host not in LOCAL_HOSTS
        out["info"].update(training=_training(), remote_devices=remote["devices"],
                           restart_needed=bool(remote["remote_enabled"] and not bound))
        return out

    @app.get("/api/settings")
    def api_settings() -> dict:
        return settings_payload()

    @app.put("/api/settings")
    def api_put_settings(request: Request, body: dict = Body(...)) -> dict:
        """Save owner settings (whitelisted fields only) through the safe config writer."""
        from ..inbox import is_enabled, set_enabled
        from ..settings_store import update_config

        body = {k: v for k, v in body.items() if k != "info"}
        pairs = views.flatten(body)
        unknown = [k for k, _v in pairs if k not in views.SETTINGS_FIELDS]
        if unknown:
            raise HTTPException(400, "הגדרה לא מוכרת: " + ", ".join(sorted(unknown)[:5]))
        if not pairs:
            raise HTTPException(400, "לא נשלחו הגדרות לשמירה")
        current = dict(views.flatten({k: v for k, v in views.settings_view(ctx).items()
                                      if k != "info"}))
        changed = dict(pairs)
        if any(k in views.LOCAL_ONLY_SETTINGS and changed[k] != current.get(k)
               for k in changed) and auth.require_local(request):
            raise Denied(auth.REASON_LOCAL_ONLY)
        remote_before = auth.remote_enabled(ctx.settings)
        telegram_before = ctx.settings.get("notify.telegram.enabled", False) is True
        turn_inbox_on = changed.get("inbox.enabled") is True and not is_enabled(ctx.settings)
        if turn_inbox_on:
            changed.pop("inbox.enabled")
        try:
            if changed:
                update_config(ctx.settings, views.nest(list(changed.items())))
            if turn_inbox_on:
                set_enabled(ctx, True)  # records the clips already there first (not processed)
        except SettingsError as exc:
            raise HTTPException(400, str(exc)) from None
        if remote_before and not auth.remote_enabled(ctx.settings):
            auth.revoke_all(ctx.db)   # phone access off: every phone is logged out
            auth.clear_pin(ctx.db)    # and the next switch-on shows a fresh PIN
        ensure_inbox(app)
        if telegram_before != (ctx.settings.get("notify.telegram.enabled", False) is True):
            ensure_telegram(app)
        emit(ctx.db, "owner", "ההגדרות נשמרו")
        return {"ok": True, "saved": sorted(k for k, _v in pairs), "settings": settings_payload()}

    # ---------------------------------------------------------------- phone access (§6)
    @app.post("/api/auth/login")
    async def api_login(request: Request) -> JSONResponse:
        raw = await read_body(request, 4096)
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {}
        pin = data.get("pin") if isinstance(data, dict) else None
        label = "".join(ch for ch in (request.headers.get("user-agent") or "")
                        if ch.isprintable())[:80]
        out = await asyncio.to_thread(auth.login, ctx.db, pin, auth.client_host(request),
                                      label=label)
        response = JSONResponse(out.body, status_code=out.status, headers=out.headers)
        if out.token:
            response.set_cookie(auth.COOKIE_NAME, out.token, **auth.COOKIE_KWARGS)
        return response

    @app.post("/api/auth/logout")
    def api_logout(request: Request) -> JSONResponse:
        token = request.cookies.get(auth.COOKIE_NAME)
        revoked = auth.revoke_session(ctx.db, token) if token else False
        response = JSONResponse({"ok": True, "revoked": bool(revoked)})
        response.delete_cookie(auth.COOKIE_NAME, **auth.COOKIE_DELETE_KWARGS)
        return response

    @app.post("/api/auth/revoke-all", dependencies=pc_only)
    def api_revoke_all() -> dict:
        count = auth.revoke_all(ctx.db)
        emit(ctx.db, "owner", "כל המכשירים נותקו מהגישה מהטלפון", "warning")
        return {"ok": True, "revoked": count}

    def remote_payload(pairing: dict | None = None) -> dict:
        summary = auth.status_summary(ctx.db, ctx.settings)
        bound = app.state.bind_host not in LOCAL_HOSTS
        out = {**summary, "port": port, "bound_to_lan": bound,
               "restart_needed": bool(summary["remote_enabled"] and not bound),
               "pin": None, "url": None, "urls": [], "qr_svg": None}
        if not summary["remote_enabled"]:
            return out
        if pairing is not None:
            out.update(pin=pairing["pin"], url=pairing["url"], urls=pairing["urls"],
                       qr_svg=pairing["qr_svg"], pin_set=True,
                       pin_created_at=auth.pin_created_at(ctx.db))
            return out
        urls = auth.phone_urls(port)
        out.update(urls=urls, url=urls[0] if urls else None,
                   qr_svg=auth.qr_svg(urls[0]) if urls else None)
        return out

    @app.get("/api/remote", dependencies=pc_only)
    def api_remote() -> dict:
        """Phone access screen (PC only). Read-only: a PIN is made only by ``POST
        /api/remote/pin`` (a GET cannot be forged into rotating it from another site); the
        page asks for one when ``pin_set`` is false."""
        return remote_payload()

    @app.post("/api/remote/pin", dependencies=pc_only)
    def api_remote_new_pin() -> dict:
        """"קוד חדש": a fresh PIN, shown once (phones already logged in stay logged in)."""
        if not auth.remote_enabled(ctx.settings):
            raise HTTPException(409, auth.MSG_REMOTE_DISABLED)
        return remote_payload(auth.start_pairing(ctx.db, port))

    # ---------------------------------------------------------------- connections (§2, §11)
    def _connect_key(platform: str) -> str:
        from .. import connect
        if platform not in connect.CONNECT_KEYS:
            raise HTTPException(404, "פלטפורמה לא מוכרת")
        return platform

    def _tile(key: str) -> dict:
        from .. import connect
        tile = views.tile_view(connect.platform_state(key, ctx.settings, ctx.db, check=False))
        if key == "telegram":
            from ..notify.telegram import telegram_status
            tile["telegram"] = telegram_status(ctx)
        return tile

    @app.get("/api/connections")
    def api_connections(check: bool = True) -> dict:
        """All tiles (``check`` = really verify, cached 10 minutes). Names only, never values."""
        from .. import connect
        from ..config import keyring_usable
        from ..notify.telegram import telegram_status

        tiles = [views.tile_view(t) for t in connect.state(ctx.settings, ctx.db, check=check,
                                                           keys=connect.CONNECT_KEYS)]
        status = telegram_status(ctx)
        for tile in tiles:
            if tile["key"] == "telegram":
                tile["telegram"] = status
        return {"tiles": tiles, "telegram": status, "studio": views.studio_summary(ctx),
                "keys_saved_in": "keyring" if keyring_usable() else "file",
                "training": _training()}

    @app.post("/api/connect/youtube/client-secret", dependencies=pc_only)
    async def api_youtube_client_secret(request: Request) -> dict:
        """The Google ``client_secret*.json`` (raw JSON body or multipart field ``file``)."""
        from python_multipart.multipart import parse_options_header

        from .. import connect

        raw = await read_body(request, MAX_CLIENT_SECRET_BYTES)
        ctype, opts = parse_options_header(request.headers.get("content-type", ""))
        data: bytes | None = raw
        if ctype == b"multipart/form-data":
            boundary = opts.get(b"boundary")
            try:
                data = multipart_file(raw, boundary) if boundary else None
            except Exception:
                data = None
        if not data:
            raise HTTPException(400, "לא נשלח קובץ")
        try:
            result = await asyncio.to_thread(connect.save_client_secret, ctx.settings, data)
        except connect.ConnectError as exc:
            raise HTTPException(400, str(exc)) from None
        emit(ctx.db, "owner", "קובץ ההתחברות של גוגל נשמר")
        return {**result, "tile": _tile("youtube")}

    @app.post("/api/connect/youtube/start", dependencies=pc_only)
    def api_youtube_start() -> dict:
        """"התחבר עם גוגל": Google's login opens in the browser on this PC."""
        from .. import connect

        return connect.start_youtube_oauth(ctx.settings, open_browser=True,
                                           background=not app.state.inline_jobs)

    @app.get("/api/connect/youtube/state")
    def api_youtube_state() -> dict:
        from .. import connect

        return connect.youtube_oauth_state()

    @app.post("/api/connect/import/scan", dependencies=pc_only)
    def api_import_scan(body: ScanBody | None = None) -> dict:
        """"ייבוא חיבורים קיימים": what the old studio and this PC already have (names and
        paths only). The results stay on the server; apply refers to them by id."""
        from .. import importer

        extra: Path | None = None
        text = (body.extra_path or "").strip() if body else ""
        if text:
            if "\x00" in text:
                raise HTTPException(400, "נתיב לא תקין")
            extra = Path(text).expanduser()
            if not extra.exists():
                raise HTTPException(400, "לא מצאתי את התיקייה הזאת")
        report = importer.studio_report(ctx.settings, extra)
        found = importer.scan([extra] if extra else [], ctx.settings)
        if report.get("found") and not any(f.kind == "studio" and Path(f.path) == Path(
                report["dir"]) for f in found):
            found.insert(0, importer.Found("studio", str(report["dir"]), "נמצא הסטודיו הקיים"))
        app.state.import_scan = {"at": time.time(), "items": {f.id: f for f in found}}
        return {"ok": True, "studio": report,
                "items": [{**f.to_dict(), "selected": bool(f.ok)} for f in found]}

    @app.post("/api/connect/import/apply", dependencies=pc_only)
    def api_import_apply(body: ApplyBody) -> dict:
        from .. import connect, importer

        scan = app.state.import_scan
        if not scan or time.time() - scan["at"] > IMPORT_SCAN_TTL_S:
            raise HTTPException(409, "צריך לסרוק שוב")
        wanted = list(dict.fromkeys(str(i) for i in body.ids))
        items = [scan["items"][i] for i in wanted if i in scan["items"]]
        unknown = [i for i in wanted if i not in scan["items"]]
        if not items:
            raise HTTPException(400, "לא נבחר שום דבר לייבוא")
        results = importer.import_found(items, ctx.settings, overwrite=body.overwrite,
                                        verify=True)
        results += [{"id": i, "kind": "", "path": "", "imported": [], "skipped": [],
                     "problem": "הפריט לא נמצא — סרוק שוב", "verify": {}} for i in unknown]
        # .env keys were invalidated + verified by import_found; Google files and the studio
        # link change what the YouTube tiles show, so those are checked afresh next time.
        if any(r.get("imported") and r.get("kind") in ("google_client_secret", "google_token",
                                                       "studio") for r in results):
            connect.invalidate(ctx.settings, "youtube", ctx.db)
        views.clear_studio_cache()
        emit(ctx.db, "owner", f"ייבוא חיבורים: {sum(1 for r in results if r.get('imported'))} "
                              "פריטים יובאו")
        tiles = connect.state(ctx.settings, ctx.db, check=False, keys=connect.CONNECT_KEYS)
        return {"ok": not any(r.get("problem") for r in results), "results": results,
                "next_connect": importer.next_connect_steps(),
                "tiles": [views.tile_view(t) for t in tiles]}

    @app.post("/api/connect/{platform}/keys", dependencies=pc_only)
    async def api_connect_keys(platform: str, request: Request) -> dict:
        """Keys typed in the wizard ``{"values": {NAME: value}}``: only this platform's names,
        stored in the OS keyring (else .env), then verified for real. Never echoed back."""
        from .. import connect

        key = _connect_key(platform)
        raw = await read_body(request, MAX_KEYS_BODY_BYTES)
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            raise HTTPException(400, "פרטי החיבור לא תקינים") from None
        values = data.get("values") if isinstance(data, dict) else None
        if not isinstance(values, dict) or not values or len(values) > 12:
            raise HTTPException(400, "פרטי החיבור לא תקינים")
        if not all(isinstance(n, str) and _ENV_NAME_RE.match(n) for n in values):
            raise HTTPException(400, "שם שדה לא תקין")
        if not all(isinstance(v, str) for v in values.values()):
            raise HTTPException(400, "כל ערך חייב להיות טקסט")
        try:
            tile = await asyncio.to_thread(connect.save_keys, key, values, ctx.settings, ctx.db)
        except (connect.ConnectError, SettingsError) as exc:
            raise HTTPException(400, str(exc)) from None
        except Exception as exc:  # never put exception text (could hold a value) in a reply
            log.warning("saving keys for %s failed: %s", key, type(exc).__name__)
            raise HTTPException(500, "השמירה נכשלה — נסה שוב") from None
        saved = list(tile.pop("saved", []))
        if saved:
            emit(ctx.db, "owner", f"המפתחות ל{connect.NAMES_HE.get(key, key)} נשמרו")
        if key == "telegram":
            ensure_telegram(app)
        out = views.tile_view(tile)
        return {"ok": bool(out.get("connected")), "saved": saved, "tile": out}

    @app.post("/api/connect/{platform}/verify")
    def api_connect_verify(platform: str) -> dict:
        """Check the connection for real now (read-only call, no cache)."""
        from .. import connect

        key = _connect_key(platform)
        connect.invalidate(ctx.settings, key, ctx.db)
        result = connect.verify(key, ctx.settings, use_cache=False, db=ctx.db)
        return {"ok": bool(result.get("connected")), "tile": _tile(key)}

    @app.post("/api/connect/{platform}/disconnect", dependencies=pc_only)
    def api_connect_disconnect(platform: str) -> dict:
        """"נתק": remove OUR stored keys / token file (the old studio's are never touched)."""
        from .. import connect

        key = _connect_key(platform)
        out = connect.disconnect(key, ctx.settings, ctx.db)
        emit(ctx.db, "owner", f"החיבור ל{connect.NAMES_HE.get(key, key)} נותק", "warning")
        if key == "telegram":
            ensure_telegram(app)
        return {"ok": True, "removed": out["removed"], "kept_shared": out["kept_shared"],
                "studio": out["studio"], "system": out.get("system", []),
                "also_disconnected": out.get("also_disconnected", []), "tile": _tile(key)}

    @app.post("/api/connect/{platform}/live", response_model=None)
    def api_connect_live(platform: str, body: LiveBody) -> Any:
        """The per-platform "העלאה אמיתית" switch. Turning it ON needs ``confirm: true``."""
        from ..platforms import registry
        from ..settings_store import set_platform_live

        if platform not in registry.ALL_KEYS:
            raise HTTPException(404, "פלטפורמה לא מוכרת")
        name = views.platform_chip(platform)["name_he"]
        if body.live and not body.confirm:
            question = f"מעכשיו הקליפים יעלו באמת ל{name}. בטוח?"
            return JSONResponse({"ok": False, "reason": "confirm_required", "detail": question,
                                 "confirm_he": question}, status_code=400)
        try:
            set_platform_live(ctx.settings, platform, body.live)
        except SettingsError as exc:
            raise HTTPException(409, str(exc)) from None
        emit(ctx.db, "owner", f"{name}: העלאה אמיתית הופעלה" if body.live
             else f"{name}: חזר למצב אימון", "warning" if body.live else "info")
        out: dict[str, Any] = {"ok": True, "tile": _tile(platform), "warning_he": ""}
        if body.live:
            studio = views.studio_summary(ctx)
            if studio["daemon_active"]:
                out["warning_he"] = studio["question_he"]
        return out

    # ---------------------------------------------------------------- telegram (§11.5)
    def _bot_name() -> str | None:
        from .. import connect
        try:
            return connect.platform_state("telegram", ctx.settings, ctx.db,
                                          check=False).get("account_name")
        except Exception:
            return None

    @app.post("/api/telegram/pair", dependencies=pc_only)
    def api_telegram_pair() -> dict:
        """A 6-digit pairing code for the PC screen; the owner sends ``/start <code>``.

        Re-pairing first forgets the chat paired until now, so a lost or compromised phone
        loses its approve buttons at once. ``issued_at`` lets the page wait for THIS pairing
        (``/api/telegram/state`` -> ``paired_at`` newer than it)."""
        from ..config import secret
        from ..notify import telegram
        from ..settings_store import update_config

        if not secret("TELEGRAM_BOT_TOKEN"):
            raise HTTPException(409, "קודם מדביקים את המפתח של הבוט (חיבורים ← טלגרם)")
        enabled_now = ctx.settings.get("notify.telegram.enabled", False) is not True
        if enabled_now:
            try:
                update_config(ctx.settings, {"notify": {"telegram": {"enabled": True}}})
            except SettingsError as exc:
                raise HTTPException(400, str(exc)) from None
        ensure_telegram(app)
        issued_at = time.time()
        code = telegram.new_pairing_code(ctx, unpair=True)
        bot = _bot_name()
        return {"ok": True, "code": code, "expires_in_s": telegram.PAIRING_TTL_S, "bot": bot,
                "issued_at": issued_at,
                "instructions_he": f"פתח את הבוט{' ' + bot if bot else ''} בטלגרם ושלח לו: "
                                   f"/start {code}",
                "enabled_now": enabled_now, "status": telegram.telegram_status(ctx)}

    @app.get("/api/telegram/state")
    def api_telegram_state() -> dict:
        from ..notify.telegram import telegram_status

        return {**telegram_status(ctx), "bot": _bot_name()}

    # ---------------------------------------------------------------- inbox (§11.12)
    @app.get("/api/inbox")
    def api_inbox() -> dict:
        from ..inbox import inbox_status

        status = inbox_status(ctx)
        return {"enabled": status["enabled"], "folders": status["folders"],
                "previous_clips": views.previous_clips(ctx, status),
                "waiting": [{"name": w.get("name")} for w in status["waiting"]],
                "done": [{"name": d.get("name"), "action": d.get("action"),
                          "video_id": d.get("video_id")} for d in status["done"]]}

    @app.post("/api/inbox/prepare")
    def api_inbox_prepare(body: PrepareBody) -> dict:
        """"תכין לפרסום" for one previous clip (it ends at an approval, nothing is published)."""
        from ..inbox import check_prepare, inbox_status

        clip = views.find_clip(inbox_status(ctx), body.clip_id)
        if clip is None:
            raise HTTPException(404, "הקליפ הזה לא ברשימת הקליפים הקודמים")
        problem = check_prepare(ctx, clip["path"])  # answer now, not silently in the background
        if problem:
            raise HTTPException(409, problem)
        emit(ctx.db, "owner", f"הבעלים ביקש להכין לפרסום: {clip.get('name')}")
        if not app.state.inline_jobs:
            run_job(prepare_clip_safely, ctx, clip["path"])
            return {"ok": True, "executing": True, "name": clip.get("name")}
        result = prepare_clip_safely(ctx, clip["path"])
        return {"ok": bool(result.get("ok")), "executing": False, "name": clip.get("name"),
                "video_id": result.get("video_id"), "error": result.get("error")}

    # ---------------------------------------------------------------- upload
    @app.post("/api/upload")
    async def api_upload(request: Request) -> dict:
        from python_multipart.multipart import parse_options_header

        max_bytes = _max_upload_bytes()
        ctype, opts = parse_options_header(request.headers.get("content-type", ""))
        boundary = opts.get(b"boundary")
        if ctype != b"multipart/form-data" or not boundary:
            raise HTTPException(400, "יש לשלוח multipart/form-data")
        try:
            declared = int(request.headers.get("content-length", "0") or 0)
        except ValueError:
            declared = 0
        if declared > max_bytes + 1024 * 1024:
            raise HTTPException(413, "הקובץ גדול מדי (מקסימום "
                                     f"{max_bytes / 1024 ** 3:.1f}GB)")
        dest_dir = ctx.settings.data_dir / "videos"
        dest_dir.mkdir(parents=True, exist_ok=True)
        upload = _MultipartUpload(boundary, dest_dir, max_bytes)
        try:
            async for chunk in request.stream():
                if chunk:
                    await asyncio.to_thread(upload.write, chunk)
            path = await asyncio.to_thread(upload.finish)
        except UploadRejected as exc:
            upload.abort()
            raise HTTPException(exc.status, exc.message) from None
        except HTTPException:
            upload.abort()
            raise
        except Exception as exc:
            upload.abort()
            log.warning("upload failed: %s", type(exc).__name__)
            raise HTTPException(400, "ההעלאה נכשלה — אולי אין מספיק מקום בדיסק. נסה שוב") \
                from None

        title = clean_text(upload.fields.get("title", ""), MAX_TITLE_LEN)
        vid = ctx.db.add_video(str(path), title)
        ctx.db.update_video(vid, meta={"original_name": upload.file_name, "size_bytes": upload.size,
                                       "source": "dashboard"})
        emit(ctx.db, "owner", f"סרטון חדש הועלה: {upload.file_name}", "info", vid)
        run_job(run_pipeline_safely, ctx, vid)
        return {"ok": True, "video_id": vid, "file_name": path.name, "size": upload.size}

    return app
