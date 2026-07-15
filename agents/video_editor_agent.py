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

    if sub_step == "building":
        return {"kind": "text", "text": "⏳ Still building your edit — B-roll takes a few minutes. I'll send it when ready!"}

    if sub_step == "awaiting_plan_ok":
        if low in ("approve", "yes", "ok", "okay", "go", "looks good", "perfect", "make it", "continue"):
            intent["_sub_step"] = "building"
            session.agent_intent = intent
            save_session(session)
            _send(phone, {"kind": "text",
                          "text": "🎬 Building your edit! Generating B-roll for each scene and compositing "
                                  "you over it with zoom cuts.\n⏱ This takes a few minutes — I'll send it when it's ready ☕"})
            threading.Thread(target=_build_bg, args=(phone, session, intent), daemon=True).start()
            return {"kind": "none"}
        if "regenerate" in low or "again" in low or "redo" in low:
            intent["_sub_step"] = "planning"
            session.agent_intent = intent
            save_session(session)
            threading.Thread(target=_plan_bg, args=(phone, session, intent), daemon=True).start()
            return {"kind": "none"}
        return {"kind": "text", "text": "Reply *approve* to build the video, or *regenerate* to re-plan."}

    if sub_step == "awaiting_publish":
        if "regenerate" in low or "again" in low or "redo" in low or "rebuild" in low:
            intent["_sub_step"] = "building"
            session.agent_intent = intent
            save_session(session)
            _send(phone, {"kind": "text", "text": "🔁 Rebuilding your reel with fresh B-roll…"})
            threading.Thread(target=_build_bg, args=(phone, session, intent), daemon=True).start()
            return {"kind": "none"}
        if low in ("skip", "later", "draft", "no"):
            session.step = STEP_CHOOSE_CONTENT_TYPE
            session.agent_intent = None
            save_session(session)
            return {"kind": "text", "text": "👍 Saved as a draft. Type *create* whenever you want to make more."}
        if low in ("approve", "yes", "ok", "okay", "perfect", "love it", "done", "great",
                   "post", "post now", "publish", "go"):
            intent["_sub_step"] = "publishing"
            session.agent_intent = intent
            save_session(session)
            threading.Thread(target=_publish_bg, args=(phone, session, intent), daemon=True).start()
            return {"kind": "text", "text": "📤 Publishing your reel to Instagram…"}
        return {"kind": "text",
                "text": "Reply *post now* to publish to Instagram, *regenerate* to rebuild the reel, or *skip* to save as draft."}

    if sub_step == "publishing":
        return {"kind": "text", "text": "⏳ Publishing in progress — one moment!"}

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

        # 2) Transcript + word-level timing (for kinetic captions)
        _send(phone, {"kind": "text", "text": "📝 Transcribing what you say..."})
        transcript, words = voice.transcribe_video_words(src)
        if not transcript:
            transcript = voice.transcribe_video_url(src) or ""
        intent["_transcript"] = transcript
        intent["_words"] = words

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


_SCENE_CYCLE = ["grid", "solid", "cardboard", "split", "solid"]
_SOLID_COLORS = ["#E7B10A", "#7A1F2B", "#2E6E8E", "#C24914"]   # yellow, maroon, blue, orange


def _plan_to_scenes(segments: list[dict], duration: float) -> list[dict]:
    """
    Turn the edit-plan segments into designed Hormozi-style scenes:
      - backgrounds cycle grid → solid → cardboard → split
      - grid scenes get the inward arrow ring + lens; cardboard scenes get the
        sticker presenter + scribble circle; solid/split scenes get big-text-behind
      - emphasis segments punch-zoom
    """
    scenes: list[dict] = []
    solid_i = 0
    for i, seg in enumerate(segments):
        bg = _SCENE_CYCLE[i % len(_SCENE_CYCLE)]
        emphasis = bool(seg.get("emphasis"))
        cap = (seg.get("caption") or "").strip()
        big_words = " ".join(cap.split()[:2]).upper() if cap else ""

        scene = {
            "start": seg["start"], "end": seg["end"],
            "bg": bg,
            "presenter": "sticker" if bg == "cardboard" else "full",
            "doodle": "arrows" if bg == "grid" else ("circle" if bg == "cardboard" else "none"),
            "lens": bg == "grid",
            "zoom": "punch" if emphasis else "none",
            "emphasis": emphasis,
            "bigText": big_words if bg in ("solid", "split") else "",
            "color": "", "color2": "",
        }
        if bg == "solid":
            scene["color"] = _SOLID_COLORS[solid_i % len(_SOLID_COLORS)]
            solid_i += 1
        elif bg == "split":
            scene["color"] = "#EDE6D6"
            scene["color2"] = "#E7B10A"
        scenes.append(scene)
    return scenes


