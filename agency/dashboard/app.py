"""FastAPI dashboard: JSON API + a single Hebrew RTL page.

Staff / pipeline / scheduler modules are imported lazily inside handlers so this module
imports even while those parts are missing or broken.
"""
from __future__ import annotations

import asyncio
import math
import re
import secrets
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field

from ..events import emit
from ..models import Approval, VideoStatus
from ..staff.base import Context, build_context

HERE = Path(__file__).resolve().parent
TEMPLATES_DIR = HERE / "templates"
STATIC_DIR = HERE / "static"

ALLOWED_EXTENSIONS: frozenset[str] = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi"})
DEFAULT_MAX_UPLOAD_BYTES = 4 * 1024 ** 3  # 4 GB
MAX_TITLE_LEN = 200
MAX_CHAT_LEN = 4000
MAX_NOTE_LEN = 1000
MAX_AMOUNT_ILS = 10_000_000

# Header every state-changing request must carry. Browsers cannot add a custom header to a
# cross-site request without a CORS preflight (which we never allow), so this blocks CSRF.
CSRF_HEADER = "x-agency-request"

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
WILDCARD_HOSTS = {"0.0.0.0", "::", "[::]", ""}

# Non-loopback binds need this per-launch key (in the printed URL -> HttpOnly cookie).
ACCESS_COOKIE = "agency_key"
ACCESS_HEADER = "x-agency-key"

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
    "finance": "מנהל כספים (CFO)",
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
    """Per platform: enabled in config, credentials present (names of missing secrets only)."""
    from ..platforms import registry

    rows = []
    for key in registry.ALL_KEYS:
        cfg = settings.get(f"publishing.platforms.{key}", {}) or {}
        row: dict[str, Any] = {"key": key, "display_he": PLATFORM_TITLES.get(key, key),
                               "enabled": bool(cfg.get("enabled", False)),
                               "dry_run": settings.dry_run, "configured": False,
                               "missing_secrets": [], "error": None}
        try:
            adapter = registry.get_adapter(key, settings)
            row["display_he"] = getattr(adapter, "display_he", row["display_he"])
            row["configured"] = bool(adapter.configured())
            missing = getattr(adapter, "missing_secrets", None)
            row["missing_secrets"] = list(missing()) if callable(missing) else []
        except Exception as exc:  # adapter module missing/broken: show it, do not crash
            row["error"] = f"{type(exc).__name__}: {exc}"
        if key == "reddit":
            row["subreddits"] = list(cfg.get("subreddits") or [])
        row["live"] = row["enabled"] and row["configured"] and not settings.dry_run
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
            "stats": p.get("stats") or {}, "why": (plan.get("extra") or {}).get("why", "")}


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


# --------------------------------------------------------------------------- app factory
def create_app(ctx: Context | None = None) -> FastAPI:
    """Build the dashboard app. ``app.state.inline_jobs = True`` runs background work inline."""
    ctx = ctx or build_context()
    app = FastAPI(title="CS2 Agency", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx
    app.state.inline_jobs = False
    app.state.jobs = []  # live background threads (list[threading.Thread])
    jinja = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)),
                        autoescape=select_autoescape(["html"]))
    bind_host = str(ctx.settings.get("dashboard.host", "127.0.0.1") or "").strip().lower()
    extra_hosts = {str(h).lower() for h in (ctx.settings.get("dashboard.allowed_hosts", []) or [])}
    # The Host allow-list is ALWAYS on (DNS-rebinding defence), wildcard binds included:
    # there it also admits this machine's own names / interface IPs.
    allowed_hosts = LOCAL_HOSTS | extra_hosts
    if bind_host in WILDCARD_HOSTS:
        allowed_hosts |= machine_hosts()
    else:
        allowed_hosts.add(f"[{bind_host}]" if ":" in bind_host and not
                          bind_host.startswith("[") else bind_host)
    # Reachable from other devices -> every request needs the per-launch secret key.
    loopback_only = bind_host in LOCAL_HOSTS
    access_key = None if loopback_only else secrets.token_urlsafe(24)
    app.state.access_key = access_key
    app.state.allowed_hosts = allowed_hosts

    def run_job(fn: Callable[..., Any], *args: Any) -> None:
        if app.state.inline_jobs:
            fn(*args)
            return
        t = threading.Thread(target=fn, args=args, daemon=True, name=f"job-{fn.__name__}")
        app.state.jobs = [j for j in app.state.jobs if j.is_alive()] + [t]
        t.start()

    @app.middleware("http")
    async def guard(request: Request, call_next):
        """Host check (DNS rebinding), access key on LAN binds, CSRF header + Origin on
        writes, no caching of the API."""
        host = request.headers.get("host") or ""
        if split_host(host) not in allowed_hosts:
            return JSONResponse({"detail": "host not allowed"}, status_code=403)
        set_cookie = False
        if access_key is not None:
            given = (request.cookies.get(ACCESS_COOKIE) or request.headers.get(ACCESS_HEADER)
                     or "")
            url_key = request.query_params.get("key") or ""
            if url_key and request.url.path == "/" and secrets.compare_digest(url_key,
                                                                            access_key):
                set_cookie = True
            elif not (given and secrets.compare_digest(given, access_key)):
                return JSONResponse({"detail": "נדרש מפתח גישה: פתח את הקישור עם ?key= "
                                               "שהודפס כשהפעלת את לוח הבקרה"}, status_code=401)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if request.headers.get(CSRF_HEADER) != "1":
                return JSONResponse({"detail": "missing X-Agency-Request header"},
                                    status_code=403)
            origin = request.headers.get("origin") or ""
            if origin and origin != "null":
                from urllib.parse import urlsplit
                if urlsplit(origin).netloc.lower() != host.lower():
                    return JSONResponse({"detail": "origin not allowed"}, status_code=403)
        response = await call_next(request)
        if set_cookie:
            response.set_cookie(ACCESS_COOKIE, access_key, httponly=True, samesite="strict",
                                path="/")
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        return response

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ---------------------------------------------------------------- page
    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        html = jinja.get_template("index.html").render(
            channel=ctx.settings.get("channel.name", "") or "", dry_run=ctx.settings.dry_run,
            max_upload_gb=round(_max_upload_bytes() / 1024 ** 3, 2),
            extensions=" ".join(sorted(ALLOWED_EXTENSIONS)))
        return HTMLResponse(html)

    def _max_upload_bytes() -> int:
        gb = ctx.settings.get("dashboard.max_upload_gb")
        try:
            return int(float(gb) * 1024 ** 3) if gb else DEFAULT_MAX_UPLOAD_BYTES
        except (TypeError, ValueError):
            return DEFAULT_MAX_UPLOAD_BYTES

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
            raise HTTPException(400, f"ההעלאה נכשלה: {type(exc).__name__}") from None

        title = clean_text(upload.fields.get("title", ""), MAX_TITLE_LEN)
        vid = ctx.db.add_video(str(path), title)
        ctx.db.update_video(vid, meta={"original_name": upload.file_name, "size_bytes": upload.size,
                                       "source": "dashboard"})
        emit(ctx.db, "owner", f"סרטון חדש הועלה: {upload.file_name}", "info", vid)
        run_job(run_pipeline_safely, ctx, vid)
        return {"ok": True, "video_id": vid, "file_name": path.name, "size": upload.size}

    return app
