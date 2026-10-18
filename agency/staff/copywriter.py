"""Copywriter: titles, descriptions, tags and hashtags per platform, brand rules and limits
enforced in code.

Public copy is ENGLISH (the owner's live studio posts in English for the international CS2
audience). After the model writes, the code removes blocked words (a title containing one is
rejected), puts the brand hashtags first, appends the AI label / music credit, and fits every
platform's limits (the YouTube description in UTF-8 bytes).
"""
from __future__ import annotations

import re
from typing import Any

from ..brand import Brand, detect_ai, detect_music, load_brand
from ..models import Video
from ..platforms._common import truncate_utf8
from ._playbook import CS2_PLAYBOOK, PLATFORM_LIMITS, truncate
from .base import StaffMember

COPY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "posts": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "platform": {"type": "string"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "hashtags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["platform", "title", "description", "tags", "hashtags"]}},
    },
    "required": ["posts"],
}

YOUTUBE_PLATFORMS = {"youtube", "youtube_shorts"}
YOUTUBE_DESCRIPTION_BYTES = 5000  # Hebrew letters are 2 bytes each

# Every platform is written in English (international CS2 audience), like the owner's studio.
ENGLISH_PLATFORMS = {"youtube", "youtube_shorts", "tiktok", "instagram_reels", "facebook", "x",
                     "reddit", "discord"}

# Mock titles: sell the player and the moment, trolling not dry, never a self-mocking fail.
_TITLE_EN = {
    "ace": "{weapon} ACE on {map}, but you see the last bullet",
    "clutch": "The {map} clutch nobody saw coming",
    "funny": "Bro really thought he had me on {map}",
    "fail": "He really thought that would work on {map}",
    "tutorial": "The {map} trick that wins this round",
    "montage": "{weapon} on {map}, every shot is a movie",
    "highlight": "{weapon} on {map}, shot like a movie",
    "other": "Legendary moment on {map}, shot like a movie",
}
_CLIP_WORDS = {"ace": "ace", "clutch": "clutch", "funny": "funny moment", "fail": "outplay",
               "tutorial": "tip", "montage": "montage", "highlight": "highlight",
               "other": "moment"}
_TROLL_LINE = {
    "ace": "Five down. Nobody asked for a replay, so here's a movie.",
    "clutch": "They had the numbers. We had the last bullet.",
    "funny": "He was so confident. That was the problem.",
    "fail": "He had a plan. It did not include us.",
    "tutorial": "Steal this before your teammates do.",
    "montage": "Pick your favourite frame. We can't.",
    "highlight": "Nobody in that lobby was ready for this.",
    "other": "Rewatch it. We'll wait.",
}
_QUESTION = {
    "ace": "Who's your pick for the fifth kill?",
    "clutch": "Would you have taken this fight?",
    "funny": "Would you have fallen for it?",
    "fail": "Be honest, would you have peeked?",
    "tutorial": "Did you know this one?",
}
_HEBREW_RE = re.compile(r"[֐-׿]")


def _clean_tag(tag: str) -> str:
    return re.sub(r"\s+", " ", str(tag).replace("#", "").replace(",", " ")).strip()


def _clean_hashtag(tag: str) -> str:
    body = re.sub(r"[\s#,]+", "", str(tag))
    return f"#{body}" if body else ""


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.lower()
        if item and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _fit_tags(tags: list[str], total: int) -> list[str]:
    """Keep tags (in order) while the YouTube character budget allows.

    YouTube counts commas between tags and wraps tags containing spaces in quotes.
    """
    kept: list[str] = []
    used = 0
    for tag in tags:
        cost = len(tag) + (2 if " " in tag else 0) + (1 if kept else 0)
        if len(tag) > 100 or used + cost > total:
            continue
        kept.append(tag)
        used += cost
    return kept


