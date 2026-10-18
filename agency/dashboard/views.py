"""Owner-facing views for the v2 dashboard (SPEC_V2 §1-§3, §11): approval cards with human
times, the money line, the studio summary, previous inbox clips, connection tiles and the
small settings screen.

Pure functions over the staff ``Context`` (no FastAPI here). Owner-facing text is Hebrew with
no technical words and no database ids; nothing here ever returns a secret value.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..models import Approval
from ..staff._playbook import PLATFORM_HE, local_tz, parse_iso
from ..staff.base import Context

THUMB_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".webp": "image/webp"}
MAX_THUMB_BYTES = 10 * 1024 * 1024
DAYS_HE = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]  # datetime.weekday() order
ASAP_HE = "מיד כשתאשר"
CHANNEL_DEFAULT = "Imagination"
STUDIO_CACHE_S = 60.0
CARD_KIND_ORDER = {"publish": 0, "spend": 1, "reply": 2}

# Connections page wording (SPEC_V2 §2 / §11.9).
STUDIO_SOURCE_HE = "מחובר דרך הסטודיו הקיים"
DIRECT_YOUTUBE_WARNING_HE = ("העלאה ישירה מפרויקט גוגל שלא אומת עולה כסרטון פרטי. "
                             "מומלץ להעלות דרך Zernio.")
# The check could not reach the service (offline / service down): nothing is broken.
UNCHECKED_STATUS_HE = "החיבור שמור"
UNCHECKED_NOTE_HE = "לא הצלחתי לבדוק אותו עכשיו (אין אינטרנט?). אבדוק שוב לבד."
PARTIAL_HE = "חסרים עוד פרטים בחיבור"  # == connect.PARTIAL_HE
PARTIAL_STATUS_HE = "כמעט מחובר — חסר עוד פרט"
# The import card before the owner linked the studio (its keys were found in place).
STUDIO_FOUND_HE = ("החיבורים שלו עובדים גם כאן, בלי להעתיק מפתחות. בלחיצה אחת נביא גם את "
                   "הסגנון, שעות ההעלאה והקליפים שכבר עלו.")

_ID_RE = re.compile(r"\s*#\d+\b")
_TECH_PAREN_RE = re.compile(r"\s*\([A-Za-z0-9_.]+\)")


# ----------------------------------------------------------------------------- small helpers
def _now(now: datetime | None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now if now.tzinfo else now.replace(tzinfo=timezone.utc)


def _num(value: Any) -> float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number if number == number else 0.0  # NaN -> 0


def ils(value: Any) -> str:
    """``12`` / ``12.5`` / ``1,250`` for the money line (no trailing .00)."""
    number = round(_num(value), 2)
    return f"{number:,.0f}" if number.is_integer() else f"{number:,.2f}"


def no_ids(text: Any) -> str:
    """Owner text without database ids like "#17" (SPEC_V2 banned words)."""
    return re.sub(r"\s{2,}", " ", _ID_RE.sub("", str(text or ""))).strip()


def platform_chip(key: str) -> dict:
    return {"key": key, "name_he": PLATFORM_HE.get(key, key)}


def when_he(settings, iso: str | None, now: datetime | None = None, *,
            asap: str = ASAP_HE) -> str:
    """Human Hebrew time in the channel time zone: "היום 18:00", "מחר 14:30",
    "יום שישי 20:00" (within a week), "23.10 20:00" (later). Unset or already passed ->
    ``asap`` (a waiting post goes out right after the owner approves)."""
    when = parse_iso(iso)
    current = _now(now)
    if when is None or when <= current + timedelta(minutes=1):
        return asap
    tz = local_tz(settings)
    at = when.astimezone(tz)
    today = current.astimezone(tz).date()
    days = (at.date() - today).days
    clock = f"{at:%H:%M}"
    if days == 0:
        return f"היום {clock}"
    if days == 1:
        return f"מחר {clock}"
    if days < 7:
        return f"יום {DAYS_HE[at.weekday()]} {clock}"
    return f"{at:%d.%m} {clock}"


# ----------------------------------------------------------------------------- thumbnails
def thumbnail_path(ctx: Context, video) -> Path | None:
    """The video's thumbnail file, only when it really lies inside the data folder (resolved,
    symlinks followed), is an image and is not huge. Anything else -> None."""
    if video is None:
        return None
    raw = (video.meta or {}).get("thumbnail")
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        return None
    try:
        data_dir = ctx.settings.data_dir.resolve()
        path = Path(raw)
        if not path.is_absolute():
            path = data_dir / path
        path = path.resolve(strict=True)
        if not path.is_relative_to(data_dir) or not path.is_file():
            return None
        if path.suffix.lower() not in THUMB_TYPES or path.stat().st_size > MAX_THUMB_BYTES:
            return None
    except (OSError, ValueError, RuntimeError):
        return None
    return path


def thumbnail_url(ctx: Context, video) -> str | None:
    return f"/api/videos/{video.id}/thumbnail" if thumbnail_path(ctx, video) else None


# ----------------------------------------------------------------------------- approval cards
def _video(ctx: Context, video_id: Any):
    if video_id is None or not str(video_id).isdigit():
        return None
    return ctx.db.get_video(int(video_id))


def _plans_with_posts(ctx: Context, a: Approval) -> list[tuple[dict, dict | None]]:
    """(plan, post) pairs of a publish approval. The DB post is the truth (it is what gets
    published); the payload snapshot is the fallback."""
    payload = a.payload or {}
    ids = payload.get("post_ids") if isinstance(payload.get("post_ids"), list) else []
    snapshot = [p for p in payload.get("plans") or [] if isinstance(p, dict)]
    out: list[tuple[dict, dict | None]] = []
    for i, pid in enumerate(ids):
        try:
            post = ctx.db.get_post(int(pid))
        except (TypeError, ValueError):
            post = None
        if post is not None:
            out.append((post.get("plan") or {}, post))
        elif i < len(snapshot):
            out.append((snapshot[i], None))
    if not out:
        out = [(p["plan"] if isinstance(p.get("plan"), dict) else p, None) for p in snapshot]
    return out


def card_why(platform: str, why: Any) -> str:
    """The strategist's reason without its "<platform> ב-HH:MM: " lead: on the card the
    platform and the time are already in the row's header."""
    text = str(why or "").strip()
    name = PLATFORM_HE.get(platform, platform)
    if name and text.startswith(f"{name} ב-"):
        text = re.sub(r"^[^:]*?ב-\d{1,2}:\d{2}:\s*", "", text, count=1)
    return text


