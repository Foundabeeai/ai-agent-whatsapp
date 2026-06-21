"""
UGC Presentation Video sub-agent.

A presenter (avatar or the user's own photo) talks to camera about a product/
property/service, with photos of it shown behind them (slideshow), auto-captioned.

Pipeline (built stage by stage):
  STAGE 1 (this file, implemented):
    - collect presenter (Maya / George avatar, or own photo) + a product/property link
      (+ optional clothes change)
    - scrape the link (Apify/Zillow/direct) → facts + high-quality photos
    - SeedDream → put presenter on a GREEN SCREEN (2:3), keeping identity
    - VLM gender → pick voice; write presentation script; ElevenLabs Flash → audio (S3)
    - send the green-screen image + script for approval
  STAGE 2+ (next): talking-head video (veed/fabric-1.0) → slideshow from photos →
    chromakey overlay → autocaption → upload → approve → publish.

Internal sub_steps (intent["_sub_step"]):
  awaiting_presenter   — choosing avatar / own photo
  awaiting_link        — waiting for the product/property link
  generating_assets    — building green-screen image + script + audio
  awaiting_assets_ok    — green-screen + script shown, awaiting approval
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

import config
import db
from session_store import UserSession, save_session, STEP_AGENT_REEL, STEP_CHOOSE_CONTENT_TYPE
from tools import groq_ai, image_gen, voice, aws_storage
from tools.url_context import find_all_urls, scrape_url

logger = logging.getLogger(__name__)


def _send(phone: str, payload: dict, tts: bool = False) -> None:
    from workflow import _send_async
    _send_async(phone, payload, tts=tts)


# ── Entry ───────────────────────────────────────────────────────────────────

def start(phone: str, session: UserSession, intent: dict) -> dict:
    intent["reel_type"] = "ugc_presentation"
    intent["_sub_step"] = "awaiting_presenter"
    session.agent_intent = intent
    session.step = STEP_AGENT_REEL
    save_session(session)
    return {
        "kind": "text",
        "text": (
            "🎥 *UGC Presentation Video* — here's how it works:\n"
            "  *Step 1* → pick who presents\n"
            "  *Step 2* → send the product/property link\n"
            "  Then I build everything (presenter + voice-over + photos behind them) and "
            "send it for your approval.\n\n"
            "*Step 1 — who should present?*\n"
            "1️⃣ *Maya* — female avatar\n"
            "2️⃣ *George* — male avatar\n"
            "📸 Or *send your own photo*\n\n"
            "_Tip: you can also change the outfit — e.g. \"me in a navy suit\"._"
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
    msg      = (button_payload or clean or "").strip()
    low      = msg.lower()

    # ── Choose presenter ───────────────────────────────────────────────────
    if sub_step == "awaiting_presenter":
        has_image = bool(media_urls and any(t.startswith("image/") for t in media_types))
        if has_image:
            # Own photo → upload, then ask about the outfit before generating
            user_id = session.verified_user_id or phone
            presenter_url = None
            for url, mt in zip(media_urls, media_types):
                if mt.startswith("image/"):
                    up = aws_storage.upload_from_url(url, user_id=user_id, media_kind="presenter")
                    if up.get("ok"):
                        presenter_url = up["s3_url"]
                    break
            if not presenter_url:
                return {"kind": "text", "text": "😕 Couldn't read that photo — please send it again."}
            intent["_presenter_url"] = presenter_url
            intent["_sub_step"] = "awaiting_outfit"
            session.agent_intent = intent
            save_session(session)
            return {"kind": "text",
                    "text": "📸 Got your photo!\n\n👔 *What outfit should you wear in the video?*\n"
                            "e.g. _\"a navy business suit\"_, _\"a casual white shirt\"_, "
                            "_\"a black blazer\"_…\n\nOr reply *same* to keep the outfit in your photo."}

        # Avatar choice
        presenter_url = None
        if "maya" in low or low in ("1", "1️⃣") or "female" in low:
            presenter_url = config.AVATAR_MAYA_URL
        elif "george" in low or low in ("2", "2️⃣") or "male" in low:
            presenter_url = config.AVATAR_GEORGE_URL
        if not presenter_url:
            return {"kind": "text",
                    "text": "Reply *1* (Maya), *2* (George), or send your own photo to present. 🎥"}
        intent["_presenter_url"] = presenter_url
        intent["_sub_step"] = "awaiting_link"
        session.agent_intent = intent
        save_session(session)
        return {"kind": "text",
                "text": "👍 Presenter set!\n\n*Step 2 — send the product/property link* 🔗\n"
                        "(e.g. your Zillow listing or store page). I'll pull the details and photos, "
                        "then build the whole video automatically."}

    # ── Outfit choice (own-photo path) ─────────────────────────────────────
    if sub_step == "awaiting_outfit":
        if msg and low not in ("same", "keep", "keep same", "no", "none", "as is", "as-is", "skip"):
            intent["_clothes_prompt"] = msg
        else:
            intent["_clothes_prompt"] = ""   # keep their real outfit
        intent["_sub_step"] = "awaiting_link"
        session.agent_intent = intent
        save_session(session)
        outfit_note = (f"👔 Outfit: _{intent['_clothes_prompt']}_\n\n"
                       if intent.get("_clothes_prompt") else "👔 Keeping your original outfit.\n\n")
        return {"kind": "text",
                "text": outfit_note + "*Step 2 — send the product/property link* 🔗\n"
                        "(Zillow listing, store page, etc.) — I'll pull the details and photos."}

    # ── Collect the link, then build Stage-1 assets ────────────────────────
    if sub_step == "awaiting_link":
        urls = find_all_urls(msg) if msg else []
        if not urls:
            return {"kind": "text", "text": "🔗 Please paste the product/property link to continue."}
        intent["_sub_step"] = "generating_assets"
        intent["_link"] = urls[0]
        session.agent_intent = intent
        save_session(session)
        _send(phone, {"kind": "text",
                      "text": "⚙️ Building your presentation:\n• Pulling the details & photos\n"
                              "• Placing your presenter on a green screen\n• Writing the script & voice-over\n"
                              "⏱ ~2 minutes ☕"})
        threading.Thread(target=_build_stage1, args=(phone, session, intent), daemon=True).start()
        return {"kind": "none"}

    if sub_step == "generating_assets":
        return {"kind": "text", "text": "⏳ Still building your presentation — hang tight!"}

    # ── Approve the script/voice → build the full video ────────────────────
    if sub_step == "awaiting_assets_ok":
        if low in ("approve", "yes", "ok", "okay", "go", "looks good", "perfect", "next", "continue"):
            intent["_sub_step"] = "building_video"
            session.agent_intent = intent
            save_session(session)
            _send(phone, {"kind": "text",
                          "text": "🎬 Building your video — talking presenter + photo slideshow + captions.\n"
                                  "⏱ This takes a few minutes (the lip-sync step is the slow part). I'll send it when ready."})
            threading.Thread(target=_build_full_video, args=(phone, session, intent), daemon=True).start()
            return {"kind": "none"}
        if "regenerate" in low or "again" in low:
            intent["_sub_step"] = "generating_assets"
            session.agent_intent = intent
            save_session(session)
            threading.Thread(target=_build_stage1, args=(phone, session, intent), daemon=True).start()
            return {"kind": "none"}
        return {"kind": "text", "text": "Reply *approve* to build the video, or *regenerate* to redo. 🎥"}

    if sub_step == "building_video":
        return {"kind": "text", "text": "⏳ Still rendering your video — hang tight, almost there!"}

    # ── Final video approval → publish ─────────────────────────────────────
    if sub_step == "awaiting_final_publish":
        if low in ("post", "post now", "publish", "yes", "approve", "go", "now", "ok", "okay"):
            threading.Thread(target=_publish_final, args=(phone, session, intent), daemon=True).start()
            return {"kind": "text", "text": "📤 Publishing your reel to Instagram..."}
        if low in ("skip", "no", "later", "cancel"):
            session.step = STEP_CHOOSE_CONTENT_TYPE
            session.agent_intent = None
            save_session(session)
            return {"kind": "text", "text": "👍 Saved as draft. Type *create* anytime."}
        if "regenerate" in low or "again" in low:
            intent["_sub_step"] = "building_video"
            session.agent_intent = intent
            save_session(session)
            threading.Thread(target=_build_full_video, args=(phone, session, intent), daemon=True).start()
            return {"kind": "text", "text": "🔄 Rebuilding the video..."}
        return {"kind": "text", "text": "Reply *post now* to publish, *regenerate* to rebuild, or *skip*."}

    # Unknown → restart presenter selection
    return start(phone, session, intent)


# ── Stages 2-5: full video build ─────────────────────────────────────────────

def _build_full_video(phone: str, session: UserSession, intent: dict) -> None:
    from tools import video_gen
    try:
        greenscreen_url = intent.get("_greenscreen_url", "")
        audio_url       = intent.get("_audio_url", "")
        photos          = intent.get("_scraped_photos", [])
        if not greenscreen_url or not audio_url:
            raise RuntimeError("missing green-screen image or voice-over")

        # Stage 2 — talking-head lip-sync (veed/fabric-1.0)
        _send(phone, {"kind": "text", "text": "🗣 Step 1/3 — animating your presenter (lip-sync)..."})
        lip = video_gen.generate_lipsync_video(greenscreen_url, audio_url, resolution="720p")
        if not lip.get("ok") or not lip.get("bytes"):
            raise RuntimeError(f"lip-sync failed: {lip.get('error')}")

        # Stage 3+4 — slideshow behind + chromakey the presenter on top
        _send(phone, {"kind": "text", "text": "🖼 Step 2/3 — placing you in front of the property photos..."})
        comp = video_gen.compose_presentation_video(lip["bytes"], photos)
        if not comp.get("ok") or not comp.get("bytes"):
            raise RuntimeError(f"composition failed: {comp.get('error')}")

        # Stage 5 — burned-in captions (tiktok-short-captions, brand highlight)
        _send(phone, {"kind": "text", "text": "💬 Step 3/3 — adding captions..."})
        captioned = video_gen.add_tiktok_captions(comp["bytes"], highlight_color="#FCD738")
        if captioned:
            final_bytes = captioned
        else:
            final_bytes = comp["bytes"]
            _send(phone, {"kind": "text",
                          "text": "⚠️ Captioning step didn't return a result this time — sending the "
                                  "video without captions. (We'll see why in the logs.)"})

        # Upload final video
        up = aws_storage.upload_bytes(final_bytes, content_type="video/mp4",
                                      extension="mp4", folder=f"{phone}/ugc_presentation")
        final_url = up.get("s3_url")
        if not final_url:
            raise RuntimeError("final upload failed")
        intent["_final_video_url"] = final_url
        intent["_sub_step"] = "awaiting_final_publish"
        session.agent_intent = intent
        save_session(session)

        _send(phone, {"kind": "media", "text": "🎬 Your UGC presentation reel is ready!",
                      "media_url": final_url})
        _send(phone, {"kind": "text",
                      "text": "Reply:\n✅ *post now* — publish to Instagram\n🔄 *regenerate* — rebuild\n⏭ *skip* — save as draft"})
    except Exception as exc:
        logger.exception("ugc_presentation _build_full_video failed: %s", exc)
        intent["_sub_step"] = "awaiting_assets_ok"
        session.agent_intent = intent
        save_session(session)
        _send(phone, {"kind": "text",
                      "text": f"😕 Video build failed: {exc}\nReply *approve* to try again or *regenerate*."})


def _publish_final(phone: str, session: UserSession, intent: dict) -> None:
    from tools import zerini
    try:
        final_url = intent.get("_final_video_url", "")
        caption   = intent.get("_script", "")
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
            _send(phone, {"kind": "text", "text": "✅ *Posted to Instagram!* 🎉 What's next?"}, tts=True)
        else:
            _send(phone, {"kind": "text", "text": f"😕 Publish failed: {result.get('error')}"})
    except Exception as exc:
        logger.exception("ugc_presentation _publish_final failed: %s", exc)
        _send(phone, {"kind": "text", "text": f"😕 Publish failed: {exc}"})


# ── Stage 1 background worker ────────────────────────────────────────────────

def _build_stage1(phone: str, session: UserSession, intent: dict) -> None:
    try:
        presenter_url  = intent.get("_presenter_url", "")
        link           = intent.get("_link", "")
        clothes_prompt = intent.get("_clothes_prompt", "")
        brand          = session.brand_profile() if session.onboarding_complete else {}

        # 1) Scrape the link for facts + photos
        ctx = scrape_url(link, phone)
        facts  = ctx.get("summary", "") if ctx.get("ok") else ""
        photos = ctx.get("image_urls", []) if ctx.get("ok") else []
        if not facts and not photos:
            _send(phone, {"kind": "text",
                          "text": "⚠️ I couldn't pull details from that link. Send a different link or a few photos."})
            intent["_sub_step"] = "awaiting_link"
            session.agent_intent = intent
            save_session(session)
            return
        intent["_scraped_photos"]  = photos
        intent["_scraped_facts"]   = facts

        # 2) Green-screen presenter — true 9:16, no stretching (keeps identity)
        _send(phone, {"kind": "text", "text": "🟢 Placing your presenter on a green screen (9:16)..."})
        gs = image_gen.generate_greenscreen_portrait(
            presenter_url, clothes_prompt=clothes_prompt, user_id=phone)
        if not gs.get("ok") or not gs.get("url"):
            raise RuntimeError(f"green-screen generation failed: {gs.get('error')}")
        intent["_greenscreen_url"] = gs["url"]   # already a 9:16 S3 URL

        # 3) Gender → voice, write script, synth audio
        gender = groq_ai.detect_gender_from_image(presenter_url)
        voice_id = "Paul" if gender == "male" else "Rachel"
        script = groq_ai.generate_presentation_script(facts or session.brand_description or link,
                                                      brand, target_seconds=20)
        intent["_script"] = script
        _send(phone, {"kind": "text", "text": "🎙 Recording the voice-over..."})
        audio_url = voice.synthesize_and_upload(script, voice_id=voice_id)
        intent["_audio_url"] = audio_url
        intent["_voice_gender"] = gender

        # 4) Deliver Stage-1 assets for approval
        intent["_sub_step"] = "awaiting_assets_ok"
        session.agent_intent = intent
        save_session(session)

        # (Green-screen image is an internal asset — not shown in chat.)
        if audio_url:
            _send(phone, {"kind": "media", "text": "🎙 Voice-over", "media_url": audio_url})
        _send(phone, {"kind": "text",
                      "text": f"📝 *Script:*\n_{script}_\n\n"
                              f"✅ Reply *approve* to build the full video\n🔄 or *regenerate*"})
    except Exception as exc:
        logger.exception("ugc_presentation _build_stage1 failed: %s", exc)
        _send(phone, {"kind": "text", "text": f"😕 Something went wrong building the presentation: {exc}\n"
                                              "Type *reset* to start over."})
