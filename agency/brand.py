"""Brand profile (SPEC_V2 §11.6): who the channel is, what it never says, which hashtags go out.

Read from config ``brand:`` with the owner's proven defaults (brand "Imagination",
@imaginationcs2). The copywriter, strategist and promoter put it in their prompts, and the code
ENFORCES it: blocked words are removed (a title that contains one is rejected), the brand
hashtags always go out first within each platform's limits, and AI shots / music get their
label and credit line. YouTube descriptions are limited in UTF-8 BYTES (5000), not characters.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .config import Settings

DEFAULT_NAME = "Imagination"
DEFAULT_HANDLE = "@imaginationcs2"
DEFAULT_BIO = "Legendary CS2 moments. Real demo. Shot like a movie."
DEFAULT_HASHTAGS = ["#CS2", "#CounterStrike2", "#CS2Edit"]

# Skin gambling / case-opening sites, cheats, scams and fake engagement. Matched as whole words
# (case-insensitive; single words also match plain suffixes: "hack" -> "hacks", "hacking").
DEFAULT_BLOCKED_WORDS = [
    # skin gambling, case opening and their sites
    "skin gambling", "skins gambling", "gambling", "betting", "case opening", "case battle",
    "case battles", "free skins", "skin giveaway", "giveaway", "promo code", "referral code",
    "gamdom", "csgoroll", "hellcase", "key-drop", "keydrop", "csgoempire", "clash.gg",
    "skinclub", "farmskins", "datdrop", "csgo500", "duelbits", "rollbit", "stake.com",
    "bc.game", "csgonet",
    # cheats
    "cheat", "hack", "aimbot", "wallhack", "triggerbot", "spinbot", "injector",
    # fake engagement (iron rule 4)
    "sub4sub", "sub for sub", "follow for follow", "buy views", "buy likes", "buy followers",
    "free followers",
    # Hebrew equivalents
    "צ'יט", "צ'יטים", "צ׳יט", "צ׳יטים", "האק", "האקים", "הימורים", "הימורי סקינים",
    "פתיחת קופסאות", "פתיחת קייסים", "סקינים בחינם",
]
# Always blocked, even when the owner edits the list (cheats, gambling sites, fake engagement).
ALWAYS_BLOCKED = ["aimbot", "wallhack", "skin gambling", "free skins", "sub4sub", "sub for sub",
                  "buy views", "buy likes", "buy followers"]

DEFAULT_STYLE_NOTES = "\n".join([
    "Funny, trolling captions with confident banter - never dry, never corporate.",
    "Hook within the first second: the first line names the moment (weapon, map, 1vX).",
    "When a real viewer comment is available, open with it (quote it) and answer it.",
    'The vibe is "pro + crazy AI": real skill plus an impossible cinematic shot.',
    "Never self-mocking fail gags - we never laugh at our own mistakes.",
    "Sell the player and the moment, never the technique.",
])
DEFAULT_AI_LABEL = "A short part was restyled with AI. The rest is the real demo."
DEFAULT_MUSIC_CREDIT = True  # credit the track whenever we know it ("Music: <track>")

# Brand rule: how many hashtags each platform gets (stricter than the platform allows).
# YouTube shows exactly the 3 brand tags; "#Shorts" on Shorts does not count.
HASHTAG_CAPS: dict[str, int] = {"youtube": 3, "youtube_shorts": 3, "tiktok": 5,
                                "instagram_reels": 5, "facebook": 5, "x": 2, "reddit": 0,
                                "discord": 3}
# Hashtags that do nothing but look spammy (never sent).
SPAM_HASHTAGS = {"#fyp", "#foryou", "#foryoupage", "#viral", "#xyzbca", "#sub4sub",
                 "#follow4follow", "#f4f", "#like4like", "#l4l"}

YOUTUBE_PLATFORMS = {"youtube", "youtube_shorts"}
YOUTUBE_DESCRIPTION_BYTES = 5000  # YouTube counts UTF-8 bytes; Hebrew letters take 2 each
# Text limits used by ``Brand.enforce`` (characters; YouTube additionally bytes).
TEXT_LIMITS: dict[str, int] = {"youtube": 5000, "youtube_shorts": 5000, "tiktok": 2200,
                               "instagram_reels": 2200, "facebook": 5000, "x": 280,
                               "reddit": 10000, "discord": 2000}
# Platforms whose post has no caption to label (Reddit posts a title + the video).
NO_TAIL_PLATFORMS = {"reddit"}

HASHTAG_RE = re.compile(r"#\w+", re.U)
_MUSIC_RE = re.compile(r"(?:music|song|track|soundtrack|שיר|מוזיקה)\s*[:：\-–]\s*"
                       r"([^\n|;#]{2,80})", re.I)
_AI_RE = re.compile(r"(?<![a-z])ai(?![a-z])|בינה מלאכותית", re.I)


def _utf8_len(text: str) -> int:
    return len((text or "").encode("utf-8"))


def _truncate(text: str, limit: int) -> str:
    from .staff._playbook import truncate
    return truncate(text, limit)


def _truncate_bytes(text: str, max_bytes: int) -> str:
    from .platforms._common import truncate_utf8
    return truncate_utf8(text, max_bytes, _truncate)


def normalize_hashtag(tag: Any) -> str:
    """``"cs2 edit"`` -> ``"#cs2edit"``; empty string for nothing usable."""
    body = re.sub(r"[\s#,]+", "", str(tag or ""))
    return f"#{body}" if body else ""


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item.lower() not in seen:
            seen.add(item.lower())
            out.append(item)
    return out


def _word_pattern(word: str) -> re.Pattern[str]:
    """Whole-word, case-insensitive; spaces/hyphens interchangeable; plain suffixes allowed."""
    parts = [re.escape(p) for p in re.split(r"[\s\-]+", word.strip()) if p]
    body = r"[\s\-]*".join(parts)
    if len(parts) == 1 and re.fullmatch(r"[a-z]{3,}", word.strip().lower()):
        body += r"(?:s|es|ed|er|ers|ing|y)?"
    return re.compile(rf"(?<!\w){body}(?!\w)", re.I | re.U)


def detect_music(*texts: Any) -> str:
    """Track name from the owner's note ("music: Mozart phonk"), or ""."""
    for text in texts:
        m = _MUSIC_RE.search(str(text or ""))
        if m:
            return m.group(1).strip(" .,-–\"'")
    return ""