def plan_card(settings, plan: dict, post: dict | None, *, editable: bool,
              now: datetime | None = None) -> dict:
    platform = str(plan.get("platform") or (post or {}).get("platform") or "")
    extra = plan.get("extra") or {}
    return {**platform_chip(platform), "platform": platform,
            "title": plan.get("title") or "", "description": plan.get("description") or "",
            "hashtags": list(plan.get("hashtags") or []), "tags": list(plan.get("tags") or []),
            "scheduled_at": plan.get("scheduled_at"),
            "when_he": when_he(settings, plan.get("scheduled_at"), now),
            "why": card_why(platform, extra.get("why")),
            "edited": bool(extra.get("edited_by_owner")),
            "editable": bool(editable and (post is None
                                           or post.get("status") == "awaiting_approval"))}


def _main_title(plans: list[dict]) -> str:
    for key in ("youtube", "youtube_shorts"):
        for plan in plans:
            if plan["platform"] == key and plan["title"]:
                return plan["title"]
    return next((p["title"] for p in plans if p["title"]), "")


def cfo_check(ctx: Context, amount: float) -> tuple[bool, str]:
    """The CFO's verdict on a spend, in plain Hebrew (config key names dropped)."""
    try:
        from ..staff.finance import CFO
        ok, reason = CFO(ctx).can_spend(amount)
    except Exception:  # finance unavailable: say nothing rather than something wrong
        return False, ""
    return bool(ok), _TECH_PAREN_RE.sub("", str(reason or "")).strip()


