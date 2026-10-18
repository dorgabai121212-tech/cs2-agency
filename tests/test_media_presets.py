"""The owner's encoding presets (SPEC_V2 §11.7): exact ffmpeg args, and real encodes of a tiny
clip when ffmpeg is installed (tests use the fast x264 speed via media.preset_speed)."""
from __future__ import annotations

import pytest

from agency.media import ffmpeg

H720 = {"width": 1280, "height": 720, "fps": 30.0, "duration": 3.0, "has_audio": True,
        "vcodec": "h264", "acodec": "aac", "pix_fmt": "yuv420p",
        "container": "mov,mp4,m4a,3gp,3g2,mj2"}
MEASURED = {"input_i": -21.8, "input_tp": -17.7, "input_lra": 0.1, "input_thresh": -31.8,
            "target_offset": -0.0}
needs_ffmpeg = pytest.mark.skipif(not ffmpeg.have_ffmpeg(), reason="ffmpeg not installed")


def _value(args: list[str], flag: str, nth: int = 0) -> str:
    idx = [i for i, a in enumerate(args) if a == flag][nth]
    return args[idx + 1]


def test_vertical_preset_args():
    args = ffmpeg.vertical_args("in.mp4", "out.mp4", info=H720, start=1.0, end=21.0,
                                measured=MEASURED)
    assert args[:4] == ["ffmpeg", "-y", "-ss", "1.000"] and _value(args, "-t") == "20.000"
    vf = _value(args, "-vf")
    assert "crop='min(iw,ih*9/16)':'min(ih,iw*16/9)'" in vf  # horizontal: centre crop
    assert "scale=1080:1920:flags=lanczos" in vf and "fps=30" in vf and "yuv420p" in vf
    assert _value(args, "-c:v") == "libx264" and _value(args, "-profile:v") == "high"
    assert _value(args, "-crf") == "17" and _value(args, "-preset") == "slow"
    assert _value(args, "-maxrate") == "20M" and _value(args, "-g") == "60"
    af = _value(args, "-af")
    assert af.startswith("loudnorm=I=-14:TP=-1:LRA=11:linear=true:measured_I=-21.8")
    assert af.endswith("aresample=48000")
    assert _value(args, "-c:a") == "aac" and _value(args, "-b:a") == "192k"
    assert _value(args, "-ar") == "48000" and args[-1] == "out.mp4"
    # ≤60 fps, vertical sources are padded (nothing cropped away), speed override for tests
    vert = ffmpeg.vertical_args("in.mp4", "o.mp4", info={**H720, "width": 1080, "height": 1920,
                                                         "fps": 120.0}, speed="ultrafast")
    assert "pad=1080:1920" in _value(vert, "-vf") and "fps=60" in _value(vert, "-vf")
    assert _value(vert, "-preset") == "ultrafast"
    assert _value(vert, "-af") == "aresample=48000"  # nothing measured -> no loudnorm
    silent = ffmpeg.vertical_args("in.mp4", "o.mp4", info={**H720, "has_audio": False})
    assert "-an" in silent and "-af" not in silent
    assert ffmpeg.resolve_speed("warp9", "slow") == "slow"


def test_youtube_hq_preset_args():
    args = ffmpeg.youtube_hq_args("in.mp4", "yt.mp4", info=H720)
    vf = _value(args, "-vf")
    assert vf.startswith("scale=2560:1440:flags=lanczos")
    assert _value(args, "-crf") == "15" and _value(args, "-b:a") == "256k"
    assert _value(args, "-preset") == "slow" and _value(args, "-af") == "aresample=48000"
    assert ffmpeg.youtube_hq_size({**H720, "width": 1920, "height": 1080}) == (2560, 1440)
    assert ffmpeg.youtube_hq_args("in.mp4", "yt.mp4",
                                  info={**H720, "width": 2560, "height": 1440}) is None
    shorts = ffmpeg.youtube_hq_args("in.mp4", "ys.mp4", info=H720, vertical=True, start=0,
                                    end=20, measured=MEASURED, loudnorm=True)
    assert "scale=1440:2560:flags=lanczos" in _value(shorts, "-vf")
    assert _value(shorts, "-af").startswith("loudnorm=I=-14")
    long_form = ffmpeg.youtube_hq_args("in.mp4", "y.mp4", info={**H720, "duration": 900.0})
    assert _value(long_form, "-preset") == "medium"  # "slow" would take hours