def detect_ai(*texts: Any) -> bool:
    """True when the owner's note / file name says the clip has AI shots ("Deagle 3K, AI")."""
    return any(_AI_RE.search(str(t or "").replace("_", " ")) for t in texts)


@dataclass
class Brand:
    """The channel's brand profile (config ``brand:`` merged over the owner's defaults)."""

    name: str = DEFAULT_NAME
    handle: str = DEFAULT_HANDLE
    bio: str = DEFAULT_BIO
    hashtags: list[str] = field(default_factory=lambda: list(DEFAULT_HASHTAGS))
    blocked_words: list[str] = field(default_factory=lambda: list(DEFAULT_BLOCKED_WORDS))
    style_notes: str = DEFAULT_STYLE_NOTES
    ai_label: str = DEFAULT_AI_LABEL      # the disclosure line ("" = no text label)
    ai_label_always: bool = False         # config ``ai_label: true`` = label every clip
    music_credit: str | bool = DEFAULT_MUSIC_CREDIT  # True = credit known tracks; str = fixed

    def __post_init__(self) -> None:
        self._patterns = [(w, _word_pattern(w)) for w in self.blocked_words if w.strip()]

    def to_dict(self) -> dict:
        """Plain dict (no secrets here: the brand is public text)."""
        return {k: v for k, v in asdict(self).items() if not k.startswith("_")}

    # ------------------------------------------------------------------ blocked words
    def blocked_in(self, text: Any) -> list[str]:
        """Blocked words/phrases found in ``text`` (as configured), in list order."""
        value = str(text or "")
        if not value:
            return []
        hashtag_bodies = [re.sub(r"[\s\-_]+", "", h.lower()) for h in HASHTAG_RE.findall(value)]
        hits: list[str] = []
        for word, pattern in self._patterns:
            if pattern.search(value):
                hits.append(word)
                continue
            # "#freeskins" / "#CaseOpening": compare hashtag bodies without separators too
            key = re.sub(r"[\s\-_]+", "", word.lower())
            if len(key) >= 5 and any(key in body for body in hashtag_bodies):
                hits.append(word)
        return hits

    def is_clean(self, text: Any) -> bool:
        return not self.blocked_in(text)

    def clean(self, text: Any) -> str:
        """``text`` with every blocked word/phrase (and hashtags containing one) removed.
        Lines that contained nothing blocked are kept exactly as they were."""
        value = str(text or "")
        if not value:
            return ""
        lines = []
        for original in value.splitlines():
            line = original
            for tag in HASHTAG_RE.findall(line):
                if self.blocked_in(tag):
                    line = re.sub(rf"(?<!\w){re.escape(tag)}(?!\w)", " ", line)
            for _word, pattern in self._patterns:
                line = pattern.sub(" ", line)
            if line != original:  # tidy only what we touched
                line = re.sub(r"(?<!\S)#(?!\w)", " ", line)        # a lone "#" left behind
                line = re.sub(r"\(\s*\)|\[\s*\]|\"\s*\"", " ", line)  # empty brackets/quotes
                line = re.sub(r"[ \t]{2,}", " ", line)
                line = re.sub(r"\s+([,.!?;:])", r"\1", line)
                line = re.sub(r"([,;:])(?:\s*[,;:])+", r"\1", line)
                line = re.sub(r"^[\s,;:]+|[\s,;:]+$", "", line)
            lines.append(line)
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

    # ------------------------------------------------------------------ hashtags
    def hashtag_cap(self, platform: str) -> int:
        return HASHTAG_CAPS.get(platform, 5)

    def required_hashtags(self, platform: str) -> list[str]:
        """The brand hashtags this platform always gets (first ``cap`` of them)."""
        tags = _dedupe([normalize_hashtag(h) for h in self.hashtags])
        return [t for t in tags if t and not self.blocked_in(t)][: self.hashtag_cap(platform)]

    def merge_hashtags(self, platform: str, hashtags: list[Any] | None) -> list[str]:
        """Brand hashtags first, then the writer's (cleaned, deduped, no spam), capped."""
        cap = self.hashtag_cap(platform)
        extra = [normalize_hashtag(h) for h in hashtags or []]
        shorts = [h for h in extra if h.lower() == "#shorts"] if platform == "youtube_shorts" \
            else []
        extra = [h for h in extra if h and h.lower() not in SPAM_HASHTAGS
                 and h.lower() != "#shorts" and not self.blocked_in(h)]
        merged = _dedupe([*self.required_hashtags(platform), *extra])[:cap]
        return [*shorts[:1], *merged]

    # ------------------------------------------------------------------ labels
    def ai_line(self, has_ai: bool) -> str:
        """The AI disclosure line for this clip ("" when it does not apply)."""
        if not (has_ai or self.ai_label_always):
            return ""
        return self.ai_label or (DEFAULT_AI_LABEL if self.ai_label_always else "")

    def music_line(self, track: str | None) -> str:
        """``"Music: <track>"`` when the track is known (or the fixed credit from config)."""
        track = (track or "").strip()
        if track and self.music_credit is not False:
            return track if track.lower().startswith("music") else f"Music: {track}"
        if isinstance(self.music_credit, str) and self.music_credit.strip():
            credit = self.music_credit.strip()
            return credit if credit.lower().startswith("music") else f"Music: {credit}"
        return ""

    def tail_lines(self, platform: str, *, has_ai: bool = False, music: str | None = None
                   ) -> list[str]:
        """Lines the code appends to the description (music credit, AI label)."""
        if platform in NO_TAIL_PLATFORMS:
            return []
        return [line for line in (self.music_line(music), self.ai_line(has_ai)) if line]

    # ------------------------------------------------------------------ enforcement
    def enforce(self, text: str, platform: str) -> str:
        """Final post text: blocked words removed, missing brand hashtags appended, and the
        result fitted to ``platform``'s limit (YouTube: 5000 UTF-8 bytes). Hashtags beyond
        the brand's cap for this platform are dropped (brand ones are kept first)."""
        body = self.clean(text)
        required = self.required_hashtags(platform)
        cap = self.hashtag_cap(platform)
        body = self._limit_inline_hashtags(body, required, cap)
        present = {h.lower() for h in HASHTAG_RE.findall(body)}
        need = [h for h in required if h.lower() not in present]
        limit = TEXT_LIMITS.get(platform, 5000)
        byte_limit = YOUTUBE_DESCRIPTION_BYTES if platform in YOUTUBE_PLATFORMS else None
        tail = " ".join(need)
        sep = " " if platform == "x" else "\n\n"
        if tail and len(tail) + len(sep) < limit:
            room = limit - len(tail) - len(sep)
            byte_room = byte_limit - _utf8_len(sep + tail) if byte_limit else None
            body = self._fit(body, room, byte_room)
            return f"{body}{sep}{tail}" if body else tail
        return self._fit(body, limit, byte_limit)

    @staticmethod
    def _limit_inline_hashtags(body: str, required: list[str], cap: int) -> str:
        """Drop duplicate/spam hashtags and the writer's extras beyond the brand cap (the
        brand hashtags always keep their slots, whether present or appended)."""
        req = {h.lower() for h in required}
        budget = max(0, cap - len(req))
        seen: set[str] = set()
        others = 0
        out: list[str] = []
        pos = 0
        for m in HASHTAG_RE.finditer(body):
            low = m.group(0).lower()
            drop = low in seen or low in SPAM_HASHTAGS
            if not drop and low not in req:
                others += 1
                drop = others > budget
            seen.add(low)
            out.append(body[pos:m.start()])
            if not drop:
                out.append(m.group(0))
            pos = m.end()
        out.append(body[pos:])
        lines = [re.sub(r"[ \t]{2,}", " ", ln).rstrip() for ln in "".join(out).splitlines()]
        return "\n".join(lines).strip()

    @staticmethod
    def _fit(text: str, limit: int, byte_limit: int | None = None) -> str:
        out = _truncate(text, max(0, limit))
        if byte_limit is not None:
            out = _truncate_bytes(out, max(0, byte_limit))
        return out

    # ------------------------------------------------------------------ prompts
    def prompt_block(self) -> str:
        """English brand brief for staff prompts (public brand text only, never secrets)."""
        blocked = ", ".join(f'"{w}"' for w in self.blocked_words[:80])
        tags = " ".join(self.required_hashtags("youtube")) or "(none)"
        ai = (f'Clips with AI shots get this exact label (added in code): "{self.ai_line(True)}".'
              if self.ai_line(True) else "No AI label is configured.")
        return (
            f'Brand: "{self.name}" ({self.handle}). Bio/tagline: "{self.bio}".\n'
            f"Brand hashtags (always first, added in code): {tags}.\n"
            f"Owner's style (binding):\n"
            + "\n".join(f"- {line.strip(' -')}" for line in self.style_notes.splitlines()
                        if line.strip())
            + f"\nNever use these words or anything about them: {blocked}.\n{ai}\n"
            "Music: credit the track only when it is given to you; never invent a track.")


