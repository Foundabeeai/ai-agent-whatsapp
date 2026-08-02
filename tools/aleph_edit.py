"""
Runway Gen-4 Aleph restyle (Replicate) — edit-native video restyling.

This is the drop-in replacement for the arcads-omniflash skill's
`arcads_generate_video_omni_flash`: you pass the REAL footage plus a prompt and
Aleph repaints the video around the person, rather than generating new footage.

Hard model limits (verified from the Replicate schema):
  • input video must be < 16 MB
  • only the FIRST 5 SECONDS of the input are used

So anything longer is chunked into <=5s pieces cut on beat boundaries (the seam
hides inside a style change), each restyled in its own call, then concatenated
and the ORIGINAL voice relaid on top — exactly the skill's Phase D/E/G.

Public API:
    build_scene_prompt(beats, style_notes) -> str
    restyle_clip(video_url, prompt, reference_image, aspect_ratio) -> {"ok","bytes"}
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

_MODEL = "runwayml/gen4-aleph"
_MAX_CLIP_SECS = 5.0        # model only consumes the first 5s
_MAX_UPLOAD_MB = 15.0       # stay under the 16 MB cap
_MAX_WAIT = 600
_POLL = 10

# The identity lock, verbatim in spirit from the skill: video-as-truth framing.
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
    """
    Compose the skill's prompt structure for one clip:
      identity lock → timestamped beat timeline (one dominant transform each) → keep clause.
    `beats` are RELATIVE to this clip: [{"start","end","scene","text","camera"}]
    """
    lines = [_IDENTITY_LOCK, "Apply these scene changes in sequence:"]
    for b in beats:
        s = float(b.get("start", 0)); e = float(b.get("end", 0))
        scene = (b.get("scene") or "").strip()
        text = (b.get("text") or "").strip()
        cam = (b.get("camera") or "").strip()
        parts = [scene] if scene else []
        if text:
            parts.append(f'giant bold on-screen text reading "{text}" behind the person')
        if cam:
            parts.append(cam)
        lines.append(f"{s:.1f} to {e:.1f}s: " + ". ".join(p for p in parts if p) + ".")
    if style_notes:
        lines.append(f"Overall look: {style_notes}.")
    lines.append(_KEEP_CLAUSE)
    return "\n".join(lines)


def _shrink_under_cap(path: str) -> str:
    """Re-encode until the clip is under the upload cap."""
    for crf in (24, 28, 32):
        if os.path.getsize(path) / 1e6 <= _MAX_UPLOAD_MB:
            return path
        out = path.replace(".mp4", f"_c{crf}.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-i", path, "-c:v", "libx264", "-crf", str(crf),
             "-preset", "veryfast", "-vf", "scale='min(1080,iw)':-2", "-an", out],
            capture_output=True, timeout=300)
        if os.path.exists(out):
            path = out
    return path


@_gated("video")
def restyle_clip(video_url_or_path: str, prompt: str,
                 reference_image: str | None = None,
                 aspect_ratio: str = "9:16") -> dict:
    """Restyle a single <=5s clip with Aleph. Returns {"ok": True, "bytes": ...}."""
    if not config.REPLICATE_API_TOKEN:
        return {"ok": False, "error": "REPLICATE_API_TOKEN not set"}
    inp: dict = {
        "video": video_url_or_path,
        "prompt": prompt,
        "aspect_ratio": aspect_ratio if aspect_ratio in
        ("16:9", "9:16", "4:3", "3:4", "1:1", "21:9") else "9:16",
    }
    if reference_image:
        inp["reference_image"] = reference_image
    try:
        pred = replicate.predictions.create(model=_MODEL, input=inp)
    except Exception as exc:
        logger.error("aleph create failed: %s", exc)
        return {"ok": False, "error": f"could not start aleph: {exc}"}

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
                data = requests.get(url, timeout=180).content
            except Exception as exc:
                return {"ok": False, "error": f"download failed: {exc}"}
            return {"ok": True, "bytes": data, "url": url}
        if pred.status in ("failed", "canceled"):
            return {"ok": False, "error": pred.error or pred.status}
        time.sleep(_POLL); elapsed += _POLL
    try:
        pred.cancel()
    except Exception:
        pass
    return {"ok": False, "error": f"aleph timed out after {_MAX_WAIT}s"}


def restyle_video(source_url: str, beats: list[dict], style_notes: str = "",
                  reference_image: str | None = None,
                  aspect_ratio: str = "9:16",
                  progress=None) -> dict:
    """
    Full Phase D→G: chunk the source into <=5s pieces cut on beat boundaries,
    restyle each with Aleph, concat, and relay the ORIGINAL audio.
    `beats` are absolute-time [{"start","end","scene","text","camera"}].
    Returns {"ok": True, "bytes": final_mp4, "chunks": n}.
    """
    tmp = tempfile.mkdtemp()
    try:
        src = os.path.join(tmp, "src.mp4")
        with open(src, "wb") as f:
            f.write(requests.get(source_url, timeout=300).content)

        # duration
        pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                             "-of", "csv=p=0", src], capture_output=True, text=True)
        try:
            duration = float((pr.stdout or "0").strip())
        except ValueError:
            duration = 0.0
        if duration <= 0:
            return {"ok": False, "error": "could not read source duration"}

        # ── chunk boundaries: <=5s, snapped to the nearest beat edge ──
        edges = sorted({0.0, duration} | {float(b["start"]) for b in beats if b.get("start")})
        bounds, cur = [], 0.0
        while cur < duration - 0.05:
            target = cur + _MAX_CLIP_SECS
            snapped = [e for e in edges if cur + 1.5 < e <= target]
            nxt = max(snapped) if snapped else min(target, duration)
            bounds.append((cur, min(nxt, duration)))
            cur = nxt
        logger.info("aleph: %d chunk(s) for %.1fs source", len(bounds), duration)

        styled: list[str] = []
        for i, (a, b) in enumerate(bounds):
            if progress:
                progress(i + 1, len(bounds))
            piece = os.path.join(tmp, f"chunk{i}.mp4")
            subprocess.run(["ffmpeg", "-y", "-ss", str(a), "-t", str(b - a), "-i", src,
                            "-c:v", "libx264", "-crf", "20", "-preset", "veryfast", "-an", piece],
                           capture_output=True, timeout=300)
            if not os.path.exists(piece):
                continue
            piece = _shrink_under_cap(piece)

            # beats that fall inside this chunk, re-based to chunk-relative time
            local = [{**bt, "start": max(0.0, float(bt["start"]) - a),
                      "end": min(b - a, float(bt["end"]) - a)}
                     for bt in beats if float(bt["end"]) > a and float(bt["start"]) < b]
            prompt = build_scene_prompt(local or [{"start": 0, "end": b - a,
                                                   "scene": style_notes or "restyle the background"}],
                                        style_notes)
            # Runway requires an approved Content-Type on the asset URL — a raw
            # file upload is served as application/octet-stream and is REJECTED.
            # Upload to S3 with an explicit video/mp4 type and pass that URL.
            from tools import aws_storage as _s3
            with open(piece, "rb") as fh:
                _up = _s3.upload_bytes(fh.read(), content_type="video/mp4",
                                       extension="mp4", folder="aleph_chunks")
            chunk_url = _up.get("s3_url")
            if not chunk_url:
                logger.warning("aleph chunk %d upload failed: %s", i, _up.get("error"))
                styled.append(piece)
                continue
            res = restyle_clip(chunk_url, prompt, reference_image, aspect_ratio)
            if not res.get("ok") or not res.get("bytes"):
                logger.warning("aleph chunk %d failed: %s — using original", i, res.get("error"))
                styled.append(piece)
                continue
            outp = os.path.join(tmp, f"styled{i}.mp4")
            with open(outp, "wb") as fh:
                fh.write(res["bytes"])
            styled.append(outp)

        if not styled:
            return {"ok": False, "error": "no chunks were produced"}

        # ── concat (normalise first so concat never fails on mismatched params) ──
        norm: list[str] = []
        for i, p in enumerate(styled):
            n = os.path.join(tmp, f"norm{i}.mp4")
            subprocess.run(["ffmpeg", "-y", "-i", p, "-vf",
                            "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=24",
                            "-c:v", "libx264", "-crf", "20", "-preset", "veryfast", "-an", n],
                           capture_output=True, timeout=300)
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

        # ── relay the ORIGINAL voice (skill: never trust generated audio) ──
        final = os.path.join(tmp, "final.mp4")
        subprocess.run(["ffmpeg", "-y", "-i", silent, "-i", src, "-map", "0:v", "-map", "1:a?",
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest",
                        "-movflags", "+faststart", final], capture_output=True, timeout=600)
        use = final if os.path.exists(final) else silent
        with open(use, "rb") as f:
            data = f.read()
        return {"ok": True, "bytes": data, "chunks": len(norm)}
    except Exception as exc:
        logger.error("aleph restyle_video failed: %s", exc, exc_info=True)
        return {"ok": False, "error": str(exc)}
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