def _caption_with_hashtags(text: str, hashtags: list[str], limit: int) -> tuple[str, list[str]]:
    """Fit ``text`` + " " + space-joined hashtags into ``limit``.

    Hashtags may use at most half of the budget (extra ones are dropped); the text is cut to
    whatever room is left.
    """
    tags = list(hashtags)
    while tags and len(" ".join(tags)) + 1 > limit // 2:
        tags.pop()
    if not tags:
        return truncate(text, limit), []
    return truncate(text, limit - 1 - len(" ".join(tags))), tags


def tags_total_len(tags: list[str]) -> int:
    """YouTube's tag-length accounting (commas + quotes for multi-word tags)."""
    return sum(len(t) + (2 if " " in t else 0) for t in tags) + max(0, len(tags) - 1)


def enforce_limits(platform: str, copy: dict) -> dict:
    """Return a cleaned copy of ``copy`` that is guaranteed to fit ``platform``'s limits.

    - YouTube/Shorts: title ≤100 chars, description ≤5000 UTF-8 bytes, tags total ≤500 chars; Shorts gets #Shorts.
    - TikTok / Instagram Reels: description + " " + hashtags ≤2200; Reels ≤30 hashtags in caption.
    - X: description + hashtags ≤280 (title ≤280). Reddit: title ≤300, no hashtags.
    """
    limits = PLATFORM_LIMITS.get(platform, {"title": 100, "description": 5000, "hashtags": 10})
    title = re.sub(r"\s+", " ", str(copy.get("title") or "")).strip()
    description = str(copy.get("description") or "").strip()
    tags = _dedupe([_clean_tag(t) for t in copy.get("tags") or []])
    hashtags = _dedupe([_clean_hashtag(h) for h in copy.get("hashtags") or []])

    if platform == "youtube_shorts" and "#shorts" not in {h.lower() for h in hashtags}:
        hashtags.insert(0, "#Shorts")

    max_hashtags = limits.get("hashtags", 10)
    if "caption" in limits and platform == "instagram_reels":
        inline = len(re.findall(r"#\w+", description))
        max_hashtags = max(0, min(max_hashtags, 30 - inline))
    hashtags = hashtags[:max_hashtags]

    title = truncate(title, limits.get("title", 100))
    if "caption" in limits:
        description, hashtags = _caption_with_hashtags(description, hashtags, limits["caption"])
    else:
        description = truncate(description, limits.get("description", 5000))
    if platform in YOUTUBE_PLATFORMS:  # YouTube counts the description in UTF-8 BYTES
        description = truncate_utf8(description, YOUTUBE_DESCRIPTION_BYTES, truncate)
    if "tags_total" in limits:
        tags = _fit_tags(tags, limits["tags_total"])
    else:
        tags = tags[:30]
    return {"title": title, "description": description, "tags": tags, "hashtags": hashtags}


def _append_tail(platform: str, copy: dict, tail: list[str]) -> str:
    """Description with the brand's tail lines (music credit, AI label) appended, cutting the
    body - never the label - so the platform limit still holds together with the hashtags."""
    description = copy["description"]
    missing = [line for line in tail if line.lower() not in description.lower()]
    if not missing:
        return description
    limits = PLATFORM_LIMITS.get(platform, {"description": 5000})
    sep = "\n"
    tail_text = sep.join(missing)
    if "caption" in limits:
        tags = " ".join(copy["hashtags"])
        room = limits["caption"] - (len(tags) + 1 if tags else 0)
    else:
        room = limits.get("description", 5000)
    body_room = room - len(tail_text) - (len(sep) if description else 0)
    if body_room < 0:
        return description  # the label cannot fit at all (never happens with real limits)
    body = truncate(description, body_room) if description else ""
    if platform in YOUTUBE_PLATFORMS:
        byte_room = YOUTUBE_DESCRIPTION_BYTES - len((sep + tail_text).encode("utf-8"))
        body = truncate_utf8(body, max(0, byte_room), truncate)
    return f"{body}{sep}{tail_text}" if body else tail_text


