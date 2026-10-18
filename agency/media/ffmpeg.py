"""Thin, safe wrappers around ffmpeg/ffprobe (list args, never shell=True).

Besides the basic helpers (probe, vertical clip, trim, thumbnail, frames) this module holds the
owner's proven encoding presets, ported from his studio (studio/media.py, SPEC_V2 §11.7):

- ``vertical``   1080x1920, ≤60 fps, x264 High crf 17 preset slow, two-pass loudnorm to
                 −14 LUFS, AAC 192k 48 kHz (Shorts / TikTok / Reels master).
- ``youtube_hq`` YouTube-only: short side upscaled to 1440 (lanczos), crf 15, AAC 256k —
                 YouTube gives ≥1440p uploads its high-bitrate encodes ("always", the owner).
- ``long``       the 16:9 master goes out as-is when it is already a sane MP4, else crf 16.
- ``preview``    small H.264 preview for the Telegram card (≤45 MB).

Each preset has an ``*_args`` builder (pure, testable) and an ``encode_*`` runner. ``speed``
overrides the x264 preset (config ``media.preset_speed``; tests use ``ultrafast``).
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("agency.media")

FFMPEG_TIMEOUT_S = 900
ENCODE_TIMEOUT_S = 6 * 3600
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW: no console flashes when started hidden on Windows

X264_SPEEDS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow",
               "slower", "veryslow")
LOUDNORM_TARGET = "I=-14:TP=-1:LRA=11"
VERTICAL_SIZE = (1080, 1920)
VERTICAL_HQ_SIZE = (1440, 2560)
HQ_SHORT_SIDE = 1440
MAX_FPS = 60
PREVIEW_MAX_MB = 45
PREVIEW_HARD_LIMIT = 49 * 1024 * 1024  # Telegram bots may upload ≤50 MB


@dataclass(frozen=True)
class Preset:
    """One encoding recipe (video crf / x264 speed / audio bitrate / loudness)."""

    name: str
    crf: int
    speed: str
    audio_bitrate: str
    loudnorm: bool
    maxrate: str | None = None
    bufsize: str | None = None
    closed_gop: bool = False


PRESETS: dict[str, Preset] = {
    "vertical": Preset("vertical", crf=17, speed="slow", audio_bitrate="192k", loudnorm=True,
                       maxrate="20M", bufsize="40M", closed_gop=True),
    "youtube_hq": Preset("youtube_hq", crf=15, speed="slow", audio_bitrate="256k",
                         loudnorm=False),
    "long": Preset("long", crf=16, speed="slow", audio_bitrate="256k", loudnorm=False),
}


class FFmpegError(RuntimeError):
    """ffmpeg is missing or a command failed."""


def _bin(name: str) -> str | None:
    return shutil.which(name)


def have_ffmpeg() -> bool:
    """True when both ffmpeg and ffprobe are on PATH."""
    return bool(_bin("ffmpeg") and _bin("ffprobe"))


def _run(args: list[str], timeout: int = FFMPEG_TIMEOUT_S) -> subprocess.CompletedProcess:
    exe = _bin(args[0])
    if not exe:
        raise FFmpegError(f"{args[0]} not found on PATH")
    # -hide_banner keeps stderr short. Output is decoded as UTF-8 (what ffmpeg writes) with
    # errors="replace": on Windows the default ANSI code page cannot decode Hebrew file names.
    extra = {"creationflags": NO_WINDOW} if os.name == "nt" else {}
    try:
        proc = subprocess.run([exe, "-hide_banner", *args[1:]], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, check=False,
                              stdin=subprocess.DEVNULL, **extra)
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"{args[0]} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-5:]
        raise FFmpegError(f"{args[0]} failed ({proc.returncode}): " + " | ".join(tail))
    return proc


def _parse_fps(rate: str | None) -> float | None:
    if not rate or rate in ("0/0", "0"):
        return None
    try:
        if "/" in rate:
            num, den = rate.split("/", 1)
            return round(float(num) / float(den), 3) if float(den) else None
        return round(float(rate), 3)
    except ValueError:
        return None


def probe(path: str | Path) -> dict:
    """{duration, width, height, fps, has_audio, vcodec, acodec, pix_fmt, container, size}
    via ffprobe; {} if ffprobe missing or fails."""
    if not _bin("ffprobe") or not Path(path).exists():
        return {}
    try:
        proc = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
                     "-show_streams", str(path)], timeout=60)
        data = json.loads(proc.stdout or "{}")
    except (FFmpegError, ValueError) as exc:
        log.warning("probe failed for %s: %s", path, exc)
        return {}
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    fmt = data.get("format", {}) or {}
    duration = fmt.get("duration") or video.get("duration")
    size = fmt.get("size")
    return {
        "duration": float(duration) if duration else None,
        "width": int(video["width"]) if video.get("width") else None,
        "height": int(video["height"]) if video.get("height") else None,
        "fps": _parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate")),
        "has_audio": bool(audio),
        "vcodec": video.get("codec_name"),
        "acodec": audio.get("codec_name"),
        "pix_fmt": video.get("pix_fmt"),
        "container": fmt.get("format_name"),
        "size": int(size) if str(size or "").isdigit() else None,
    }


def _prepare(dst: str | Path) -> Path:
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    return dst


def vertical_clip(src: str | Path, dst: str | Path, start: float, end: float) -> Path:
    """Cut [start, end] and center-crop to 9:16, scaled to 1080x1920 (H.264 + AAC)."""
    dst = _prepare(dst)
    start = max(0.0, float(start))
    duration = max(0.1, float(end) - start)
    vf = "crop='min(iw,ih*9/16)':'min(ih,iw*16/9)',scale=1080:1920,setsar=1"
    _run(["ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{duration:.3f}",
          "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
          "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
          "-movflags", "+faststart", str(dst)])
    return dst


def trim(src: str | Path, dst: str | Path, start: float, end: float) -> Path:
    """Cut [start, end] keeping the original aspect (re-encoded for accurate cuts)."""
    dst = _prepare(dst)
    start = max(0.0, float(start))
    duration = max(0.1, float(end) - start)
    _run(["ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{duration:.3f}",
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
          "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(dst)])
    return dst


def thumbnail(src: str | Path, dst: str | Path, t: float) -> Path:
    """Single JPEG frame at time ``t`` (seconds), max 1280px wide."""
    dst = _prepare(dst)
    _run(["ffmpeg", "-y", "-ss", f"{max(0.0, float(t)):.3f}", "-i", str(src), "-frames:v", "1",
          "-vf", "scale='min(1280,iw)':-2", "-q:v", "2", str(dst)], timeout=120)
    return dst


def grab_frames(src: str | Path, out_dir: str | Path, n: int = 6) -> list[Path]:
    """Grab ``n`` evenly spaced JPEG frames (for vision analysis). [] if ffmpeg unavailable."""
    if not have_ffmpeg() or n <= 0:
        return []
    info = probe(src)
    duration = info.get("duration") or 0.0
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    frames: list[Path] = []
    for i in range(n):
        t = duration * (i + 0.5) / n if duration else float(i)
        dst = out / f"frame_{i:02d}.jpg"
        try:
            _run(["ffmpeg", "-y", "-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1",
                  "-vf", "scale='min(768,iw)':-2", "-q:v", "4", str(dst)], timeout=120)
        except FFmpegError as exc:
            log.warning("frame grab at %.2fs failed: %s", t, exc)
            continue
        if dst.exists() and dst.stat().st_size > 0:
            frames.append(dst)
    return frames


# =========================================================================== presets
def resolve_speed(speed: str | None, default: str) -> str:
    """A valid x264 ``-preset`` (``speed`` override when valid, else ``default``)."""
    value = str(speed or "").strip().lower()
    return value if value in X264_SPEEDS else default


def _even(value: float) -> int:
    return max(2, int(round(value / 2.0)) * 2)


def target_fps(info: dict) -> int:
    """Source fps rounded, capped at 60 (60 when unknown)."""
    fps = info.get("fps") or 0
    return min(MAX_FPS, int(round(fps)) or MAX_FPS)


def _window(start: float | None, end: float | None) -> tuple[list[str], list[str]]:
    """(-ss before -i, -t after -i) for an optional [start, end] cut."""
    if start is None and end is None:
        return [], []
    begin = max(0.0, float(start or 0.0))
    pre = ["-ss", f"{begin:.3f}"] if begin > 0 else []
    post = ["-t", f"{max(0.1, float(end) - begin):.3f}"] if end is not None else []
    return pre, post


def loudness(src: str | Path, start: float | None = None, end: float | None = None
             ) -> dict | None:
    """First loudnorm pass: measured input loudness (LUFS / dBTP), or None when it cannot be
    measured (no audio, digital silence, ffmpeg missing)."""
    if not have_ffmpeg():
        return None
    pre, post = _window(start, end)
    try:
        proc = _run(["ffmpeg", "-nostats", *pre, "-i", str(src), *post, "-vn",
                     "-af", f"loudnorm={LOUDNORM_TARGET}:print_format=json", "-f", "null", "-"],
                    timeout=ENCODE_TIMEOUT_S)
    except FFmpegError as exc:
        log.info("loudness analysis failed: %s", exc)
        return None
    blocks = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", proc.stderr or "", re.S)
    if not blocks:
        return None
    try:
        data = json.loads(blocks[-1])
        out = {k: float(data[k]) for k in ("input_i", "input_tp", "input_lra", "input_thresh",
                                           "target_offset")}
    except (KeyError, ValueError):
        return None
    # Digital silence reports -inf, which the second pass rejects: "not measurable".
    if not all(math.isfinite(v) for v in out.values()) or out["input_i"] < -70:
        return None
    return out


def loudnorm_filter(measured: dict | None) -> str:
    """Audio filter: second (linear) loudnorm pass with the measured values + 48 kHz;
    plain resampling when nothing was measured."""
    if not measured:
        return "aresample=48000"
    return (f"loudnorm={LOUDNORM_TARGET}:linear=true:"
            f"measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:"
            f"measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:"
            f"offset={measured['target_offset']},aresample=48000")


def vertical_filter(info: dict, size: tuple[int, int] = VERTICAL_SIZE) -> str:
    """9:16 picture: a horizontal source is centre-cropped (the CS2 crosshair is centred), a
    vertical source is scaled + padded so nothing the creator framed is lost."""
    w, h = size
    if info.get("width") and info.get("height") and info["height"] > info["width"]:
        base = (f"scale={w}:{h}:flags=lanczos:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2")
    else:
        base = f"crop='min(iw,ih*9/16)':'min(ih,iw*16/9)',scale={w}:{h}:flags=lanczos"
    return f"{base},setsar=1,fps={target_fps(info)},format=yuv420p"


def _encode_args(src: str | Path, dst: str | Path, *, vf: str, preset: Preset, speed: str,
                 info: dict, start: float | None = None, end: float | None = None,
                 measured: dict | None = None, loudnorm: bool | None = None,
                 gop: bool = True) -> list[str]:
    pre, post = _window(start, end)
    fps = target_fps(info)
    args = ["ffmpeg", "-y", *pre, "-i", str(src), *post, "-map", "0:v:0", "-vf", vf,
            "-c:v", "libx264", "-profile:v", "high", "-preset", speed, "-crf", str(preset.crf)]
    if preset.maxrate:
        args += ["-maxrate", preset.maxrate, "-bufsize", preset.bufsize or preset.maxrate]
    if gop:
        args += ["-g", str(fps * 2)]
    if preset.closed_gop:
        args += ["-flags", "+cgop"]
    args += ["-movflags", "+faststart"]
    if info.get("has_audio") is False:
        args += ["-an"]
    else:
        use_loudnorm = preset.loudnorm if loudnorm is None else loudnorm
        args += ["-map", "0:a:0?", "-af", loudnorm_filter(measured if use_loudnorm else None),
                 "-c:a", "aac", "-b:a", preset.audio_bitrate, "-ar", "48000"]
    return [*args, str(dst)]


def vertical_args(src: str | Path, dst: str | Path, *, info: dict, start: float | None = None,
                  end: float | None = None, measured: dict | None = None,
                  speed: str | None = None) -> list[str]:
    """ffmpeg args of the ``vertical`` preset (1080x1920 crf17 slow, −14 LUFS, AAC 192k)."""
    preset = PRESETS["vertical"]
    return _encode_args(src, dst, vf=vertical_filter(info, VERTICAL_SIZE), preset=preset,
                        speed=resolve_speed(speed, preset.speed), info=info, start=start,
                        end=end, measured=measured)


def youtube_hq_size(info: dict, vertical: bool = False) -> tuple[int, int] | None:
    """Output size with the short side at 1440, or None when the source already is ≥1440
    (then the master itself is the YouTube file). Vertical cuts are always 1440x2560."""
    if vertical:
        return VERTICAL_HQ_SIZE
    w, h = info.get("width"), info.get("height")
    if not w or not h:
        return None
    short = min(w, h)
    if short >= HQ_SHORT_SIDE:
        return None
    k = HQ_SHORT_SIDE / short
    return _even(w * k), _even(h * k)


def youtube_hq_args(src: str | Path, dst: str | Path, *, info: dict, vertical: bool = False,
                    start: float | None = None, end: float | None = None,
                    measured: dict | None = None, loudnorm: bool = False,
                    speed: str | None = None) -> list[str] | None:
    """ffmpeg args of the ``youtube_hq`` preset (short side 1440 lanczos, crf15, AAC 256k);
    None when no upscale is needed. Long sources (>3 min) use x264 "medium" (slow takes hours)."""
    size = youtube_hq_size(info, vertical)
    if size is None:
        return None
    preset = PRESETS["youtube_hq"]
    if vertical:
        vf = vertical_filter(info, size)
    else:
        vf = f"scale={size[0]}:{size[1]}:flags=lanczos,setsar=1,fps={target_fps(info)}," \
             "format=yuv420p"
    duration = (float(end) - float(start or 0)) if end is not None else info.get("duration")
    default = preset.speed if (duration or 0) <= 180 else "medium"
    return _encode_args(src, dst, vf=vf, preset=preset, speed=resolve_speed(speed, default),
                        info=info, start=start, end=end, measured=measured, loudnorm=loudnorm)


def is_sane_master(info: dict) -> bool:
    """True when the source can go out untouched: MP4/MOV with H.264/HEVC video and AAC (or no)
    audio, in yuv420p (or unknown pixel format)."""
    container = str(info.get("container") or "")
    return ("mp4" in container or "mov" in container) \
        and info.get("vcodec") in ("h264", "hevc") \
        and info.get("acodec") in ("aac", None) \
        and info.get("pix_fmt") in ("yuv420p", "yuvj420p", None)


def long_args(src: str | Path, dst: str | Path, *, info: dict, speed: str | None = None
              ) -> list[str]:
    """ffmpeg args of the ``long`` preset (re-encode crf16 slow, AAC 256k, size unchanged)."""
    preset = PRESETS["long"]
    return _encode_args(src, dst, vf="format=yuv420p", preset=preset,
                        speed=resolve_speed(speed, preset.speed), info=info, gop=False)


def preview_args(src: str | Path, dst: str | Path, *, info: dict, max_mb: float = PREVIEW_MAX_MB,
                 attempt: int = 0) -> list[str]:
    """ffmpeg args for the Telegram preview: bitrate from the size budget, 720/480/360p."""
    duration = max(float(info.get("duration") or 1.0), 1.0)
    audio_kbps = 0 if info.get("has_audio") is False else 64
    budget = max_mb * 0.9 ** attempt
    total_kbps = int(budget * 8192 / duration)
    video_kbps = min(2500, total_kbps - audio_kbps - 16)
    short_side = 720 if video_kbps >= 900 else (480 if video_kbps >= 350 else 360)
    video_kbps = max(80, video_kbps)
    vertical = bool(info.get("width") and info.get("height") and info["height"] > info["width"])
    scale = (f"scale={short_side}:-2:flags=lanczos" if vertical
             else f"scale=-2:{short_side}:flags=lanczos")
    args = ["ffmpeg", "-y", "-i", str(src), "-map", "0:v:0",
            "-vf", f"{scale},fps={30 if video_kbps >= 350 else 24},format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{video_kbps}k",
            "-maxrate", f"{video_kbps}k", "-bufsize", f"{video_kbps * 2}k",
            "-movflags", "+faststart"]
    if audio_kbps:
        args += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", f"{audio_kbps}k", "-ac", "2"]
    else:
        args += ["-an"]
    return [*args, str(dst)]


def _info(src: str | Path, info: dict | None) -> dict:
    info = info or probe(src)
    if not info:
        raise FFmpegError("ffprobe could not read the video")
    return info


def encode_vertical(src: str | Path, dst: str | Path, start: float | None = None,
                    end: float | None = None, *, info: dict | None = None,
                    measured: dict | None = None, loudnorm: bool = True,
                    speed: str | None = None) -> Path:
    """The ``vertical`` preset; measures loudness first (two-pass) unless ``measured`` given."""
    dst = _prepare(dst)
    info = _info(src, info)
    if loudnorm and measured is None and info.get("has_audio"):
        measured = loudness(src, start, end)
    _run(vertical_args(src, dst, info=info, start=start, end=end,
                       measured=measured if loudnorm else None, speed=speed),
         timeout=ENCODE_TIMEOUT_S)
    return dst


def encode_youtube_hq(src: str | Path, dst: str | Path, *, vertical: bool = False,
                      start: float | None = None, end: float | None = None,
                      info: dict | None = None, measured: dict | None = None,
                      loudnorm: bool = False, speed: str | None = None) -> Path | None:
    """The ``youtube_hq`` preset; None when the source already has a ≥1440 short side."""
    info = _info(src, info)
    if loudnorm and measured is None and info.get("has_audio"):
        measured = loudness(src, start, end)
    args = youtube_hq_args(src, dst, info=info, vertical=vertical, start=start, end=end,
                           measured=measured, loudnorm=loudnorm, speed=speed)
    if args is None:
        return None
    dst = _prepare(dst)
    _run(args, timeout=ENCODE_TIMEOUT_S)
    return dst


def long_master(src: str | Path, dst: str | Path, *, info: dict | None = None,
                speed: str | None = None) -> Path:
    """The 16:9 master: ``src`` itself when it is a sane MP4 (as-is), else re-encoded crf16."""
    info = _info(src, info)
    if is_sane_master(info):
        return Path(src)
    dst = _prepare(dst)
    _run(long_args(src, dst, info=info, speed=speed), timeout=ENCODE_TIMEOUT_S)
    return dst


def encode_preview(src: str | Path, dst: str | Path, *, info: dict | None = None,
                   max_mb: float = PREVIEW_MAX_MB) -> Path:
    """Small preview that always fits Telegram's bot upload limit (re-checked after encoding)."""
    dst = _prepare(dst)
    info = _info(src, info)
    for attempt in range(3):
        _run(preview_args(src, dst, info=info, max_mb=max_mb, attempt=attempt),
             timeout=ENCODE_TIMEOUT_S)
        if dst.stat().st_size <= PREVIEW_HARD_LIMIT:
            return dst
    raise FFmpegError("preview does not fit Telegram's 50 MB limit")