def approval_card(ctx: Context, a: Approval, now: datetime | None = None) -> dict:
    """One "מחכה לך" card. Publish: video title, thumbnail, platform chips, human times and
    per-platform plans (editable while pending). Spend: amount in ₪ + the CFO's reason."""
    from ..home import video_title

    payload = a.payload or {}
    pending = a.status == "pending"
    video = _video(ctx, payload.get("video_id"))
    card: dict[str, Any] = {"id": a.id, "kind": a.kind, "status": a.status,
                            "created_at": a.created_at,
                            "video_id": video.id if video else None,
                            "thumbnail_url": thumbnail_url(ctx, video) if video else None}
    if a.kind == "publish":
        pairs = _plans_with_posts(ctx, a)
        plans = [plan_card(ctx.settings, plan, post, editable=pending, now=now)
                 for plan, post in pairs]
        far = datetime.max.replace(tzinfo=timezone.utc)
        plans.sort(key=lambda p: parse_iso(p["scheduled_at"]) or far)  # in posting order
        title = video_title(video, [post for _plan, post in pairs if post]) if video \
            else no_ids(a.title)
        times = sorted((p for p in plans if parse_iso(p["scheduled_at"])),
                       key=lambda p: parse_iso(p["scheduled_at"]))
        card.update(title=title, main_title=_main_title(plans),
                    platforms=[platform_chip(p["platform"]) for p in plans],
                    when_he=times[0]["when_he"] if times else ASAP_HE,
                    plans=plans, chat_prefill=f"לגבי {title}: ")
        return card
    platform = str(payload.get("platform") or "")
    chip = platform_chip(platform) if platform else None
    card["platform"] = chip
    card["video_title"] = video_title(video) if video else None
    if a.kind == "spend":
        ok, reason = cfo_check(ctx, a.cost_ils)
        title = (f"קידום ממומן ב{chip['name_he']}" if chip and payload.get("action") ==
                 "paid_promo" else no_ids(a.title))
        card.update(title=title, cost_ils=round(_num(a.cost_ils), 2),
                    cost_he=f"{ils(a.cost_ils)} ₪", cfo_ok=ok, cfo_reason=reason,
                    why=str(payload.get("why") or ""), days=payload.get("days"))
    elif a.kind == "reply":
        card.update(title="תשובה לתגובה של צופה", comment=str(payload.get("comment") or ""),
                    reply=str(payload.get("text") or ""))
    else:
        plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
        card.update(title=no_ids(a.title), text=str(plan.get("description") or
                                                     plan.get("title") or ""))
    return card


RETRY_GRACE_S = 30  # a just-approved item is still on its way, not "stuck"


def needs_you(ctx: Context, now: datetime | None = None) -> list[dict]:
    """Every pending approval as a card: publish first, then spend, then the rest (oldest
    first inside each group). Approved items whose execution did not go through (a reply that
    was not sent, a refused spend, an interrupted upload) come back too, with ``retry: true``
    and ``retry_he``: the card's main button runs them again (approve resumes them)."""
    current = now or datetime.now(timezone.utc)
    stuck = []
    for a in ctx.db.list_approvals("approved"):
        decided = parse_iso(a.decided_at)
        if decided is None or (current - decided).total_seconds() >= RETRY_GRACE_S:
            stuck.append(a)
    rows = sorted(ctx.db.list_approvals("pending") + stuck,
                  key=lambda a: (CARD_KIND_ORDER.get(a.kind, 3), a.created_at or "",
                                 a.id or 0))
    cards = []
    for a in rows:
        card = approval_card(ctx, a, now)
        if a.status == "approved":
            card.update(retry=True, retry_he=RETRY_HE.get(a.kind, RETRY_HE["other"]))
        cards.append(card)
    return cards


RETRY_HE = {"reply": "אישרת, אבל התשובה לא נשלחה. לנסות שוב?",
            "spend": "אישרת, אבל ההוצאה לא נרשמה. לנסות שוב?",
            "publish": "אישרת, אבל ההעלאה נקטעה באמצע. להמשיך?",
            "other": "אישרת, אבל זה לא בוצע. לנסות שוב?"}


# ----------------------------------------------------------------------------- money
def money_line(budget: dict) -> dict:
    """``{spent, income, budget, remaining, pct, sentence}`` for this month."""
    spent, income = _num(budget.get("spent")), _num(budget.get("income"))
    total = _num(budget.get("budget"))
    pct = min(100, round(spent / total * 100)) if total > 0 else 0
    return {"spent": round(spent, 2), "income": round(income, 2), "budget": round(total, 2),
            "remaining": round(_num(budget.get("remaining")), 2), "pct": pct,
            "sentence": f"החודש: הוצאות {ils(spent)} ₪ · הכנסות {ils(income)} ₪"}


# ----------------------------------------------------------------------------- studio
_studio_lock = threading.Lock()
_studio_cache: dict[str, tuple[float, dict]] = {}


def _daemon(studio: Path) -> dict:
    """``importer.daemon_activity`` cached for a minute (it may run ``schtasks``)."""
    from .. import importer

    key = str(studio)
    now = time.monotonic()
    with _studio_lock:
        hit = _studio_cache.get(key)
        if hit is not None and now - hit[0] < STUDIO_CACHE_S:
            return hit[1]
    try:
        value = importer.daemon_activity(studio)
    except Exception:  # never let the old studio break the home screen
        value = {"active": False, "question_he": "", "disable_commands": []}
    with _studio_lock:
        _studio_cache[key] = (now, value)
    return value


