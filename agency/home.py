"""Home screen facts (SPEC_V2 §1): the manager's line, results, the team and live progress.

Everything here is rule-based (no LLM), so it is instant and TRUTHFUL:
- a clip "עלה" (went up) only when a post really went live; posts made in training mode are
  "מוכן" / "באימון" and the sentence never says "עלה" for them;
- numbers come from real stats of really published posts (imported studio history included);
- progress comes from the real video status and staff status, not from timers.
Owner-facing text is Hebrew with no technical words and no database ids.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import VideoStatus
from .staff._playbook import PLATFORM_HE, local_tz, parse_iso
from .staff.base import Context

PROBLEM_WINDOW = timedelta(hours=24)      # failures older than this no longer nag
PROCESSING_WINDOW = timedelta(hours=12)   # an in-flight video older than this is stale
# A failed timeline (with its "נסה שוב" button) stays exactly as long as the sentence says
# "משהו נתקע … אפשר לנסות שוב" — never a promise without the button.
FAILED_VISIBLE = PROBLEM_WINDOW
READY_VISIBLE = timedelta(minutes=10)     # a finished timeline stays visible this long
STALE_WORKING = timedelta(hours=3)        # "working" without an update for this long = idle

TEAM_ORDER = ["manager", "analyst", "editor", "copywriter", "strategist", "publisher",
              "promoter", "community", "analytics", "finance"]
TEAM_TITLES_HE = {"manager": 'המנכ"ל', "analyst": "אנליסט וידאו", "editor": "עורך וידאו",
                  "copywriter": "קופירייטר", "strategist": "אסטרטג תוכן", "publisher": "מפרסם",
                  "promoter": "מקדם", "community": "מנהל קהילה", "analytics": "אנליסט נתונים",
                  "finance": "מנהל כספים"}
_IDLE_HE = {"manager": "מחכה לקליפ הבא שלך", "analyst": "מחכה לקליפ הבא",
            "editor": "מחכה לקליפ הבא", "copywriter": "מחכה לקליפ הבא",
            "strategist": "מחכה לקליפ הבא", "publisher": "מחכה לאישור שלך",
            "promoter": "מחכה לקליפ הבא", "community": "מחכה לתגובות חדשות",
            "analytics": "מחכה למספרים חדשים", "finance": "שומר על התקציב"}
_WORKING_HE = {"manager": "מתאם את הצוות", "analyst": "צופה בקליפ", "editor": "חותך גרסאות",
               "copywriter": "כותב כותרות", "strategist": "בוחר פלטפורמות ושעות",
               "publisher": "מעלה את הקליפ", "promoter": "מכין רעיונות קידום",
               "community": "מכין תשובות לתגובות", "analytics": "בודק את המספרים",
               "finance": "בודק את התקציב"}
_PROBLEM_HE = "משהו נתקע אצלי. אפשר לנסות שוב."
_TECH_RE = re.compile(r"#\d|dry.?run|\bapi\b|token|secret|oauth|webhook|json|config|yaml|"
                      r"\benv\b|pipeline|error|exception|traceback|http|\[|\]", re.I)
_HEBREW_RE = re.compile(r"[֐-׿]")

STEPS: list[tuple[str, str, str | None]] = [
    ("watch", "צופה בקליפ", "analyst"),
    ("cut", "חותך לאנכי", "editor"),
    ("write", "כותב כותרות", "copywriter"),
    ("plan", "בוחר פלטפורמות", "strategist"),
    ("ready", "מוכן לאישור", None),
]
_STEP_OF_FAILURE = {"analyst": 0, "editor": 1, "copywriter": 2, "strategist": 3}
_IN_FLIGHT = {VideoStatus.NEW, VideoStatus.ANALYZED, VideoStatus.EDITED, VideoStatus.PLANNED}

# Hebrew numbers: masculine (סרטון) and feminine (בקשה) forms.
_MASC = {2: "שני", 3: "שלושה", 4: "ארבעה", 5: "חמישה", 6: "שישה", 7: "שבעה", 8: "שמונה",
         9: "תשעה", 10: "עשרה"}
_MASC_ALONE = {1: "אחד", 2: "שניים", **{k: v for k, v in _MASC.items() if k > 2}}
_FEM = {2: "שתי", 3: "שלוש", 4: "ארבע", 5: "חמש", 6: "שש", 7: "שבע", 8: "שמונה", 9: "תשע",
        10: "עשר"}


def _videos_he(n: int) -> str:
    if n == 1:
        return "סרטון אחד"
    return f"{_MASC.get(n, n)} סרטונים"


def _requests_he(n: int) -> str:
    if n == 1:
        return "בקשה אחת"
    return f"{_FEM.get(n, n)} בקשות"


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _now(now: datetime | None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now if now.tzinfo else now.replace(tzinfo=timezone.utc)


def greeting(settings, now: datetime | None = None) -> str:
    """Time-aware Hebrew greeting in the channel time zone."""
    hour = _now(now).astimezone(local_tz(settings)).hour
    if 5 <= hour < 12:
        return "בוקר טוב"
    if 12 <= hour < 17:
        return "צהריים טובים"
    if 17 <= hour < 22:
        return "ערב טוב"
    return "לילה טוב"


# ----------------------------------------------------------------------------- post facts
def is_real_post(post: dict) -> bool:
    """True only for a post that really went live (not training mode, not a fake id)."""
    if post.get("status") != "published":
        return False
    result = post.get("result") or {}
    ext = str(post.get("external_id") or result.get("external_id") or "")
    if ext.startswith("dryrun-"):
        return False
    return result.get("dry_run") is False and result.get("ok", True) is not False


def published_at(post: dict, table: dict | None = None) -> datetime | None:
    """When the post went out (best evidence; aware UTC).

    - imported studio posts keep their real publish time in ``created_at``;
    - ours record it when they go live (``plan.extra.published_at``, written by the
      publisher), and the adapters' own record (kv ``platforms.published_at``, pass it as
      ``table``) covers older posts;
    - only then is it guessed — never from the planned slot when the post went out before
      it, and never from ``updated_at`` once a stats refresh has rewritten it.
    """
    plan = post.get("plan") or {}
    extra = plan.get("extra") or {}
    stats = post.get("stats") or {}
    result = post.get("result") or {}
    if extra.get("source") == "studio":
        return parse_iso(post.get("created_at"))
    for value in (extra.get("published_at"), stats.get("published_at"),
                  result.get("published_at")):
        when = parse_iso(value)
        if when:
            return when
    ext = post.get("external_id") or result.get("external_id")
    if isinstance(table, dict) and ext:
        when = parse_iso(table.get(f"{post.get('platform')}:{ext}"))
        if when:
            return when
    scheduled = parse_iso(post.get("scheduled_at"))
    updated = parse_iso(post.get("updated_at"))
    created = parse_iso(post.get("created_at"))
    if not stats.get("fetched_at"):  # nothing rewrote the row since it went out
        if scheduled and updated and scheduled <= updated:
            return scheduled
        return updated or scheduled or created
    if scheduled and created and created <= scheduled and (
            updated is None or scheduled <= updated):
        return scheduled  # went out at its slot (the stats refresh came later)
    return created or scheduled or updated


def _can_fetch_stats(ctx: Context, platform: str) -> bool:
    """Can any connection we have bring this platform's numbers? (No network.)"""
    try:
        from .platforms import registry
        adapter = registry.get_adapter(platform, ctx.settings)
        if getattr(adapter, "zernio_platform", None) is not None:  # via Zernio
            return adapter.zernio_platform == "youtube" and bool(
                adapter._direct_youtube().configured())
        return bool(adapter.configured())
    except Exception:
        return False


