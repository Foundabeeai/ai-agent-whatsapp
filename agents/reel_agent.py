"""
Reel Sub-Agent

Three reel types (cinematic / ugc / ad), smart flow:
  - cinematic: product image → art director → SeedDream → video pipeline
  - ugc:       description → AI script → voice → user photo → compose
  - ad:        product image → AI script → voice → avatar → compose

Only asks for what the intent didn't already resolve.
Voice-confirmed responses use TTS.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import db
from session_store import (
    UserSession,
    get_session,
    save_session,
    STEP_AGENT_REEL,
    STEP_REEL_PRODUCT_IMAGE,
    STEP_REEL_DESCRIBE_PRODUCT,
    STEP_REEL_UGC_DESCRIBE,
    STEP_REEL_AD_PRODUCT_IMAGE,
    STEP_REEL_AD_DESCRIBE,
)
from tools import groq_ai, aws_storage

logger = logging.getLogger(__name__)

_REEL_TYPES = {"cinematic", "ugc", "ad"}


def _send(phone: str, payload: dict, tts: bool = False) -> None:
    from workflow import _send_async
    _send_async(phone, payload, tts=tts)


# ── Entry point ───────────────────────────────────────────────────────────

def start(phone: str, session: UserSession, intent: dict) -> dict:
    """
    Harness routes here when content_type == 'reel'.
    If reel_type is already known, jump straight to the right sub-flow.
    Otherwise ask once, warmly.
    """
    reel_type   = intent.get("reel_type")
    description = intent.get("description", "").strip()
    media_urls  = intent.get("_media_urls", [])
    media_types = intent.get("_media_types", [])
    voice_ok    = intent.get("_voice_confirmed", False)
    has_image   = bool(media_urls and any(t.startswith("image/") for t in media_types))

    # UGC Presentation Video is its own multi-stage sub-agent
    if reel_type == "ugc_presentation":
        from agents import ugc_presentation_agent
        return ugc_presentation_agent.start(phone, session, intent)

    # AI Video Editor (user uploads their own video to be edited)
    if reel_type == "video_editor":
        from agents import video_editor_agent
        return video_editor_agent.start(phone, session, intent)

    if not reel_type or reel_type not in _REEL_TYPES:
        intent["_sub_step"] = "awaiting_reel_type"
        session.agent_intent = intent
        session.step = STEP_AGENT_REEL
        save_session(session)
        return {
            "kind": "text",
            "text": (
                "🎬 What kind of reel?\n\n"
                "1️⃣ *Cinematic* — product video, premium/editorial look\n"
                "2️⃣ *UGC* — talking-head, authentic creator style\n"
                "3️⃣ *Ad* — scripted advertisement with avatar\n\n"
                "Reply *1*, *2*, or *3* — or just say the style you want!"
            ),
        }

    return _start_reel_type(phone, session, intent, reel_type, description, has_image,
                            media_urls, media_types, voice_ok)


def _start_reel_type(
    phone: str,
    session: UserSession,
    intent: dict,
    reel_type: str,
    description: str,
    has_image: bool,
    media_urls: list[str],
    media_types: list[str],
    voice_ok: bool,
) -> dict:
    """Bridge: pre-fill session fields then hand off to existing reel workflow handlers."""
    # Pre-fill session from intent so the legacy reel handlers work correctly
    session.content_type = "reel"
    session.reel_type    = reel_type
    if description:
        session.reel_product_description = description
        session.description = description
    session.agent_intent = intent

    if reel_type == "cinematic":
        if has_image:
            # Upload immediately then jump to describe step
            threading.Thread(
                target=_upload_and_advance_cinematic,
                args=(phone, session, intent, media_urls, media_types, voice_ok),
                daemon=True,
            ).start()
            _send(phone, {"kind": "text", "text": "🎬 *Cinematic reel* — uploading your product image..."})
            return {"kind": "none"}

        if session.reference_image_url:
            session.reel_product_image_url = session.reference_image_url
            save_session(session)
            # Jump to description step using existing workflow
            session.step = STEP_REEL_DESCRIBE_PRODUCT
            save_session(session)
            _send(phone, {
                "kind": "text",
                "text": (
                    f"🎬 *Cinematic reel!*\n"
                    f"Using your brand image as the base.\n\n"
                    "Describe your product/service in a sentence or two:"
                ),
            }, tts=voice_ok)
            return {"kind": "none"}

        session.step = STEP_REEL_PRODUCT_IMAGE
        save_session(session)
        _send(phone, {
            "kind": "text",
            "text": "🎬 *Cinematic reel!* Send your product or brand image to get started.",
        }, tts=voice_ok)
        return {"kind": "none"}

    if reel_type == "ugc":
        # Safety net: a talking reel ABOUT a product/property (or with a link / "show/
        # background" intent) belongs in the presentation flow, not the plain talking-head.
        _desc_low = f"{description} {intent.get('style_notes','')}".lower()
        _presentation_signals = ("property", "listing", "real estate", "house", "home",
                                 "product", "for sale", "background", "behind", "show",
                                 "showcase", "presenting", "presentation", "zillow", "http")
        if description and any(s in _desc_low for s in _presentation_signals):
            from agents import ugc_presentation_agent
            return ugc_presentation_agent.start(phone, session, intent)

        if description:
            # Pre-fill description and send user to UGC describe step.
            # Workflow.py's STEP_REEL_UGC_DESCRIBE handler will use the description
            # already set on the session and generate the script inline.
            session.reel_product_description = description
            session.step = STEP_REEL_UGC_DESCRIBE
            save_session(session)
            _send(phone, {
                "kind": "text",
                "text": f"🎤 *UGC reel!* Based on: _{description}_\n\nConfirm this topic or add more detail:",
            }, tts=voice_ok)
            return {"kind": "none"}

        session.step = STEP_REEL_UGC_DESCRIBE
        save_session(session)
        _send(phone, {
            "kind": "text",
            "text": "🎤 *UGC reel!* Tell me about your product/service — I'll write the script:",
        }, tts=voice_ok)
        return {"kind": "none"}

    if reel_type == "ad":
        if has_image:
            threading.Thread(
                target=_upload_and_advance_ad,
                args=(phone, session, intent, media_urls, media_types, voice_ok),
                daemon=True,
            ).start()
            _send(phone, {"kind": "text", "text": "📢 *Ad reel!* Uploading your image..."})
            return {"kind": "none"}

        session.step = STEP_REEL_AD_PRODUCT_IMAGE
        save_session(session)
        _send(phone, {
            "kind": "text",
            "text": "📢 *Ad reel!* Send your product or brand image:",
        }, tts=voice_ok)
        return {"kind": "none"}

    # Fallback
    return start(phone, session, intent)


# ── Step handler ──────────────────────────────────────────────────────────

def handle_step(
    phone: str,
    session: UserSession,
    clean: str,
    button_payload: Optional[str],
    media_urls: list[str],
    media_types: list[str],
    voice_confirmed: bool,
) -> dict:
    """
    Dispatches to the correct handler depending on what the reel agent is waiting for.

    Once the reel type is resolved and we're inside a STEP_REEL_* step, we delegate
    back to workflow.py's existing reel step handlers — they already do the heavy lifting.
    """
    intent   = session.agent_intent or {}
    sub_step = intent.get("_sub_step", "")
    choice   = (button_payload or clean).lower().strip()

    # UGC Presentation Video handles all its own steps
    if intent.get("reel_type") == "ugc_presentation":
        from agents import ugc_presentation_agent
        return ugc_presentation_agent.handle_step(
            phone, session, clean, button_payload, media_urls, media_types, voice_confirmed)

    # AI Video Editor handles all its own steps
    if intent.get("reel_type") == "video_editor":
        from agents import video_editor_agent
        return video_editor_agent.handle_step(
            phone, session, clean, button_payload, media_urls, media_types, voice_confirmed)

    # ── Still need to know reel type ──────────────────────────────────────
    if sub_step == "awaiting_reel_type":
        reel_type = _parse_reel_type(choice)
        if not reel_type:
            return {
                "kind": "text",
                "text": "Reply *1* for Cinematic, *2* for UGC, *3* for Ad reel — or just say the style!",
            }
        intent["reel_type"] = reel_type
        intent["_sub_step"] = ""
        description = intent.get("description", "")
        has_image   = bool(media_urls and any(t.startswith("image/") for t in media_types))
        session.agent_intent = intent
        return _start_reel_type(
            phone, session, intent, reel_type, description,
            has_image, media_urls, media_types, voice_confirmed,
        )

    # Once reel type is selected, session.step moves to STEP_REEL_* and workflow.py handles it.
    # This shouldn't be called with an unknown sub_step — restart.
    return start(phone, session, intent)


# ── Background helpers ────────────────────────────────────────────────────

def _upload_and_advance_cinematic(
    phone: str,
    session: UserSession,
    intent: dict,
    media_urls: list[str],
    media_types: list[str],
    voice_ok: bool,
) -> None:
    try:
        user_id = session.verified_user_id or phone
        for url, mt in zip(media_urls, media_types):
            if mt.startswith("image/"):
                up = aws_storage.upload_from_url(url, user_id=user_id, media_kind="reel_product")
                if up.get("ok"):
                    session.reel_product_image_url = up["s3_url"]
                    session.step = STEP_REEL_DESCRIBE_PRODUCT
                    save_session(session)
                    _send(phone, {
                        "kind": "text",
                        "text": (
                            "✅ Image uploaded!\n\n"
                            "Now describe your product/service in a sentence or two:"
                        ),
                    }, tts=voice_ok)
                    return

        session.step = STEP_REEL_PRODUCT_IMAGE
        save_session(session)
        _send(phone, {"kind": "text", "text": "😕 Couldn't upload that image. Please send it again."})
    except Exception as exc:
        logger.exception("_upload_and_advance_cinematic failed: %s", exc)
        _send(phone, {"kind": "text", "text": f"😕 Upload failed: {exc}"})


def _upload_and_advance_ad(
    phone: str,
    session: UserSession,
    intent: dict,
    media_urls: list[str],
    media_types: list[str],
    voice_ok: bool,
) -> None:
    try:
        user_id = session.verified_user_id or phone
        for url, mt in zip(media_urls, media_types):
            if mt.startswith("image/"):
                up = aws_storage.upload_from_url(url, user_id=user_id, media_kind="reel_ad")
                if up.get("ok"):
                    session.reel_product_image_url = up["s3_url"]
                    session.step = STEP_REEL_AD_DESCRIBE
                    save_session(session)
                    _send(phone, {
                        "kind": "text",
                        "text": "✅ Got it! Describe your product/service briefly:",
                    }, tts=voice_ok)
                    return

        session.step = STEP_REEL_AD_PRODUCT_IMAGE
        save_session(session)
        _send(phone, {"kind": "text", "text": "😕 Couldn't upload. Please resend your image."})
    except Exception as exc:
        logger.exception("_upload_and_advance_ad failed: %s", exc)
        _send(phone, {"kind": "text", "text": f"😕 Upload failed: {exc}"})


# ── Utility ───────────────────────────────────────────────────────────────

def _parse_reel_type(choice: str) -> Optional[str]:
    if choice in {"1", "cinematic", "product", "product reel", "product video"}:
        return "cinematic"
    if choice in {"2", "ugc", "talking head", "talking-head", "creator", "selfie"}:
        return "ugc"
    if choice in {"3", "ad", "advertisement", "scripted"}:
        return "ad"
    # Fuzzy
    if "cinematic" in choice or "product" in choice:
        return "cinematic"
    if "ugc" in choice or "talking" in choice or "face" in choice:
        return "ugc"
    if "ad" in choice or "advert" in choice:
        return "ad"
    return None
