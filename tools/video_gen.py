"""
Video generation tools for BeeQ Reels.

Models used:
  prunaai/p-video       — image-to-video (cinematic product reel)
  veed/fabric-1.0       — image + audio → talking-head lip-sync video
  chenxwh/openvoice     — voice cloning (reference audio + text → cloned speech)
  openai/gpt-image-2    — image generation / img2img (via image_gen.py)
"""

from __future__ import annotations

import logging
import os
import time

import threading
from contextlib import contextmanager

import requests
import replicate as _replicate

import config
from tools.replicate_queue import gated as _gated

# ── Concurrency cap for LOCAL video compositing (moviepy/ffmpeg) ─────────────
# Each local composite uses ~1 GB RAM + a full core for minutes. Cap how many
# run simultaneously so a burst of reel requests can't OOM/throttle the box.
_VIDEO_SEMA = threading.BoundedSemaphore(max(1, getattr(config, "VIDEO_BUILD_CONCURRENCY", 2)))


@contextmanager
def _video_slot(label: str = "video"):
    """Block until a local-compositing slot is free, then hold it for the build."""
    waited = _VIDEO_SEMA.acquire(blocking=False)
    if not waited:
        logger.info("video_gen: %s waiting for a free compositing slot (cap=%s)...",
                    label, config.VIDEO_BUILD_CONCURRENCY)
        _VIDEO_SEMA.acquire()   # block until one frees up
    logger.info("video_gen: %s acquired compositing slot", label)
    try:
        yield
    finally:
        _VIDEO_SEMA.release()
        logger.info("video_gen: %s released compositing slot", label)

# Pillow 10+ removed Image.ANTIALIAS (and other constants) that moviepy still calls.
# Shim them to the modern Resampling enums so moviepy resize works.
try:
    from PIL import Image as _PILImage
    for _old, _new in (("ANTIALIAS", "LANCZOS"), ("BICUBIC", "BICUBIC"),
                       ("BILINEAR", "BILINEAR"), ("NEAREST", "NEAREST")):
        if not hasattr(_PILImage, _old):
            setattr(_PILImage, _old, getattr(_PILImage.Resampling, _new))
except Exception:
    pass

logger = logging.getLogger(__name__)

os.environ.setdefault("REPLICATE_API_TOKEN", config.REPLICATE_API_TOKEN)

_P_VIDEO_MODEL  = "prunaai/p-video"
_FABRIC_MODEL   = "veed/fabric-1.0"
_OPENVOICE_MODEL = "chenxwh/openvoice"


# ---------------------------------------------------------------------------
# Image-to-Video  (prunaai/p-video)
# ---------------------------------------------------------------------------

def generate_video_from_image(
    image_url: str,
    prompt: str,
    duration: int = 5,
    aspect_ratio: str = "9:16",
    resolution: str = "720p",
    fps: int = 24,
) -> dict:
    """
    Generate a short video from a still image using prunaai/p-video.
    Returns {"ok": True, "url": "...", "bytes": b"..."} or {"ok": False, "error": "..."}.
    """
    if not config.REPLICATE_API_TOKEN:
        return {"ok": False, "error": "REPLICATE_API_TOKEN not set"}
    try:
        prediction = _replicate.predictions.create(
            model=_P_VIDEO_MODEL,
            input={
                "image":                  image_url,
                "prompt":                 prompt,
                "duration":               duration,
                "aspect_ratio":           aspect_ratio,
                "resolution":             resolution,
                "fps":                    fps,
                "save_audio":             False,
                "draft":                  False,
                "no_op":                  False,
                "prompt_upsampling":      False,
                "disable_safety_filter":  True,
            },
        )
    except Exception as exc:
        logger.error("video_gen.p-video create failed: %s", exc)
        return {"ok": False, "error": f"Could not start p-video: {exc}"}

    _MAX_VIDEO_WAIT = 600  # 10 minutes
    _POLL = 10
    elapsed = 0
    while elapsed < _MAX_VIDEO_WAIT:
        try:
            prediction.reload()
        except Exception as exc:
            logger.warning("video_gen.p-video poll error: %s", exc)
            time.sleep(_POLL)
            elapsed += _POLL
            continue

        status = prediction.status
        if status == "succeeded":
            url = _resolve_url(prediction.output)
            if not url:
                return {"ok": False, "error": f"p-video returned unexpected output: {prediction.output!r}"}
            video_bytes = _fetch(url)
            return {"ok": True, "url": url, "bytes": video_bytes}

        if status in ("failed", "canceled"):
            err = getattr(prediction, "error", None) or status
            logger.error("video_gen.p-video %s: %s", status, err)
            return {"ok": False, "error": f"p-video {status}: {err}"}

        time.sleep(_POLL)
        elapsed += _POLL

    try:
        prediction.cancel()
    except Exception:
        pass
    return {"ok": False, "error": f"p-video timed out after {_MAX_VIDEO_WAIT}s"}


