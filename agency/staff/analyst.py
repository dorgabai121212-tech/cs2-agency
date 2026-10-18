"""Video analyst: watches the clip (ffprobe + frames + Claude vision) and describes it."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import media
from ..brand import detect_ai
from ..models import Video
from ._playbook import CLIP_TYPES, CS2_PLAYBOOK, detect_clip_type, detect_map, detect_weapons
from .base import StaffMember

ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "clip_type": {"type": "string", "enum": CLIP_TYPES},
        "map": {"type": "string"},
        "weapons": {"type": "array", "items": {"type": "string"}},
        "highlights": {"type": "array", "items": {
            "type": "object",
            "properties": {"t": {"type": "number"}, "desc": {"type": "string"}},
            "required": ["t", "desc"]}},
        "hook_seconds": {"type": "number"},
        "mood": {"type": "string"},
        "summary_he": {"type": "string"},
        "summary_en": {"type": "string"},
        "best_short_window": {"type": "object",
                              "properties": {"start": {"type": "number"},
                                             "end": {"type": "number"}},
                              "required": ["start", "end"]},
        "audience": {"type": "array", "items": {"type": "string"}},
        "score_1_10": {"type": "integer"},
        "ai_shots": {"type": "boolean"},
    },
    "required": ["clip_type", "map", "weapons", "highlights", "hook_seconds", "mood",
                 "summary_he", "summary_en", "best_short_window", "audience", "score_1_10",
                 "ai_shots"],
}

_SUMMARY_HE = {
    "ace": "אייס — חמישה הריגות ברצף", "clutch": "קלאץ' בסיבוב לחוץ",
    "funny": "רגע מצחיק מהמשחק", "fail": "פייל קלאסי שכולם יזדהו איתו",
    "tutorial": "מדריך קצר עם טיפ שימושי", "montage": "מונטאז' של הריגות",
    "highlight": "הייליט חזק מהמשחק", "other": "קליפ מהמשחק",
}


class VideoAnalyst(StaffMember):
    """Understands what is in the clip so the rest of the team can market it."""

    name = "analyst"
    title_he = "אנליסט וידאו"
    role_prompt = (
        "You are the Video Analyst of the marketing team of \"Imagination\", a Counter-Strike 2 "
        "channel of movie-grade moments rebuilt from real demos. You have watched thousands of "
        "CS2 clips and know what goes viral with the international CS2 audience.\n"
        "From the frames and metadata you receive, identify: the clip type (ace = 5 kills in a "
        "round, clutch = winning a 1vX, funny, fail, highlight, tutorial = lineups/tips, "
        "montage), the map (recognise Mirage palace/A-site, Inferno banana, Dust2 long, Nuke "
        "outside, Anubis canal, Ancient, Vertigo, Overpass, Train by their landmarks and HUD), "
        "the weapons on screen (kill-feed and HUD), and the key moments with timestamps.\n"
        "Pick best_short_window: the strongest 10-45 second stretch for a vertical short, "
        "starting ON the action so the hook lands in the first 1-2 seconds (never start on the "
        "buy menu or a walk). hook_seconds = how many seconds until something exciting happens "
        "(lower is better; the owner wants the hook within 1 second). Score 1-10 honestly for "
        "viral potential: a clean 1v4 clutch or an ACE with a deagle is 8+, a single kill is "
        "3-4. audience = who will care (e.g. 'international CS2 players', 'AWPers', 'new "
        "players learning Mirage').\n"
        "ai_shots = true when part of the clip is visibly AI-restyled (or the creator's note "
        "says AI); the copy then carries an AI label.\n"
        "Write summary_en in English (it feeds the public copy) and summary_he as one short, "
        "natural Hebrew line for the owner. Never invent details you cannot see; if unsure about "
        "the map say \"unknown\". A clip of an enemy's mistake is the enemy's fail, never a "
        "self-mocking one.\n\n" + CS2_PLAYBOOK)

    def analyze(self, video: Video) -> dict:
        """Probe + frame grab + Claude vision → analysis dict (see SPEC)."""
        label = (video.title_hint or "").strip()[:40]
        self.status("working", f"צופה ב\"{label}\"" if label else "צופה בקליפ")
        info = media.probe(video.path)
        frames: list[Path] = []
        if media.have_ffmpeg():
            frames = media.grab_frames(video.path, self.settings.data_dir / "frames"
                                       / str(video.id), n=6)
        else:
            self.emit("ffmpeg לא מותקן — מנתח לפי שם הקובץ בלבד", "warning", video.id)
        duration = float(info.get("duration") or 0.0)

        content: list[dict] = []
        for frame in frames:
            try:
                content.append(self.llm.image_block(frame))
            except OSError:
                continue
        content.append({"type": "text", "text": self._brief(video, info, len(frames))})

        raw = self.llm.complete_json(self.role_prompt, content, ANALYSIS_SCHEMA,
                                     mock_value=self._mock_analysis(video, duration))
        analysis = self._normalize(raw, duration)
        analysis["probe"] = info
        self.status("idle", "סיימתי לצפות בקליפ")
        return analysis

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _brief(video: Video, info: dict, n_frames: int) -> str:
        return (
            f"Analyse this CS2 clip.\nFile name: {Path(video.path).name}\n"
            f"Creator's note / title hint: {video.title_hint or '(none)'}\n"
            f"Probe: duration={info.get('duration')}s, size={info.get('width')}x"
            f"{info.get('height')}, fps={info.get('fps')}, audio={info.get('has_audio')}\n"
            f"{n_frames} frames attached, evenly spaced through the clip in order.\n"
            "Return the JSON analysis.")

    @staticmethod
    def _mock_analysis(video: Video, duration: float) -> dict:
        """Realistic offline analysis derived from the title hint and file name."""
        text = f"{video.title_hint} {Path(video.path).stem}"
        clip_type = detect_clip_type(text)
        cs_map = detect_map(text) or "Mirage"
        weapons = detect_weapons(text) or ["AK-47"]
        dur = duration if duration > 0 else 30.0
        start = 0.0 if dur <= 20 else min(2.0, dur * 0.05)
        end = min(dur, start + 35.0)
        mid = round((start + end) / 2, 1)
        return {
            "clip_type": clip_type,
            "map": cs_map,
            "weapons": weapons,
            "highlights": [
                {"t": round(start, 1), "desc": f"Opening kill with the {weapons[0]}"},
                {"t": mid, "desc": "Key moment of the round"},
            ],
            "hook_seconds": 1.0,
            "mood": "funny" if clip_type in ("funny", "fail") else "hype",
            "summary_he": f"{_SUMMARY_HE.get(clip_type, 'קליפ מהמשחק')} ב-{cs_map} עם "
                          f"{weapons[0]}.",
            "summary_en": f"{'An' if clip_type[0] in 'aeiou' else 'A'} {clip_type} on {cs_map} "
                          f"with the {weapons[0]}.",
            "best_short_window": {"start": round(start, 2), "end": round(end, 2)},
            "audience": ["international CS2 community", f"{cs_map} players",
                         "CS2 cinematic edit fans"],
            "score_1_10": {"ace": 8, "clutch": 8, "funny": 7, "tutorial": 6}.get(clip_type, 6),
            "ai_shots": detect_ai(video.title_hint, Path(video.path).stem),
        }

    @staticmethod
    def _normalize(raw: dict, duration: float) -> dict:
        """Clamp/validate model output so downstream code can trust it."""
        a = dict(raw or {})
        if a.get("clip_type") not in CLIP_TYPES:
            a["clip_type"] = "other"
        a["map"] = str(a.get("map") or "")
        if a["map"].lower() == "unknown":
            a["map"] = ""
        a["weapons"] = [str(w) for w in a.get("weapons") or []][:10]
        a["highlights"] = [h for h in a.get("highlights") or [] if isinstance(h, dict)][:20]
        a["audience"] = [str(x) for x in a.get("audience") or []][:10]
        try:
            a["score_1_10"] = max(1, min(10, int(a.get("score_1_10") or 5)))
        except (TypeError, ValueError):
            a["score_1_10"] = 5
        try:
            a["hook_seconds"] = max(0.0, float(a.get("hook_seconds") or 0.0))
        except (TypeError, ValueError):
            a["hook_seconds"] = 0.0
        for key in ("mood", "summary_he", "summary_en"):
            a[key] = str(a.get(key) or "")
        a["ai_shots"] = a.get("ai_shots") is True
        win = a.get("best_short_window") or {}
        try:
            start, end = float(win.get("start", 0)), float(win.get("end", 0))
        except (TypeError, ValueError):
            start, end = 0.0, 0.0
        if duration > 0:
            start = max(0.0, min(start, duration))
            end = min(end, duration) if end > start else duration
        start = max(0.0, start)
        if end <= start:
            end = start + 30.0
        end = min(end, start + 60.0)  # Shorts/Reels/TikTok-friendly
        a["best_short_window"] = {"start": round(start, 2), "end": round(end, 2)}
        return a