def finalize_copy(platform: str, raw: dict, brand: Brand, *, has_ai: bool = False,
                  music: str | None = None, fallback_title: str = "") -> dict:
    """Brand rules + platform limits for one platform's copy (what actually gets posted).

    1. A title with a blocked word is REJECTED (replaced by ``fallback_title``); blocked words
       are removed from the description, and tags/hashtags containing one are dropped.
    2. Brand hashtags first, then the writer's, capped per platform (no #fyp spam).
    3. ``enforce_limits`` (lengths, YouTube bytes), then the AI label / music credit lines.
    """
    title = str(raw.get("title") or "")
    if not title.strip() or brand.blocked_in(title):
        title = fallback_title or brand.clean(title) or brand.name
    title = brand.clean(title)
    description = brand.clean(raw.get("description") or "")
    tags = [t for t in (raw.get("tags") or []) if str(t).strip() and brand.is_clean(t)]
    if platform == "reddit":
        hashtags: list[str] = []
    else:
        hashtags = brand.merge_hashtags(platform, list(raw.get("hashtags") or []))
    out = enforce_limits(platform, {"title": title, "description": description, "tags": tags,
                                    "hashtags": hashtags})
    out["description"] = _append_tail(platform, out, brand.tail_lines(platform, has_ai=has_ai,
                                                                      music=music))
    return out


