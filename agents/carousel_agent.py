"""
Carousel Sub-Agent

Research-backed carousel creation:
  - Determines slide count from intent (default 3 data slides + 1 hook)
  - Generates branded background images
  - Composes research carousel with Pillow
  - Injects style skill into caption
  - Handles approve → publish flow

Internal sub_steps (stored in intent["_sub_step"]):
  "awaiting_topic"         — description was unclear
  "awaiting_count_confirm" — ask how many slides (only if user didn't specify)
  "awaiting_caption_choice"
  "awaiting_publish"
  "awaiting_schedule_time"
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
    STEP_AGENT_CAROUSEL,
    STEP_CHOOSE_CONTENT_TYPE,
)
from tools import groq_ai, aws_storage, image_gen
from tools.carousel_composer import make_research_carousel

logger = logging.getLogger(__name__)


def _send(phone: str, payload: dict, tts: bool = False) -> None:
    from workflow import _send_async
    _send_async(phone, payload, tts=tts)


# ── Entry point ───────────────────────────────────────────────────────────

def start(phone: str, session: UserSession, intent: dict) -> dict:
    description = intent.get("description", "").strip()
    count_raw   = intent.get("count", 0)
    voice_ok    = intent.get("_voice_confirmed", False)

    if not description:
        intent["_sub_step"] = "awaiting_topic"
        session.agent_intent = intent
        save_session(session)
        return {
            "kind": "text",
            "text": "📑 What should the carousel be about? Give me a topic or idea:",
        }

    # Determine slide count — if specified use it, else default 3 data slides
    slide_count = max(1, int(count_raw or 3))
    intent["_slide_count"] = slide_count

    _send(phone, {
        "kind": "text",
        "text": (
            f"📑 *Creating carousel:* _{description}_\n"
            f"📊 {slide_count} research slides + hook\n"
            f"⏱ ~{(slide_count + 1) * 2} minutes ☕"
        ),
    }, tts=voice_ok)

    intent["_sub_step"] = "generating"
    session.agent_intent = intent
    save_session(session)

    threading.Thread(
        target=_generate_bg,
        args=(phone, session, intent),
        daemon=True,
    ).start()
    return {"kind": "none"}


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
    intent   = session.agent_intent or {}
    sub_step = intent.get("_sub_step", "")
    choice   = (button_payload or clean).lower().strip()

    if sub_step == "awaiting_topic":
        if clean:
            intent["description"] = clean
            return start(phone, session, intent)
        return {"kind": "text", "text": "📑 What's the carousel topic?"}

    if sub_step == "awaiting_caption_choice":
        if choice in {"approve", "yes", "looks good", "perfect", "great"}:
            return _ask_publish(phone, session, intent, voice_confirmed)

        if "custom" in choice or "write" in choice or "change" in choice:
            intent["_sub_step"] = "awaiting_custom_caption"
            session.agent_intent = intent
            save_session(session)
            return {"kind": "text", "text": "✏️ Write your caption:"}

        if "regenerate" in choice or "new" in choice:
            threading.Thread(
                target=_regen_caption_bg, args=(phone, session, intent), daemon=True
            ).start()
            return {"kind": "none"}

        return {
            "kind": "text",
            "text": "✅ *approve* · ✏️ type a custom caption · 🔄 *regenerate*",
        }

    if sub_step == "awaiting_custom_caption":
        if clean:
            intent["_caption"] = clean
            return _ask_publish(phone, session, intent, voice_confirmed)
        return {"kind": "text", "text": "✏️ Type your caption:"}

    if sub_step == "awaiting_publish":
        if choice in {"now", "publish now", "post now", "publish", "post it", "yes"}:
            intent["publish_action"] = "now"
            session.agent_intent = intent
            save_session(session)
            threading.Thread(target=_publish_bg, args=(phone, session, intent), daemon=True).start()
            return {"kind": "none"}

        if choice in {"schedule", "later", "schedule it"}:
            intent["_sub_step"] = "awaiting_schedule_time"
            session.agent_intent = intent
            save_session(session)
            return {"kind": "text", "text": "⏰ When? (e.g. *tomorrow 9am*, *Friday 3pm*)"}

        return {"kind": "text", "text": "📤 *now* or *schedule*?"}

    if sub_step == "awaiting_schedule_time":
        if clean:
            intent["publish_action"] = "schedule"
            intent["scheduled_at"]   = clean
            session.agent_intent = intent
            save_session(session)
            threading.Thread(target=_publish_bg, args=(phone, session, intent), daemon=True).start()
            return {"kind": "none"}
        return {"kind": "text", "text": "⏰ When? (e.g. *tomorrow 9am*)"}

    return start(phone, session, intent)


# ── Background workers ────────────────────────────────────────────────────

def _generate_bg(phone: str, session: UserSession, intent: dict) -> None:
    try:
        user_id     = session.verified_user_id or phone
        description = intent.get("description", "")
        slide_count = int(intent.get("_slide_count", 3))
        use_style   = intent.get("use_style_skill", True)
        style_skill = (session.post_style_skill or db.get_post_style_skill(phone)) if use_style else None
        brand       = session.brand_profile() if session.onboarding_complete else {}
        style_ctx   = groq_ai.style_skill_to_prompt_context(style_skill) if style_skill else ""

        # Step 1: Generate carousel content (research + hook)
        _send(phone, {"kind": "text", "text": "📊 Researching data and crafting slides..."})
        carousel_content = groq_ai.generate_research_carousel_content(
            topic=description, brand=brand, slide_count=slide_count
        )
        brand_hex = groq_ai.get_brand_hex_colors(session.brand_colors or "")

        # Step 2: Generate background images
        total_slides = 1 + slide_count
        n_bg = max(1, total_slides // 2)
        _send(phone, {"kind": "text", "text": f"🎨 Generating {n_bg} background image(s)..."})

        import requests as _req
        hook_bytes: Optional[bytes] = None
        extra_bg: list[bytes] = []

        brand_with_style = {**brand, "_style_context": style_ctx} if style_ctx else brand
        for bi in range(n_bg):
            bg_prompt = (
                f"Cinematic editorial photo for {brand.get('brand_name','the brand')}: {description}. "
                f"Brand colors: {session.brand_colors or 'professional dark tones'}. "
                "No text, no logos, dramatic commercial lighting, magazine quality."
            )
            ref = [session.brand_assets[0]] if session.brand_assets else None
            gen = image_gen.generate_image(bg_prompt, aspect_ratio="1:1", reference_urls=ref)
            if gen.get("ok"):
                r = _req.get(gen["url"], timeout=30)
                if r.ok:
                    if bi == 0:
                        hook_bytes = r.content
                    else:
                        extra_bg.append(r.content)

        if not hook_bytes:
            raise RuntimeError("Background image generation failed")

        # Step 3: Compose carousel slides with Pillow
        _send(phone, {"kind": "text", "text": f"🖼 Rendering {total_slides} slides..."})
        slide_images = make_research_carousel(
            content=carousel_content,
            hook_image_bytes=hook_bytes,
            extra_bg_bytes=extra_bg,
            brand_colors=brand_hex,
            username=session.instagram_username or session.brand_name or "brand",
            brand_name=session.brand_name or "",
            avatar_url=session.brand_assets[0] if session.brand_assets else None,
        )

        # Upload slides
        s3_urls = []
        for img_bytes in slide_images:
            up = aws_storage.upload_bytes(
                img_bytes,
                content_type="image/png",
                extension="png",
                folder=f"{user_id}/carousels",
            )
            if up.get("ok"):
                s3_urls.append(up["s3_url"])

        if not s3_urls:
            raise RuntimeError("No slides uploaded")

        caption = groq_ai.generate_caption_with_style(
            description, "carousel",
            website_url=session.website_url or "",
            style_skill=style_skill,
        )

        _finish_generation(phone, session, intent, s3_urls, caption,
                           [carousel_content.get("hook", description)])

    except Exception as exc:
        logger.exception("carousel_agent _generate_bg failed: %s", exc)
        _send(phone, {"kind": "text", "text": f"😕 Carousel generation failed: {exc}\nType *reset* to try again."})


def _regen_caption_bg(phone: str, session: UserSession, intent: dict) -> None:
    try:
        description = intent.get("description", "")
        use_style   = intent.get("use_style_skill", True)
        style_skill = (session.post_style_skill or db.get_post_style_skill(phone)) if use_style else None
        caption = groq_ai.generate_caption_with_style(
            description, "carousel",
            website_url=session.website_url or "",
            style_skill=style_skill,
        )
        intent["_caption"]  = caption
        intent["_sub_step"] = "awaiting_caption_choice"
        session.agent_intent = intent
        save_session(session)
        _send(phone, {"kind": "text",
                      "text": f"✍️ New caption:\n\n{caption}\n\n✅ *approve* · ✏️ *custom* · 🔄 *regenerate*"})
    except Exception as exc:
        _send(phone, {"kind": "text", "text": f"😕 Caption failed: {exc}"})


def _publish_bg(phone: str, session: UserSession, intent: dict) -> None:
    try:
        from tools import zerini
        from dateutil import parser as dp
        from datetime import timezone

        image_urls  = intent.get("_image_urls", [])
        caption     = intent.get("_caption", "")
        prompts     = intent.get("_prompts", [])
        publish_now = intent.get("publish_action") == "now"
        sched_raw   = intent.get("scheduled_at")

        if publish_now:
            result = zerini.publish_now(account_id=session.zerini_account_id or "",
                                        image_urls=image_urls, caption=caption,
                                        profile_id=session.zerini_profile_id or "")
            post_id = result.get("post_id") if result.get("ok") else None
            db.log_post(phone_number=phone, content_type="carousel",
                        image_urls=image_urls, caption=caption,
                        prompts=prompts, zerini_post_id=post_id, status="published")
            _send(phone, {"kind": "text",
                          "text": "✅ *Carousel published!* 🎉\n\nWhat would you like to create next?"
                          }, tts=True)
        else:
            sched_dt = None
            if sched_raw:
                try:
                    sched_dt = dp.parse(sched_raw, fuzzy=True)
                    if sched_dt.tzinfo is None:
                        sched_dt = sched_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    pass
            result = zerini.schedule_post(account_id=session.zerini_account_id or "",
                                          image_urls=image_urls, caption=caption,
                                          scheduled_at=sched_dt,
                                          profile_id=session.zerini_profile_id or "")
            post_id = result.get("post_id") if result.get("ok") else None
            db.log_post(phone_number=phone, content_type="carousel",
                        image_urls=image_urls, caption=caption,
                        prompts=prompts, zerini_post_id=post_id,
                        scheduled_at=sched_dt, status="scheduled")
            t = sched_dt.strftime("%b %d at %H:%M UTC") if sched_dt else sched_raw
            _send(phone, {"kind": "text",
                          "text": f"⏰ *Carousel scheduled* for {t} ✓\n\nWhat's next?"
                          }, tts=True)

        session.step = STEP_CHOOSE_CONTENT_TYPE
        session.agent_intent = None
        session.agent_missing_field = None
        save_session(session)

    except Exception as exc:
        logger.exception("carousel _publish_bg failed: %s", exc)
        _send(phone, {"kind": "text", "text": f"😕 Publish failed: {exc}"})


# ── Helpers ───────────────────────────────────────────────────────────────

def _finish_generation(
    phone: str,
    session: UserSession,
    intent: dict,
    s3_urls: list[str],
    caption: str,
    prompts: list[str],
) -> None:
    voice_ok = intent.get("_voice_confirmed", False)
    for url in s3_urls:
        _send(phone, {"kind": "media", "text": "", "media_url": url})
        time.sleep(1.5)

    _send(phone, {
        "kind": "text",
        "text": (
            f"✍️ *Caption:*\n\n{caption}\n\n"
            "✅ *approve* · ✏️ custom caption · 🔄 *regenerate*"
        ),
    }, tts=voice_ok)

    intent["_image_urls"] = s3_urls
    intent["_caption"]    = caption
    intent["_prompts"]    = prompts
    intent["_sub_step"]   = "awaiting_caption_choice"
    session.agent_intent  = intent
    session.step          = STEP_AGENT_CAROUSEL
    save_session(session)


def _ask_publish(phone: str, session: UserSession, intent: dict, voice_ok: bool) -> dict:
    intent["_sub_step"] = "awaiting_publish"
    session.agent_intent = intent
    save_session(session)
    _send(phone, {"kind": "text", "text": "📤 *Publish now* or *Schedule*?"}, tts=voice_ok)
    return {"kind": "none"}
