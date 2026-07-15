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

    if sub_step == "awaiting_final_ok":
        if low in ("approve", "yes", "ok", "okay", "go", "looks good", "perfect", "continue"):
            intent["_sub_step"] = "captioning"
            session.agent_intent = intent
            save_session(session)
            _send(phone, {"kind": "text",
                          "text": "✨ Adding punchy captions and a title card now (Remotion)…\n⏱ ~2-3 min ☕"})
            threading.Thread(target=_caption_bg, args=(phone, session, intent), daemon=True).start()
            return {"kind": "none"}
        if "regenerate" in low or "again" in low or "redo" in low or "rebuild" in low:
            intent["_sub_step"] = "building"
            session.agent_intent = intent
            save_session(session)
            _send(phone, {"kind": "text", "text": "🔁 Rebuilding your edit with fresh B-roll…"})
            threading.Thread(target=_build_bg, args=(phone, session, intent), daemon=True).start()
            return {"kind": "none"}
        return {"kind": "text", "text": "Reply *approve* to add captions, or *regenerate* to rebuild the B-roll."}

    if sub_step == "captioning":
        return {"kind": "text", "text": "⏳ Rendering captions — hang tight, almost there!"}

    if sub_step == "awaiting_publish":
        if "regenerate" in low or "again" in low or "redo" in low:
            intent["_sub_step"] = "captioning"
            session.agent_intent = intent
            save_session(session)
            _send(phone, {"kind": "text", "text": "🔁 Re-rendering the captioned cut…"})
            threading.Thread(target=_caption_bg, args=(phone, session, intent), daemon=True).start()
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
                "text": "Reply *post now* to publish to Instagram, *regenerate* to re-render captions, or *skip* to save as draft."}

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
    STAGE 2: for each edit-plan segment, generate a B-roll clip (SeedDream seed image
    → prunaai/p-video), then chroma-key the user's green-screen video over the B-roll
    timeline with per-segment zoom. Sends the composited video back for review.
    """
    try:
        from tools import image_gen
        plan     = intent.get("_edit_plan", {}) or {}
        segments = plan.get("segments", []) or []
        gs_url   = intent.get("_greenscreen_video_url", "")
        src_url  = intent.get("_src_video_url", "")
        duration = float(intent.get("_duration") or 15.0)
        user_id  = session.verified_user_id or phone

        if not gs_url or not segments:
            raise RuntimeError("missing green-screen video or edit plan")

        # Clamp segment times to the real video length
        for s in segments:
            s["start"] = max(0.0, min(float(s.get("start", 0)), duration))
            s["end"]   = max(s["start"] + 0.5, min(float(s.get("end", duration)), duration))
        segments.sort(key=lambda s: s["start"])

        broll: list[dict] = []
        total = len(segments)
        for i, seg in enumerate(segments, 1):
            seg_dur = max(1, int(round(seg["end"] - seg["start"])))
            prompt  = (seg.get("broll_prompt") or "cinematic b-roll footage").strip()
            _send(phone, {"kind": "text", "text": f"🎥 Scene {i}/{total}: {prompt[:70]}…"})

            clip_url = None
            try:
                img = image_gen.generate_image(prompt, aspect_ratio="2:3")
                seed_url = img.get("s3_url") or img.get("url") if img.get("ok") else None
                if seed_url:
                    vid = video_gen.generate_video_from_image(
                        seed_url, prompt, duration=min(seg_dur, 10), aspect_ratio="9:16")
                    if vid.get("ok") and vid.get("url"):
                        clip_url = vid["url"]
            except Exception as exc:
                logger.warning("video_editor: broll scene %d failed: %s", i, exc)

            broll.append({"start": seg["start"], "end": seg["end"],
                          "url": clip_url, "zoom": seg.get("zoom", "none")})

        # Drop segments whose B-roll failed (compositor uses dark filler for missing urls)
        broll = [b for b in broll if b.get("url")] or broll

        _send(phone, {"kind": "text", "text": "🧩 Compositing you over the B-roll…"})
        comp = video_gen.compose_editor_video(gs_url, src_url or gs_url, broll)
        if not comp.get("ok") or not comp.get("bytes"):
            raise RuntimeError(f"composite failed: {comp.get('error')}")

        up = aws_storage.upload_bytes(comp["bytes"], content_type="video/mp4",
                                      extension="mp4", folder=f"{phone}/video_editor")
        final_url = up.get("s3_url") or up.get("permanent_url")
        intent["_edited_video_url"] = final_url
        intent["_sub_step"] = "awaiting_final_ok"
        session.agent_intent = intent
        save_session(session)

        _send(phone, {"kind": "media", "media_url": final_url,
                      "text": "🎬 Here's your edited cut — you over AI B-roll with zoom effects!\n\n"
                              "Next I'll add punchy captions & overlays. Reply *approve* if you like the base, "
                              "or *regenerate* to rebuild the B-roll."})
    except Exception as exc:
        logger.exception("video_editor _build_bg failed: %s", exc)
        intent["_sub_step"] = "awaiting_plan_ok"
        session.agent_intent = intent
        save_session(session)
        _send(phone, {"kind": "text",
                      "text": f"😕 Couldn't build the edit: {exc}\nReply *approve* to try again or *regenerate* to re-plan."})


def _caption_bg(phone: str, session: UserSession, intent: dict) -> None:
    """
    STAGE 3: render trending captions + title card over the Stage 2 base cut
    using the Node/Remotion project, then send the final trending cut for review.
    """
    try:
        from tools import remotion_render
        plan     = intent.get("_edit_plan", {}) or {}
        base_url = intent.get("_edited_video_url", "")
        duration = float(intent.get("_duration") or 15.0)
        if not base_url:
            raise RuntimeError("no base cut to caption")

        # Caption track from the edit-plan segments (already timed + emphasis flags)
        captions = []
        for s in plan.get("segments", []) or []:
            cap = (s.get("caption") or "").strip()
            if not cap:
                continue
            captions.append({
                "start": max(0.0, float(s.get("start", 0))),
                "end":   max(float(s.get("start", 0)) + 0.6, float(s.get("end", duration))),
                "text":  cap,
                "emphasis": bool(s.get("emphasis")),
            })

        out = remotion_render.render_captions(
            video_src=base_url,
            captions=captions,
            duration_sec=duration,
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
                      "text": "🎬 Your trending cut is ready — B-roll, chroma-key, zoom cuts and captions!"})
        _send(phone, {"kind": "text",
                      "text": "Reply:\n✅ *post now* — publish to Instagram\n🔄 *regenerate* — re-render captions\n"
                              "⏭ *skip* — save as draft"})
    except Exception as exc:
        logger.exception("video_editor _caption_bg failed: %s", exc)
        intent["_sub_step"] = "awaiting_final_ok"
        session.agent_intent = intent
        save_session(session)
        # Fall back to the un-captioned base cut so the user still has a usable video.
        base_url = intent.get("_edited_video_url")
        if base_url:
            _send(phone, {"kind": "media", "media_url": base_url,
                          "text": f"⚠️ Caption render hit a snag ({exc}). Here's your edit without captions.\n"
                                  "Reply *approve* to retry captions or *regenerate* to rebuild."})
        else:
            _send(phone, {"kind": "text", "text": f"😕 Caption render failed: {exc}\nReply *approve* to retry."})


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