class Copywriter(StaffMember):
    """Writes platform-native English copy in the brand's voice (owner notes stay Hebrew)."""

    name = "copywriter"
    title_he = "קופירייטר"

    @property
    def brand(self) -> Brand:
        return load_brand(self.settings)

    @property
    def role_prompt(self) -> str:  # type: ignore[override]
        return self.build_prompt(self.brand)

    @staticmethod
    def build_prompt(brand: Brand) -> str:
        """The copywriter's system prompt for this brand (ported from the studio's prompt)."""
        tags = " ".join(brand.required_hashtags("youtube"))
        return (
            f'You are the Copywriter / SEO specialist of "{brand.name}" ({brand.handle}), a '
            "Counter-Strike 2 channel run by Dor (Israel). The channel's signature: movie-grade "
            "CS2 moments rebuilt from real demos with cinematic cameras, real motion blur and "
            "bullet-time on the deciding bullet, sometimes with a short AI-restyled shot.\n"
            "You write the public text for every requested platform. Answer only with the "
            "structured output.\n\n"
            + brand.prompt_block() + "\n\n"
            "Hard rules:\n"
            "- Public copy is in ENGLISH for the international CS2 audience (no Hebrew in "
            "titles, captions or tags).\n"
            "- Sell the PLAYER and the MOMENT, never the technique. Good: \"ropz's 1v4 that "
            "saved the Major, but you see the last bullet\". Bad: \"HLAE Blender bullet-time "
            "cinematic\".\n"
            "- Never invent facts. Use only what the analysis and Dor's note say; never guess a "
            "pro's name, a score or an event. Dor's own gameplay is played by "
            f'"{brand.name}" - never pretend it is a pro.\n'
            "- Never imply the content is official or endorsed by Valve, a team, a player or a "
            "tournament organiser. No mocking real players. Nothing political.\n"
            "- Funny, trolling, confident - never dry. The hook lands in the first second: the "
            "first line names the moment (weapon, map, ace/clutch/1vX). If a real viewer "
            "comment is given, open with it in quotes and answer it.\n"
            "- Never a self-mocking fail gag. A clip of an enemy's mistake is the ENEMY's fail.\n"
            "- Titles build curiosity with a concrete hook (stakes, numbers, the impossible "
            "part). No clickbait lies, no ALL CAPS titles, at most one emoji per caption and "
            "none in YouTube titles.\n"
            "- Never mention skin gambling, case opening, betting, cheats or hacks, \"free "
            "skins\", promo codes or giveaways. No calls to sub4sub, no fake engagement.\n"
            "- Hashtags go ONLY in the `hashtags` arrays, never inside titles/descriptions; the "
            f"system puts the brand hashtags ({tags}) first and appends the AI label and the "
            "music credit itself.\n\n"
            "Per platform:\n"
            "- youtube (16:9): title <= 70 chars with the searchable words (CS2 + player/map/"
            "weapon), no emoji. Description: line 1 = searchable words incl. \"CS2 edit\", then "
            "1-2 lines of story, then the bio line. Tags: 10-15 search phrases (CS2, "
            "Counter-Strike 2, CS2 edit, cs2 highlights, map, weapon, clip type).\n"
            "- youtube_shorts: one title of 40-60 chars, curiosity + one keyword.\n"
            "- tiktok: the FIRST caption line contains the words people search (CS2, map, "
            "weapon, ace/clutch/1vX); 3-5 hashtags; never #fyp / #foryou / #viral.\n"
            "- instagram_reels: short caption that names \"Counter-Strike 2\" explicitly, "
            "max 5 hashtags.\n"
            "- facebook: short caption, 3-5 hashtags.\n"
            "- x: one punchy line (<= 200 chars), no links, 1-2 hashtags.\n"
            "- reddit: descriptive, humble title, no hashtags, no emojis, no self-promo tone.\n"
            "- discord: a short hype line for Dor's own server.\n"
            "Return one entry per requested platform.\n\n" + CS2_PLAYBOOK)

    def write(self, video: Video, analysis: dict, platforms: list[str]) -> dict[str, dict]:
        """Copy per platform: {platform: {title, description, tags, hashtags}} (brand rules and
        limits enforced in code)."""
        brand = self.brand
        label = self._label(video)
        self.status("working", f"כותב כותרות ל\"{label}\"" if label else "כותב כותרות")
        has_ai = bool(analysis.get("ai_shots")) or detect_ai(video.title_hint)
        music = (str(analysis.get("music") or "") or str((video.meta or {}).get("music") or "")
                 or detect_music(video.title_hint))
        comment = str((video.meta or {}).get("viewer_comment") or "").strip()
        fallbacks = {p: self._mock_copy(video, analysis, p, brand) for p in platforms}
        mock = {"posts": [{"platform": p, **fallbacks[p]} for p in platforms]}
        user = (
            f"Dor's note / title hint: {video.title_hint or '(none)'}\n"
            f"Analysis: clip_type={analysis.get('clip_type')}, map={analysis.get('map')}, "
            f"weapons={analysis.get('weapons')}, mood={analysis.get('mood')}, "
            f"score={analysis.get('score_1_10')}/10, hook at {analysis.get('hook_seconds')}s\n"
            f"Summary: {analysis.get('summary_en')}\nHighlights: {analysis.get('highlights')}\n"
            f"Clip has AI-restyled shots: {'yes' if has_ai else 'no'}\n"
            f"Music credit: {music or '(none given - do not invent one)'}\n"
            f"Real viewer comment to open with: {comment or '(none)'}\n"
            f"Write copy for these platforms: {', '.join(platforms)}.")
        data = self.llm.complete_json(self.role_prompt, user, COPY_SCHEMA, mock_value=mock)

        by_platform = {str(p.get("platform")): p for p in data.get("posts") or []
                       if isinstance(p, dict)}
        out: dict[str, dict] = {}
        for p in platforms:
            raw = by_platform.get(p) or fallbacks[p]
            out[p] = finalize_copy(p, raw, brand, has_ai=has_ai, music=music,
                                   fallback_title=fallbacks[p]["title"])
        self.status("idle", "הכותרות מוכנות")
        return out

    # ------------------------------------------------------------------ mock copy
    @staticmethod
    def _label(video: Video) -> str:
        hint = (video.title_hint or "").strip()
        return truncate(hint, 40) if hint else ""

    @staticmethod
    def _hint_title(video: Video, brand: Brand) -> str:
        """The owner's own English title hint, when it is usable as a public title."""
        hint = re.sub(r"\s+", " ", (video.title_hint or "")).strip()
        hint = re.split(r"\s*[|;]\s*|\s+-\s+(?:music|song)\b|\b(?:music|song)\s*:", hint,
                        flags=re.I)[0].strip()
        hint = re.sub(r"[\s,(\-]*\bAI\b\)?\s*$", "", hint).strip(" ,-")  # "Deagle 3K, AI"
        if (not 8 <= len(hint) <= 70 or _HEBREW_RE.search(hint) or not brand.is_clean(hint)
                or "#" in hint or hint.isupper()):
            return ""
        return hint[0].upper() + hint[1:]

    def _mock_copy(self, video: Video, analysis: dict, platform: str,
                   brand: Brand | None = None) -> dict:
        """Deterministic English copy in the brand's voice (offline mode and fallbacks)."""
        brand = brand or self.brand
        clip = analysis.get("clip_type") or "highlight"
        if clip not in _TITLE_EN:
            clip = "other"
        cs_map = analysis.get("map") or "CS2"
        weapon = (analysis.get("weapons") or ["AK-47"])[0]
        fmt = {"map": cs_map, "weapon": weapon}
        title = self._hint_title(video, brand) or _TITLE_EN[clip].format(**fmt)
        clip_word = _CLIP_WORDS[clip]
        search_line = f"CS2 {cs_map} {weapon} {clip_word}".replace("CS2 CS2", "CS2")
        troll = _TROLL_LINE[clip]
        question = _QUESTION.get(clip, "Which moment should we shoot next?")
        comment = str((video.meta or {}).get("viewer_comment") or "").strip()
        opener = f"\"{truncate(comment, 120)}\" - challenge accepted." if comment else ""
        summary = str(analysis.get("summary_en") or "").strip()
        map_tag = re.sub(r"[^A-Za-z0-9]", "", cs_map)
        extra_tags = [f"#{map_tag}" if map_tag and map_tag != "CS2" else "", "#cs2clips"]
        tags = ["CS2", "Counter-Strike 2", "CS2 edit", "cs2 highlights", cs_map, weapon,
                f"CS2 {clip_word}", f"{cs_map} {clip_word}", "counter strike 2",
                f"{brand.name} CS2", "cinematic CS2"]
        lines = [line for line in (opener, troll) if line]
        if platform == "youtube":
            desc = "\n".join(x for x in (f"{search_line} | CS2 edit", *lines, summary, brand.bio)
                             if x)
            return {"title": title, "description": desc,
                    "tags": tags, "hashtags": list(brand.hashtags)}
        if platform == "youtube_shorts":
            return {"title": title, "description": "\n".join([search_line, *lines]),
                    "tags": tags, "hashtags": list(brand.hashtags)}
        if platform == "tiktok":
            return {"title": title, "description": "\n".join([f"{search_line}. {title}",
                                                              question]),
                    "tags": tags[:8], "hashtags": [*brand.hashtags, *extra_tags]}
        if platform == "instagram_reels":
            return {"title": title,
                    "description": "\n".join([f"Counter-Strike 2 on {cs_map}: {title}.",
                                              *lines[:1], question]),
                    "tags": tags[:8], "hashtags": [*brand.hashtags, *extra_tags]}
        if platform == "facebook":
            return {"title": title, "description": "\n".join([f"{title}.", troll, question]),
                    "tags": tags[:10], "hashtags": [*brand.hashtags, extra_tags[0]]}
        if platform == "x":
            return {"title": title, "description": f"{title}. {troll}", "tags": [],
                    "hashtags": list(brand.hashtags[:2])}
        if platform == "reddit":
            plain = f"{weapon} {clip_word} on {cs_map}, rebuilt from the real demo (CS2)"
            return {"title": plain.encode("ascii", "ignore").decode().strip(),
                    "description": summary.encode("ascii", "ignore").decode(), "tags": [],
                    "hashtags": []}
        return {"title": title, "description": f"New clip is out: {title}. {troll}",
                "tags": tags[:10], "hashtags": list(brand.hashtags)}
