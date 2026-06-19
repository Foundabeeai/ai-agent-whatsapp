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
    voice_ok    = intent.get("_voice_confirmed", False)

    if not description:
        intent["_sub_step"] = "awaiting_topic"
        session.agent_intent = intent
        save_session(session)
        return {
            "kind": "text",
            "text": "📑 What should the carousel be about? Give me a topic or idea:",
        }

    # Ask for photos/links + slide count once, unless we already have inputs.
    scraped_imgs = intent.get("_scraped_image_urls", [])
    media_urls   = intent.get("_media_urls", [])
    has_media    = bool(media_urls and any(t.startswith("image/") for t in intent.get("_media_types", [])))
    if not intent.get("_carousel_ready") and not scraped_imgs and not has_media:
        intent["_sub_step"] = "awaiting_carousel_setup"
        session.agent_intent = intent
        save_session(session)
        return {
            "kind": "text",
            "text": (
                f"📑 Let's build your carousel: _{description}_\n\n"
                "📸 *Send photos or a link* to feature them in the slides (real product/property photos),\n"
                "or reply *skip* to design it from research/concept.\n\n"
                "🔢 How many slides? Reply a number *3–8* (default *4*)."
            ),
        }

    _begin_carousel_generation(phone, session, intent, voice_ok)
    return {"kind": "none"}


