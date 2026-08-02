"""
Omni restyle — edit-native video restyling via Kling v3 Omni (Replicate).

This is the faithful stand-in for the arcads-omniflash skill's
`arcads_generate_video_omni_flash`. Kling v3 Omni's constraints line up with the
skill's documented Omni Flash limits almost exactly:

  • reference_video with video_reference_type="base"  → real video EDITING
  • reference video duration 3-10s                    → the skill's 3-10s limit
  • reference_images (max 4 alongside video)          → the skill's style frames
  • multiple timed scene changes in ONE call          → timestamped prompt
  • keep_original_sound                               → we still relay the source
                                                        audio locally (the skill:
                                                        never trust generated audio)

Anything longer than 10s is chunked on BEAT boundaries (3s floor, 10s ceiling) so
each seam hides inside a style change — the skill's Phase D/E/G.

Public API:
    build_scene_prompt(beats, style_notes) -> str
    restyle_clip(video_url, prompt, reference_images, aspect_ratio) -> {"ok","bytes"}
    restyle_video(source_url, beats, ...) -> {"ok","bytes","chunks"}
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import time

import replicate
import requests

import config
from tools.replicate_queue import gated as _gated

logger = logging.getLogger(__name__)

_MODEL = "kwaivgi/kling-v3-omni-video"
_MIN_CLIP = 3.0     # model floor (and the skill's floor)
_MAX_CLIP = 10.0    # model ceiling
_MAX_WAIT = 900
_POLL = 10

_IDENTITY_LOCK = (
    "Edit this video. Preserve the person exactly as they appear in the source video, frame by "
    "frame: same face, same features, same skin, same hair, same body, same lip movement, same "
    "gesture timing. Do not redraw, regenerate, restyle, or beautify the person; treat their "
    "footage as locked and edit only what surrounds them (background, typography, overlays, crops)."
)
_KEEP_CLAUSE = (
    "Execute every camera move as written; through every camera move it is the same person, same "
    "face, same body, same identity. Do not change the person's timing or speed. No audio changes."
)


def build_scene_prompt(beats: list[dict], style_notes: str = "") -> str:
    """Identity lock → timestamped beat timeline (one dominant transform each) → keep clause."""
    lines = [_IDENTITY_LOCK, "Apply these scene changes in sequence:"]
    for b in beats:
        s, e = float(b.get("start", 0)), float(b.get("end", 0))
        parts = []
        if (b.get("scene") or "").strip():
            parts.append(b["scene"].strip())
        if (b.get("text") or "").strip():
            parts.append(f'giant bold on-screen text reading "{b["text"].strip()}"')
        if (b.get("camera") or "").strip():
            parts.append(b["camera"].strip())
        lines.append(f"{s:.1f} to {e:.1f}s: " + ". ".join(parts) + ".")
    if style_notes:
        lines.append(f"Overall look: {style_notes}.")
    lines.append(_KEEP_CLAUSE)
    return "\n".join(lines)[:2500]      # model caps the prompt at 2500 chars


@_gated("video")
def restyle_clip(video_url: str, prompt: str,
                 reference_images: list[str] | None = None,
                 aspect_ratio: str = "9:16") -> dict:
    """Restyle one 3-10s clip. Returns {"ok": True, "bytes": ...}."""
    if not config.REPLICATE_API_TOKEN:
        return {"ok": False, "error": "REPLICATE_API_TOKEN not set"}
    inp: dict = {
        "prompt": prompt,
        "reference_video": video_url,
        "video_reference_type": "base",     # 'base' = video EDITING (not just style ref)
        "mode": "pro",                      # 1080p
        "aspect_ratio": aspect_ratio if aspect_ratio in ("16:9", "9:16", "1:1") else "9:16",
        "keep_original_sound": True,
        "generate_audio": False,            # mutually exclusive with reference video
    }
    if reference_images:
        inp["reference_images"] = reference_images[:4]   # max 4 alongside a video

    try:
        pred = replicate.predictions.create(model=_MODEL, input=inp)
    except Exception as exc:
        logger.error("kling-omni create failed: %s", exc)
        return {"ok": False, "error": f"could not start kling omni: {exc}"}

    elapsed = 0
    while elapsed < _MAX_WAIT:
        try:
            pred.reload()
        except Exception:
            time.sleep(_POLL); elapsed += _POLL; continue
        if pred.status == "succeeded":
            out = pred.output
            url = None
            if isinstance(out, list) and out:
                raw = out[0]; url = raw.url if hasattr(raw, "url") else str(raw)
            elif isinstance(out, str):
                url = out
            elif hasattr(out, "url"):
                url = out.url
            if not url:
                return {"ok": False, "error": f"unexpected output: {out!r}"}
            try:
                return {"ok": True, "bytes": requests.get(url, timeout=300).content, "url": url}
            except Exception as exc:
                return {"ok": False, "error": f"download failed: {exc}"}
        if pred.status in ("failed", "canceled"):
            return {"ok": False, "error": pred.error or pred.status}
        time.sleep(_POLL); elapsed += _POLL
    try:
        pred.cancel()
    except Exception:
        pass
    return {"ok": False, "error": f"kling omni timed out after {_MAX_WAIT}s"}


def _chunk_bounds(duration: float, beats: list[dict]) -> list[tuple[float, float]]:
    """Chunks of 3-10s, snapped to beat boundaries; never emit a sub-3s tail."""
    edges = sorted({float(b["start"]) for b in beats if b.get("start")} | {duration})
    bounds: list[tuple[float, float]] = []
    cur = 0.0
    while cur < duration - 0.05:
        hi = min(cur + _MAX_CLIP, duration)
        lo = cur + _MIN_CLIP
        snapped = [e for e in edges if lo <= e <= hi]
        nxt = max(snapped) if snapped else hi
        remaining = duration - nxt
        if 0 < remaining < _MIN_CLIP:
            # A sub-3s tail can't be its own chunk. Absorb it if that keeps us
            # under the ceiling, otherwise pull this boundary back so the tail
            # grows to a legal length.
            if duration - cur <= _MAX_CLIP:
                nxt = duration
            else:
                nxt = max(cur + _MIN_CLIP, duration - _MIN_CLIP)
        bounds.append((cur, nxt))
        cur = nxt
    if not bounds:
        bounds = [(0.0, duration)]
    # a single clip shorter than the 3s floor can't be edited by the model
    if bounds[0][1] - bounds[0][0] < _MIN_CLIP:
        return []
    return bounds


def restyle_video(source_url: str, beats: list[dict], style_notes: str = "",
                  reference_images: list[str] | None = None,
                  aspect_ratio: str = "9:16", progress=None) -> dict:
    """Chunk → restyle each → concat → relay the ORIGINAL voice."""
    tmp = tempfile.mkdtemp()
    try:
        from tools import aws_storage as _s3
        src = os.path.join(tmp, "src.mp4")
        with open(src, "wb") as f:
            f.write(requests.get(source_url, timeout=300).content)

        pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                             "-of", "csv=p=0", src], capture_output=True, text=True)
        try:
            duration = float((pr.stdout or "0").strip())
        except ValueError:
            duration = 0.0
        if duration < _MIN_CLIP:
            return {"ok": False,
                    "error": f"video is {duration:.1f}s — needs at least {_MIN_CLIP:.0f}s to restyle"}

        bounds = _chunk_bounds(duration, beats)
        if not bounds:
            return {"ok": False, "error": "could not split the video into valid chunks"}
        logger.info("kling-omni: %d chunk(s) for %.1fs source", len(bounds), duration)

        styled: list[str] = []
        for i, (a, b) in enumerate(bounds):
            if progress:
                progress(i + 1, len(bounds))
            piece = os.path.join(tmp, f"chunk{i}.mp4")
            subprocess.run(["ffmpeg", "-y", "-ss", str(a), "-t", str(b - a), "-i", src,
                            "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
                            "-pix_fmt", "yuv420p", "-an", piece],
                           capture_output=True, timeout=300)
            if not os.path.exists(piece):
                continue
            # Serve with an explicit video/mp4 Content-Type (raw uploads are rejected)
            with open(piece, "rb") as fh:
                up = _s3.upload_bytes(fh.read(), content_type="video/mp4",
                                      extension="mp4", folder="omni_chunks")
            url = up.get("s3_url")
            if not url:
                styled.append(piece); continue

            local = [{**bt, "start": max(0.0, float(bt["start"]) - a),
                      "end": min(b - a, float(bt["end"]) - a)}
                     for bt in beats if float(bt["end"]) > a and float(bt["start"]) < b]
            prompt = build_scene_prompt(
                local or [{"start": 0, "end": b - a, "scene": style_notes or "restyle the background"}],
                style_notes)
            res = restyle_clip(url, prompt, reference_images, aspect_ratio)
            if not res.get("ok") or not res.get("bytes"):
                logger.warning("kling-omni chunk %d failed: %s — keeping original", i, res.get("error"))
                styled.append(piece); continue
            outp = os.path.join(tmp, f"styled{i}.mp4")
            with open(outp, "wb") as fh:
                fh.write(res["bytes"])
            styled.append(outp)

        if not styled:
            return {"ok": False, "error": "no chunks were produced"}

        norm: list[str] = []
        for i, p in enumerate(styled):
            n = os.path.join(tmp, f"norm{i}.mp4")
            subprocess.run(["ffmpeg", "-y", "-i", p, "-vf",
                            "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=24",
                            "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
                            "-pix_fmt", "yuv420p", "-an", n], capture_output=True, timeout=300)
            if os.path.exists(n):
                norm.append(n)
        lst = os.path.join(tmp, "list.txt")
        with open(lst, "w") as f:
            for p in norm:
                f.write(f"file '{p}'\n")
        silent = os.path.join(tmp, "silent.mp4")
        subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst,
                        "-c", "copy", silent], capture_output=True, timeout=600)
        if not os.path.exists(silent):
            return {"ok": False, "error": "concat failed"}

        final = os.path.join(tmp, "final.mp4")
        subprocess.run(["ffmpeg", "-y", "-i", silent, "-i", src, "-map", "0:v", "-map", "1:a?",
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest",
                        "-movflags", "+faststart", final], capture_output=True, timeout=600)
        use = final if os.path.exists(final) else silent
        with open(use, "rb") as f:
            return {"ok": True, "bytes": f.read(), "chunks": len(norm)}
    except Exception as exc:
        logger.error("kling-omni restyle_video failed: %s", exc, exc_info=True)
        return {"ok": False, "error": str(exc)}
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