# ---------------------------------------------------------------------------
# Talking-head lip-sync  (veed/fabric-1.0)
# ---------------------------------------------------------------------------

@_gated("lipsync")
def generate_lipsync_video(
    image_url: str,
    audio_url: str,
    resolution: str = "720p",
) -> dict:
    """
    Generate a lip-sync talking-head video from a portrait image + audio.
    Uses veed/fabric-1.0.
    Polls manually so long audio (60+ sec) never hits a read timeout.
    Returns {"ok": True, "url": "...", "bytes": b"..."} or {"ok": False, "error": "..."}.
    """
    if not config.REPLICATE_API_TOKEN:
        return {"ok": False, "error": "REPLICATE_API_TOKEN not set"}
    try:
        prediction = _replicate.predictions.create(
            model=_FABRIC_MODEL,
            input={
                "image":      image_url,
                "audio":      audio_url,
                "resolution": resolution,
            },
        )
    except Exception as exc:
        logger.error("video_gen.fabric create failed: %s", exc)
        return {"ok": False, "error": f"Could not start lipsync: {exc}"}

    # Poll until done — 20-minute hard cap (long audio can take 10+ min)
    _MAX_LIPSYNC_WAIT = 1200  # 20 minutes
    _POLL = 10
    elapsed = 0
    while elapsed < _MAX_LIPSYNC_WAIT:
        try:
            prediction.reload()
        except Exception as exc:
            logger.warning("video_gen.fabric poll error: %s", exc)
            time.sleep(_POLL)
            elapsed += _POLL
            continue

        status = prediction.status
        if status == "succeeded":
            url = _resolve_url(prediction.output)
            if not url:
                return {"ok": False, "error": f"fabric returned unexpected output: {prediction.output!r}"}
            video_bytes = _fetch(url)
            return {"ok": True, "url": url, "bytes": video_bytes}

        if status in ("failed", "canceled"):
            err = getattr(prediction, "error", None) or status
            logger.error("video_gen.fabric %s: %s", status, err)
            return {"ok": False, "error": f"Lipsync {status}: {err}"}

        time.sleep(_POLL)
        elapsed += _POLL

    try:
        prediction.cancel()
    except Exception:
        pass
    return {"ok": False, "error": f"Lipsync timed out after {_MAX_LIPSYNC_WAIT}s"}


# ---------------------------------------------------------------------------
# Voice cloning  (chenxwh/openvoice)
# ---------------------------------------------------------------------------

def clone_voice(
    reference_audio_url: str,
    text: str,
    language: str = "American English",
) -> dict:
    """
    Clone a voice from a reference audio URL and synthesize `text`.
    Uses chenxwh/openvoice.
    Returns {"ok": True, "url": "...", "bytes": b"..."} or {"ok": False, "error": "..."}.
    """
    if not config.REPLICATE_API_TOKEN:
        return {"ok": False, "error": "REPLICATE_API_TOKEN not set"}
    try:
        output = _replicate.run(
            _OPENVOICE_MODEL,
            input={
                "reference_audio": reference_audio_url,
                "text":            text[:200],
                "language":        language,
            },
        )
        url = _resolve_url(output)
        if not url:
            return {"ok": False, "error": f"openvoice returned unexpected output: {output!r}"}
        audio_bytes = _fetch(url)
        return {"ok": True, "url": url, "bytes": audio_bytes}
    except Exception as exc:
        logger.error("video_gen.openvoice failed: %s", exc)
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Video merge  (ffmpeg via moviepy)
# ---------------------------------------------------------------------------