def test_long_master_as_is_else_crf16(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(ffmpeg, "_run", lambda args, timeout=0: ran.append(args))
    src = tmp_path / "master.mp4"
    src.write_bytes(b"x")
    assert ffmpeg.long_master(src, tmp_path / "long.mp4", info=H720) == src and ran == []
    webm = {**H720, "container": "matroska,webm", "vcodec": "vp9", "acodec": "opus"}
    out = ffmpeg.long_master(src, tmp_path / "long.mp4", info=webm)
    assert out == tmp_path / "long.mp4"
    assert _value(ran[0], "-crf") == "16" and _value(ran[0], "-b:a") == "256k"
    assert _value(ran[0], "-preset") == "slow"


def test_preview_budget():
    args = ffmpeg.preview_args("in.mp4", "p.mp4", info={**H720, "duration": 30.0})
    assert "scale=-2:720" in _value(args, "-vf") and _value(args, "-b:v") == "2500k"
    long_one = ffmpeg.preview_args("in.mp4", "p.mp4", info={**H720, "duration": 1800.0})
    kbps = int(_value(long_one, "-b:v")[:-1])
    assert kbps * 1800 / 8192 < ffmpeg.PREVIEW_MAX_MB and "scale=-2:360" in _value(long_one,
                                                                                   "-vf")


@needs_ffmpeg
def test_presets_on_a_tiny_clip(tiny_video, tmp_path):
    info = ffmpeg.probe(tiny_video)
    assert info["vcodec"] == "h264" and info["acodec"] == "aac" and ffmpeg.is_sane_master(info)
    vert = ffmpeg.encode_vertical(tiny_video, tmp_path / "v.mp4", 0.5, 2.5, info=info,
                                  speed="ultrafast")
    vinfo = ffmpeg.probe(vert)
    assert (vinfo["width"], vinfo["height"]) == (1080, 1920) and vinfo["acodec"] == "aac"
    assert 1.5 <= vinfo["duration"] <= 2.6
    measured = ffmpeg.loudness(vert)
    assert measured is not None and abs(measured["input_i"] + 14) <= 1.5  # −14 LUFS
    hq = ffmpeg.encode_youtube_hq(tiny_video, tmp_path / "y.mp4", info=info, speed="ultrafast")
    assert (ffmpeg.probe(hq)["width"], ffmpeg.probe(hq)["height"]) == (2560, 1440)
    preview = ffmpeg.encode_preview(tiny_video, tmp_path / "p.mp4", info=info)
    assert preview.stat().st_size <= ffmpeg.PREVIEW_MAX_MB * 1024 * 1024


@needs_ffmpeg
def test_editor_uses_presets(ctx, tiny_video):
    from agency.staff.editor import VideoEditor

    video_id = ctx.db.add_video(str(tiny_video), "אייס")
    video = ctx.db.get_video(video_id)
    analysis = {"best_short_window": {"start": 0.5, "end": 2.5}, "highlights": [{"t": 1.0}]}
    editor = VideoEditor(ctx)
    assert editor.media_options()["speed"] == "ultrafast"  # tests' config
    by = {v.platform: v for v in editor.make_variants(video, analysis)}
    assert by["tiktok"].path.endswith("vertical_9x16.mp4") and by["tiktok"].aspect == "9:16"
    assert by["instagram_reels"].path == by["tiktok"].path
    assert by["youtube_shorts"].path.endswith("youtube_shorts_1440.mp4")
    assert by["youtube"].path.endswith("youtube_1440.mp4")
    assert by["facebook"].path == str(tiny_video)  # the master goes out as-is
    meta = ctx.db.get_video(video_id).meta
    assert set(meta["encodes"]) >= {"vertical", "youtube_shorts_hq", "youtube_hq", "long"}
    assert "preview" not in meta  # Telegram is off -> no preview encode

    ctx.settings.raw["media"] = {"presets": {"enabled": False}}  # legacy quick cut
    by = {v.platform: v for v in editor.make_variants(video, analysis)}
    assert by["youtube"].path == str(tiny_video) and by["youtube_shorts"].path == by[
        "tiktok"].path


def test_editor_options_parsing(ctx):
    from agency.staff.editor import VideoEditor

    ctx.settings.raw["media"] = {"presets": False, "preset_speed": "bogus"}
    opts = VideoEditor(ctx).media_options()
    assert opts["presets"] is False and opts["speed"] is None and opts["preview"] is False
    ctx.settings.raw["media"] = {"presets": {"youtube_hq": False, "loudnorm": False}}
    ctx.settings.raw["notify"] = {"telegram": {"enabled": True}}
    opts = VideoEditor(ctx).media_options()
    assert opts == {"presets": True, "youtube_hq": False, "loudnorm": False, "speed": None,
                    "preview": True}
