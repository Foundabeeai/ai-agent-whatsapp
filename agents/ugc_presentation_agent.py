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
        presenter_url = None
        has_image = bool(media_urls and any(t.startswith("image/") for t in media_types))
        if has_image:
            user_id = session.verified_user_id or phone
            for url, mt in zip(media_urls, media_types):
                if mt.startswith("image/"):
                    up = aws_storage.upload_from_url(url, user_id=user_id, media_kind="presenter")
                    if up.get("ok"):
                        presenter_url = up["s3_url"]
                    break
        elif "maya" in low or low in ("1", "1️⃣") or "female" in low:
            presenter_url = config.AVATAR_MAYA_URL
        elif "george" in low or low in ("2", "2️⃣") or "male" in low:
            presenter_url = config.AVATAR_GEORGE_URL

        # Capture an optional clothes-change instruction from free text
        if msg and not has_image and low not in ("1", "2", "maya", "george"):
            intent["_clothes_prompt"] = msg

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

    # ── Approve the green-screen + script ──────────────────────────────────
    if sub_step == "awaiting_assets_ok":
        if low in ("approve", "yes", "ok", "okay", "go", "looks good", "perfect", "next", "continue"):
            _send(phone, {"kind": "text",
                          "text": "🎬 Great! Building the talking video + slideshow next... (this stage is "
                                  "coming online — your green-screen image, script and voice-over are ready)."})
            # STAGE 2+ hook: talking video → slideshow → chromakey → captions → publish
            return {"kind": "none"}
        if "regenerate" in low or "again" in low:
            intent["_sub_step"] = "generating_assets"
            session.agent_intent = intent
            save_session(session)
            threading.Thread(target=_build_stage1, args=(phone, session, intent), daemon=True).start()
            return {"kind": "none"}
        return {"kind": "text", "text": "Reply *approve* to continue, or *regenerate* to redo. 🎥"}

    # Unknown → restart presenter selection
    return start(phone, session, intent)


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
