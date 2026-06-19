"""
BeeQ Harness — smart intent routing for verified, onboarded users.

Replaces the old template-style STEP_CHOOSE_CONTENT_TYPE → STEP_COLLECT_DESCRIPTION
flow with a single GPT-OSS intent-extraction call that:
  1. Reads text + audio transcript + media signals in one shot
  2. Routes to the correct sub-agent (image_post / carousel / reel)
  3. Only asks for what's genuinely missing, in one warm conversational sentence

Voice flow:
  • Transcription confirmation → always sent as TEXT (already done in workflow.py)
  • After confirmation → sub-agents use TTS for key response messages
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

import db
from session_store import (
    UserSession,
    get_session,
    save_session,
    STEP_CHOOSE_CONTENT_TYPE,
    STEP_AGENT_COLLECTING,
    STEP_AGENT_IMAGE_POST,
    STEP_AGENT_CAROUSEL,
    STEP_AGENT_REEL,
)
from tools import groq_ai

logger = logging.getLogger(__name__)


def _send_scraping_notice(phone: str) -> None:
    from workflow import _send_async
    _send_async(phone, {
        "kind": "text",
        "text": "🔍 Found a link — scraping content and images from it now...",
    })

# ── Steps that the harness owns (returned users only) ─────────────────────
HARNESS_STEPS = {
    STEP_CHOOSE_CONTENT_TYPE,  # legacy entry — harness intercepts this
    STEP_AGENT_COLLECTING,     # harness asked for one missing field
    STEP_AGENT_IMAGE_POST,     # inside image-post sub-agent
    STEP_AGENT_CAROUSEL,       # inside carousel sub-agent
    STEP_AGENT_REEL,           # inside reel sub-agent
}


def _session_context(session: UserSession) -> dict:
    """Build the context dict that extract_full_intent needs."""
    style_skill = session.post_style_skill or db.get_post_style_skill(session.phone_number)
    return {
        "brand_name":           session.brand_name or "",
        "brand_description":    session.brand_description or "",
        "brand_voice":          session.brand_voice or "",
        "social_goal":          session.social_goal or "",
        "has_style_skill":      bool(style_skill),
        "recent_content_types": session.content_type or "",
    }


def route(
    phone: str,
    session: UserSession,
    body: str,
    button_payload: Optional[str],
    media_urls: list[str],
    media_types: list[str],
    audio_transcript: Optional[str] = None,
    voice_confirmed: bool = False,
) -> dict:
    """
    Main harness entry point for post-onboarding messages.

    voice_confirmed=True means the user just confirmed a transcription — the
    sub-agents should use TTS for their response (they call _send_async with tts=True).
    """
    from agents import image_post_agent, carousel_agent, reel_agent

    clean = (body or "").strip()
    choice = clean.lower()

    # ── Map legacy numbered menu choices into natural language ──────────────
    # Users sometimes reply "1", "2", "3" from old menus or habit.
    _NUM_MAP = {"1": "image post", "2": "carousel", "3": "reel",
                "📸": "image post", "🎠": "carousel", "🎬": "reel"}
    if choice in _NUM_MAP and not button_payload:
        clean = _NUM_MAP[choice]
        choice = clean

    # ── Detect fresh-start / reset messages ────────────────────────────────
    # If mid-agent and user sends a greeting or short ambiguous message,
    # clear agent state and treat as a new conversation.
    _RESET_WORDS = {
        "hi", "hello", "hey", "hiya", "sup", "yo", "start", "restart",
        "reset", "start over", "begin", "new", "menu", "back", "cancel",
        "stop", "exit", "done", "quit", "skip", "nevermind", "never mind",
        "forget it", "nvm", "no thanks", "not now",
    }
    _in_agent = session.step in (STEP_AGENT_IMAGE_POST, STEP_AGENT_CAROUSEL,
                                  STEP_AGENT_REEL, STEP_AGENT_COLLECTING)
    # Also reset if the sub_step is empty/None (agent stuck with no active step)
    _sub_step = (session.agent_intent or {}).get("_sub_step", "")
    _stuck = _in_agent and not _sub_step
    if _in_agent and not button_payload and not media_urls:
        if choice in _RESET_WORDS or _stuck:
            session.step = STEP_CHOOSE_CONTENT_TYPE
            session.agent_intent = None
            session.agent_missing_field = None
            save_session(session)
            logger.info("harness: reset trigger '%s' (stuck=%s) from %s — clearing agent state",
                        clean, _stuck, phone)
            return _ask_what_to_create(phone, session)

    # ── If we're inside a sub-agent step, delegate directly ────────────────
    if session.step == STEP_AGENT_IMAGE_POST:
        return image_post_agent.handle_step(phone, session, clean, button_payload,
                                            media_urls, media_types, voice_confirmed)
    if session.step == STEP_AGENT_CAROUSEL:
        return carousel_agent.handle_step(phone, session, clean, button_payload,
                                          media_urls, media_types, voice_confirmed)
    if session.step == STEP_AGENT_REEL:
        return reel_agent.handle_step(phone, session, clean, button_payload,
                                      media_urls, media_types, voice_confirmed)

    # ── Collecting mode: harness asked one question, user is answering ──────
    if session.step == STEP_AGENT_COLLECTING:
        return _handle_collecting(phone, session, clean, media_urls, media_types, voice_confirmed)

    # ── Fresh intent extraction ─────────────────────────────────────────────
    has_image = any(t.startswith("image/") for t in media_types)
    has_video = any(t.startswith("video/") for t in media_types)
    ctx = _session_context(session)

    # If user is just pressing "create" or opening the menu with no context, show the
    # smart prompt rather than calling the LLM on an empty string.
    if not clean and not audio_transcript and not has_image and not has_video:
        return _ask_what_to_create(phone, session)

    # Detect URLs in the message/transcript and scrape them immediately
    from tools.url_context import find_all_urls, scrape_url as _scrape_url
    full_text = " ".join(filter(None, [clean, audio_transcript]))
    found_urls = find_all_urls(full_text)
    scraped_contexts: list[dict] = []
    if found_urls:
        _send_scraping_notice(phone)
        for u in found_urls[:2]:  # cap at 2 URLs per message
            logger.info("harness: scraping URL %s for %s", u, phone)
            ctx_data = _scrape_url(u, phone)
            if ctx_data.get("ok"):
                scraped_contexts.append(ctx_data)
                logger.info("harness: scraped %s — %d images, summary len=%d",
                            u, len(ctx_data["image_urls"]), len(ctx_data["summary"]))
            else:
                logger.warning("harness: scrape failed for %s: %s", u, ctx_data.get("error"))

    # Build enriched text for intent extraction
    enriched_text = clean
    if scraped_contexts:
        enriched_text += "\n\n[SCRAPED URL CONTEXT — use this as the primary content source:]"
        for sc in scraped_contexts:
            enriched_text += (
                f"\nURL: {sc['url']}\nTitle: {sc['title']}\nKey facts: {sc['summary']}\n"
            )

    # Extract full intent in one shot
    intent = groq_ai.extract_full_intent(
        text_body=enriched_text,
        audio_transcript=audio_transcript,
        has_image=has_image,
        has_video=has_video,
        session_context=ctx,
    )

    # Attach media urls and scraped images to the intent so sub-agents can pick them up
    intent["_media_urls"]  = media_urls
    intent["_media_types"] = media_types
    intent["_voice_confirmed"] = voice_confirmed

    # Merge scraped images into media_urls so agents treat them as reference images
    if scraped_contexts:
        all_scraped_imgs = []
        for sc in scraped_contexts:
            all_scraped_imgs.extend(sc["image_urls"])
        intent["_scraped_image_urls"] = all_scraped_imgs
        intent["_scraped_summaries"]  = [sc["summary"] for sc in scraped_contexts]
        # If user didn't attach an image but we scraped some, mark as having scraped refs
        if all_scraped_imgs and not has_image:
            intent["_has_scraped_images"] = True

    logger.info(
        "harness intent for %s: type=%s confidence=%.2f ready=%s missing=%s",
        phone, intent["content_type"], intent["confidence"],
        intent["ready_to_generate"], intent["missing_fields"],
    )

    ct = intent["content_type"]

    # Unknown content type — ask the smart question
    if ct == "unknown" or not intent["ready_to_generate"]:
        session.agent_intent = intent
        session.agent_missing_field = ", ".join(intent["missing_fields"])
        session.step = STEP_AGENT_COLLECTING
        save_session(session)
        return {"kind": "text", "text": f"🐝 {intent['smart_question']}"}

    # Route to sub-agent
    return _route_to_agent(phone, session, intent, ct)


def _route_to_agent(phone: str, session: UserSession, intent: dict, ct: str) -> dict:
    from agents import image_post_agent, carousel_agent, reel_agent

    session.agent_intent = intent

    if ct == "image_post":
        session.step = STEP_AGENT_IMAGE_POST
        save_session(session)
        return image_post_agent.start(phone, session, intent)

    if ct == "carousel":
        session.step = STEP_AGENT_CAROUSEL
        save_session(session)
        return carousel_agent.start(phone, session, intent)

    if ct == "reel":
        session.step = STEP_AGENT_REEL
        save_session(session)
        return reel_agent.start(phone, session, intent)

    # Should not reach here, but safe fallback
    return _ask_what_to_create(phone, session)


def _handle_collecting(
    phone: str,
    session: UserSession,
    clean: str,
    media_urls: list[str],
    media_types: list[str],
    voice_confirmed: bool,
) -> dict:
    """User has answered the harness's single clarifying question. Re-extract and route."""
    partial = session.agent_intent or {}
    missing = session.agent_missing_field or ""

    # Merge user's answer into partial intent
    has_image = any(t.startswith("image/") for t in media_types)
    has_video = any(t.startswith("video/") for t in media_types)

    # Scrape any URLs in this follow-up message too
    from tools.url_context import find_all_urls, scrape_url as _scrape_url
    found_urls = find_all_urls(clean)
    scraped_contexts: list[dict] = []
    if found_urls:
        _send_scraping_notice(phone)
        for u in found_urls[:2]:
            ctx_data = _scrape_url(u, phone)
            if ctx_data.get("ok"):
                scraped_contexts.append(ctx_data)

    # Re-run intent extraction with the previous partial merged into the prompt
    ctx = _session_context(session)
    prior_desc = partial.get("description", "")
    prior_ct   = partial.get("content_type", "unknown")

    # Build a combined message: prior context + user's new answer
    combined = clean
    if prior_ct and prior_ct != "unknown":
        combined = f"Content type: {prior_ct}. " + combined
    if prior_desc and prior_desc not in combined:
        combined = f"Topic: {prior_desc}. " + combined
    if scraped_contexts:
        combined += "\n\n[SCRAPED URL CONTEXT:]"
        for sc in scraped_contexts:
            combined += f"\nURL: {sc['url']}\nTitle: {sc['title']}\nKey facts: {sc['summary']}\n"

    new_intent = groq_ai.extract_full_intent(
        text_body=combined,
        audio_transcript=None,
        has_image=has_image,
        has_video=has_video,
        session_context=ctx,
    )
    # Carry forward media and scraped context from original intent
    new_intent["_media_urls"]  = media_urls or partial.get("_media_urls", [])
    new_intent["_media_types"] = media_types or partial.get("_media_types", [])
    new_intent["_voice_confirmed"] = voice_confirmed
    if scraped_contexts:
        all_imgs = [img for sc in scraped_contexts for img in sc["image_urls"]]
        new_intent["_scraped_image_urls"] = all_imgs
        new_intent["_scraped_summaries"]  = [sc["summary"] for sc in scraped_contexts]
    else:
        new_intent["_scraped_image_urls"] = partial.get("_scraped_image_urls", [])
        new_intent["_scraped_summaries"]  = partial.get("_scraped_summaries", [])

    ct = new_intent["content_type"]

    if ct == "unknown" or not new_intent["ready_to_generate"]:
        session.agent_intent = new_intent
        session.agent_missing_field = ", ".join(new_intent["missing_fields"])
        save_session(session)
        return {"kind": "text", "text": f"🐝 {new_intent['smart_question']}"}

    return _route_to_agent(phone, session, new_intent, ct)


def _ask_what_to_create(phone: str, session: UserSession) -> dict:
    session.step = STEP_AGENT_COLLECTING
    session.agent_intent = {}
    session.agent_missing_field = "content_type,description"
    save_session(session)
    return {
        "kind": "text",
        "text": (
            "🐝 Hey! What would you like to create today?\n\n"
            "You can say something like:\n"
            "• _Make a post about our new product launch_ 📸\n"
            "• _Create a carousel on 5 skincare tips_ 📑\n"
            "• _Cinematic reel for my coffee brand_ 🎬\n\n"
            "Or just send a voice note or an image and I'll figure it out!"
        ),
    }
