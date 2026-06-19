"""
Image Post Sub-Agent

Handles the full lifecycle of creating a single image post:
  1. Infers everything possible from intent (no template questions)
  2. Art-director pipeline when a product image is attached
  3. Pure AI generation when no image
  4. Injects stored style skill into both image prompt and caption
  5. Sends images for approval → caption choice → publish

Internal steps (stored in session.step = STEP_AGENT_IMAGE_POST):
  sub_step stored in session.agent_intent["_sub_step"]:
    "awaiting_product_image"  — asked user to send product photo
    "awaiting_caption_choice" — images sent, waiting approve/regenerate
    "awaiting_publish"        — caption ready, waiting publish/schedule
    "awaiting_schedule_time"  — user chose schedule, waiting for time
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
    STEP_AGENT_IMAGE_POST,
    STEP_CHOOSE_CONTENT_TYPE,
    STEP_GENERATING,
    STEP_PUBLISHING,
)
from tools import groq_ai, aws_storage, image_gen
from tools.carousel_composer import stamp_post_image

logger = logging.getLogger(__name__)

# ── Helpers imported lazily from workflow to avoid circular import ─────────

def _send(phone: str, payload: dict, tts: bool = False) -> None:
    from workflow import _send_async
    _send_async(phone, payload, tts=tts)


def _upload_image_url(url: str, user_id: str) -> Optional[str]:
    up = aws_storage.upload_from_url(url, user_id=user_id, media_kind="product_ref")
    return up.get("s3_url") if up.get("ok") else None


# ── Entry point called by harness ─────────────────────────────────────────

def start(phone: str, session: UserSession, intent: dict) -> dict:
    """
    Harness calls this when it's confident the user wants an image post.
    Either kicks off generation immediately or asks the one thing that's missing.
    """
    description  = intent.get("description", "").strip()
    use_ref      = intent.get("use_reference_image", False)
    media_urls   = intent.get("_media_urls", [])
    media_types  = intent.get("_media_types", [])
    voice_ok     = intent.get("_voice_confirmed", False)
    scraped_imgs = intent.get("_scraped_image_urls", [])

    has_image = bool(media_urls and any(t.startswith("image/") for t in media_types))

    # Description is mandatory — should be filled by harness, but double-check
    if not description:
        intent["_sub_step"] = "awaiting_description"
        session.agent_intent = intent
        save_session(session)
        return {
            "kind": "text",
            "text": "📸 What's this post about? Give me a quick description and I'll handle the rest.",
        }

    # If user says "use this image" and actually sent one → upload + go
    if has_image and use_ref:
        intent["_sub_step"] = "generating"
        session.agent_intent = intent
        save_session(session)
        threading.Thread(
            target=_generate_with_image_bg,
            args=(phone, session, intent, media_urls, media_types),
            daemon=True,
        ).start()
        _send(phone, {"kind": "text", "text": "🚀 Got it! Sending to the art director now..."})
        return {"kind": "none"}

    # User shared a link → never ask for an image.
    # Use scraped photos as the base if we got any; otherwise generate from scratch
    # using the scraped text context. Either way, go straight to generation.
    url_provided = intent.get("_url_provided", False)
    if (scraped_imgs or url_provided) and not has_image:
        intent["_sub_step"] = "generating"
        session.agent_intent = intent
        save_session(session)
        if scraped_imgs:
            threading.Thread(
                target=_generate_with_image_bg,
                args=(phone, session, intent, [], []),
                daemon=True,
            ).start()
            msg = (f"🔗 Got it — using the photos from your link as the base.\n"
                   f"🎨 Creating your post: _{description}_\n⏱ ~90 seconds ☕")
        else:
            threading.Thread(
                target=_generate_no_image_bg,
                args=(phone, session, intent),
                daemon=True,
            ).start()
            msg = (f"🎨 Creating your post: _{description}_\n⏱ ~90 seconds ☕")
        _send(phone, {"kind": "text", "text": msg}, tts=voice_ok)
        return {"kind": "none"}

    # No product image but user might want to provide one
    # Only ask if they didn't already decline (use_reference_image was False in intent)
    if not has_image and intent.get("confidence", 1.0) > 0.7:
        # Smart: if they have a reference_image_url from onboarding, use it silently
        if session.reference_image_url:
            intent["_sub_step"] = "generating"
            intent["_use_onboarding_ref"] = True
            session.agent_intent = intent
            save_session(session)
            threading.Thread(
                target=_generate_with_image_bg,
                args=(phone, session, intent, [], []),
                daemon=True,
            ).start()
            _send(phone, {
                "kind": "text",
                "text": (
                    f"🎨 Using your brand reference image as the base...\n"
                    f"Creating: _{description}_"
                ),
            })
            return {"kind": "none"}

        # Ask if they want to attach a product image
        intent["_sub_step"] = "awaiting_product_image"
        session.agent_intent = intent
        save_session(session)
        return {
            "kind": "text",
            "text": (
                f"📸 On it! Got your topic: _{description}_\n\n"
                "Do you have a product image to use as the base?\n"
                "• Send it now for AI to build around it\n"
                "• Or reply *skip* and I'll generate from scratch 🎨"
            ),
        }

    # Pure AI generation (no product image, confident)
    intent["_sub_step"] = "generating"
    session.agent_intent = intent
    save_session(session)
    threading.Thread(
        target=_generate_no_image_bg,
        args=(phone, session, intent),
        daemon=True,
    ).start()
    _send(phone, {
        "kind": "text",
        "text": f"🚀 Creating your post: _{description}_\n⏱ ~90 seconds ☕",
    }, tts=voice_ok)
    return {"kind": "none"}


# ── Step handler — called on subsequent messages inside this agent ─────────

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

    # ── Waiting for product image ──────────────────────────────────────────
    if sub_step == "awaiting_product_image":
        has_image = bool(media_urls and any(t.startswith("image/") for t in media_types))

        if has_image:
            intent["use_reference_image"] = True
            intent["_sub_step"] = "generating"
            session.agent_intent = intent
            save_session(session)
            threading.Thread(
                target=_generate_with_image_bg,
                args=(phone, session, intent, media_urls, media_types),
                daemon=True,
            ).start()
            _send(phone, {"kind": "text", "text": "🚀 Got it! Art director is analyzing your image..."})
            return {"kind": "none"}

        if msg:
            action, _ = classify_action(msg, "product_image", ["skip", "wait"])
            if action != "wait":
                # anything that isn't "I'm about to send an image" → generate from scratch
                intent["use_reference_image"] = False
                intent["_sub_step"] = "generating"
                session.agent_intent = intent
                save_session(session)
                threading.Thread(
                    target=_generate_no_image_bg,
                    args=(phone, session, intent),
                    daemon=True,
                ).start()
                _send(phone, {
                    "kind": "text",
                    "text": f"🎨 Creating from scratch: _{intent.get('description')}_\n⏱ ~90 seconds ☕",
                }, tts=voice_confirmed)
                return {"kind": "none"}

        return {"kind": "text", "text": "📸 Send your product image, or say *skip* to generate from scratch."}

    # ── Waiting for caption choice ─────────────────────────────────────────
    if sub_step == "awaiting_caption_choice":
        current_caption = intent.get("_caption", "")
        action, value = classify_action(
            msg, "caption_choice",
            ["approve", "regenerate", "custom", "custom_text"],
            extra_context=f"Caption: {current_caption[:200]}" if current_caption else "",
        )

        if action == "approve":
            return _ask_publish_action(phone, session, intent, voice_confirmed)

        if action == "regenerate":
            intent["_sub_step"] = "generating"
            session.agent_intent = intent
            save_session(session)
            threading.Thread(target=_regenerate_caption_bg, args=(phone, session, intent), daemon=True).start()
            return {"kind": "none"}

        if action == "custom_text":
            intent["_caption"] = value or msg
            session.agent_intent = intent
            save_session(session)
            return _ask_publish_action(phone, session, intent, voice_confirmed)

        if action == "custom":
            intent["_sub_step"] = "awaiting_custom_caption"
            session.agent_intent = intent
            save_session(session)
            return {"kind": "text", "text": "✏️ Type your caption and I'll use it:"}

        return {"kind": "text", "text": "✅ *approve* · ✏️ type a custom caption · 🔄 *regenerate*"}

    # ── Waiting for custom caption ─────────────────────────────────────────
    if sub_step == "awaiting_custom_caption":
        if msg:
            intent["_caption"] = msg
            session.agent_intent = intent
            save_session(session)
            return _ask_publish_action(phone, session, intent, voice_confirmed)
        return {"kind": "text", "text": "✏️ Type your caption:"}

    # ── Waiting for publish action ─────────────────────────────────────────
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
            return {"kind": "text", "text": "⏰ When should I post it? (e.g. *tomorrow at 9am* or *Friday 3pm*)"}

        return {"kind": "text", "text": "📤 Publish *now*, or *schedule* for a specific time?"}

    # ── Waiting for schedule time ──────────────────────────────────────────
    if sub_step == "awaiting_schedule_time":
        if msg:
            action, value = classify_action(msg, "schedule_time", ["time", "unknown"])
            intent["publish_action"] = "schedule"
            intent["scheduled_at"]   = value or msg
            session.agent_intent = intent
            save_session(session)
            threading.Thread(target=_publish_bg, args=(phone, session, intent), daemon=True).start()
            return {"kind": "none"}
        return {"kind": "text", "text": "⏰ When? (e.g. *tomorrow 9am*, *Friday 3pm*)"}

    # ── Awaiting description (rare) ────────────────────────────────────────
    if sub_step == "awaiting_description":
        if msg:
            intent["description"] = msg
            intent["_sub_step"] = "generating"
            session.agent_intent = intent
            save_session(session)
            return start(phone, session, intent)
        return {"kind": "text", "text": "What's this post about?"}

    # Unknown sub-step — only restart if nothing generated yet
    if intent.get("_image_urls") or intent.get("_caption"):
        return {"kind": "text", "text": "✅ *approve* · ✏️ custom caption · 🔄 *regenerate*"}
    return start(phone, session, intent)


# ── Background workers ────────────────────────────────────────────────────

def _generate_with_image_bg(
    phone: str,
    session: UserSession,
    intent: dict,
    media_urls: list[str],
    media_types: list[str],
) -> None:
    """Art-director pipeline: VLM analyze → SeedDream img2img → S3 → caption → send."""
    try:
        user_id     = session.verified_user_id or phone
        description = intent.get("description", "")
        use_style   = intent.get("use_style_skill", True)
        count       = max(1, int(intent.get("count") or 1))
        brand       = session.brand_profile() if session.onboarding_complete else {}
        style_skill = (session.post_style_skill or db.get_post_style_skill(phone)) if use_style else None

        scraped_imgs      = intent.get("_scraped_image_urls", [])
        scraped_summaries = intent.get("_scraped_summaries", [])
        scraped_ctx = ("\n\nContext from the link the user shared:\n" + "\n".join(scraped_summaries)
                       if scraped_summaries else "")

        # Resolve the reference image:
        #   1. WhatsApp-attached image (upload to S3)
        #   2. Best scraped image from a shared link (already an S3 URL)
        #   3. Onboarding reference image
        ref_url = session.reference_image_url
        if media_urls:
            for url, mt in zip(media_urls, media_types):
                if mt.startswith("image/"):
                    up = aws_storage.upload_from_url(url, user_id=user_id, media_kind="product_ref")
                    if up.get("ok"):
                        ref_url = up["s3_url"]
                        session.reference_image_url = ref_url
                        save_session(session)
                    break
        elif scraped_imgs:
            ref_url = scraped_imgs[0]  # already a high-quality S3 presigned URL

        if not ref_url:
            # Fall back to text-only generation (carries scraped_ctx via intent)
            return _generate_no_image_bg(phone, session, intent)

        # Art director — fold scraped property/product facts into the brief
        _send(phone, {"kind": "text", "text": "🎬 Art director analyzing the image..."})
        style_ctx = groq_ai.style_skill_to_prompt_context(style_skill) if style_skill else ""
        ad_brief  = description + scraped_ctx
        if style_ctx:
            ad_brief += f"\n\nStyle guidance:\n{style_ctx}"
        ad_result = groq_ai.art_director_analyze(
            image_url=ref_url,
            description=ad_brief,
            brand=brand,
        )
        poster_prompt = ad_result["prompt"]
        strategy  = ad_result.get("strategy", "reimagine")
        camera    = ad_result.get("camera_choice", "")
        camera_ln = f"\n📷 *Camera:* {camera}" if camera else ""
        strat_msg = (
            f"✨ *Reimagining the scene* around your product...{camera_ln}"
            if strategy == "reimagine"
            else f"✨ *Upgrading cinematic quality in place*...{camera_ln}"
        )
        _send(phone, {"kind": "text", "text": strat_msg})
        _send(phone, {"kind": "text", "text": f"⏱ ~{count * 90}s — grabbing a ☕?"})

        # SeedDream img2img
        logo_url = next(
            (a for a in (session.brand_assets or []) if a != ref_url), None
        )
        gen = image_gen.generate_product_posts(
            prompts=[poster_prompt] * count,
            product_image_url=ref_url,
            logo_url=logo_url,
            aspect_ratio="1:1",
        )
        if not gen.get("ok"):
            raise RuntimeError(gen.get("error", "generation failed"))

        # Upload composited bytes
        _send(phone, {"kind": "text", "text": "☁️ Uploading..."})
        s3_urls = []
        for img_bytes in gen["bytes_list"]:
            up = aws_storage.upload_bytes(
                img_bytes,
                content_type="image/jpeg",
                extension="jpg",
                folder=f"{user_id}/posts",
            )
            if up.get("ok"):
                s3_urls.append(up["s3_url"])

        # Stamp profile badge
        s3_urls = _stamp_images(
            s3_urls, session=session, user_id=user_id
        )

        # Generate caption with style skill (include scraped property/product facts)
        caption = groq_ai.generate_caption_with_style(
            description + scraped_ctx, "image_post",
            website_url=session.website_url or "",
            style_skill=style_skill,
        )

        _finish_generation(phone, session, intent, s3_urls, caption, [poster_prompt] * count)

    except Exception as exc:
        logger.exception("image_post_agent generate_with_image_bg failed: %s", exc)
        _send(phone, {"kind": "text", "text": f"😕 Something went wrong generating your post. ({exc})\nTry again or type *reset*."})


def _generate_no_image_bg(phone: str, session: UserSession, intent: dict) -> None:
    """Pure AI generation: GPT-OSS prompts → Replicate → S3 → caption."""
    try:
        user_id     = session.verified_user_id or phone
        description = intent.get("description", "")
        count       = max(1, int(intent.get("count") or 1))
        use_style   = intent.get("use_style_skill", True)
        style_notes = intent.get("style_notes", "")
        brand       = session.brand_profile() if session.onboarding_complete else {}
        style_skill = (session.post_style_skill or db.get_post_style_skill(phone)) if use_style else None

        style_ctx = groq_ai.style_skill_to_prompt_context(style_skill) if style_skill else ""
        brand_with_style = {**brand, "_style_context": style_ctx} if style_ctx else brand

        # Use scraped URL context if available
        scraped_imgs      = intent.get("_scraped_image_urls", [])
        scraped_summaries = intent.get("_scraped_summaries", [])
        scraped_ctx = ("\n\nContext from linked URL:\n" + "\n".join(scraped_summaries)
                       if scraped_summaries else "")

        full_desc = description + scraped_ctx
        if style_notes:
            full_desc += f" ({style_notes})"

        prompts = groq_ai.generate_image_prompts(full_desc, count=count, brand=brand_with_style)

        # Prefer scraped images as reference, fall back to brand assets
        ref_urls = scraped_imgs[:1] if scraped_imgs else (session.brand_assets[:1] if session.brand_assets else None)
        gen = image_gen.generate_images(prompts, content_type="image_post", reference_urls=ref_urls)
        if not gen.get("ok"):
            raise RuntimeError(gen.get("error", "generation failed"))

        _send(phone, {"kind": "text", "text": "☁️ Uploading..."})
        s3_result = aws_storage.upload_urls(gen["urls"], user_id, media_kind="post")
        s3_urls   = s3_result.get("s3_urls") or []

        s3_urls = _stamp_images(s3_urls, session=session, user_id=user_id)

        caption_desc = description + scraped_ctx
        caption = groq_ai.generate_caption_with_style(
            caption_desc, "image_post",
            website_url=session.website_url or "",
            style_skill=style_skill,
        )

        _finish_generation(phone, session, intent, s3_urls, caption, prompts)

    except Exception as exc:
        logger.exception("image_post_agent generate_no_image_bg failed: %s", exc)
        _send(phone, {"kind": "text", "text": f"😕 Generation failed. ({exc})\nTry again or type *reset*."})


def _regenerate_caption_bg(phone: str, session: UserSession, intent: dict) -> None:
    try:
        description = intent.get("description", "")
        use_style   = intent.get("use_style_skill", True)
        style_skill = (session.post_style_skill or db.get_post_style_skill(phone)) if use_style else None
        caption = groq_ai.generate_caption_with_style(
            description, "image_post",
            website_url=session.website_url or "",
            style_skill=style_skill,
        )
        intent["_caption"] = caption
        intent["_sub_step"] = "awaiting_caption_choice"
        session.agent_intent = intent
        save_session(session)
        _send(phone, {"kind": "text", "text": f"✍️ New caption:\n\n{caption}\n\n✅ *approve* · ✏️ *custom* · 🔄 *regenerate*"})
    except Exception as exc:
        _send(phone, {"kind": "text", "text": f"😕 Caption failed: {exc}"})


def _publish_bg(phone: str, session: UserSession, intent: dict) -> None:
    try:
        from tools import zerini
        image_urls   = intent.get("_image_urls", [])
        caption      = intent.get("_caption", "")
        prompts      = intent.get("_prompts", [])
        publish_now  = intent.get("publish_action") == "now"
        scheduled_at = intent.get("scheduled_at")

        if publish_now:
            result = zerini.publish_now(
                account_id=session.zerini_account_id or "",
                image_urls=image_urls,
                caption=caption,
                profile_id=session.zerini_profile_id or "",
            )
            post_id = result.get("post_id") if result.get("ok") else None
            db.log_post(
                phone_number=phone,
                content_type="image_post",
                image_urls=image_urls,
                caption=caption,
                prompts=prompts,
                zerini_post_id=post_id,
                status="published",
            )
            _send(phone, {
                "kind": "text",
                "text": "✅ *Published!* Your post is live 🎉\n\nReady for the next one? Just say the word!",
            }, tts=True)
        else:
            from dateutil import parser as dp
            from datetime import timezone
            sched_dt = None
            if scheduled_at:
                try:
                    sched_dt = dp.parse(scheduled_at, fuzzy=True)
                    if sched_dt.tzinfo is None:
                        sched_dt = sched_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    sched_dt = None

            result = zerini.schedule_post(
                account_id=session.zerini_account_id or "",
                image_urls=image_urls,
                caption=caption,
                scheduled_at=sched_dt,
                profile_id=session.zerini_profile_id or "",
            )
            post_id = result.get("post_id") if result.get("ok") else None
            db.log_post(
                phone_number=phone,
                content_type="image_post",
                image_urls=image_urls,
                caption=caption,
                prompts=prompts,
                zerini_post_id=post_id,
                scheduled_at=sched_dt,
                status="scheduled",
            )
            time_str = sched_dt.strftime("%b %d at %H:%M UTC") if sched_dt else scheduled_at
            _send(phone, {
                "kind": "text",
                "text": f"⏰ *Scheduled* for {time_str} ✓\n\nWhat would you like to create next?",
            }, tts=True)

        # Reset for next creation
        session.step = STEP_CHOOSE_CONTENT_TYPE
        session.agent_intent = None
        session.agent_missing_field = None
        session.content_type = None
        session.description  = None
        save_session(session)

    except Exception as exc:
        logger.exception("image_post _publish_bg failed: %s", exc)
        _send(phone, {"kind": "text", "text": f"😕 Couldn't publish: {exc}\nTry again or type *reset*."})


# ── Helpers ───────────────────────────────────────────────────────────────

def _finish_generation(
    phone: str,
    session: UserSession,
    intent: dict,
    s3_urls: list[str],
    caption: str,
    prompts: list[str],
) -> None:
    """Send images + caption to user, set up caption-choice sub-step."""
    if not s3_urls:
        _send(phone, {"kind": "text", "text": "😕 Generation produced no images. Please try again."})
        return

    voice_ok = intent.get("_voice_confirmed", False)

    # Send images — WhatsApp requires a non-empty body on media messages
    for url in s3_urls:
        _send(phone, {"kind": "media", "text": "📸", "media_url": url})
        time.sleep(1.5)

    # Send caption for review
    _send(phone, {
        "kind": "text",
        "text": (
            f"✍️ *Suggested caption:*\n\n{caption}\n\n"
            "✅ Reply *approve*\n"
            "✏️ Type a custom caption\n"
            "🔄 Say *regenerate* for a new one"
        ),
    }, tts=voice_ok)

    intent["_image_urls"] = s3_urls
    intent["_caption"]    = caption
    intent["_prompts"]    = prompts
    intent["_sub_step"]   = "awaiting_caption_choice"
    session.agent_intent  = intent
    session.step          = STEP_AGENT_IMAGE_POST
    save_session(session)


def _ask_publish_action(
    phone: str, session: UserSession, intent: dict, voice_ok: bool
) -> dict:
    intent["_sub_step"] = "awaiting_publish"
    session.agent_intent = intent
    save_session(session)
    _send(phone, {
        "kind": "text",
        "text": "📤 Ready to go! *Publish now* or *Schedule* for later?",
    }, tts=voice_ok)
    return {"kind": "none"}


def _stamp_images(
    s3_urls: list[str], session: UserSession, user_id: str
) -> list[str]:
    """Add profile badge to images using user's style compositor. Returns new S3 URLs."""
    try:
        username   = session.instagram_username or session.brand_name or "brand"
        brand_name = session.brand_name or ""
        avatar_url = session.brand_assets[0] if session.brand_assets else None
        compositor = db.get_post_style_compositor(session.phone_number)
        stamped = []
        for url in s3_urls:
            try:
                import requests as _req
                r = _req.get(url, timeout=15)
                r.raise_for_status()
                stamped_bytes = stamp_post_image(
                    r.content,
                    username=username,
                    brand_name=brand_name,
                    avatar_url=avatar_url,
                    style_compositor=compositor,
                )
                up = aws_storage.upload_bytes(
                    stamped_bytes,
                    content_type="image/png",
                    extension="png",
                    folder=f"{user_id}/posts_stamped",
                )
                stamped.append(up["s3_url"] if up.get("ok") else url)
            except Exception:
                stamped.append(url)
        return stamped
    except Exception:
        return s3_urls