def load_brand(settings: Settings | None) -> Brand:
    """The brand profile from config ``brand:``; missing fields fall back to the owner's
    proven defaults (name/handle also fall back to ``channel.name`` / ``channel.handle``)."""
    raw = (settings.get("brand", {}) if settings is not None else {}) or {}
    if not isinstance(raw, dict):
        raw = {}

    def text(key: str, *fallbacks: Any) -> str:
        for value in (raw.get(key), *fallbacks):
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    channel_name = settings.get("channel.name") if settings is not None else ""
    channel_handle = settings.get("channel.handle") if settings is not None else ""
    handle = text("handle", channel_handle, DEFAULT_HANDLE)
    if handle and not handle.startswith("@"):
        handle = f"@{handle}"

    hashtags = raw.get("hashtags")
    hashtags = ([normalize_hashtag(h) for h in hashtags if normalize_hashtag(h)]
                if isinstance(hashtags, list) else list(DEFAULT_HASHTAGS))
    blocked = raw.get("blocked_words")
    blocked = ([str(w).strip() for w in blocked if str(w or "").strip()]
               if isinstance(blocked, list) else list(DEFAULT_BLOCKED_WORDS))
    blocked = _dedupe([*blocked, *ALWAYS_BLOCKED])

    ai_raw = raw.get("ai_label", DEFAULT_AI_LABEL)
    if ai_raw is True:
        ai_label, ai_always = DEFAULT_AI_LABEL, True
    elif isinstance(ai_raw, str):
        ai_label, ai_always = ai_raw.strip(), False
    else:  # False / None: no text label (platform AI flags still follow the clip)
        ai_label, ai_always = "", False

    music_raw = raw.get("music_credit", DEFAULT_MUSIC_CREDIT)
    music: str | bool = music_raw.strip() if isinstance(music_raw, str) else \
        (music_raw is not False)

    return Brand(name=text("name", channel_name, DEFAULT_NAME), handle=handle,
                 bio=text("bio", DEFAULT_BIO), hashtags=_dedupe(hashtags),
                 blocked_words=blocked, style_notes=text("style_notes", DEFAULT_STYLE_NOTES),
                 ai_label=ai_label, ai_label_always=ai_always, music_credit=music)


__all__ = ["ALWAYS_BLOCKED", "DEFAULT_BLOCKED_WORDS", "DEFAULT_HASHTAGS", "HASHTAG_CAPS",
           "Brand", "detect_ai", "detect_music", "load_brand", "normalize_hashtag"]
