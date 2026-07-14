"""
AI Video Editor sub-agent.

User sends their talking video → we turn it into a trending short-form cut:
  matting → green screen → transcript → LLM edit plan → (later stages) B-roll +
  chromakey composite + Remotion captions/overlays → final render.

Pipeline (built stage by stage):
  STAGE 1 (this file, implemented):
    - user sends a video → upload to S3
    - robust_video_matting → green-screen version (chroma-key ready)
    - Whisper transcript of what they say
    - LLM edit plan (segments: B-roll prompts, zoom, captions, overlays)
    - send the plan back for approval
  STAGE 2+ (next): p-video B-roll per segment → chromakey user over B-roll →
    Remotion captions/zoom/infographics → final render → S3 → approve → publish.

Internal sub_steps (intent["_sub_step"]):
  awaiting_video      — waiting for the user to send their video
  planning            — matting + transcript + edit plan running
  awaiting_plan_ok     — plan shown, awaiting approval
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from session_store import UserSession, save_session, STEP_AGENT_REEL, STEP_CHOOSE_CONTENT_TYPE
from tools import aws_storage, video_gen, voice, groq_ai

logger = logging.getLogger(__name__)


def _send(phone: str, payload: dict, tts: bool = False) -> None:
    from workflow import _send_async
    _send_async(phone, payload, tts=tts)


def start(phone: str, session: UserSession, intent: dict) -> dict:
    intent["reel_type"] = "video_editor"
    intent["_sub_step"] = "awaiting_video"
    session.agent_intent = intent
    session.step = STEP_AGENT_REEL
    save_session(session)
    return {
        "kind": "text",
        "text": (
            "🎬 *AI Video Editor*\n\n"
            "Send me your talking video and I'll turn it into a trending cut — "
            "I'll remove your background, add B-roll behind you, punchy captions, and zoom effects.\n\n"
            "📹 *Send your video now.*"
        ),
    }


def handle_step(
    phone: str,
    session: UserSession,
    clean: str,
    button_payload: Optional[str],
    media_urls: list[str],
    media_types: list[str],
    voice_confirmed: bool,
) -> dict:
    intent   = session.agent_intent or {}
    sub_step = intent.get("_sub_step", "")
    low      = (button_payload or clean or "").strip().lower()

    if sub_step == "awaiting_video":
        has_video = bool(media_urls and any(t.startswith("video/") for t in media_types))
        if not has_video:
            return {"kind": "text", "text": "📹 Please send your talking video (a normal WhatsApp video)."}
        # Upload the video to S3
        user_id = session.verified_user_id or phone
        video_url = None
        for url, mt in zip(media_urls, media_types):
            if mt.startswith("video/"):
                up = aws_storage.upload_from_url(url, user_id=user_id, media_kind="editor_src")
                if up.get("ok"):
                    video_url = up["s3_url"]
                break
        if not video_url:
            return {"kind": "text", "text": "😕 Couldn't read that video — please send it again."}
        intent["_src_video_url"] = video_url
        intent["_sub_step"] = "planning"
        session.agent_intent = intent
        save_session(session)
        _send(phone, {"kind": "text",
                      "text": "🎬 Got your video! Analyzing it:\n• Removing your background (green screen)\n"
                              "• Transcribing what you say\n• Planning the trending cut\n⏱ ~2 minutes ☕"})
        threading.Thread(target=_plan_bg, args=(phone, session, intent), daemon=True).start()
        return {"kind": "none"}

    if sub_step == "planning":
        return {"kind": "text", "text": "⏳ Still analyzing your video — hang tight!"}

    if sub_step == "awaiting_plan_ok":
        if low in ("approve", "yes", "ok", "okay", "go", "looks good", "perfect", "make it", "continue"):
            _send(phone, {"kind": "text",
                          "text": "🎬 Great! Building the full edit next — B-roll, chromakey composite, "
                                  "captions and effects. (This stage is coming online — your green-screen "
                                  "video, transcript and edit plan are ready.)"})
            # STAGE 2+ hook: B-roll → chromakey → Remotion captions/overlays → render → publish
            return {"kind": "none"}
        if "regenerate" in low or "again" in low or "redo" in low:
            intent["_sub_step"] = "planning"
            session.agent_intent = intent
            save_session(session)
            threading.Thread(target=_plan_bg, args=(phone, session, intent), daemon=True).start()
            return {"kind": "none"}
        return {"kind": "text", "text": "Reply *approve* to build the video, or *regenerate* to re-plan."}

    return start(phone, session, intent)


def _plan_bg(phone: str, session: UserSession, intent: dict) -> None:
    try:
        src = intent.get("_src_video_url", "")
        brand = session.brand_profile() if session.onboarding_complete else {}

        # 1) Matting → green screen
        _send(phone, {"kind": "text", "text": "🟢 Removing your background..."})
        matte = video_gen.matte_video_greenscreen(src)
        if not matte.get("ok") or not matte.get("url"):
            raise RuntimeError(f"matting failed: {matte.get('error')}")
        # persist to S3
        import requests as _req
        gbytes = _req.get(matte["url"], timeout=120).content
        gup = aws_storage.upload_bytes(gbytes, content_type="video/mp4", extension="mp4",
                                       folder=f"{phone}/video_editor")
        intent["_greenscreen_video_url"] = gup.get("s3_url") or matte["url"]

        # 2) Transcript
        _send(phone, {"kind": "text", "text": "📝 Transcribing what you say..."})
        transcript = voice.transcribe_video_url(src) or ""
        intent["_transcript"] = transcript

        # 3) Duration (for planning)
        duration = _video_duration_seconds(src)
        intent["_duration"] = duration

        # 4) Edit plan
        _send(phone, {"kind": "text", "text": "🎬 Planning your trending cut..."})
        plan = groq_ai.generate_video_edit_plan(transcript, duration, brand)
        intent["_edit_plan"] = plan
        intent["_sub_step"] = "awaiting_plan_ok"
        session.agent_intent = intent
        save_session(session)

        # 5) Show the plan
        segs = plan.get("segments", [])
        lines = [f"🎬 *{plan.get('title','Your edit')}*", f"_{plan.get('story','')}_", ""]
        for i, s in enumerate(segs, 1):
            cap = s.get("caption") or ""
            zoom = s.get("zoom", "none")
            lines.append(f"*{i}.* {s.get('start',0):.0f}-{s.get('end',0):.0f}s · zoom:{zoom} · "
                         f"B-roll: _{(s.get('broll_prompt') or '')[:60]}_" + (f"\n   💬 {cap}" if cap else ""))
        lines.append(f"\n📣 {plan.get('cta','')}")
        lines.append("\n✅ Reply *approve* to build it, or *regenerate* to re-plan.")
        _send(phone, {"kind": "text", "text": "\n".join(lines)})
    except Exception as exc:
        logger.exception("video_editor _plan_bg failed: %s", exc)
        intent["_sub_step"] = "awaiting_video"
        session.agent_intent = intent
        save_session(session)
        _send(phone, {"kind": "text",
                      "text": f"😕 Couldn't analyze that video: {exc}\nSend a different video or type *reset*."})


def _video_duration_seconds(url: str) -> float:
    """Best-effort duration via moviepy; falls back to 15s."""
    try:
        import tempfile, os as _os, requests as _req
        from moviepy.editor import VideoFileClip
        tmp = tempfile.mktemp(suffix=".mp4")
        with open(tmp, "wb") as f:
            f.write(_req.get(url, timeout=120).content)
        clip = VideoFileClip(tmp)
        d = float(clip.duration or 15.0)
        clip.close(); _os.remove(tmp)
        return d
    except Exception:
        return 15.0
