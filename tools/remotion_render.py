"""
Remotion render bridge — Stage 3 of the AI Video Editor.

Takes the Stage 2 base cut (user chroma-keyed over B-roll) plus a caption track,
and renders punchy trending captions + a title card on top via the Node/Remotion
project in ../remotion. Returns the final MP4 bytes.

Requires Node + the remotion project's deps installed (`npm install` in remotion/).
The first render also downloads a headless Chromium via Remotion (cached afterwards).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile

from tools.tracing import traceable

logger = logging.getLogger(__name__)

_REMOTION_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "remotion")
_COMPOSITION = "CaptionedVideo"
_ENTRY = os.path.join("src", "index.ts")


def _deps_ready() -> bool:
    return os.path.isdir(os.path.join(_REMOTION_DIR, "node_modules", "remotion"))


def render_layer(
    scenes: list[dict],
    words: list[dict],
    duration_sec: float,
    layer: str = "back",
    bg_video: str = "",
    fps: int = 24,
    width: int = 1080,
    height: int = 1920,
    caption_pos: str = "bottom",
    timeout: int = 1200,
) -> dict:
    """
    Render ONE graphics layer of the Hormozi-style reel in Remotion. Only OPAQUE
    video is ever fed to Remotion (transparent *input* hangs; transparent *output*
    ProRes blows the disk) — the presenter is chroma-keyed by ffmpeg in between.
      layer="back"  → opaque backgrounds + big-text-behind (h264 mp4)
      layer="front" → bg_video (back+presenter, opaque) + doodles + lens + captions (h264 mp4)
    Returns {"ok": True, "bytes": b"...", "ext": "mp4"} or {"ok": False, "error": "..."}.
    """
    if not _deps_ready():
        return {"ok": False, "error": "remotion deps not installed (run npm install in remotion/)"}
    if not scenes:
        return {"ok": False, "error": "no scenes to render"}

    transparent = False  # everything is opaque h264 now
    props = {
        "fps": int(fps),
        "width": int(width),
        "height": int(height),
        "durationInFrames": max(1, int(round(duration_sec * fps))),
        "layer": layer,
        "bgVideo": bg_video or "",
        "captionPos": caption_pos if caption_pos in ("top", "bottom") else "bottom",
        "scenes": [
            {
                "start": float(s.get("start", 0)),
                "end": float(s.get("end", 0)),
                "bg": str(s.get("bg", "solid")),
                "color": str(s.get("color", "")),
                "color2": str(s.get("color2", "")),
                "brollSrc": str(s.get("brollSrc", "")),
                "presenter": str(s.get("presenter", "full")),
                "bigText": str(s.get("bigText", "")),
                "doodle": str(s.get("doodle", "none")),
                "emoji": str(s.get("emoji", "")),
                "info": {
                    "type": str((s.get("info") or {}).get("type", "none")),
                    "value": float((s.get("info") or {}).get("value") or 0),
                    "label": str((s.get("info") or {}).get("label", "")),
                    "suffix": str((s.get("info") or {}).get("suffix", "")),
                    "icon": str((s.get("info") or {}).get("icon", "")),
                } if isinstance(s.get("info"), dict) else {"type": "none"},
                "transition": str(s.get("transition", "flash")),
                "zoom": str(s.get("zoom", "none")),
                "lens": bool(s.get("lens", False)),
                "emphasis": bool(s.get("emphasis", False)),
            }
            for s in (scenes or [])
        ],
        "words": [
            {"start": float(w.get("start", 0)), "end": float(w.get("end", 0)), "text": str(w.get("text", "")).strip()}
            for w in (words or [])
            if str(w.get("text", "")).strip()
        ],
    }

    ext = "mov" if transparent else "mp4"
    tmp = tempfile.mkdtemp()
    props_path = os.path.join(tmp, "props.json")
    out_path = os.path.join(tmp, f"layer.{ext}")
    with open(props_path, "w") as f:
        json.dump(props, f)

    cmd = [
        "npx", "remotion", "render", _ENTRY, _COMPOSITION, out_path,
        f"--props={props_path}",
        "--timeout", "120000",
        "--log", "error",
    ]
    if transparent:
        # ProRes 4444 carries alpha on OUTPUT (reliable, unlike transparent input).
        # Transparent output requires PNG image frames.
        cmd += ["--codec", "prores", "--prores-profile", "4444",
                "--pixel-format", "yuva444p10le", "--image-format", "png"]
    else:
        cmd += ["--codec", "h264"]
    env = dict(os.environ)
    env.setdefault("REMOTION_DISABLE_TELEMETRY", "1")

    try:
        proc = subprocess.run(
            cmd, cwd=_REMOTION_DIR, env=env,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"remotion render timed out after {timeout}s"}
    except Exception as exc:
        return {"ok": False, "error": f"remotion invoke failed: {exc}"}

    if proc.returncode != 0 or not os.path.exists(out_path):
        tail = (proc.stderr or proc.stdout or "")[-800:]
        logger.error("remotion render failed (%s): %s", proc.returncode, tail)
        return {"ok": False, "error": f"remotion render failed: {tail}"}

    try:
        with open(out_path, "rb") as f:
            data = f.read()
    except Exception as exc:
        return {"ok": False, "error": f"could not read remotion output: {exc}"}

    logger.info("remotion render (%s): %d bytes", layer, len(data))
    return {"ok": True, "bytes": data, "ext": ext}
