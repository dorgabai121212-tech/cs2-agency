"""Shared CS2 content-marketing knowledge and small helpers used by several staff members.

Private module (not part of the public contract): constants for maps, clip types, platform
limits and posting windows, plus time-zone helpers that work even without the ``tzdata`` package.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone, tzinfo

from ..config import Settings

log = logging.getLogger("agency.staff")

CLIP_TYPES = ["ace", "clutch", "funny", "highlight", "tutorial", "montage", "fail", "other"]

# Active-duty + popular CS2 maps (display name by lowercase key).
CS2_MAPS: dict[str, str] = {
    "mirage": "Mirage", "inferno": "Inferno", "dust2": "Dust2", "dust 2": "Dust2",
    "nuke": "Nuke", "ancient": "Ancient", "anubis": "Anubis", "vertigo": "Vertigo",
    "overpass": "Overpass", "train": "Train", "office": "Office", "italy": "Italy",
    "מיראז'": "Mirage", "מיראז": "Mirage", "אינפרנו": "Inferno", "דאסט": "Dust2",
    "נוק": "Nuke", "אנוביס": "Anubis", "ורטיגו": "Vertigo", "אנשיינט": "Ancient",
}

CS2_WEAPONS = ["AK-47", "M4A4", "M4A1-S", "AWP", "Desert Eagle", "USP-S", "Glock-18",
               "SSG 08", "MAC-10", "MP9", "Galil AR", "FAMAS", "P250", "Five-SeveN", "Tec-9",
               "Zeus x27", "Knife", "HE Grenade", "Molotov"]

_CLIP_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("ace", ("ace", "אייס", "5k", "חמישה")),
    ("clutch", ("clutch", "קלאץ", "1v", "1 נגד", "1vs")),
    ("tutorial", ("tutorial", "guide", "lineup", "smoke", "מדריך", "סמוק", "טיפ", "how to")),
    ("funny", ("funny", "lol", "מצחיק", "צחוק", "troll", "טרול", "meme")),
    ("fail", ("fail", "פייל", "כישלון", "oops")),
    ("montage", ("montage", "מונטאז", "frag movie")),
    ("highlight", ("highlight", "הייליט", "קיל", "kill", "4k", "3k")),
]

# Hard caps enforced in code by the copywriter (characters, Python code points).
PLATFORM_LIMITS: dict[str, dict[str, int]] = {
    "youtube": {"title": 100, "description": 5000, "tags_total": 500, "hashtags": 15},
    "youtube_shorts": {"title": 100, "description": 5000, "tags_total": 500, "hashtags": 15},
    "tiktok": {"title": 2200, "caption": 2200, "hashtags": 30},
    "instagram_reels": {"title": 2200, "caption": 2200, "hashtags": 30},
    "facebook": {"title": 255, "description": 5000, "hashtags": 30},
    "x": {"title": 280, "caption": 280, "hashtags": 5},
    "reddit": {"title": 300, "description": 10000, "hashtags": 0},
    "discord": {"title": 256, "caption": 2000, "hashtags": 10},
}

VERTICAL_PLATFORMS = {"youtube_shorts", "tiktok", "instagram_reels"}
HORIZONTAL_PLATFORMS = {"youtube", "facebook", "x", "reddit", "discord"}

# Which platforms fit which clip type, best first (strategist uses this as its baseline).
PLATFORM_FIT: dict[str, list[str]] = {
    "ace": ["youtube_shorts", "tiktok", "instagram_reels", "youtube", "x", "reddit", "discord"],
    "clutch": ["youtube_shorts", "tiktok", "youtube", "instagram_reels", "x", "reddit", "discord"],
    "funny": ["tiktok", "youtube_shorts", "instagram_reels", "x", "reddit", "discord"],
    "fail": ["tiktok", "youtube_shorts", "instagram_reels", "x", "discord"],
    "highlight": ["youtube_shorts", "tiktok", "instagram_reels", "youtube", "x", "discord"],
    "tutorial": ["youtube", "youtube_shorts", "tiktok", "reddit", "instagram_reels", "discord"],
    "montage": ["youtube", "youtube_shorts", "instagram_reels", "facebook", "x", "discord"],
    "other": ["youtube_shorts", "tiktok", "youtube", "instagram_reels", "x", "facebook",
              "discord"],
}

# Default local posting slots (hour, minute) in the channel time zone (Israel by default).
# Hebrew audience: after school/work 18:00-22:00; international: 21:00-23:00 IL hits the EU
# evening and the US East afternoon.
POSTING_SLOTS: dict[str, tuple[int, int]] = {
    "youtube": (17, 30),          # long-form: publish before the evening peak so it indexes
    "youtube_shorts": (19, 0),
    "tiktok": (20, 0),
    "instagram_reels": (20, 30),
    "facebook": (19, 30),
    "x": (21, 0),
    "reddit": (22, 0),            # r/GlobalOffensive is US/EU-heavy
    "discord": (19, 15),
}

CS2_PLAYBOOK = """\
Shared agency knowledge (Counter-Strike 2 content marketing):
- Audience: CS2 players aged ~14-30, mostly male, very meme-literate, allergic to clickbait that
  does not deliver. Israeli audience speaks Hebrew but uses English CS slang (ACE, clutch, eco,
  rush B, one-tap, flick, wallbang, spray, smoke, molly, lineup, entry, lurk, retake).
  International audience is English-first (EU + NA), found on r/GlobalOffensive, X and YouTube.
