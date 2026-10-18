"""Strategist: which enabled platforms fit this clip, and when to post (channel time zone).

Default times are the owner's proven ones (``publishing.default_times``: YouTube 17:00,
Facebook 17:40, Instagram 20:00, TikTok 20:40, Israel time). The model may deviate only with a
stated reason, and the code always keeps Instagram/TikTok at least 30 minutes after YouTube.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any

from ..brand import detect_ai, load_brand
from ..models import PostPlan, Variant, Video
from ..platforms.registry import enabled_keys
from ._playbook import (AFTER_YOUTUBE_MIN, CLIP_TYPE_HE, CS2_PLAYBOOK, PLATFORM_FIT,
                        PLATFORM_HE, YOUTUBE_FAMILY, default_times, local_tz, next_slot,
                        parse_hhmm, parse_iso, to_utc_iso)
from .base import StaffMember

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "posts": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "platform": {"type": "string"},
                "publish_at_local": {"type": "string",
                                     "description": "YYYY-MM-DDTHH:MM in the channel time zone"},
                "why": {"type": "string"},
                "deviation_reason": {"type": "string",
                                     "description": "Empty when using the default time"},
            },
            "required": ["platform", "publish_at_local", "why", "deviation_reason"]}},
        "overall": {"type": "string"},
    },
    "required": ["posts", "overall"],
}

STAGGER = timedelta(minutes=20)       # two posts never share the same minute
DEVIATION_TOLERANCE = timedelta(minutes=5)
MIN_REASON_LEN = 6
SHORT_CLIP_S = 60.0                   # a clip this short goes to YouTube as a Short only


def one_youtube_upload(candidates: list[str], variants: list[Variant]) -> list[str]:
    """One upload per YouTube channel: ``youtube`` and ``youtube_shorts`` are the same
    account (the double-post guard treats them as one). A vertical or short clip goes to
    Shorts only; a long horizontal video goes to YouTube in full AND as a vertical Shorts cut
    — but only when the Shorts cut really is a different file. When both would get the very
    same file (no ffmpeg, or the vertical encode failed) only one is kept: Shorts for a
    vertical / short clip, the full YouTube upload otherwise (unknown size too)."""
    if "youtube" not in candidates or "youtube_shorts" not in candidates:
        return candidates
    full = next((v for v in variants if v.platform == "youtube"), None)
    if full is None:
        return candidates
    short = full.aspect == "9:16" or (full.duration_s is not None
                                      and float(full.duration_s) <= SHORT_CLIP_S)
    if short:
        return [p for p in candidates if p != "youtube"]
    shorts = next((v for v in variants if v.platform == "youtube_shorts"), None)
    if shorts is not None and _same_file(shorts.path, full.path):
        return [p for p in candidates if p != "youtube_shorts"]
    return candidates


def _same_file(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    try:
        from pathlib import Path
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False

_WHY_HE = {
    "youtube": "השעה שעובדת לך ביוטיוב. סרטון מלא ממשיך להביא צפיות מחיפוש לאורך זמן.",
    "youtube_shorts": "השעה המוכחת שלך לשורטס. הוק בשנייה הראשונה מחזיק צפייה עד הסוף.",
    "facebook": "אחרי יוטיוב, כמו שעבד לך עד היום.",
    "instagram_reels": "שעת השיא של הערב, ותמיד לפחות חצי שעה אחרי יוטיוב.",
    "tiktok": "סוגר את הערב כשהקהל ער, לפחות חצי שעה אחרי יוטיוב.",
    "x": "ציוץ קצר עם הקליפ, בשעה שגם אירופה ערה.",
    "reddit": "רק בקהילות שבחרת, בערב של אירופה.",
    "discord": "עדכון לקהילה שלך כשהקליפ יוצא.",
}
GAP_NOTE_HE = "הזזתי כדי שיעלה לפחות חצי שעה אחרי יוטיוב."
QUIET_NOTE_HE = "הזזתי לסוף השעות השקטות שביקשת."


def _hm(dt: datetime, tz) -> str:
    return dt.astimezone(tz).strftime("%H:%M")


MAX_DAYS_AHEAD = 14


def default_schedule(settings, platforms: list[str], now: datetime,
                     busy: dict[str, list[datetime]] | None = None) -> dict[str, datetime]:
    """Proven-time slots for ``platforms`` (aware datetimes), all on YouTube's day.

    - YouTube/Shorts anchor the package: the next occurrence of their time, ≥30 min from now.
    - Every other platform goes out on the same local day at its own time, never before
      YouTube; Instagram and TikTok at least 30 minutes after the last YouTube post.
    - Without YouTube in the plan, each platform takes its own next slot.
    - Two posts never share the same minute (the later one moves by 20 minutes).
    - ``busy`` (platform -> times of clips already queued there): one clip per platform per
      day, so a day that already has one moves the package to the next free day.
    """
    times = default_times(settings)
    tz = local_tz(settings)
    out: dict[str, datetime] = {}
    used: set[datetime] = set()
    taken = {p: {w.astimezone(tz).date() for w in ws} for p, ws in (busy or {}).items()}

    def free_from(slot: datetime, keys: list[str]) -> datetime:
        for _ in range(MAX_DAYS_AHEAD):
            if not any(slot.astimezone(tz).date() in taken.get(k, ()) for k in keys):
                break
            slot += timedelta(days=1)  # same wall-clock time, next day
        return slot

    def take(slot: datetime) -> datetime:
        while slot in used:
            slot += STAGGER
        used.add(slot)
        return slot

    youtube = [p for p in platforms if p in YOUTUBE_FAMILY]
    if not youtube:
        for p in platforms:
            out[p] = take(free_from(next_slot(settings, *times.get(p, (19, 0)), after=now),
                                    [p]))
        return out
    first = min(youtube, key=lambda p: times.get(p, (17, 0)))
    anchor = free_from(next_slot(settings, *times.get(first, (17, 0)), after=now), platforms)
    day = anchor.astimezone(tz).date()

    def on_day(p: str) -> datetime:
        h, m = times.get(p, (19, 0))
        return max(datetime.combine(day, time(h, m), tzinfo=tz), anchor)

    for p in youtube:
        out[p] = take(on_day(p))
    last_youtube = max(out[p] for p in youtube)
    for p in platforms:
        if p in out:
            continue
        slot = on_day(p)
        gap = AFTER_YOUTUBE_MIN.get(p)
        if gap:
            slot = max(slot, last_youtube + timedelta(minutes=gap))
        out[p] = take(slot)
    return out


class Strategist(StaffMember):
    """Decides platforms and posting times; explains every decision."""

    name = "strategist"
    title_he = "אסטרטג תוכן"

    @property
    def role_prompt(self) -> str:  # type: ignore[override]
        brand = load_brand(self.settings)
        return (
            f'You are the Content Strategist of "{brand.name}" ({brand.handle}), a Counter-'
            "Strike 2 channel of movie-grade moments rebuilt from real demos. You decide WHICH "
            "of the enabled candidate platforms this specific clip should go to and WHEN, and "
            "you explain each choice to the owner in ONE short, plain Hebrew sentence (no "
            "technical words).\n"
            "Platforms: the owner's proven package is every short on YouTube Shorts, Facebook, "
            "Instagram and TikTok. Tutorials/lineups -> YouTube long-form first; montages -> "
            "YouTube + Facebook. Reddit only for genuinely impressive or useful clips and only "
            "to subreddits the owner configured. Low scores (<=4) -> fewer platforms, not more.\n"
            "Timing: use the DEFAULT time given for each platform (the owner's proven times in "
            "his time zone). Deviate only when you have a concrete reason (e.g. a big CS2 event "
            "tonight, an analytics insight), and then write that reason in deviation_reason "
            "(one Hebrew sentence); a time without a reason is replaced by the default. "
            "Instagram and TikTok always go at least 30 minutes after YouTube. Never schedule in "
            "the past; give publish_at_local as YYYY-MM-DDTHH:MM local time.\n\n" + CS2_PLAYBOOK)

    def relevant_platforms(self, analysis: dict) -> list[str]:
        """Enabled platforms ordered by fit for the clip type (low scores get fewer)."""
        enabled = enabled_keys(self.settings)
        fit = PLATFORM_FIT.get(analysis.get("clip_type") or "other", PLATFORM_FIT["other"])
        ordered = [p for p in fit if p in enabled]
        if not ordered:
            ordered = list(enabled)
        if "reddit" in ordered and not self.settings.get("publishing.platforms.reddit.subreddits"):
            ordered.remove("reddit")  # never post to Reddit without owner-listed subreddits
        try:
            score = int(analysis.get("score_1_10") or 5)
        except (TypeError, ValueError):
            score = 5
        if score <= 4 and len(ordered) > 3:
            ordered = ordered[:3]
        return ordered

    def plan(self, video: Video, analysis: dict, variants: list[Variant],
             copy: dict[str, dict]) -> list[PostPlan]:
        """One PostPlan per chosen platform, scheduled (UTC ISO) with reasoning in extra['why']."""
        label = (video.title_hint or "").strip()[:40]
        self.status("working", f"בוחר פלטפורמות ל\"{label}\"" if label else "בוחר פלטפורמות")
        candidates = one_youtube_upload([p for p in self.relevant_platforms(analysis)
                                         if any(v.platform == p for v in variants)], variants)
        tz = local_tz(self.settings)
        now = datetime.now(timezone.utc)
        defaults = default_schedule(self.settings, candidates, now,
                                    busy=self._queued(candidates, video.id, now))
        local = {p: defaults[p].astimezone(tz).strftime("%Y-%m-%dT%H:%M") for p in candidates}
        mock = {
            "posts": [{"platform": p, "publish_at_local": local[p],
                       "why": self._why(p, defaults[p]), "deviation_reason": ""}
                      for p in candidates],
            "overall": self._overall(analysis, candidates, defaults),
        }
        user = (
            f"Now (local, {self.settings.get('publishing.timezone', 'Asia/Jerusalem')}): "
            f"{now.astimezone(tz).strftime('%A %Y-%m-%d %H:%M')}\n"
            f"Enabled candidate platforms (choose a subset, best first): {candidates}\n"
            f"DEFAULT time per platform (local): {local}\n"
            f"Clip: type={analysis.get('clip_type')}, map={analysis.get('map')}, "
            f"score={analysis.get('score_1_10')}/10, mood={analysis.get('mood')}, "
            f"summary={analysis.get('summary_en')}\n"
            f"Audience: {analysis.get('audience')}\n"
            f"Titles: { {p: (copy.get(p) or {}).get('title') for p in candidates} }")
        data = self.llm.complete_json(self.role_prompt, user, PLAN_SCHEMA, mock_value=mock)

        chosen: list[tuple[str, datetime, str, str]] = []  # platform, when, why, deviation
        seen: set[str] = set()
        for item in data.get("posts") or []:
            p = str((item or {}).get("platform") or "")
            if p not in candidates or p in seen:  # code decides what is allowed, not the model
                continue
            seen.add(p)
            when, deviation = self._accept_time(item, defaults[p], now)
            chosen.append((p, when, str(item.get("why") or "").strip(), deviation))
        if not chosen and candidates:  # model chose nothing usable → fall back to defaults
            chosen = [(p, defaults[p], "", "") for p in candidates]

        times = self._apply_rules({p: w for p, w, _, _ in chosen}, now)
        has_ai = bool(analysis.get("ai_shots")) or detect_ai(video.title_hint)
        plans: list[PostPlan] = []
        for p, _when, why, deviation in chosen:
            when, notes = times[p]
            text = why if why and why != self._why(p, defaults[p]) else self._why(p, when)
            if deviation:
                text = f"{text} (שעה אחרת מהרגיל: {deviation})"
            for note in notes:
                text = f"{text} {note}"
            plan = self._make_plan(p, variants, copy, when, text, analysis)
            plan.extra["default_time"] = to_utc_iso(defaults[p])
            if deviation:
                plan.extra["deviation_reason"] = deviation
            if has_ai:
                plan.extra["ai_label"] = True  # adapters flag AI content (e.g. YouTube, TikTok)
            plans.append(plan)
        overall = str(data.get("overall") or mock["overall"])
        fresh = self.db.get_video(video.id) if video.id is not None else None
        thumb = ((fresh.meta if fresh else video.meta) or {}).get("thumbnail")
        for pl in plans:
            pl.extra["overall"] = overall
            if thumb and pl.extra.get("aspect") == "16:9":
                pl.extra["thumbnail"] = thumb  # YouTube / Reddit adapters use it
        names = ", ".join(PLATFORM_HE.get(p.platform, p.platform) for p in plans)
        self.status("idle", f"בחרתי: {names}" if plans else "לא נמצאה פלטפורמה מתאימה")
        return plans

    # ------------------------------------------------------------------ helpers
    _QUEUED = ("awaiting_approval", "scheduled", "publishing", "published")

    def _queued(self, platforms: list[str], video_id: int | None, now: datetime
                ) -> dict[str, list[datetime]]:
        """Times of OTHER clips already waiting / scheduled / out on these platforms (from
        yesterday on); YouTube and Shorts count as one channel."""
        family = set(YOUTUBE_FAMILY)
        out: dict[str, list[datetime]] = {}
        for post in self.db.list_posts():
            if post.get("video_id") == video_id or post.get("status") not in self._QUEUED:
                continue
            when = parse_iso(post.get("scheduled_at"))
            if when is None or when < now - timedelta(days=1):
                continue
            keys = family if post["platform"] in family else {post["platform"]}
            for key in keys & set(platforms):
                out.setdefault(key, []).append(when)
        return out

    def _accept_time(self, item: dict, default: datetime, now: datetime
                     ) -> tuple[datetime, str]:
        """The model's time if valid and (when it differs from the default) explained."""
        when = self._parse_local(item.get("publish_at_local"), now)
        reason = str(item.get("deviation_reason") or "").strip()
        if when is None:
            return default, ""
        if abs(when - default) <= DEVIATION_TOLERANCE:
            return default, ""
        if len(reason) < MIN_REASON_LEN:
            return default, ""  # deviating without a stated reason is not allowed
        return when, reason

    def _apply_rules(self, times: dict[str, datetime], now: datetime
                     ) -> dict[str, tuple[datetime, list[str]]]:
        """Quiet hours; YouTube posts never share a minute; then Instagram/TikTok ≥30 min
        after the LAST YouTube post; the rest are staggered (only ever moved later, so the
        YouTube gap still holds)."""
        out: dict[str, tuple[datetime, list[str]]] = {}
        for p, when in times.items():
            moved = self._out_of_quiet_hours(when)
            out[p] = (moved, [QUIET_NOTE_HE] if moved != when else [])
        used: set[datetime] = set()

        def stagger(keys: list[str]) -> None:
            for p in sorted(keys, key=lambda k: out[k][0]):
                when, notes = out[p]
                while when in used:
                    when += STAGGER
                used.add(when)
                out[p] = (when, notes)

        youtube = [p for p in out if p in YOUTUBE_FAMILY]
        stagger(youtube)
        if youtube:
            last = max(out[p][0] for p in youtube)
            for p, (when, notes) in list(out.items()):
                gap = AFTER_YOUTUBE_MIN.get(p)
                if gap and when < last + timedelta(minutes=gap):
                    out[p] = (last + timedelta(minutes=gap), [*notes, GAP_NOTE_HE])
        stagger([p for p in out if p not in YOUTUBE_FAMILY])
        return out

    def _out_of_quiet_hours(self, when: datetime) -> datetime:
        """Move ``when`` to the end of the owner's quiet hours if it falls inside them."""
        if self.settings.get("publishing.quiet_hours.enabled") is not True:
            return when
        start = parse_hhmm(self.settings.get("publishing.quiet_hours.start"))
        end = parse_hhmm(self.settings.get("publishing.quiet_hours.end"))
        if not start or not end or start == end:
            return when
        tz = local_tz(self.settings)
        loc = when.astimezone(tz)
        minutes = loc.hour * 60 + loc.minute
        s, e = start[0] * 60 + start[1], end[0] * 60 + end[1]
        inside = s <= minutes < e if s < e else (minutes >= s or minutes < e)
        if not inside:
            return when
        end_dt = datetime.combine(loc.date(), time(*end), tzinfo=tz)
        if end_dt <= loc:
            end_dt += timedelta(days=1)
        return end_dt

    def _parse_local(self, value: Any, now: datetime) -> datetime | None:
        """Parse model-provided local time; reject past/invalid/too-far (>30 days) times."""
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=local_tz(self.settings))
        if dt < now + timedelta(minutes=5) or dt > now + timedelta(days=30):
            return None
        return dt

    def _why(self, platform: str, when: datetime) -> str:
        name = PLATFORM_HE.get(platform, platform)
        return f"{name} ב-{_hm(when, local_tz(self.settings))}: {_WHY_HE.get(platform, '')}".strip()

    @staticmethod
    def _make_plan(platform: str, variants: list[Variant], copy: dict[str, dict],
                   when: datetime, why: str, analysis: dict) -> PostPlan:
        variant = next(v for v in variants if v.platform == platform)
        c = copy.get(platform) or {}
        return PostPlan(platform=platform, variant_path=variant.path,
                        title=c.get("title", ""), description=c.get("description", ""),
                        tags=list(c.get("tags") or []), hashtags=list(c.get("hashtags") or []),
                        scheduled_at=to_utc_iso(when),
                        extra={"why": why, "aspect": variant.aspect,
                               "duration_s": variant.duration_s,
                               "clip_type": analysis.get("clip_type")})

    def _overall(self, analysis: dict, platforms: list[str],
                 when: dict[str, datetime] | None = None) -> str:
        clip = CLIP_TYPE_HE.get(analysis.get("clip_type") or "", "רגע מטורף")
        if not platforms:
            return "אין פלטפורמה פעילה — צריך להדליק לפחות אחת בעמוד החיבורים."
        first = platforms[0]
        if when:
            first = min(platforms, key=lambda p: when[p])
        name = PLATFORM_HE.get(first, first)
        at = f" ב-{_hm(when[first], local_tz(self.settings))}" if when else ""
        later = [PLATFORM_HE.get(p, p) for p in platforms
                 if p in AFTER_YOUTUBE_MIN and p != first]
        tail = f", ו{' ו'.join(later)} אחרי יוטיוב" if later else ""
        return f"קליפ {clip}: פותחים ב{name}{at}{tail}, בשעות שכבר עבדו לך."