def clear_studio_cache() -> None:
    with _studio_lock:
        _studio_cache.clear()


def studio_imported(settings) -> bool:
    """The owner linked the old studio (``agency import`` / the import card wrote studio.dir)."""
    text = str(settings.get("studio.dir") or "").strip()
    return bool(text) and text.lower() not in ("off", "none", "false", "0")


def studio_summary(ctx: Context) -> dict:
    """``{found, daemon_active, imported, question_he, disable_commands, keys_missing,
    message_he}`` — names and paths only, never secret values."""
    from .. import importer

    try:
        studio = importer.find_studio_dir(None, ctx.settings)
    except Exception:
        studio = None
    if studio is None:
        return {"found": False, "daemon_active": False, "imported": False, "question_he": "",
                "disable_commands": [], "keys_missing": False, "message_he": ""}
    daemon = _daemon(studio)
    try:
        keys_missing = any(s["name"] == "ZERNIO_API_KEY" for s in importer.next_connect_steps())
    except Exception:
        keys_missing = False
    imported = studio_imported(ctx.settings)
    if keys_missing:
        message = importer.STUDIO_KEYS_MISSING_HE
    else:
        message = importer.STUDIO_LINKED_HE if imported else STUDIO_FOUND_HE
    return {"found": True, "daemon_active": bool(daemon.get("active")),
            "imported": imported,
            "question_he": daemon.get("question_he") or "",
            "disable_commands": list(daemon.get("disable_commands") or []),
            "keys_missing": keys_missing, "message_he": message}


# ----------------------------------------------------------------------------- inbox clips
def clip_id(path: str) -> str:
    """Stable short id for an inbox clip (the dashboard never sends paths back)."""
    return hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:12]


def previous_clips(ctx: Context, status: dict | None = None) -> list[dict]:
    """"קליפים קודמים": clips that were already in the inbox folder when the watcher was
    turned on (not processed) — ``[{id, name, size_mb}]``."""
    if status is None:
        from ..inbox import inbox_status
        status = inbox_status(ctx)
    return [{"id": clip_id(c["path"]), "name": c.get("name") or Path(c["path"]).name,
             "size_mb": round(_num(c.get("size")) / 1024 ** 2, 1)}
            for c in status.get("baseline") or []]


def find_clip(status: dict, cid: str) -> dict | None:
    return next((c for c in status.get("baseline") or [] if clip_id(c["path"]) == cid), None)


# ----------------------------------------------------------------------------- connections
def tile_view(tile: dict) -> dict:
    """A connections tile plus its one-line state: ``state`` connected | unchecked |
    needs_fix | not_connected, ``status_he`` (+ ``note_he`` for unchecked), and the wording
    for the studio link / direct YouTube. ``unchecked`` = the keys are there but the check
    could not reach the service (no internet): calm, not "needs fixing"."""
    out = dict(tile)
    name = out.get("account_name")
    out["note_he"] = ""
    if out.get("connected"):
        out["state"] = "connected"
        out["status_he"] = f"מחובר ✓ {name}" if name else "מחובר ✓"
    elif out.get("unreachable") and not out.get("missing"):
        out["state"] = "unchecked"
        out["status_he"] = UNCHECKED_STATUS_HE
        out["note_he"] = UNCHECKED_NOTE_HE
    elif out.get("missing") and out.get("problem") == PARTIAL_HE:
        # e.g. the Meta ids came from the studio but the key is not pasted yet: nothing is
        # broken, the connection is just not finished (the wizard asks only for what is missing)
        out["state"] = "not_connected"
        out["status_he"] = PARTIAL_STATUS_HE
    elif out.get("problem"):
        out["state"] = "needs_fix"
        out["status_he"] = "צריך תיקון"
    else:
        out["state"] = "not_connected"
        out["status_he"] = "לא מחובר"
    out["source_he"] = STUDIO_SOURCE_HE if out.get("source") == "studio" \
        and out["state"] in ("connected", "unchecked") else ""
    out["warning_he"] = DIRECT_YOUTUBE_WARNING_HE if out.get("key") in (
        "youtube", "youtube_shorts") and out.get("via") == "direct" else ""
    if out.get("key") != "telegram":
        out["can_go_live"] = not out.get("missing")
        out["confirm_live_he"] = (f"מעכשיו הקליפים יעלו באמת ל{out.get('name_he', '')}. "
                                  "בטוח?")
    return out


