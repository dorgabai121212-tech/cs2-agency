"""Video editor: produces per-platform variants (16:9 original, 9:16 short) and a thumbnail."""
from __future__ import annotations

from .. import media
from ..media.ffmpeg import FFmpegError
from ..models import Variant, Video
from ..platforms.registry import ALL_KEYS, enabled_keys
from ._playbook import CS2_PLAYBOOK, VERTICAL_PLATFORMS
from .base import StaffMember

MAX_SHORT_S = 60.0


class VideoEditor(StaffMember):
    """Cuts the clip into the formats each platform wants."""

    name = "editor"
    title_he = "עורך וידאו"
    role_prompt = (
        "You are the Video Editor of a CS2 content agency. You cut vertical 9:16 shorts for "
        "YouTube Shorts, TikTok and Instagram Reels (max 60s, ideally 15-35s, start on the "
        "action so the hook lands in the first 1-2 seconds, end on a loopable moment), keep the "
        "16:9 original for YouTube, Facebook and X, and pick a thumbnail frame at the peak "
        "moment (kill-feed full, crosshair on target, expressive). Centre-crop keeps the "
        "crosshair, which in CS2 is always centred.\n\n" + CS2_PLAYBOOK)

    def make_variants(self, video: Video, analysis: dict) -> list[Variant]:
        """One Variant per platform; vertical clip + thumbnail when ffmpeg is available."""
        self.status("working", f"עורך גרסאות לסרטון #{video.id}")
        platforms = enabled_keys(self.settings) or list(ALL_KEYS)
        info = analysis.get("probe") or media.probe(video.path)
        duration = info.get("duration")
        out_dir = self.settings.data_dir / "variants" / str(video.id)

        vertical_path: str | None = None
        vertical_dur: float | None = None
        thumb: str | None = None
        if not media.have_ffmpeg():
            self.emit("ffmpeg לא מותקן — כל הפלטפורמות יקבלו את הקובץ המקורי (ללא חיתוך אנכי)",
                      "warning", video.id)
        else:
            start, end = self._window(analysis, duration)
            if any(p in VERTICAL_PLATFORMS for p in platforms):
                try:
                    vertical_path = str(media.vertical_clip(
                        video.path, out_dir / "vertical_9x16.mp4", start, end))
                    vertical_dur = round(end - start, 2)
                except FFmpegError as exc:
                    self.emit(f"חיתוך אנכי נכשל, משתמש במקור: {exc}", "warning", video.id)
            try:
                thumb = str(media.thumbnail(video.path, out_dir / "thumbnail.jpg",
                                            self._thumb_time(analysis, duration)))
            except FFmpegError as exc:
                self.emit(f"יצירת תמונה ממוזערת נכשלה: {exc}", "warning", video.id)

        variants: list[Variant] = []
        for p in platforms:
            if p in VERTICAL_PLATFORMS and vertical_path:
                variants.append(Variant(platform=p, path=vertical_path, aspect="9:16",
                                        duration_s=vertical_dur))
            else:
                aspect = "16:9"
                if info.get("width") and info.get("height") and info["height"] > info["width"]:
                    aspect = "9:16"
                variants.append(Variant(platform=p, path=str(video.path), aspect=aspect,
                                        duration_s=duration))
        self.db.update_video(video.id, meta={"thumbnail": thumb,
                                             "variants": [v.to_dict() for v in variants]})
        self.status("idle", f"{len(variants)} גרסאות מוכנות"
                            + (" + תמונה ממוזערת" if thumb else ""))
        return variants

    @staticmethod
    def _window(analysis: dict, duration: float | None) -> tuple[float, float]:
        win = analysis.get("best_short_window") or {}
        start = float(win.get("start") or 0.0)
        end = float(win.get("end") or (start + 30.0))
        if duration:
            start = min(max(0.0, start), max(0.0, duration - 0.5))
            end = min(end, duration)
        if end <= start:
            end = start + min(30.0, duration or 30.0)
        return start, min(end, start + MAX_SHORT_S)

    @staticmethod
    def _thumb_time(analysis: dict, duration: float | None) -> float:
        highlights = analysis.get("highlights") or []
        t = highlights[-1].get("t") if highlights else None
        if t is None:
            t = (duration or 2.0) / 2
        t = float(t)
        if duration:
            t = min(max(0.0, t), max(0.0, duration - 0.1))
        return t

    @staticmethod
    def variant_for(variants: list[Variant], platform: str) -> Variant | None:
        """Find the variant for a platform."""
        return next((v for v in variants if v.platform == platform), None)