def _begin_carousel_generation(phone: str, session: UserSession, intent: dict, voice_ok: bool) -> None:
    """Kick off carousel generation with whatever inputs we've collected."""
    description = intent.get("description", "").strip()
    slide_count = max(1, int(intent.get("_slide_count") or intent.get("count") or 4))
    intent["_slide_count"] = slide_count

    has_photos = bool(intent.get("_scraped_image_urls")) or bool(
        intent.get("_media_urls") and any(t.startswith("image/") for t in intent.get("_media_types", [])))
    src_line = "🏡 Featuring your photos" if has_photos else "📊 Research slides"

    _send(phone, {
        "kind": "text",
        "text": (
            f"📑 *Creating carousel:* _{description}_\n"
            f"{src_line} · {slide_count} slides + hook\n"
            f"⏱ ~{(slide_count + 1) * 2} minutes ☕"
        ),
    }, tts=voice_ok)

    intent["_sub_step"] = "generating"
    session.agent_intent = intent
    save_session(session)
    threading.Thread(target=_generate_bg, args=(phone, session, intent), daemon=True).start()


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
    from tools.groq_ai import classify_action

    intent   = session.agent_intent or {}
    sub_step = intent.get("_sub_step", "")
    msg      = (button_payload or clean or "").strip()

    if sub_step == "awaiting_topic":
        if msg:
            intent["description"] = msg
            return start(phone, session, intent)
        return {"kind": "text", "text": "📑 What's the carousel topic?"}

    if sub_step == "generating":
        # A message arrived while we're still building — don't start a second run.
        return {"kind": "text", "text": "⏳ Still creating your carousel — hang tight, almost there!"}

    if sub_step == "awaiting_carousel_setup":
        import re as _re
        # 1) Parse a slide count if present anywhere in the message
        m = _re.search(r"\b([3-9]|10)\b", msg)
        if m:
            intent["_slide_count"] = max(3, min(10, int(m.group(1))))

        # 2) Process any photos/links/skip in this message
        has_media = bool(media_urls and any(t.startswith("image/") for t in media_types))
        if has_media:
            user_id = session.verified_user_id or phone
            imgs = []
            for url, mt in zip(media_urls, media_types):
                if mt.startswith("image/"):
                    up = aws_storage.upload_from_url(url, user_id=user_id, media_kind="carousel_ref")
                    if up.get("ok"):
                        imgs.append(up["s3_url"])
            intent["_scraped_image_urls"] = (intent.get("_scraped_image_urls") or []) + imgs
            intent["_photos_decided"] = True
        else:
            from tools.url_context import find_all_urls, scrape_url as _scrape_url
            urls = find_all_urls(msg) if msg else []
            if urls:
                _send(phone, {"kind": "text", "text": "🔍 Found a link — pulling the photos and details now..."})
                all_imgs, summaries = [], []
                for u in urls[:2]:
                    ctx = _scrape_url(u, phone)
                    if ctx.get("ok"):
                        all_imgs.extend(ctx["image_urls"])
                        if ctx.get("summary"):
                            summaries.append(ctx["summary"])
                intent["_scraped_image_urls"] = (intent.get("_scraped_image_urls") or []) + all_imgs
                intent["_scraped_summaries"]  = (intent.get("_scraped_summaries") or []) + summaries
                intent["_photos_decided"] = True
                if not all_imgs:
                    _send(phone, {"kind": "text",
                                  "text": "⚠️ Couldn't pull photos from that link — I'll use the details I found."})
            elif msg and not m:
                # A worded reply with no number and no link → treat as skip decision
                action, _ = classify_action(msg, "product_image", ["skip", "wait"])
                if action != "wait":
                    intent["_photos_decided"] = True

        session.agent_intent = intent
        save_session(session)

        # 3) Generate only once BOTH photos-decision and slide-count are known
        photos_decided = bool(intent.get("_photos_decided"))
        have_count     = bool(intent.get("_slide_count"))
        if photos_decided and have_count:
            intent["_carousel_ready"] = True
            session.agent_intent = intent
            save_session(session)
            _begin_carousel_generation(phone, session, intent, voice_confirmed)
            return {"kind": "none"}

        # 4) Ask for whatever is still missing
        if not photos_decided:
            return {"kind": "text",
                    "text": "📸 Send photos or a link to feature, or reply *skip*."
                            + ("" if have_count else "\n🔢 And how many slides? (3–8)")}
        return {"kind": "text", "text": "🔢 Got it! How many slides would you like? Reply a number *3–8*."}

    if sub_step == "awaiting_caption_choice":
        current_caption = intent.get("_caption", "")
        action, value = classify_action(
            msg, "caption_choice",
            ["approve", "regenerate", "custom", "custom_text"],
            extra_context=f"Caption: {current_caption[:200]}" if current_caption else "",
        )

        if action == "approve":
            return _ask_publish(phone, session, intent, voice_confirmed)

        if action == "regenerate":
            threading.Thread(target=_regen_caption_bg, args=(phone, session, intent), daemon=True).start()
            return {"kind": "none"}

        if action == "custom_text":
            intent["_caption"] = value or msg
            session.agent_intent = intent
            save_session(session)
            return _ask_publish(phone, session, intent, voice_confirmed)

        if action == "custom":
            intent["_sub_step"] = "awaiting_custom_caption"
            session.agent_intent = intent
            save_session(session)
            return {"kind": "text", "text": "✏️ Type your caption:"}

        return {"kind": "text", "text": "✅ *approve* · ✏️ type a custom caption · 🔄 *regenerate*"}

    if sub_step == "awaiting_custom_caption":
        if msg:
            intent["_caption"] = msg
            session.agent_intent = intent
            save_session(session)
            return _ask_publish(phone, session, intent, voice_confirmed)
        return {"kind": "text", "text": "✏️ Type your caption:"}

    if sub_step == "awaiting_publish":
        action, _ = classify_action(msg, "publish_action", ["now", "schedule", "cancel"])

        if action == "now":
            intent["publish_action"] = "now"
            session.agent_intent = intent
            save_session(session)
            threading.Thread(target=_publish_bg, args=(phone, session, intent), daemon=True).start()
            return {"kind": "none"}

        if action == "schedule":
            intent["_sub_step"] = "awaiting_schedule_time"
            session.agent_intent = intent
            save_session(session)
            return {"kind": "text", "text": "⏰ When? (e.g. *tomorrow 9am*, *Friday 3pm*)"}

        return {"kind": "text", "text": "📤 Publish *now*, or *schedule* for a specific time?"}

    if sub_step == "awaiting_schedule_time":
        if msg:
            action, value = classify_action(msg, "schedule_time", ["time", "unknown"])
            intent["publish_action"] = "schedule"
            intent["scheduled_at"]   = value or msg
            session.agent_intent = intent
            save_session(session)
            threading.Thread(target=_publish_bg, args=(phone, session, intent), daemon=True).start()
            return {"kind": "none"}
        return {"kind": "text", "text": "⏰ When? (e.g. *tomorrow 9am*)"}

    # Unknown sub_step — only restart if nothing generated yet
    if intent.get("_image_urls") or intent.get("_caption"):
        return {"kind": "text", "text": "✅ *approve* · ✏️ custom caption · 🔄 *regenerate*"}
    return start(phone, session, intent)


# ── Background workers ────────────────────────────────────────────────────