# ----------------------------------------------------------------------------- settings
# The small settings screen (SPEC_V2 §3): only these fields can be read / written here.
SETTINGS_FIELDS: tuple[str, ...] = (
    "channel.name", "channel.handle", "channel.language",
    "brand.name", "brand.handle", "brand.bio", "brand.hashtags", "brand.blocked_words",
    "brand.style_notes", "brand.ai_label", "brand.music_credit",
    "publishing.mode",
    "publishing.quiet_hours.enabled", "publishing.quiet_hours.start",
    "publishing.quiet_hours.end",
    "finance.monthly_budget_ils", "finance.max_single_spend_ils",
    "dashboard.remote_access",
    "inbox.enabled", "inbox.folders",
    "notify.telegram.enabled",
)
# Changing these is allowed only from the PC itself (never from a phone).
LOCAL_ONLY_SETTINGS = frozenset({"dashboard.remote_access", "inbox.folders"})


def flatten(body: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """``{"a": {"b": 1}}`` -> ``[("a.b", 1)]`` (empty dicts are dropped)."""
    out: list[tuple[str, Any]] = []
    if not isinstance(body, dict):
        return [(prefix.rstrip("."), body)] if prefix else []
    for key, value in body.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            out.extend(flatten(value, dotted + "."))
        elif not isinstance(value, dict):
            out.append((dotted, value))
    return out


def nest(pairs: list[tuple[str, Any]]) -> dict:
    out: dict = {}
    for dotted, value in pairs:
        node = out
        *parents, last = dotted.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[last] = value
    return out


def settings_view(ctx: Context) -> dict:
    """Current values of :data:`SETTINGS_FIELDS` (brand fields show the effective brand) plus
    read-only ``info`` for the screen. Never contains secrets."""
    from ..brand import DEFAULT_AI_LABEL, DEFAULT_BLOCKED_WORDS, load_brand
    from ..inbox import inbox_folders, is_enabled
    from . import auth

    s = ctx.settings
    brand = load_brand(s)
    raw_brand = s.get("brand", {}) or {}
    raw_brand = raw_brand if isinstance(raw_brand, dict) else {}
    blocked = raw_brand.get("blocked_words")
    ai_label = raw_brand.get("ai_label", DEFAULT_AI_LABEL)
    music = raw_brand.get("music_credit", True)
    try:
        folders_now = [str(p) for p in inbox_folders(s)]
    except Exception:
        folders_now = []
    values = {
        "channel.name": str(s.get("channel.name", "") or "") or brand.name,
        "channel.handle": str(s.get("channel.handle", "") or "") or brand.handle,
        "channel.language": str(s.get("channel.language", "he") or "he"),
        "brand.name": brand.name, "brand.handle": brand.handle, "brand.bio": brand.bio,
        "brand.hashtags": list(brand.hashtags),
        "brand.blocked_words": list(blocked) if isinstance(blocked, list)
        else list(DEFAULT_BLOCKED_WORDS),
        "brand.style_notes": brand.style_notes,
        "brand.ai_label": ai_label if isinstance(ai_label, (str, bool)) else False,
        "brand.music_credit": music if isinstance(music, (str, bool)) else True,
        "publishing.mode": "auto" if str(s.get("publishing.mode", "approve")).strip().lower()
        == "auto" else "approve",
        "publishing.quiet_hours.enabled": s.get("publishing.quiet_hours.enabled") is True,
        "publishing.quiet_hours.start": str(s.get("publishing.quiet_hours.start", "") or ""),
        "publishing.quiet_hours.end": str(s.get("publishing.quiet_hours.end", "") or ""),
        "finance.monthly_budget_ils": _num(s.get("finance.monthly_budget_ils", 0)),
        "finance.max_single_spend_ils": _num(s.get("finance.max_single_spend_ils", 0)),
        "dashboard.remote_access": auth.remote_enabled(s),
        "inbox.enabled": is_enabled(s),
        "inbox.folders": [str(f) for f in (s.get("inbox.folders") or []) if str(f).strip()],
        "notify.telegram.enabled": s.get("notify.telegram.enabled", False) is True,
    }
    out = nest([(k, values[k]) for k in SETTINGS_FIELDS])
    out["info"] = {"inbox_folders_in_use": folders_now,
                   "local_only": sorted(LOCAL_ONLY_SETTINGS)}
    return out


__all__ = ["approval_card", "clip_id", "find_clip", "money_line", "needs_you",
           "platform_chip", "previous_clips", "settings_view", "studio_summary",
           "thumbnail_path", "thumbnail_url", "tile_view", "when_he"]
