"""Copywriter: titles, descriptions, tags and hashtags per platform — limits enforced in code."""
from __future__ import annotations

import re
from typing import Any

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

# Platforms whose audience is international (copy in English).
ENGLISH_PLATFORMS = {"reddit"}

_TITLE_HE = {
    "ace": "ACE מטורף ב-{map} עם {weapon}",
    "clutch": "קלאץ' שאף אחד לא האמין בו ב-{map}",
    "funny": "הרגע הכי מצחיק שהיה לי ב-{map}",
    "fail": "הפייל הכי כואב ב-{map}",
    "tutorial": "הטיפ שישדרג לכם את ה-{map}",
    "montage": "מונטאז' {weapon} ב-{map}",
    "highlight": "הייליט חולני ב-{map} עם {weapon}",
    "other": "רגע מטורף ב-{map}",
}
_TITLE_EN = {
    "ace": "Insane {weapon} ACE on {map}",
    "clutch": "Nobody believed in this clutch on {map}",
    "funny": "The funniest moment I've had on {map}",
    "fail": "The most painful fail on {map}",
    "tutorial": "This {map} tip will instantly improve your game",
    "montage": "{weapon} montage on {map}",
    "highlight": "Crazy {weapon} highlight on {map}",
    "other": "Crazy moment on {map}",
}


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


class Copywriter(StaffMember):
    """Writes platform-native copy in Hebrew (and English where the audience is international)."""

    name = "copywriter"
    title_he = "קופירייטר"
    role_prompt = (
        "You are the Copywriter of a marketing agency for an Israeli Counter-Strike 2 creator. "
        "You write platform-native copy that earns clicks honestly (the clip must deliver what "
        "the title promises - no fake clickbait, no fake giveaways, no 'sub for sub').\n"
        "Language: write in the channel's primary language (Hebrew by default) using the CS2 "
        "English slang Israeli gamers actually use (ACE, clutch, one-tap, eco, AWP, rush B). "
        "Reddit copy is always English. Add a short English line in descriptions for the "
        "international audience when the channel has a secondary language.\n"
        "Per platform:\n"
        "- youtube: searchable title (map + clip type + weapon), <=70 chars ideal (hard 100). "
        "Description: first 2 lines are the hook, then context, then a CTA to subscribe, 3 "
        "hashtags max (#CS2 first). Tags: CS2, Counter-Strike 2, map, weapon, clip type, "
        "Hebrew variants (קאונטר סטרייק).\n"
        "- youtube_shorts: very short punchy title <=50 chars, include #Shorts.\n"
        "- tiktok: caption that hooks + asks a question to drive comments, 3-5 hashtags "
        "(#cs2 #counterstrike2 plus niche tags like #cs2clips; no #fyp stuffing).\n"
        "- instagram_reels: caption with a hook line, a question, 5-10 hashtags (max 30).\n"
        "- x: one punchy line, 1-2 hashtags, whole post <=280 chars.\n"
        "- facebook: friendly community tone, short.\n"
        "- reddit: descriptive, humble English title, no hashtags, no emojis spam.\n"
        "- discord: short announcement for the creator's own server.\n"
        "Return one entry per requested platform.\n\n" + CS2_PLAYBOOK)

    def write(self, video: Video, analysis: dict, platforms: list[str]) -> dict[str, dict]:
        """Copy per platform: {platform: {title, description, tags, hashtags}} (limits enforced)."""
        self.status("working", f"כותב טקסטים ל-{len(platforms)} פלטפורמות")
        mock = {"posts": [{"platform": p, **self._mock_copy(video, analysis, p)}
                          for p in platforms]}
        user = (
            f"Channel: {self.settings.get('channel.name') or '(unnamed)'} "
            f"handle={self.settings.get('channel.handle') or '-'}; "
            f"primary language={self.language}; secondary="
            f"{self.settings.get('channel.secondary_language', 'en')}.\n"
            f"Creator's title hint: {video.title_hint or '(none)'}\n"
            f"Analysis: clip_type={analysis.get('clip_type')}, map={analysis.get('map')}, "
            f"weapons={analysis.get('weapons')}, mood={analysis.get('mood')}, "
            f"score={analysis.get('score_1_10')}/10\n"
            f"Summary (he): {analysis.get('summary_he')}\nSummary (en): "
            f"{analysis.get('summary_en')}\nHighlights: {analysis.get('highlights')}\n"
            f"Write copy for these platforms: {', '.join(platforms)}.")
        data = self.llm.complete_json(self.role_prompt, user, COPY_SCHEMA, mock_value=mock)

        by_platform = {str(p.get("platform")): p for p in data.get("posts") or []
                       if isinstance(p, dict)}
        out: dict[str, dict] = {}
        for p in platforms:
            raw = by_platform.get(p) or self._mock_copy(video, analysis, p)
            out[p] = enforce_limits(p, raw)
        self.status("idle", "הטקסטים מוכנים")
        return out

    # ------------------------------------------------------------------ mock copy
    def _mock_copy(self, video: Video, analysis: dict, platform: str) -> dict:
        clip = analysis.get("clip_type") or "highlight"
        cs_map = analysis.get("map") or "CS2"
        weapon = (analysis.get("weapons") or ["AK-47"])[0]
        english = platform in ENGLISH_PLATFORMS or self.language == "en"
        fmt = {"map": cs_map, "weapon": weapon}
        title_en = _TITLE_EN.get(clip, _TITLE_EN["other"]).format(**fmt)
        title = title_en if english else _TITLE_HE.get(clip, _TITLE_HE["other"]).format(**fmt)
        if video.title_hint and not english:
            title = video.title_hint
        summary = analysis.get("summary_en" if english else "summary_he") or title
        question = ("Would you have taken this fight?" if english
                    else "הייתם לוקחים את הקרב הזה? תכתבו בתגובות")
        map_tag = cs_map.replace(" ", "")
        hashtags = ["#CS2", "#CounterStrike2", f"#{map_tag}", f"#{clip}", "#cs2clips"]
        tags = ["CS2", "Counter-Strike 2", "counter strike 2", cs_map, weapon, clip,
                f"CS2 {clip}", f"{cs_map} {clip}", "קאונטר סטרייק", "קאונטר סטרייק 2",
                "CS2 ישראל", "cs2 highlights"]
        handle = self.settings.get("channel.handle") or ""
        cta = "" if english else "\nתעשו לייק ותירשמו לערוץ לעוד קליפים"
        secondary = "" if english else f"\n\nEN: {analysis.get('summary_en') or title_en}"
        if platform == "youtube":
            desc = f"{summary}\n{question}{cta}{secondary}"
            return {"title": f"{title} | CS2", "description": desc, "tags": tags,
                    "hashtags": hashtags[:3]}
        if platform == "youtube_shorts":
            return {"title": title, "description": f"{summary}\n{question}", "tags": tags,
                    "hashtags": ["#Shorts", "#CS2", f"#{map_tag}"]}
        if platform in ("tiktok", "instagram_reels"):
            return {"title": title, "description": f"{title} — {question}", "tags": tags[:8],
                    "hashtags": hashtags + (["#gaming", "#fps"] if platform != "tiktok"
                                            else [])}
        if platform == "x":
            text = f"{title}. {question}" + (f" {handle}" if handle else "")
            return {"title": title, "description": text, "tags": [], "hashtags": ["#CS2"]}
        if platform == "reddit":
            return {"title": f"{title_en} (CS2)", "description": analysis.get("summary_en")
                    or title_en, "tags": [], "hashtags": []}
        return {"title": title, "description": f"{summary}\n{question}", "tags": tags[:10],
                "hashtags": hashtags[:3]}
