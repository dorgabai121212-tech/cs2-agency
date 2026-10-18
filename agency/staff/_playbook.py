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
# The owner's proven package: every short goes to Shorts, Facebook, Instagram and TikTok.
PLATFORM_FIT: dict[str, list[str]] = {
    "ace": ["youtube_shorts", "tiktok", "instagram_reels", "facebook", "youtube", "x",
            "reddit", "discord"],
    "clutch": ["youtube_shorts", "tiktok", "instagram_reels", "facebook", "youtube", "x",
               "reddit", "discord"],
    "funny": ["tiktok", "youtube_shorts", "instagram_reels", "facebook", "x", "reddit",
              "discord"],
    "fail": ["tiktok", "youtube_shorts", "instagram_reels", "facebook", "x", "discord"],
    "highlight": ["youtube_shorts", "tiktok", "instagram_reels", "facebook", "youtube", "x",
                  "discord"],
    "tutorial": ["youtube", "youtube_shorts", "tiktok", "reddit", "instagram_reels", "discord"],
    "montage": ["youtube", "youtube_shorts", "instagram_reels", "facebook", "x", "discord"],
    "other": ["youtube_shorts", "tiktok", "instagram_reels", "facebook", "youtube", "x",
              "discord"],
}

# Fallback local posting slots (hour, minute) in the channel time zone, used only for platforms
# without a proven time (``publishing.default_times`` / ``PROVEN_TIMES``). International
# audience: 21:00-23:00 Israel time hits the EU evening and the US East afternoon.
POSTING_SLOTS: dict[str, tuple[int, int]] = {
    "youtube": (17, 0),
    "youtube_shorts": (17, 0),
    "facebook": (17, 40),
    "instagram_reels": (20, 0),
    "tiktok": (20, 40),
    "x": (21, 0),
    "reddit": (22, 0),            # r/GlobalOffensive is US/EU-heavy
    "discord": (19, 15),
}

# The owner's proven times from his live studio (Israel time, SPEC_V2 §11): YouTube 17:00,
# Facebook 17:40, Instagram 20:00, TikTok 20:40. Config ``publishing.default_times`` overrides.
PROVEN_TIMES: dict[str, str] = {"youtube": "17:00", "youtube_shorts": "17:00",
                                "facebook": "17:40", "instagram_reels": "20:00",
                                "tiktok": "20:40"}

YOUTUBE_FAMILY = ("youtube", "youtube_shorts")
# Minimum minutes after the (last) YouTube post. Instagram and TikTok cannot be taken back
# through the API, so they always go out after YouTube is already live (studio rule).
AFTER_YOUTUBE_MIN: dict[str, int] = {"instagram_reels": 30, "tiktok": 30}

PLATFORM_HE: dict[str, str] = {"youtube": "יוטיוב", "youtube_shorts": "יוטיוב Shorts",
                               "tiktok": "טיקטוק", "instagram_reels": "אינסטגרם",
                               "facebook": "פייסבוק", "x": "X", "reddit": "רדיט",
                               "discord": "דיסקורד"}

CLIP_TYPE_HE: dict[str, str] = {"ace": "אייס", "clutch": "קלאץ'", "funny": "מצחיק",
                                "fail": "רגע מטורף", "highlight": "הייליט", "tutorial": "טיפ",
                                "montage": "מונטאז'", "other": "רגע מטורף"}

_HHMM = re.compile(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$")


def parse_hhmm(value: object) -> tuple[int, int] | None:
    """``"17:40"`` -> ``(17, 40)``; None when invalid."""
    m = _HHMM.match(str(value or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def default_times(settings: Settings) -> dict[str, tuple[int, int]]:
    """Posting time per platform: config ``publishing.default_times`` > the owner's proven
    times > generic fallback slots. Invalid config values are ignored."""
    out = dict(POSTING_SLOTS)
    for key, value in PROVEN_TIMES.items():
        out[key] = parse_hhmm(value) or out.get(key, (19, 0))
    configured = settings.get("publishing.default_times", {}) or {}
    if isinstance(configured, dict):
        for key, value in configured.items():
            hm = parse_hhmm(value)
            if hm and key in out:
                out[key] = hm
    return out


CS2_PLAYBOOK = """\
Shared agency knowledge (Counter-Strike 2 content marketing for the channel "Imagination"):
- Channel: movie-grade CS2 moments rebuilt from real demos (cinematic cameras, real motion
  blur, bullet-time on the deciding bullet), sometimes with a short AI-restyled shot.
  Tagline: "Legendary CS2 moments. Real demo. Shot like a movie."
- Audience: the INTERNATIONAL CS2 community, English-first (EU + NA + the rest), players aged
  ~14-30, very meme-literate, allergic to clickbait that does not deliver. Public copy is in
  English; the owner (Dor, Israel) reads the team's notes in Hebrew.
- Maps: Mirage, Inferno, Dust2, Nuke, Ancient, Anubis, Vertigo, Overpass, Train. Map names,
  weapons (AK-47, AWP, Deagle, M4A1-S, USP-S) and "ace / clutch / 1vX" are search terms.
- Clip types: ace, clutch (1vX), funny, highlight, tutorial, montage. A clip of an enemy's
  mistake is framed as the enemy's fail; never self-mocking "fail" gags.
- Short-form (YouTube Shorts, TikTok, Instagram Reels) rewards watch-through: the hook lands
  within the first second, 15-30s is the sweet spot, vertical 9:16, loopable ending.
- YouTube: exactly 3 hashtags (#CS2 #CounterStrike2 #CS2Edit); sell the player and the moment,
  never the technique. TikTok: first caption line carries the search words, 3-5 hashtags, never
  #fyp. Instagram: name "Counter-Strike 2" explicitly, max 5 hashtags. X: no links.
  Reddit: no hashtags, no clickbait, follow each subreddit's rules, never spam several subs.
- Proven posting times (Israel, Asia/Jerusalem): YouTube 17:00, Facebook 17:40, Instagram
  20:00, TikTok 20:40. Instagram and TikTok always go at least 30 minutes after YouTube.
- Never mention skin gambling, case opening, betting, cheats or hacks, giveaways or promo codes.
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