- Maps: Mirage, Inferno, Dust2, Nuke, Ancient, Anubis, Vertigo, Overpass, Train. Map names are
  search terms; put them in titles/tags. Weapons (AK-47, AWP, Deagle, M4A1-S, USP-S) too.
- Clip types: ace, clutch (1vX), funny, fail, highlight, tutorial (smoke/flash lineups, tips),
  montage. Clutches and aces need context (score, 1v3, eco) to land; tutorials need clarity.
- Short-form algorithms (YouTube Shorts, TikTok, Instagram Reels) reward watch-through and
  rewatches: the hook must land in the first 1-2 seconds (start on the action, never on the
  menu/buy phase), keep it under 60s (15-35s is the sweet spot), vertical 9:16, loopable ending,
  big readable on-screen text. Captions should provoke comments ("would you have taken this?").
- YouTube long-form: searchable title (map + clip type + weapon), strong thumbnail, first 2 lines
  of description matter, 3 hashtags max show above the title, chapters for tutorials.
- X: short punchy text + native video, 1-2 hashtags (#CS2). Reddit: no hashtags, no clickbait,
  descriptive title, follow each subreddit's rules, never spam multiple subs at once.
- Posting times (Israel, Asia/Jerusalem): weekdays 18:00-22:00 after school/work; Friday
  13:00-16:00; Saturday after ~20:00. International: 21:00-23:00 Israel time.
- Iron rules: no fake engagement, no bots, no buying views/likes/followers, no spam, no vote
  manipulation, respect platform ToS and rate limits. Money never moves without owner approval.
"""


def detect_clip_type(text: str) -> str:
    """Guess a clip type from free text (title hint / filename); "highlight" by default."""
    low = (text or "").lower()
    for clip_type, words in _CLIP_KEYWORDS:
        if any(w in low for w in words):
            return clip_type
    return "highlight"


def detect_map(text: str) -> str:
    """Guess the CS2 map from free text; "" if none is mentioned."""
    low = (text or "").lower()
    for key, name in CS2_MAPS.items():
        if re.search(rf"(?<![a-z]){re.escape(key)}(?![a-z])", low):
            return name
    return ""


def detect_weapons(text: str) -> list[str]:
    """Weapons mentioned in free text."""
    low = (text or "").lower().replace(" ", "").replace("-", "")
    found = [w for w in CS2_WEAPONS if w.lower().replace(" ", "").replace("-", "") in low]
    if "deagle" in low and "Desert Eagle" not in found:
        found.append("Desert Eagle")
    return found


def local_tz(settings: Settings) -> tzinfo:
    """Channel time zone from config; falls back to a fixed Israel offset without tzdata."""
    name = settings.get("publishing.timezone", "Asia/Jerusalem") or "Asia/Jerusalem"
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # ZoneInfoNotFoundError (e.g. Windows without tzdata) or bad name
        log.warning("time zone %r unavailable (install 'tzdata'); using UTC+3", name)
        return timezone(timedelta(hours=3), "IL")


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp; naive values are treated as UTC. None if invalid."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_utc_iso(dt: datetime) -> str:
    """UTC ISO string (seconds precision), same format as ``models.now_iso``."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def next_slot(settings: Settings, hour: int, minute: int, *, after: datetime | None = None,
              min_lead: timedelta = timedelta(minutes=30)) -> datetime:
    """Next local occurrence of hour:minute at least ``min_lead`` after ``after`` (aware dt)."""
    tz = local_tz(settings)
    base = (after or datetime.now(timezone.utc)).astimezone(tz)
    candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    while candidate < base + min_lead:
        candidate += timedelta(days=1)
    return candidate


def truncate(text: str, limit: int, ellipsis: str = "…") -> str:
    """Cut ``text`` to at most ``limit`` chars, preferring a word boundary, adding an ellipsis."""
    text = (text or "").strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= len(ellipsis):
        return text[:limit]
    cut = text[: limit - len(ellipsis)]
    space = cut.rfind(" ")
    if space >= int(limit * 0.6):
        cut = cut[:space]
    return cut.rstrip(" ,.;:-|") + ellipsis
