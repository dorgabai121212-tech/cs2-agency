"""Strategist: which enabled platforms fit this clip, and when to post (channel time zone)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..models import PostPlan, Variant, Video
from ..platforms.registry import enabled_keys
from ._playbook import (CS2_PLAYBOOK, PLATFORM_FIT, POSTING_SLOTS, local_tz, next_slot,
                        to_utc_iso)
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
            },
            "required": ["platform", "publish_at_local", "why"]}},
        "overall": {"type": "string"},
    },
    "required": ["posts", "overall"],
}

_WHY_HE = {
    "youtube": "סרטון מלא ל-YouTube: נבנה חיפוש לאורך זמן; מעלים לפני שעת השיא כדי שיאונדקס.",
    "youtube_shorts": "Shorts: קליפ אנכי עם הוק בשנייה הראשונה — הפיד של שורטס מתגמל צפייה עד "
                      "הסוף.",
    "tiktok": "TikTok: הקהל הצעיר של CS2 נמצא שם; ערב אחרי בית ספר/עבודה זה זמן השיא בישראל.",
    "instagram_reels": "Reels: הפצה נוספת לאותו קליפ אנכי, קצת אחרי TikTok כדי לא להתחרות "
                       "בעצמנו.",
    "facebook": "פייסבוק: קהל מבוגר יותר, מתאים לסרטונים ארוכים ומונטאז'ים.",
    "x": "X: ציוץ קצר עם וידאו בשעה שגם הקהל באירופה ער.",
    "reddit": "Reddit: קהילת r/GlobalOffensive בינלאומית — מפרסמים בערב אירופה/בוקר ארה\"ב.",
    "discord": "דיסקורד: עדכון לקהילה שלך כשהסרטון יוצא.",
}


class Strategist(StaffMember):
    """Decides platforms and posting times; explains every decision."""

    name = "strategist"
    title_he = "אסטרטג תוכן"
    role_prompt = (
        "You are the Content Strategist of a CS2 creator's agency. You decide WHICH of the "
        "enabled platforms this specific clip should go to and WHEN, and you explain why in one "
        "or two Hebrew sentences per platform.\n"
        "Rules of thumb: funny/fail clips -> TikTok, Shorts, Reels first (short-form virality); "
        "aces and clutches -> Shorts/TikTok for reach plus YouTube long-form if the clip has "
        "context; tutorials/lineups -> YouTube long-form first (search traffic lasts years), "
        "then a 'one tip' short; montages -> YouTube + Facebook. Reddit only for genuinely "
        "impressive or useful clips (r/GlobalOffensive hates low-effort posts) and only to "
        "subreddits the owner configured. Low scores (<=4) -> fewer platforms, not more.\n"
        "Timing (channel time zone, Asia/Jerusalem by default): Hebrew audience peaks weekdays "
        "18:00-22:00, Friday 13:00-16:00, Saturday after 20:00 (after Shabbat); international "
        "audience 21:00-23:00 Israel time. Stagger platforms by 30-90 minutes; long-form "
        "YouTube ~1-2h before the evening peak so it is indexed. Never schedule in the past; "
        "give publish_at_local as YYYY-MM-DDTHH:MM local time.\n\n" + CS2_PLAYBOOK)

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
        self.status("working", f"מתכנן הפצה לסרטון #{video.id}")
        candidates = [p for p in self.relevant_platforms(analysis)
                      if any(v.platform == p for v in variants)]
        tz = local_tz(self.settings)
        now = datetime.now(timezone.utc)
        defaults = self._default_times(candidates, now)
        mock = {
            "posts": [{"platform": p,
                       "publish_at_local": defaults[p].astimezone(tz).strftime("%Y-%m-%dT%H:%M"),
                       "why": _WHY_HE.get(p, "")} for p in candidates],
            "overall": self._overall(analysis, candidates),
        }
        user = (
            f"Now (local, {self.settings.get('publishing.timezone', 'Asia/Jerusalem')}): "
            f"{now.astimezone(tz).strftime('%A %Y-%m-%d %H:%M')}\n"
            f"Enabled candidate platforms (choose a subset, best first): {candidates}\n"
            f"Clip: type={analysis.get('clip_type')}, map={analysis.get('map')}, "
            f"score={analysis.get('score_1_10')}/10, mood={analysis.get('mood')}, "
            f"summary={analysis.get('summary_en')}\n"
            f"Audience: {analysis.get('audience')}; channel language={self.language}\n"
            f"Titles: { {p: (copy.get(p) or {}).get('title') for p in candidates} }")
        data = self.llm.complete_json(self.role_prompt, user, PLAN_SCHEMA, mock_value=mock)

        plans: list[PostPlan] = []
        seen: set[str] = set()
        for item in data.get("posts") or []:
            p = str((item or {}).get("platform") or "")
            if p not in candidates or p in seen:  # code decides what is allowed, not the model
                continue
            seen.add(p)
            when = self._parse_local(item.get("publish_at_local"), now) or defaults[p]
            plans.append(self._make_plan(p, variants, copy, when, item.get("why") or
                                         _WHY_HE.get(p, ""), analysis))
        if not plans and candidates:  # model chose nothing usable → fall back to defaults
            plans = [self._make_plan(p, variants, copy, defaults[p], _WHY_HE.get(p, ""),
                                     analysis) for p in candidates]
        overall = str(data.get("overall") or mock["overall"])
        fresh = self.db.get_video(video.id) if video.id is not None else None
        thumb = ((fresh.meta if fresh else video.meta) or {}).get("thumbnail")
        for pl in plans:
            pl.extra["overall"] = overall
            if thumb and pl.extra.get("aspect") == "16:9":
                pl.extra["thumbnail"] = thumb  # YouTube / Reddit adapters use it
        self.status("idle", f"תוכנית: {', '.join(p.platform for p in plans) or 'אין פלטפורמות'}")
        return plans

    # ------------------------------------------------------------------ helpers
    def _default_times(self, platforms: list[str], now: datetime) -> dict[str, datetime]:
        """Heuristic Israel-time slots, staggered so no two posts share the same minute."""
        out: dict[str, datetime] = {}
        used: set[datetime] = set()
        for p in platforms:
            hour, minute = POSTING_SLOTS.get(p, (19, 0))
            slot = next_slot(self.settings, hour, minute, after=now)
            while slot in used:
                slot += timedelta(minutes=20)
            used.add(slot)
            out[p] = slot
        return out

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

    @staticmethod
    def _overall(analysis: dict, platforms: list[str]) -> str:
        clip = analysis.get("clip_type") or "highlight"
        if not platforms:
            return "אין פלטפורמות פעילות בהגדרות — הפעל לפחות אחת ב-config.yaml."
        return (f"קליפ מסוג {clip} — מתחילים ב-{platforms[0]} ומפזרים בהפרשים כדי שכל פלטפורמה "
                "תקבל את שעת השיא שלה בלי שנתחרה בעצמנו.")
