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


@traceable(run_type="tool", name="render_reel_remotion")
def render_reel(
    presenter_src: str,
    broll: list[dict],
    captions: list[dict],
    duration_sec: float,
    audio_src: str = "",
    fps: int = 24,
    width: int = 1080,
    height: int = 1920,
    title: str = "",
    cta: str = "",
    timeout: int = 1200,
) -> dict:
    """
    Composite the full studio reel in Remotion:
      - B-roll timeline underneath (hard cuts + Ken Burns zoom per segment)
      - the transparent presenter WebM on top
      - original audio, title card, and trending captions
    broll:    [{"start", "end", "src", "zoom"}]  (src = B-roll clip URL)
    captions: [{"start", "end", "text", "emphasis"}]
    Returns {"ok": True, "bytes": b"..."} or {"ok": False, "error": "..."}.
    """
    if not _deps_ready():
        return {"ok": False, "error": "remotion deps not installed (run npm install in remotion/)"}
    if not presenter_src and not broll:
        return {"ok": False, "error": "nothing to render (no presenter and no b-roll)"}

    props = {
        "fps": int(fps),
        "width": int(width),
        "height": int(height),
        "durationInFrames": max(1, int(round(duration_sec * fps))),
        "audioSrc": audio_src or "",
        "presenterSrc": presenter_src or "",
        "title": title or "",
        "cta": cta or "",
        "broll": [
            {
                "start": float(b.get("start", 0)),
                "end": float(b.get("end", 0)),
                "src": str(b.get("src", "")),
                "zoom": str(b.get("zoom", "none")),
                "emphasis": bool(b.get("emphasis", False)),
            }
            for b in (broll or [])
            if str(b.get("src", ""))
        ],
        "captions": [
            {
                "start": float(c.get("start", 0)),
                "end": float(c.get("end", 0)),
                "text": str(c.get("text", "")).strip(),
                "emphasis": bool(c.get("emphasis", False)),
            }
            for c in (captions or [])
            if str(c.get("text", "")).strip()
        ],
    }

    tmp = tempfile.mkdtemp()
    props_path = os.path.join(tmp, "props.json")
    out_path = os.path.join(tmp, "final.mp4")
    with open(props_path, "w") as f:
        json.dump(props, f)

    cmd = [
        "npx", "remotion", "render", _ENTRY, _COMPOSITION, out_path,
        f"--props={props_path}",
        "--codec", "h264",
        "--log", "error",
    ]
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

    logger.info("remotion render: %d bytes", len(data))
    return {"ok": True, "bytes": data}