def _published_table(ctx: Context) -> dict:
    """The adapters' record of real publish times (kv ``platforms.published_at``)."""
    try:
        from .platforms.instagram import PUBLISHED_AT_KV
        table = ctx.db.kv_get(PUBLISHED_AT_KV) or {}
    except Exception:
        return {}
    return table if isinstance(table, dict) else {}


def _pretty(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").replace("_", " ")).strip()


def video_title(video, posts: list[dict] | None = None) -> str:
    """A human title for a video: the owner's hint, else the post title, else the file name."""
    if video is None:
        return "קליפ"
    hint = re.split(r"\s*[|;\n]\s*|\b(?:music|song|מוזיקה|שיר)\s*:", video.title_hint or "",
                    flags=re.I)[0]
    hint = re.sub(r"[\s\-–—,.:]+$", "", hint)  # "ACE on Nuke — music: X" -> "ACE on Nuke"
    if hint.strip():
        return _pretty(hint)[:80]
    for p in posts or []:
        title = ((p.get("plan") or {}).get("title") or "").strip()
        if title:
            return _pretty(title)[:80]
    stem = Path(str(video.path or "")).stem
    if stem.startswith("studio:"):
        stem = stem.split(":", 1)[1]
    return _pretty(stem)[:80] or "קליפ"


def live_platforms(ctx: Context) -> list[str]:
    """Enabled platforms that would really publish now (connected and switched live)."""
    from .platforms import registry

    out = []
    for key in registry.enabled_keys(ctx.settings):
        try:
            live = registry.get_adapter(key, ctx.settings).is_live
            if live() if callable(live) else live:
                out.append(key)
        except Exception:  # an adapter that cannot even load is not live
            continue
    return out


# ----------------------------------------------------------------------------- sentence
def _facts(ctx: Context, now: datetime) -> dict:
    tz = local_tz(ctx.settings)
    today = now.astimezone(tz).date()
    videos = {v.id: v for v in ctx.db.list_videos(limit=10_000)}
    posts = ctx.db.list_posts()
    by_video: dict[int, list[dict]] = {}
    for p in posts:
        by_video.setdefault(p["video_id"], []).append(p)

    live_today: set[int] = set()
    training_today: set[int] = set()
    failed: set[int] = set()
    table = _published_table(ctx)
    for p in posts:
        if p["status"] == "published":
            when = published_at(p, table)
            if when and when.astimezone(tz).date() == today:
                (live_today if is_real_post(p) else training_today).add(p["video_id"])
        elif p["status"] == "failed":
            when = parse_iso(p.get("updated_at"))
            if when and now - when <= PROBLEM_WINDOW:
                failed.add(p["video_id"])
    for v in videos.values():
        created = parse_iso(v.created_at)
        if v.status == VideoStatus.FAILED and (v.meta or {}).get("failed_step") \
                and created and now - created <= PROBLEM_WINDOW:
            failed.add(v.id)
    training_today -= live_today

    # waiting for the owner: pending, plus approved ones whose execution did not go through
    # (they wait for "נסה שוב" on their card)
    pending = ctx.db.list_approvals("pending") + ctx.db.list_approvals("approved")
    pending_videos = {int((a.payload or {}).get("video_id")) for a in pending
                      if a.kind == "publish" and (a.payload or {}).get("video_id") is not None}
    # approved (or auto) posts waiting for their time: video -> first upcoming slot
    scheduled: dict[int, datetime] = {}
    for p in posts:
        when = parse_iso(p.get("scheduled_at")) if p["status"] == "scheduled" else None
        if when and when > now and p["video_id"] not in pending_videos:
            first = scheduled.get(p["video_id"])
            scheduled[p["video_id"]] = when if first is None or when < first else first
    pending_other = sum(1 for a in pending if a.kind != "publish")
    processing = [item for item in processing_view(ctx, now) if not item.get("done")
                  and not item.get("failed")]
    return {"videos": videos, "by_video": by_video, "live_today": live_today,
            "training_today": training_today, "failed": failed,
            "pending_videos": pending_videos, "pending_other": pending_other,
            "scheduled": scheduled, "processing": processing, "any_video": bool(videos)}


def _when_short(settings, when: datetime, now: datetime) -> str:
    """"להיום 17:00" / "למחר 14:30" / "ל-23.10 20:00" in the channel time zone."""
    tz = local_tz(settings)
    at = when.astimezone(tz)
    days = (at.date() - now.astimezone(tz).date()).days
    clock = f"{at:%H:%M}"
    if days == 0:
        return f"להיום {clock}"
    if days == 1:
        return f"למחר {clock}"
    return f"ל-{at:%d.%m} {clock}"


def home_sentence(ctx: Context, now: datetime | None = None) -> dict:
    """The manager's ONE friendly first-person line for the home screen (Hebrew).

    Returns ``{"greeting", "sentence", "text", "mood", "training", "live_platforms",
    "counts": {live_today, training_today, pending_videos, pending_other, scheduled, processing,
    problems}}``. ``mood`` is ``problem`` | ``attention`` | ``busy`` | ``calm``.
    Truth rules: "עלה" only for posts that really went live; training-mode posts are
    "מוכן באימון"; failures are named plainly ("נתקע").
    """
    now = _now(now)
    f = _facts(ctx, now)
    live = live_platforms(ctx)
    title_of = {vid: video_title(v, f["by_video"].get(vid)) for vid, v in f["videos"].items()}

    clauses: list[str] = []
    about_videos = False
    if f["failed"]:
        n = len(f["failed"])
        if n == 1:
            clauses.append(f"משהו נתקע ב\"{title_of.get(next(iter(f['failed'])), 'קליפ')}\" — "
                           "אפשר לנסות שוב")
        else:
            clauses.append(f"{_videos_he(n)} נתקעו — אפשר לנסות שוב")
    if f["live_today"]:
        n = len(f["live_today"])
        clauses.append("סרטון אחד עלה היום" if n == 1 else f"{_videos_he(n)} עלו היום")
        about_videos = True
    if f["training_today"]:
        n = len(f["training_today"])
        clauses.append("סרטון אחד מוכן באימון היום" if n == 1
                       else f"{_videos_he(n)} מוכנים באימון היום")
        about_videos = True
    if f["pending_videos"]:
        n = len(f["pending_videos"])
        if about_videos:
            clause = "אחד מחכה לך" if n == 1 else f"{_MASC_ALONE.get(n, n)} מחכים לך"
        else:
            clause = "סרטון אחד מחכה לך" if n == 1 else f"{_videos_he(n)} מחכים לך"
        if f["pending_other"]:
            clause += f", ועוד {_requests_he(f['pending_other'])}"
        clauses.append(clause)
    elif f["pending_other"]:
        n = f["pending_other"]
        clauses.append("בקשה אחת מחכה לך" if n == 1 else f"{_requests_he(n)} מחכות לך")
    if f["scheduled"]:
        n = len(f["scheduled"])
        if n == 1:
            vid, when = next(iter(f["scheduled"].items()))
            clauses.append(f"\"{title_of.get(vid, 'קליפ')}\" מתוזמן "
                           f"{_when_short(ctx.settings, when, now)}")
        else:
            first = min(f["scheduled"].values())
            clauses.append(f"{_videos_he(n)} מתוזמנים, הראשון "
                           f"{_when_short(ctx.settings, first, now)}")
    if f["processing"]:
        n = len(f["processing"])
        clauses.append(f"אני עובד עכשיו על \"{f['processing'][0]['video_title']}\"" if n == 1
                       else f"אני עובד עכשיו על {_videos_he(n)}")

    if clauses:
        opener = "" if f["failed"] else ("הכל רץ באימון. " if not live else "הכל רץ. ")
        sentence = opener + ", ".join(clauses) + "."
    elif not f["any_video"]:
        sentence = "זרוק את הקליפ הראשון ואני על זה."
    else:
        sentence = "הכל רגוע. זרוק קליפ ואני על זה."

    if f["failed"]:
        mood = "problem"
    elif f["pending_videos"] or f["pending_other"]:
        mood = "attention"
    elif f["processing"]:
        mood = "busy"
    else:
        mood = "calm"
    hello = greeting(ctx.settings, now)
    return {"greeting": hello, "sentence": sentence, "text": f"{hello}! {sentence}",
            "mood": mood, "training": not live, "live_platforms": live,
            "counts": {"live_today": len(f["live_today"]),
                       "training_today": len(f["training_today"]),
                       "pending_videos": len(f["pending_videos"]),
                       "pending_other": f["pending_other"],
                       "scheduled": len(f["scheduled"]),
                       "processing": len(f["processing"]), "problems": len(f["failed"])}}


# ----------------------------------------------------------------------------- results
def results_summary(ctx: Context, days: int = 7, now: datetime | None = None) -> dict:
    """Results of really published posts (imported studio history included) whose publish
    day (channel time zone) falls in the last ``days`` days, today included.

    Returns ``{"days", "views", "likes", "comments", "series": [views per day, oldest first,
    len == days], "series_days": ["YYYY-MM-DD", ...], "best_video": {"title", "views", "url"}
    | None, "per_platform": [{"key", "name_he", "views"}] (most views first), "all_time":
    {"views", "likes", "comments", "posts", "videos"}, "has_data": bool, "stats_pending":
    bool (real posts exist but no numbers were fetched yet)}``. Views of a post count on
    the day it went out (its current totals: platforms report lifetime numbers).
    Training-mode posts never count.
    """
    days = max(1, int(days or 7))
    now = _now(now)
    tz = local_tz(ctx.settings)
    today = now.astimezone(tz).date()
    first_day = today - timedelta(days=days - 1)
    day_list: list[date] = [first_day + timedelta(days=i) for i in range(days)]
    series = [0.0] * days
    totals = {"views": 0.0, "likes": 0.0, "comments": 0.0}
    all_time = {"views": 0.0, "likes": 0.0, "comments": 0.0, "posts": 0}
    per_video: dict[int, dict[str, Any]] = {}
    per_platform: dict[str, float] = {}
    real_videos: set[int] = set()
    any_stats = False
    table = _published_table(ctx)
    seen: set[str] = set()
    for p in ctx.db.list_posts():
        if not is_real_post(p):
            continue
        stats = p.get("stats") or {}
        real_videos.add(p["video_id"])
        seen.add(p["platform"])
        any_stats = any_stats or bool(stats.get("fetched_at")) or any(
            k in stats for k in ("views", "likes", "comments"))
        views, likes, comments = (_num(stats.get(k)) for k in ("views", "likes", "comments"))
        all_time["posts"] += 1
        all_time["views"] += views
        all_time["likes"] += likes
        all_time["comments"] += comments
        when = published_at(p, table)
        if when is None or when > now + timedelta(minutes=5):
            continue
        day = when.astimezone(tz).date()
        if not first_day <= day <= today:
            continue
        series[(day - first_day).days] += views
        totals["views"] += views
        totals["likes"] += likes
        totals["comments"] += comments
        per_platform[p["platform"]] = per_platform.get(p["platform"], 0.0) + views
        entry = per_video.setdefault(p["video_id"], {"views": 0.0, "url": None, "top": -1.0,
                                                     "posts": []})
        entry["views"] += views
        entry["posts"].append(p)
        if p.get("url") and views > entry["top"]:
            entry["url"], entry["top"] = p["url"], views

    best = None
    if per_video:
        vid, entry = max(per_video.items(), key=lambda kv: (kv[1]["views"], -kv[0]))
        if entry["views"] > 0:
            best = {"title": video_title(ctx.db.get_video(vid), entry["posts"]),
                    "views": int(round(entry["views"])), "url": entry["url"],
                    "video_id": vid}
    platforms = sorted(per_platform.items(), key=lambda kv: (-kv[1], kv[0]))
    return {"days": days,
            "views": int(round(totals["views"])), "likes": int(round(totals["likes"])),
            "comments": int(round(totals["comments"])),
            "series": [int(round(v)) for v in series],
            "series_days": [d.isoformat() for d in day_list],
            "best_video": best,
            "per_platform": [{"key": k, "name_he": PLATFORM_HE.get(k, k), "views": int(round(v))}
                             for k, v in platforms],
            "all_time": {**{k: int(round(v)) for k, v in all_time.items()},
                         "videos": len(real_videos)},
            "has_data": all_time["posts"] > 0,
            # clips really went up but no numbers were fetched yet (e.g. right after the
            # studio import): the screen says "on the way" instead of showing zeros
            "stats_pending": all_time["posts"] > 0 and not any_stats,
            # why: "waiting" (a connection can fetch them; next refresh) or "no_route" (none
            # of the connections can: TikTok via Zernio never, YouTube needs Google)
            "stats_reason": (None if any_stats or not all_time["posts"]
                             else "waiting" if any(_can_fetch_stats(ctx, k) for k in seen)
                             else "no_route")}


# ----------------------------------------------------------------------------- team
def _friendly(detail: str) -> bool:
    """A staff detail is shown as-is only when it is plain Hebrew with no technical bits."""
    detail = (detail or "").strip()
    return bool(detail) and len(detail) <= 90 and bool(_HEBREW_RE.search(detail)) \
        and not _TECH_RE.search(detail)


def team_view(ctx: Context, now: datetime | None = None) -> list[dict]:
    """The team row: manager first, then the enabled staff.

    ``[{"name", "title_he", "status": "idle"|"working"|"problem", "sentence"}]``. A
    "working" status with no update for 3 hours counts as idle, a problem older than 24 hours
    no longer shows. Sentences are plain Hebrew (technical details stay in advanced mode).
    """
    now = _now(now)
    live = {row["name"]: row for row in ctx.db.list_staff_status()}
    enabled = [n for n in (ctx.settings.get("staff.enabled", []) or []) if n != "manager"]
    names = ["manager", *[n for n in TEAM_ORDER if n in enabled],
             *[n for n in enabled if n not in TEAM_ORDER]]
    out = []
    for name in names:
        row = live.get(name) or {}
        raw = str(row.get("status") or "idle").lower()
        updated = parse_iso(row.get("updated_at"))
        age = now - updated if updated else None
        if raw in ("error", "failed", "problem") and (age is None or age <= PROBLEM_WINDOW):
            status = "problem"
        elif raw in ("working", "busy") and (age is None or age <= STALE_WORKING):
            status = "working"
        else:
            status = "idle"
        detail = str(row.get("detail") or "")
        if status == "problem":
            sentence = _PROBLEM_HE
        elif status == "working":
            sentence = detail if _friendly(detail) else _WORKING_HE.get(name, "עובד על זה")
            if name == "publisher" and re.search(r"dry.?run", detail, re.I):
                sentence = "מתרגל העלאה במצב אימון"
        else:
            sentence = _IDLE_HE.get(name, "פנוי")
        out.append({"name": name, "title_he": TEAM_TITLES_HE.get(name, name),
                    "status": status, "sentence": sentence})
    return out


# ----------------------------------------------------------------------------- progress
def processing_view(ctx: Context, now: datetime | None = None) -> list[dict]:
    """Timelines of videos being prepared, from the real video + staff status.

    ``[{"video_id", "video_title", "done": bool, "failed": bool, "steps": [{"key",
    "label_he", "state": "done"|"active"|"todo"|"failed"}]}]`` with the steps
    watch → cut → write → plan → ready. A finished video stays for 10 minutes (all done),
    a failed one for 6 hours (the failed step marked); stale in-flight videos are hidden.
    """
    now = _now(now)
    staff = {row["name"]: str(row.get("status") or "") for row in ctx.db.list_staff_status()}
    auto = str(ctx.settings.get("publishing.mode", "approve") or "").strip().lower() == "auto"
    pending_at: dict[int, datetime] = {}
    for a in ctx.db.list_approvals("pending"):
        vid = (a.payload or {}).get("video_id")
        created = parse_iso(a.created_at)
        if a.kind == "publish" and vid is not None and created:
            pending_at[int(vid)] = created
    out: list[dict] = []
    for v in ctx.db.list_videos(limit=200):
        if (v.meta or {}).get("source") == "studio":
            continue
        created = parse_iso(v.created_at)
        posts = None
        done_steps: int | None = None
        failed_step: int | None = None
        if v.status in _IN_FLIGHT:
            if created is None or now - created > PROCESSING_WINDOW:
                continue
            posts = ctx.db.list_posts(v.id)
            if posts:  # posts exist only after planning: e.g. planned again after "not now"
                continue
            if v.status == VideoStatus.PLANNED:
                done_steps = 4
            elif v.status == VideoStatus.EDITED:
                done_steps = 3 if staff.get("strategist") == "working" else 2
            else:
                done_steps = {VideoStatus.NEW: 0, VideoStatus.ANALYZED: 1}[v.status]
        elif v.status == VideoStatus.FAILED:
            step = _STEP_OF_FAILURE.get(str((v.meta or {}).get("failed_step") or ""))
            if step is None or created is None or now - created > FAILED_VISIBLE:
                continue
            failed_step = step
        elif v.status == VideoStatus.AWAITING_APPROVAL:
            when = pending_at.get(v.id)
            if when is None or now - when > READY_VISIBLE:
                continue
            done_steps = len(STEPS)
        elif v.status == VideoStatus.SCHEDULED and auto:
            posts = ctx.db.list_posts(v.id)
            first = min((parse_iso(p.get("created_at")) for p in posts if p.get("created_at")),
                        default=None)
            if first is None or now - first > READY_VISIBLE:
                continue
            done_steps = len(STEPS)
        else:
            continue
        steps = []
        for i, (key, label, _member) in enumerate(STEPS):
            if key == "ready" and auto:
                label = "מתוזמן לפרסום"
            if failed_step is not None:
                state = "done" if i < failed_step else ("failed" if i == failed_step else "todo")
            elif i < (done_steps or 0):
                state = "done"
            elif i == done_steps:
                state = "active"
            else:
                state = "todo"
            steps.append({"key": key, "label_he": label, "state": state})
        out.append({"video_id": v.id, "video_title": video_title(v, posts),
                    "done": failed_step is None and done_steps == len(STEPS),
                    "failed": failed_step is not None,
                    # "נסה שוב" re-runs the preparation (POST /api/videos/{id}/retry)
                    "can_retry": failed_step is not None, "steps": steps})
    return out


def home_payload(ctx: Context, now: datetime | None = None) -> dict:
    """Everything the home screen shows, in one call (for GET /api/home)."""
    now = _now(now)
    return {"manager": home_sentence(ctx, now), "results": results_summary(ctx, 7, now),
            "team": team_view(ctx, now), "processing": processing_view(ctx, now)}


__all__ = ["greeting", "home_payload", "home_sentence", "is_real_post", "live_platforms",
           "processing_view", "published_at", "results_summary", "team_view", "video_title"]
