"""Thin, safe wrappers around ffmpeg/ffprobe (list args, never shell=True)."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("agency.media")

FFMPEG_TIMEOUT_S = 900


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
    try:
        proc = subprocess.run([exe, "-hide_banner", *args[1:]], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, check=False)
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
    """{duration, width, height, fps, has_audio} via ffprobe; {} if ffprobe missing or fails."""
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
    duration = data.get("format", {}).get("duration") or video.get("duration")
    return {
        "duration": float(duration) if duration else None,
        "width": int(video["width"]) if video.get("width") else None,
        "height": int(video["height"]) if video.get("height") else None,
        "fps": _parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate")),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
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