def _build_bg(phone: str, session: UserSession, intent: dict) -> None:
    """
    Build the Hormozi-style talking-head reel (all in Remotion):
      1. turn the user's green-screen video into a TRANSPARENT WebM (green → alpha)
      2. map the edit-plan segments to designed SCENES (grid / cardboard / solid /
         split backgrounds, big-text-behind, doodles, lens, zoom punch-ins)
      3. Remotion renders: designed backgrounds + presenter (full / sticker) +
         kinetic word captions + doodles + clean cut transitions + audio.
    Produces a single production-grade reel and sends it for post/regenerate/skip.
    """
    try:
        from tools import remotion_render
        plan     = intent.get("_edit_plan", {}) or {}
        segments = plan.get("segments", []) or []
        words    = intent.get("_words", []) or []
        gs_url   = intent.get("_greenscreen_video_url", "")
        src_url  = intent.get("_src_video_url", "")
        duration = float(intent.get("_duration") or 15.0)

        if not gs_url or not segments:
            raise RuntimeError("missing green-screen video or edit plan")

        # Clamp segment times to the real video length
        for s in segments:
            s["start"] = max(0.0, min(float(s.get("start", 0)), duration))
            s["end"]   = max(s["start"] + 0.5, min(float(s.get("end", duration)), duration))
        segments.sort(key=lambda s: s["start"])

        # 1) Transparent presenter (green → alpha WebM)
        _send(phone, {"kind": "text", "text": "🟢 Cutting you out onto a transparent background…"})
        tr = video_gen.greenscreen_to_transparent_webm(gs_url)
        if not tr.get("ok") or not tr.get("bytes"):
            raise RuntimeError(f"transparency failed: {tr.get('error')}")
        pu = aws_storage.upload_bytes(tr["bytes"], content_type="video/webm",
                                      extension="webm", folder=f"{phone}/video_editor")
        presenter_src = pu.get("s3_url") or pu.get("permanent_url")

        # 2) Map segments → designed scenes
        scenes = _plan_to_scenes(segments, duration)

        # 3) Remotion render → final reel
        _send(phone, {"kind": "text", "text": "🎬 Designing your reel — backgrounds, captions, doodles and cuts…"})
        out = remotion_render.render_reel(
            presenter_src=presenter_src,
            scenes=scenes,
            words=words,
            duration_sec=duration,
            audio_src=src_url,
            fps=24, width=1080, height=1920,
            caption_pos="bottom",
        )
        if not out.get("ok") or not out.get("bytes"):
            raise RuntimeError(out.get("error") or "remotion render returned nothing")

        final_bytes = out["bytes"]
        logger.info("video_editor: final reel %.1f MB", len(final_bytes) / 1e6)
        # WhatsApp media cap is 16 MB — compress if we're over so it actually sends.
        if len(final_bytes) > 15 * 1024 * 1024:
            _send(phone, {"kind": "text", "text": "📦 Compressing for WhatsApp…"})
            final_bytes = video_gen.compress_for_whatsapp(final_bytes) or final_bytes
            logger.info("video_editor: compressed reel %.1f MB", len(final_bytes) / 1e6)

        up = aws_storage.upload_bytes(final_bytes, content_type="video/mp4",
                                      extension="mp4", folder=f"{phone}/video_editor")
        final_url = up.get("s3_url") or up.get("permanent_url")
        if not final_url:
            raise RuntimeError(f"S3 upload failed: {up.get('error')}")
        intent["_final_video_url"] = final_url
        intent["_sub_step"] = "awaiting_publish"
        session.agent_intent = intent
        save_session(session)

        # Deliver the video; always follow with the link as text so nothing is lost
        # even if Twilio rejects the media (size/type).
        _send(phone, {"kind": "media", "media_url": final_url,
                      "text": "🎬 Your studio reel is ready — you cut out over dynamic B-roll with cuts, zooms and captions!"})
        _send(phone, {"kind": "text",
                      "text": f"🔗 Direct link (if the video didn't load): {final_url}\n\n"
                              "Reply:\n✅ *post now* — publish to Instagram\n🔄 *regenerate* — rebuild the reel\n"
                              "⏭ *skip* — save as draft"})
    except Exception as exc:
        logger.exception("video_editor _build_bg failed: %s", exc)
        intent["_sub_step"] = "awaiting_plan_ok"
        session.agent_intent = intent
        save_session(session)
        _send(phone, {"kind": "text",
                      "text": f"😕 Couldn't build the reel: {exc}\nReply *approve* to try again or *regenerate* to re-plan."})


def _publish_bg(phone: str, session: UserSession, intent: dict) -> None:
    """STAGE 4: publish the final trending cut to Instagram via Zerini."""
    import db
    from tools import zerini
    try:
        plan      = intent.get("_edit_plan", {}) or {}
        final_url = intent.get("_final_video_url", "")
        if not final_url:
            raise RuntimeError("no final video to publish")

        # Caption: the plan's story + CTA, or fall back to the transcript.
        caption = "\n\n".join(p for p in [
            (plan.get("story") or "").strip(),
            (plan.get("cta") or "").strip(),
        ] if p) or (intent.get("_transcript") or "").strip()[:500]

        result = zerini.publish_now(
            account_id=session.zerini_account_id or "",
            image_urls=[final_url],
            caption=caption,
            content_type="reel",
            profile_id=session.zerini_profile_id or "",
        )
        db.log_post(phone_number=phone, content_type="reel", image_urls=[final_url],
                    caption=caption, prompts=[], status="published" if result.get("ok") else "failed")

        session.step = STEP_CHOOSE_CONTENT_TYPE
        session.agent_intent = None
        save_session(session)
        if result.get("ok"):
            _send(phone, {"kind": "text", "text": "✅ *Posted to Instagram!* 🎉 What would you like to create next?"}, tts=True)
        else:
            _send(phone, {"kind": "text", "text": f"😕 Publish failed: {result.get('error')}\nYour video is saved — try again anytime."})
    except Exception as exc:
        logger.exception("video_editor _publish_bg failed: %s", exc)
        intent["_sub_step"] = "awaiting_publish"
        session.agent_intent = intent
        save_session(session)
        _send(phone, {"kind": "text", "text": f"😕 Publish failed: {exc}\nReply *post now* to try again or *skip* to save as draft."})


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
