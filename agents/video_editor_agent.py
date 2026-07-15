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


def _build_bg(phone: str, session: UserSession, intent: dict) -> None:
    """
    STAGE 2+3 (Remotion compositor):
      1. generate a B-roll clip per edit-plan segment (SeedDream seed → prunaai/p-video)
      2. turn the user's green-screen video into a TRANSPARENT WebM (green → alpha)
      3. Remotion composites: B-roll timeline (hard cuts + Ken Burns zoom) underneath,
         the transparent presenter on top, original audio, title card and captions.
    Produces a single studio-grade reel and sends it for post/regenerate/skip.
    """
    try:
        from tools import image_gen, remotion_render
        plan     = intent.get("_edit_plan", {}) or {}
        segments = plan.get("segments", []) or []
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

        # 1) B-roll clip per segment
        broll: list[dict] = []
        total = len(segments)
        for i, seg in enumerate(segments, 1):
            seg_dur = max(1, int(round(seg["end"] - seg["start"])))
            prompt  = (seg.get("broll_prompt") or "cinematic b-roll footage").strip()
            _send(phone, {"kind": "text", "text": f"🎥 Scene {i}/{total}: {prompt[:70]}…"})

            clip_url = None
            try:
                img = image_gen.generate_image(prompt, aspect_ratio="2:3")
                seed_url = (img.get("s3_url") or img.get("url")) if img.get("ok") else None
                if seed_url:
                    vid = video_gen.generate_video_from_image(
                        seed_url, prompt, duration=min(seg_dur, 10), aspect_ratio="9:16")
                    if vid.get("ok") and vid.get("url"):
                        clip_url = vid["url"]
            except Exception as exc:
                logger.warning("video_editor: broll scene %d failed: %s", i, exc)

            if clip_url:
                broll.append({"start": seg["start"], "end": seg["end"],
                              "src": clip_url, "zoom": seg.get("zoom", "none")})

        # 2) Transparent presenter (green → alpha WebM)
        _send(phone, {"kind": "text", "text": "🟢 Cutting you out onto a transparent background…"})
        tr = video_gen.greenscreen_to_transparent_webm(gs_url)
        if not tr.get("ok") or not tr.get("bytes"):
            raise RuntimeError(f"transparency failed: {tr.get('error')}")
        pu = aws_storage.upload_bytes(tr["bytes"], content_type="video/webm",
                                      extension="webm", folder=f"{phone}/video_editor")
        presenter_src = pu.get("s3_url") or pu.get("permanent_url")

        # 3) Caption track from the plan segments
        captions = []
        for s in segments:
            cap = (s.get("caption") or "").strip()
            if cap:
                captions.append({"start": s["start"], "end": max(s["start"] + 0.6, s["end"]),
                                 "text": cap, "emphasis": bool(s.get("emphasis"))})

        # 4) Remotion composite → final studio reel
        _send(phone, {"kind": "text", "text": "🎬 Compositing your studio reel — cuts, zooms and captions…"})
        out = remotion_render.render_reel(
            presenter_src=presenter_src,
            broll=broll,
            captions=captions,
            duration_sec=duration,
            audio_src=src_url,
            fps=24, width=1080, height=1920,
            title=(plan.get("title") or "").strip(),
            cta=(plan.get("cta") or "").strip(),
        )
        if not out.get("ok") or not out.get("bytes"):
            raise RuntimeError(out.get("error") or "remotion render returned nothing")

        up = aws_storage.upload_bytes(out["bytes"], content_type="video/mp4",
                                      extension="mp4", folder=f"{phone}/video_editor")
        final_url = up.get("s3_url") or up.get("permanent_url")
        intent["_final_video_url"] = final_url
        intent["_sub_step"] = "awaiting_publish"
        session.agent_intent = intent
        save_session(session)

        _send(phone, {"kind": "media", "media_url": final_url,
                      "text": "🎬 Your studio reel is ready — you cut out over dynamic B-roll with cuts, zooms and captions!"})
        _send(phone, {"kind": "text",
                      "text": "Reply:\n✅ *post now* — publish to Instagram\n🔄 *regenerate* — rebuild the reel\n"
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