def merge_videos(
    video_bytes_list: list[bytes],
    music_bytes: bytes | None = None,
    music_url: str | None = None,
    output_fps: int = 24,
) -> dict:
    """
    Concatenate multiple MP4 clips, optionally add a music track (trimmed to video length).
    Returns {"ok": True, "bytes": b"..."} or {"ok": False, "error": "..."}.

    Uses moviepy (which wraps ffmpeg).
    """
    try:
        import tempfile, os as _os
        from moviepy.editor import VideoFileClip, concatenate_videoclips, AudioFileClip, CompositeAudioClip

        tmp_dir = tempfile.mkdtemp()
        clip_paths = []

        # Write each clip to a temp file
        for i, vb in enumerate(video_bytes_list):
            p = _os.path.join(tmp_dir, f"clip_{i}.mp4")
            with open(p, "wb") as f:
                f.write(vb)
            clip_paths.append(p)

        # Load and concatenate
        clips = [VideoFileClip(p) for p in clip_paths]
        combined = concatenate_videoclips(clips, method="compose")

        # Add music if provided
        if music_bytes or music_url:
            music_path = _os.path.join(tmp_dir, "music.mp3")
            if music_bytes:
                with open(music_path, "wb") as f:
                    f.write(music_bytes)
            else:
                r = requests.get(music_url, timeout=30)
                r.raise_for_status()
                with open(music_path, "wb") as f:
                    f.write(r.content)

            max_dur = min(combined.duration, 30)
            music_clip = AudioFileClip(music_path).subclip(0, max_dur).volumex(0.35)

            if combined.audio:
                final_audio = CompositeAudioClip([combined.audio, music_clip])
            else:
                final_audio = music_clip
            combined = combined.set_audio(final_audio)

        # Export — omit audio_codec when there is no audio track
        out_path = _os.path.join(tmp_dir, "merged.mp4")
        has_audio = combined.audio is not None
        combined.write_videofile(
            out_path,
            fps=output_fps,
            codec="libx264",
            audio_codec="aac" if has_audio else None,
            verbose=False,
            logger=None,
        )
        combined.close()
        for c in clips:
            c.close()

        with open(out_path, "rb") as f:
            merged_bytes = f.read()

        # Clean up temp files
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)

        return {"ok": True, "bytes": merged_bytes}

    except Exception as exc:
        logger.error("video_gen.merge_videos failed: %s", exc)
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Last.fm — fetch trending track name for background music suggestion
# ---------------------------------------------------------------------------