def _generate_bg(phone: str, session: UserSession, intent: dict) -> None:
    try:
        user_id     = session.verified_user_id or phone
        description = intent.get("description", "")
        slide_count = int(intent.get("_slide_count", 3))
        use_style   = intent.get("use_style_skill", True)
        style_skill = (session.post_style_skill or db.get_post_style_skill(phone)) if use_style else None
        compositor  = db.get_post_style_compositor(phone) if use_style else None
        brand       = session.brand_profile() if session.onboarding_complete else {}
        style_ctx   = groq_ai.style_skill_to_prompt_context(style_skill) if style_skill else ""

        # Scraped context from URL (if user provided a link)
        scraped_imgs  = intent.get("_scraped_image_urls", [])
        scraped_summaries = intent.get("_scraped_summaries", [])
        scraped_ctx = ("\n\nAdditional context from linked URL:\n" + "\n".join(scraped_summaries)
                       if scraped_summaries else "")

        # Step 1: Generate carousel content (research + hook)
        if scraped_summaries:
            # Real subject (property/product) — slides must describe THIS specific
            # item using the scraped facts, not generic market research.
            _send(phone, {"kind": "text", "text": "📝 Writing slides about your actual listing..."})
            enriched_description = (
                f"{description}\n\n"
                "Write the carousel slides about THIS SPECIFIC property/product using the exact "
                "facts below (price, beds, baths, size, location, key features). Each slide should "
                "highlight a real selling point from these facts — do NOT invent generic market "
                "statistics.\n" + scraped_ctx
            )
        else:
            _send(phone, {"kind": "text", "text": "📊 Researching data and crafting slides..."})
            enriched_description = description + scraped_ctx
        carousel_content = groq_ai.generate_research_carousel_content(
            topic=enriched_description, brand=brand, slide_count=slide_count
        )
        brand_hex = groq_ai.get_brand_hex_colors(session.brand_colors or "")

        # Step 2: Generate background images (use scraped images as references if available)
        total_slides = 1 + slide_count
        n_bg = max(1, total_slides // 2)
        _send(phone, {"kind": "text", "text": f"🎨 Generating {n_bg} background image(s)..."})

        import requests as _req
        hook_bytes: Optional[bytes] = None
        extra_bg: list[bytes] = []

        if scraped_imgs:
            # PRESERVE: use the user's REAL photos directly as slide backgrounds —
            # never regenerate the property/product, just feature the actual photos.
            _send(phone, {"kind": "text", "text": "🏡 Using your real photos for the slides..."})
            real_bytes: list[bytes] = []
            for u in scraped_imgs[: total_slides]:
                try:
                    r = _req.get(u, timeout=30)
                    if r.ok:
                        real_bytes.append(r.content)
                except Exception:
                    continue
            if real_bytes:
                hook_bytes = real_bytes[0]
                extra_bg   = real_bytes[1:]
        else:
            # No real photos → generate branded background images
            _send(phone, {"kind": "text", "text": f"🎨 Generating {n_bg} background image(s)..."})
            ref_images = [session.brand_assets[0]] if session.brand_assets else None
            for bi in range(n_bg):
                bg_prompt = (
                    f"Cinematic editorial photo for {brand.get('brand_name','the brand')}: {description}. "
                    f"Brand colors: {session.brand_colors or 'professional dark tones'}. "
                    "No text, no logos, dramatic commercial lighting, magazine quality."
                )
                ref = [ref_images[bi % len(ref_images)]] if ref_images else None
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
            carousel_content=carousel_content,
            hook_image_bytes=hook_bytes,
            extra_bg_bytes=extra_bg,
            brand_colors=brand_hex,
            username=session.instagram_username or session.brand_name or "brand",
            brand_name=session.brand_name or "",
            avatar_url=session.brand_assets[0] if session.brand_assets else None,
            style_compositor=compositor,
        )

        # Upload slides
        s3_urls = []
        for img_bytes in slide_images:
            up = aws_storage.upload_bytes(
                img_bytes,
                content_type="image/jpeg",
                extension="jpg",
                folder=f"{user_id}/carousels",
            )
            if up.get("ok"):
                s3_urls.append(up["s3_url"])

        if not s3_urls:
            raise RuntimeError("No slides uploaded")

        caption = groq_ai.generate_caption_with_style(
            description + scraped_ctx, "carousel",
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
        _send(phone, {"kind": "media", "text": "📸", "media_url": url})
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
