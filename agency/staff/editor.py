"""Video editor: produces per-platform variants (16:9 master, 9:16 short, YouTube 1440p) and a
thumbnail, with the owner's proven encoding presets (SPEC_V2 §11.7, ``media.presets``).

Presets on (default): vertical 1080x1920 crf17 + loudnorm −14 LUFS for TikTok / Reels, a
1440x2560 cut for YouTube Shorts and a 1440p upscale for YouTube ("always"), the 16:9 master
as-is when it is already a sane MP4, and a ≤45 MB preview when Telegram approvals are on.
Presets off: the quick legacy cut (``vertical_clip``) and the original file everywhere else.
Without ffmpeg every platform gets the original file (and a warning).
"""
from __future__ import annotations

from pathlib import Path

from .. import media
from ..media import ffmpeg
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

    # ------------------------------------------------------------------ settings
    def media_options(self) -> dict:
        """``{presets, youtube_hq, loudnorm, speed, preview}`` from config ``media.*``.

        ``media.presets`` may be a bool or ``{enabled, youtube_hq, loudnorm}``; all default on.
        ``media.preset_speed`` overrides the x264 speed (tests use ``ultrafast``).
        """
        raw = self.settings.get("media.presets", True)
        if isinstance(raw, dict):
            presets = raw.get("enabled", True) is not False
            youtube_hq = raw.get("youtube_hq", True) is not False
            loudnorm = raw.get("loudnorm", True) is not False
        else:
            presets, youtube_hq, loudnorm = raw is not False, True, True
        speed = str(self.settings.get("media.preset_speed", "") or "").strip().lower()
        preview = self.settings.get("media.preview")
        if preview is None:
            preview = self.settings.get("notify.telegram.enabled", False) is True \
                and self.settings.get("notify.telegram.send_preview", True) is not False
        return {"presets": presets, "youtube_hq": youtube_hq, "loudnorm": loudnorm,
                "speed": speed if speed in ffmpeg.X264_SPEEDS else None,
                "preview": bool(preview)}

    def _vertical_platforms(self, platforms: list[str]) -> set[str]:
        out = {p for p in platforms if p in VERTICAL_PLATFORMS}
        fb_format = str(self.settings.get("publishing.platforms.facebook.format", "auto")
                        or "auto").lower()
        if "facebook" in platforms and fb_format in ("reels", "reel"):
            out.add("facebook")  # the Page posts Reels: give it the vertical master
        return out

    # ------------------------------------------------------------------ main
    def make_variants(self, video: Video, analysis: dict) -> list[Variant]:
        """One Variant per platform; encodes + thumbnail when ffmpeg is available."""
        label = video.title_hint or Path(video.path).stem
        self.status("working", f"עורך גרסאות ל\"{label}\"")
        platforms = enabled_keys(self.settings) or list(ALL_KEYS)
        info = (media.probe(video.path) if media.have_ffmpeg() else {}) \
            or analysis.get("probe") or {}
        duration = info.get("duration")
        out_dir = self.settings.data_dir / "variants" / str(video.id)
        vertical_set = self._vertical_platforms(platforms)
        opts = self.media_options()

        files: dict[str, str | None] = {}
        thumb: str | None = None
        vertical_dur: float | None = None
        if not media.have_ffmpeg():
            self.emit("ffmpeg לא מותקן — כל הפלטפורמות יקבלו את הקובץ המקורי (ללא חיתוך אנכי)",
                      "warning", video.id)
        else:
            start, end = self._window(analysis, duration)
            if self._finished_short(info):
                # Already a finished vertical short (the owner's own edit): keep all of it,
                # hook and ending included, like the studio's encode_vertical does.
                start, end = 0.0, float(duration)
            vertical_dur = round(end - start, 2)
            if opts["presets"]:
                files = self._encode(video, info, platforms, vertical_set, start, end, out_dir,
                                     opts)
            elif vertical_set:
                files["vertical"] = self._try(video, "חיתוך אנכי", lambda: str(
                    media.vertical_clip(video.path, out_dir / "vertical_9x16.mp4", start, end)))
            try:
                thumb = str(media.thumbnail(video.path, out_dir / "thumbnail.jpg",
                                            self._thumb_time(analysis, duration)))
            except FFmpegError as exc:
                self.emit(f"יצירת תמונה ממוזערת נכשלה: {exc}", "warning", video.id)

        source_aspect = "16:9"
        if info.get("width") and info.get("height") and info["height"] > info["width"]:
            source_aspect = "9:16"
        long_path = files.get("long") or str(video.path)
        variants: list[Variant] = []
        for p in platforms:
            vertical_file = files.get("vertical")
            if p == "youtube_shorts" and files.get("youtube_shorts_hq"):
                vertical_file = files["youtube_shorts_hq"]
            if p in vertical_set and vertical_file:
                variants.append(Variant(platform=p, path=vertical_file, aspect="9:16",
                                        duration_s=vertical_dur))
            elif p == "youtube" and files.get("youtube_hq"):
                variants.append(Variant(platform=p, path=str(files["youtube_hq"]),
                                        aspect=source_aspect, duration_s=duration))
            else:
                variants.append(Variant(platform=p, path=long_path, aspect=source_aspect,
                                        duration_s=duration))
        meta = {"thumbnail": thumb, "variants": [v.to_dict() for v in variants],
                "encodes": {k: v for k, v in files.items() if v and k != "preview"}}
        if files.get("preview"):
            meta["preview"] = files["preview"]
        self.db.update_video(video.id, meta=meta)
        self.status("idle", f"{len(variants)} גרסאות מוכנות"
                            + (" + תמונה ממוזערת" if thumb else ""))
        return variants

    def _try(self, video: Video, what: str, fn) -> str | None:
        """Run one encode; on failure warn (activity feed) and return None (fallback)."""
        try:
            result = fn()
            return str(result) if result else None
        except FFmpegError as exc:
            self.emit(f"{what} נכשל, משתמש בגרסה פשוטה יותר: {exc}", "warning", video.id)
            return None

    def _encode(self, video: Video, info: dict, platforms: list[str], vertical_set: set[str],
                start: float, end: float, out_dir: Path, opts: dict) -> dict[str, str | None]:
        """The studio presets. Returns ``{vertical, youtube_shorts_hq, long, youtube_hq,
        preview}`` paths (None where skipped or failed)."""
        src, speed, loud = video.path, opts["speed"], opts["loudnorm"]
        files: dict[str, str | None] = {}
        hq_shorts = opts["youtube_hq"] and "youtube_shorts" in vertical_set
        needs_vertical = bool(vertical_set - ({"youtube_shorts"} if hq_shorts else set()))
        measured = None
        if vertical_set and loud and info.get("has_audio"):
            measured = ffmpeg.loudness(src, start, end)
        if needs_vertical:
            self.status("working", "חותך לאנכי")
            files["vertical"] = self._try(video, "חיתוך אנכי", lambda: ffmpeg.encode_vertical(
                src, out_dir / "vertical_9x16.mp4", start, end, info=info, measured=measured,
                loudnorm=loud, speed=speed))
        if hq_shorts:
            self.status("working", "מכין גרסת 1440p לשורטס")
            files["youtube_shorts_hq"] = self._try(
                video, "גרסת 1440p לשורטס", lambda: ffmpeg.encode_youtube_hq(
                    src, out_dir / "youtube_shorts_1440.mp4", vertical=True, start=start,
                    end=end, info=info, measured=measured, loudnorm=loud, speed=speed))
            if not files["youtube_shorts_hq"] and not files.get("vertical"):
                files["vertical"] = self._try(video, "חיתוך אנכי", lambda: ffmpeg.encode_vertical(
                    src, out_dir / "vertical_9x16.mp4", start, end, info=info,
                    measured=measured, loudnorm=loud, speed=speed))
        if any(p not in vertical_set for p in platforms):
            files["long"] = self._try(video, "הכנת הגרסה הרגילה", lambda: ffmpeg.long_master(
                src, out_dir / "long.mp4", info=info, speed=speed))
            if opts["youtube_hq"] and "youtube" in platforms and "youtube" not in vertical_set:
                self.status("working", "מכין גרסת 1440p ליוטיוב")
                files["youtube_hq"] = self._try(
                    video, "גרסת 1440p ליוטיוב", lambda: ffmpeg.encode_youtube_hq(
                        src, out_dir / "youtube_1440.mp4", info=info, speed=speed))
        if opts["preview"]:
            base = files.get("vertical") or files.get("youtube_shorts_hq") \
                or files.get("long") or src
            files["preview"] = self._try(video, "תצוגה מקדימה", lambda: ffmpeg.encode_preview(
                base, out_dir / "preview.mp4"))
        return files

    @staticmethod
    def _finished_short(info: dict) -> bool:
        """The source is vertical and no longer than a short may be: use the whole clip
        (``best_short_window`` only cuts a short out of horizontal or long footage)."""
        width, height, duration = info.get("width"), info.get("height"), info.get("duration")
        try:
            return bool(width and height and duration) and float(height) > float(width) \
                and 0 < float(duration) <= MAX_SHORT_S
        except (TypeError, ValueError):
            return False

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