def get_trending_music_url(mood: str = "energetic") -> dict:
    """
    Get a trending track from Last.fm chart. Returns track name + artist + preview URL.
    Last.fm does NOT provide audio previews, so we return the fallback URL from config
    and the track name/artist for display.

    Returns {"ok": True, "name": "...", "artist": "...", "audio_url": "..."}
    """
    try:
        resp = requests.get(
            "https://ws.audioscrobbler.com/2.0/",
            params={
                "method":  "chart.gettoptracks",
                "api_key": config.LASTFM_API_KEY,
                "format":  "json",
                "limit":   "10",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        tracks = data.get("tracks", {}).get("track", [])
        if tracks:
            import random
            pick = random.choice(tracks[:5])
            return {
                "ok":       True,
                "name":     pick.get("name", "Unknown"),
                "artist":   pick.get("artist", {}).get("name", "Unknown"),
                "audio_url": config.LASTFM_FALLBACK_AUDIO_URL,  # Last.fm has no preview URLs
            }
    except Exception as exc:
        logger.warning("video_gen.lastfm: %s", exc)

    return {
        "ok":       True,
        "name":     "Trending Track",
        "artist":   "",
        "audio_url": config.LASTFM_FALLBACK_AUDIO_URL,
    }


# ---------------------------------------------------------------------------
# Full Ad Reel compositor
# ---------------------------------------------------------------------------

def composite_ad_video(
    lipsync_bytes: bytes,
    user_vid1_bytes: bytes,
    user_vid2_bytes: bytes,
    cine_vid1_bytes: bytes,
    cine_vid2_bytes: bytes,
    cine_vid3_bytes: bytes,
    output_fps: int = 24,
) -> dict:
    """
    Compose the Full Ad Reel by overlaying B-roll clips on the lipsync base.

    Overlay schedule (all B-roll clips are 3s, no audio):
      0–3s          : user_vid1   (user with product/service)
      3–6s          : cine_vid1   (cinematic product shot)
      6–9s          : user_vid2   (user with product/service)
      9s → (D-12)s  : gap — lipsync only
      (D-12)→(D-9)s : cine_vid2
      (D-9)→(D-3)s  : gap — lipsync only
      (D-3)→D s     : cine_vid3

    If D < 18s, cine_vid2 is placed at the midpoint and gaps are compressed.
    The lipsync audio is always preserved at full volume.
    Returns {"ok": True, "bytes": b"..."} or {"ok": False, "error": "..."}.
    """
    try:
      with _video_slot("ad_reel"):
        import tempfile, os as _os, shutil
        from moviepy.editor import VideoFileClip, ImageClip, CompositeVideoClip

        tmp = tempfile.mkdtemp()

        def _write(name: str, data: bytes) -> str:
            p = _os.path.join(tmp, name)
            with open(p, "wb") as f:
                f.write(data)
            return p

        base_path  = _write("base.mp4",   lipsync_bytes)
        uv1_path   = _write("uv1.mp4",    user_vid1_bytes)
        uv2_path   = _write("uv2.mp4",    user_vid2_bytes)
        cv1_path   = _write("cv1.mp4",    cine_vid1_bytes)
        cv2_path   = _write("cv2.mp4",    cine_vid2_bytes)
        cv3_path   = _write("cv3.mp4",    cine_vid3_bytes)

        base = VideoFileClip(base_path)
        D    = base.duration
        W, H = base.size

        def _overlay(path: str, start: float, duration: float = 3.0):
            """Load a clip, resize to base dimensions, strip audio, set timing."""
            clip_dur = min(duration, max(0.1, D - start))
            if clip_dur <= 0:
                return None
            raw = VideoFileClip(path)
            actual_dur = min(clip_dur, raw.duration)
            c = (raw
                 .without_audio()
                 .resize((W, H))
                 .subclip(0, actual_dur)
                 .set_start(start)
                 .set_duration(actual_dur))
            return c

        # ── Compute overlay positions ──────────────────────────────────
        clips = [base]

        # Fixed head section (0–9s)
        for path, start in [(uv1_path, 0.0), (cv1_path, 3.0), (uv2_path, 6.0)]:
            c = _overlay(path, start)
            if c:
                clips.append(c)

        # Tail section — place relative to end
        if D >= 15:
            # cine_vid2 at D-12, cine_vid3 at D-3
            for path, start in [(cv2_path, D - 12.0), (cv3_path, D - 3.0)]:
                if start > 9.0:   # don't overlap with head section
                    c = _overlay(path, start)
                    if c:
                        clips.append(c)
        elif D >= 9:
            # Short video — just place cine_vid3 at the very end
            c = _overlay(cv3_path, D - 3.0)
            if c:
                clips.append(c)

        final    = CompositeVideoClip(clips, size=(W, H))
        out_path = _os.path.join(tmp, "ad_reel.mp4")
        has_audio = base.audio is not None
        final.write_videofile(
            out_path,
            fps=output_fps,
            codec="libx264",
            audio_codec="aac" if has_audio else None,
            verbose=False,
            logger=None,
        )
        final.close()
        base.close()

        with open(out_path, "rb") as f:
            result_bytes = f.read()

        shutil.rmtree(tmp, ignore_errors=True)
        return {"ok": True, "bytes": result_bytes}

    except Exception as exc:
        logger.error("composite_ad_video failed: %s", exc)
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def stamp_video(
    video_bytes: bytes,
    badge_png_bytes: bytes,
    position: tuple[int, int] = (20, 20),
    output_fps: int = 24,
) -> bytes:
    """
    Overlay a transparent PNG badge on every frame of a video using moviepy.
    `badge_png_bytes` — RGBA PNG produced by carousel_composer badge renderer.
    `position`        — (x, y) top-left corner of the badge in pixels.
    Returns MP4 bytes with badge burned in.
    """
    import tempfile, os as _os, shutil
    from moviepy.editor import VideoFileClip, ImageClip, CompositeVideoClip
    from PIL import Image
    import io as _io
    import numpy as np

    tmp_dir = tempfile.mkdtemp()
    try:
        # Write video to temp file
        vid_path = _os.path.join(tmp_dir, "input.mp4")
        with open(vid_path, "wb") as f:
            f.write(video_bytes)

        # Convert badge PNG → numpy RGBA array for moviepy
        badge_img = Image.open(_io.BytesIO(badge_png_bytes)).convert("RGBA")
        badge_arr = np.array(badge_img)

        video_clip  = VideoFileClip(vid_path)
        badge_clip  = (
            ImageClip(badge_arr)
            .set_duration(video_clip.duration)
            .set_position(position)
        )
        final = CompositeVideoClip([video_clip, badge_clip])

        out_path = _os.path.join(tmp_dir, "badged.mp4")
        has_audio = video_clip.audio is not None
        final.write_videofile(
            out_path,
            fps=output_fps,
            codec="libx264",
            audio_codec="aac" if has_audio else None,
            verbose=False,
            logger=None,
        )
        final.close()
        video_clip.close()

        with open(out_path, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def add_text_overlays(
    video_bytes: bytes,
    lines: list[str],
    output_fps: int = 24,
) -> bytes:
    """
    Burn text overlays onto a video using PIL (no ImageMagick needed).
    `lines` — list of up to 3 strings shown at evenly spaced intervals.
    Each line fades in at its timestamp and stays for ~3 seconds.
    Returns MP4 bytes with text burned in.
    """
    import tempfile, os as _os, shutil, io as _io
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    from moviepy.editor import VideoFileClip, ImageClip, CompositeVideoClip

    tmp_dir = tempfile.mkdtemp()
    try:
        # Write video to temp file
        vid_path = _os.path.join(tmp_dir, "input.mp4")
        with open(vid_path, "wb") as f:
            f.write(video_bytes)

        video_clip = VideoFileClip(vid_path)
        w, h = video_clip.size
        duration = video_clip.duration

        # Try to load a bold system font, fall back to PIL default
        # Stylised minimal font — try thin/light system fonts first
        font_size = max(40, h // 16)
        font = None
        font_candidates = [
            "/System/Library/Fonts/HelveticaNeue.ttc",
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/SFNSDisplay.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]
        for fc in font_candidates:
            try:
                font = ImageFont.truetype(fc, font_size)
                break
            except Exception:
                continue
        if font is None:
            font = ImageFont.load_default()

        overlay_clips = [video_clip]
        n = len(lines)

        for i, text in enumerate(lines):
            # Uppercase for editorial feel
            text = text.upper()

            # Evenly distribute: show each line for ~3s at its timestamp
            start_t  = (duration / (n + 1)) * (i + 1) - 1.5
            start_t  = max(0.3, start_t)
            show_dur = min(3.0, duration - start_t)
            if show_dur <= 0:
                continue

            # Full-width transparent canvas
            canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            draw   = ImageDraw.Draw(canvas)

            try:
                bbox = draw.textbbox((0, 0), text, font=font)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            except AttributeError:
                tw, th = draw.textsize(text, font=font)

            x = (w - tw) // 2

            # Vertical positions: top / centre / bottom
            margin = h // 10
            y_positions = [margin, (h - th) // 2, h - margin - th]
            y = y_positions[i] if i < len(y_positions) else (h - th) // 2

            # Minimal frosted pill — very subtle, near-black at 55% opacity
            pad_x, pad_y = 28, 14
            draw.rounded_rectangle(
                [x - pad_x, y - pad_y, x + tw + pad_x, y + th + pad_y],
                radius=8,
                fill=(10, 10, 10, 140),
            )
            # Fine 1-px shadow offset then crisp white text
            draw.text((x + 1, y + 1), text, font=font, fill=(0, 0, 0, 120))
            draw.text((x,     y    ), text, font=font, fill=(255, 255, 255, 245))

            arr = np.array(canvas)
            text_clip = (
                ImageClip(arr)
                .set_start(start_t)
                .set_duration(show_dur)
                .set_position(("left", "top"))
            )
            overlay_clips.append(text_clip)

        final = CompositeVideoClip(overlay_clips, size=(w, h))
        out_path = _os.path.join(tmp_dir, "overlaid.mp4")
        has_audio = video_clip.audio is not None
        final.write_videofile(
            out_path,
            fps=output_fps,
            codec="libx264",
            audio_codec="aac" if has_audio else None,
            verbose=False,
            logger=None,
        )
        final.close()
        video_clip.close()

        with open(out_path, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@_gated("captions")
def add_tiktok_captions(
    video_bytes: bytes,
    highlight_color: str = "#FFFF00",
) -> bytes | None:
    """
    Add TikTok-style auto-captions to a video using
    shreejalmaharjan-27/tiktok-short-captions on Replicate.
    The highlight colour is set to the brand colour.
    Returns captioned MP4 bytes, or None on failure (caller falls back to original).
    """
    if not config.REPLICATE_API_TOKEN:
        logger.warning("video_gen.captions: REPLICATE_API_TOKEN not set")
        return None

    try:
        import tempfile, os as _os
        import config as _cfg
        from tools import aws_storage as _s3

        # Upload video to S3 so the model can fetch it via public URL
        tmp_upload = _s3.upload_bytes(
            video_bytes,
            content_type="video/mp4",
            extension="mp4",
            folder=f"{_cfg.AWS_BASE_DIR}/tmp/caption_input",
        )
        if not tmp_upload.get("ok"):
            logger.error("video_gen.captions: tmp upload failed: %s", tmp_upload.get("error"))
            return None
        video_url = tmp_upload["s3_url"]

        # Community model → must be run with a pinned version hash (owner/name alone 404s).
        run_ref = ("shreejalmaharjan-27/tiktok-short-captions:"
                   "46bf1c12c77ad1782d6f87828d4d8ba4d48646b8e1271b490cb9e95ccdbc4504")
        base_input = {
            "video":            video_url,   # this model's field is "video"
            "highlight_color":  highlight_color,
        }
        # Bigger captions. font_size may not be a valid field on every version, so if
        # the model rejects it we retry with just the known-good inputs.
        try:
            output = _replicate.run(run_ref, input={**base_input, "font_size": 90})
        except Exception as exc:
            logger.warning("video_gen.captions: font_size rejected (%s) — retrying without it", exc)
            output = _replicate.run(run_ref, input=base_input)
        logger.info("video_gen.captions: ran %s → output type=%s value=%r",
                    run_ref[:80], type(output).__name__, str(output)[:200])

        # Resolve output URL (handles str / list / dict / FileOutput)
        url = _resolve_url(output)
        if not url:
            logger.error("video_gen.captions: could not resolve url from output=%r", output)
            return None

        captioned_bytes = _fetch(url, timeout=180)
        logger.info("video_gen.captions: got %d bytes", len(captioned_bytes))
        return captioned_bytes

    except Exception as exc:
        logger.error("video_gen.captions failed: %s", exc, exc_info=True)
        return None


def _resolve_url(output) -> str | None:
    """Extract a URL string from various Replicate output shapes (incl. FileOutput)."""
    if output is None:
        return None
    if isinstance(output, str) and output.startswith("http"):
        return output
    if isinstance(output, list) and output:
        return _resolve_url(output[0])
    if isinstance(output, dict):
        for k in ("url", "output", "video", "audio", "output_video"):
            if k in output:
                return _resolve_url(output[k])
    # Replicate FileOutput: .url may be a property OR a method; str() also yields the URL
    u = getattr(output, "url", None)
    if callable(u):
        try:
            u = u()
        except Exception:
            u = None
    if isinstance(u, str) and u.startswith("http"):
        return u
    try:
        s = str(output)
        if s.startswith("http"):
            return s
    except Exception:
        pass
    return None


def _fetch(url: str, timeout: int = 120) -> bytes:
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.content


# ---------------------------------------------------------------------------
# UGC Presentation: slideshow + chromakey overlay
# ---------------------------------------------------------------------------

_CHROMA_GREEN = (0, 177, 64)   # #00b140 — must match the green-screen image


def _cover_9x16(img_bytes: bytes, size=(1080, 1920)):
    """Crop/scale an image to fill a 9:16 frame (cover), return a numpy RGB array."""
    from io import BytesIO as _BytesIO
    from PIL import Image as _Image
    import numpy as _np
    cw, ch = size
    im = _Image.open(_BytesIO(img_bytes)).convert("RGB")
    sw, sh = im.size
    scale = max(cw / sw, ch / sh)
    nw, nh = max(1, int(sw * scale)), max(1, int(sh * scale))
    im = im.resize((nw, nh), _Image.LANCZOS)
    left, top = (nw - cw) // 2, (nh - ch) // 2
    im = im.crop((left, top, left + cw, top + ch))
    return _np.array(im)


def compose_presentation_video(
    lipsync_bytes: bytes,
    photo_urls: list[str],
    output_fps: int = 24,
) -> dict:
    """
    Build the UGC presentation reel:
      - background = slideshow of the scraped product/property photos (9:16), timed
        to the talking-video length
      - foreground = the green-screen talking person with the green removed (chromakey)
      The presenter's audio is preserved.
    Returns {"ok": True, "bytes": b"..."} or {"ok": False, "error": "..."}.
    """
    try:
      with _video_slot("presentation"):
        import tempfile, os as _os, shutil
        from moviepy.editor import (VideoFileClip, ImageClip,
                                    concatenate_videoclips, CompositeVideoClip)
        from moviepy.video.fx.all import mask_color

        W, H = 1080, 1920
        tmp = tempfile.mkdtemp()
        base_path = _os.path.join(tmp, "talk.mp4")
        with open(base_path, "wb") as f:
            f.write(lipsync_bytes)

        talk = VideoFileClip(base_path)
        D = talk.duration or 10.0

        # ── Background slideshow from photos (cover to 9:16) ────────────────
        imgs = []
        for u in (photo_urls or [])[:12]:
            try:
                imgs.append(_cover_9x16(_fetch(u, timeout=40)))
            except Exception:
                continue
        if imgs:
            per = max(1.5, D / len(imgs))
            slides = [ImageClip(arr).set_duration(per) for arr in imgs]
            slideshow = concatenate_videoclips(slides, method="compose").set_duration(D)
        else:
            # no photos → plain dark background
            from moviepy.editor import ColorClip
            slideshow = ColorClip(size=(W, H), color=(15, 15, 15)).set_duration(D)
        slideshow = slideshow.resize((W, H))

        # ── Foreground talking person, green removed — HALF size, bottom-left ──
        # Higher thr removes more green (incl. fringe/shaded green); larger s softens
        # the matte edge so it doesn't look cut-out.
        margin = 40
        person = (talk
                  .fx(mask_color, color=list(_CHROMA_GREEN), thr=160, s=20)
                  .resize((W, H))     # normalise to full 9:16 first
                  .resize(0.5)        # then halve → 540x960
                  .set_position((margin, H - (H // 2) - margin))  # bottom-left
                  .set_duration(D))

        final = CompositeVideoClip([slideshow, person], size=(W, H)).set_duration(D)
        if talk.audio is not None:
            final = final.set_audio(talk.audio)

        out_path = _os.path.join(tmp, "presentation.mp4")
        final.write_videofile(out_path, fps=output_fps, codec="libx264",
                              audio_codec="aac" if talk.audio is not None else None,
                              verbose=False, logger=None)
        final.close(); talk.close()
        with open(out_path, "rb") as f:
            data = f.read()
        shutil.rmtree(tmp, ignore_errors=True)
        return {"ok": True, "bytes": data}
    except Exception as exc:
        logger.error("compose_presentation_video failed: %s", exc)
        return {"ok": False, "error": str(exc)}


_AUTOCAPTION_MODEL = ("fictions-ai/autocaption:"
                      "18a45ff0d95feb4449d192bbdc06b4a6df168fa33def76dfc51b78ae224b599b")


def add_autocaption(video_bytes: bytes) -> bytes | None:
    """
    Burn animated captions onto a video using fictions-ai/autocaption.
    Falls back to the TikTok-caption model, then to the original video.
    """
    if not config.REPLICATE_API_TOKEN:
        return video_bytes
    try:
        from tools import aws_storage as _s3
        up = _s3.upload_bytes(video_bytes, content_type="video/mp4", extension="mp4",
                              folder=f"{config.AWS_BASE_DIR}/tmp/autocaption_in")
        if not up.get("ok"):
            return video_bytes
        video_url = up["s3_url"]
        try:
            output = _replicate.run(_AUTOCAPTION_MODEL, input={
                "video_file_input": video_url,
                "output_video": True,
                "fps": 30,
            })
            url = _resolve_url(output)
            if url:
                return _fetch(url, timeout=180)
        except Exception as exc:
            logger.warning("autocaption (fictions-ai) failed, trying fallback: %s", exc)
        # Fallback to the existing tiktok-caption helper
        fb = add_tiktok_captions(video_bytes)
        return fb or video_bytes
    except Exception as exc:
        logger.warning("add_autocaption error: %s", exc)
        return video_bytes
