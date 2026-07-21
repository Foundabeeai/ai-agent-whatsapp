"""
Reel creation orchestrators for BeeQ.

Two reel types:
  Cinematic Product Reel — product image → 2 enhanced images → 2×5s videos → merged + music
  UGC Video             — user photo → contextual avatar image → TTS audio → lip-sync video

Both workers run in a background thread and call `send_fn` / `save_fn` callbacks
so they stay decoupled from the Flask request context.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

import requests as _requests

import config
from session_store import STEP_REEL_APPROVAL, STEP_CHOOSE_CONTENT_TYPE
from tools.carousel_composer import stamp_post_image, render_badge_png

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Avatar constants
# ---------------------------------------------------------------------------
# Default avatar URLs — must be public S3 URLs that Replicate/SeedDream can fetch.
# Set AVATAR_MAYA_URL / AVATAR_GEORGE_URL in .env after uploading the images.
# If not set, we generate a simple solid-colour placeholder and upload it to S3
# on first use, storing the result in _avatar_cache so we only do it once.
# ---------------------------------------------------------------------------
_AVATAR_MAYA_URL   = config.AVATAR_MAYA_URL   or ""
_AVATAR_GEORGE_URL = config.AVATAR_GEORGE_URL or ""
_avatar_cache: dict[str, str] = {}   # {"maya": s3_url, "george": s3_url}


def _get_avatar_url(gender: str) -> str:
    """
    Return a reliable public S3 URL for the default avatar.
    Uses .env values if set; otherwise generates a plain coloured placeholder
    and uploads it once, caching the result for the process lifetime.
    """
    from tools import aws_storage

    key = "george" if gender == "male" else "maya"
    env_url = _AVATAR_GEORGE_URL if gender == "male" else _AVATAR_MAYA_URL

    if env_url:
        return env_url
    if key in _avatar_cache:
        return _avatar_cache[key]

    # Generate a simple coloured placeholder with PIL
    try:
        from PIL import Image, ImageDraw
        import io as _io
        # 720×1280 (9:16) solid background with a centred circle silhouette
        colour = (80, 60, 120) if gender == "male" else (120, 60, 100)
        img = Image.new("RGB", (720, 1280), colour)
        draw = ImageDraw.Draw(img)
        # Head
        draw.ellipse([260, 200, 460, 420], fill=(220, 180, 150))
        # Body
        draw.ellipse([180, 380, 540, 900], fill=(200, 160, 130))
        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        img_bytes = buf.getvalue()
    except Exception as exc:
        logger.error("_get_avatar_url: PIL failed: %s", exc)
        # Absolute last resort — 1×1 white pixel
        import base64
        img_bytes = base64.b64decode(
            "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkS"
            "Ew8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJ"
            "CQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIy"
            "MjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIAAhEBAxEB/8QAFgABAQEAAAAAAAAAAAAA"
            "AAAAAAf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFAEBAAAAAAAAAAAAAAAAAAAAAP/E"
            "ABQRAQAAAAAAAAAAAAAAAAAAAAD/2gAMAwEAAhEDEQA/AJYAAP/Z"
        )

    up = aws_storage.upload_bytes(
        img_bytes,
        content_type="image/jpeg",
        extension="jpg",
        folder=f"{config.AWS_BASE_DIR}/avatars",
    )
    if up.get("ok"):
        # Must be a permanent (non-presigned) URL — SeedDream rejects presigned URLs
        url = up.get("permanent_url") or up["s3_url"]
        _avatar_cache[key] = url
        logger.info("_get_avatar_url: cached %s avatar → %s", key, url[:60])
        return url

    logger.error("_get_avatar_url: S3 upload failed: %s", up.get("error"))
    return ""


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def _to_9x16(image_bytes: bytes) -> bytes:
    """
    Centre-crop any image to exact 9:16 ratio using PIL.
    SeedDream outputs 2:3 (wider than 9:16); this trims the sides so the
    frame is native portrait for p-video.
    """
    from PIL import Image
    import io as _io
    img = Image.open(_io.BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    target_w = int(h * 9 / 16)
    if target_w > w:
        # Image is narrower than 9:16 — pad sides with black
        new_img = Image.new("RGB", (target_w, h), (0, 0, 0))
        new_img.paste(img, ((target_w - w) // 2, 0))
        img = new_img
    elif target_w < w:
        # Centre-crop to 9:16
        left = (w - target_w) // 2
        img = img.crop((left, 0, left + target_w, h))
    buf = _io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def _fetch_crop_upload(replicate_url: str, folder: str, user_id: str) -> str:
    """
    Download a SeedDream Replicate URL, crop to 9:16, upload to S3.
    Returns the S3 presigned URL (usable by p-video / fabric).
    """
    from tools import aws_storage
    raw = _requests.get(replicate_url, timeout=60)
    raw.raise_for_status()
    cropped = _to_9x16(raw.content)
    up = aws_storage.upload_bytes(
        cropped,
        content_type="image/jpeg",
        extension="jpg",
        folder=f"{user_id}/{folder}",
    )
    if not up.get("ok"):
        raise RuntimeError(f"S3 upload failed: {up.get('error')}")
    return up.get("permanent_url") or up["s3_url"]


# ---------------------------------------------------------------------------
# Public orchestrators
# ---------------------------------------------------------------------------

def create_cinematic_reel_bg(
    session,
    phone: str,
    send_fn: Callable[[str, dict], None],
    save_fn: Callable,
) -> None:
    """
    Background worker — Cinematic Product Reel.

    Steps:
    1. Analyse product image with Groq vision (if not already done)
    2. Generate 2 cinematic product prompts
    3. Enhance product image twice via GPT-image-2 img2img
    4. Animate each enhanced image with prunaai/p-video (5s each)
    5. Fetch trending music track
    6. Merge both clips + music into one 10s MP4
    7. Upload to S3 and send to user
    """
    from tools import aws_storage, image_gen, video_gen, groq_ai

    user_id = session.verified_user_id or phone

    try:
        # ── Step 1: Describe the product ──────────────────────────────────
        product_desc = session.reel_product_description
        if not product_desc and session.reel_product_image_url:
            send_fn(phone, {"kind": "text", "text": "🔍 Analysing your product image..."})
            product_desc = groq_ai.analyze_product_image(session.reel_product_image_url)
            session.reel_product_description = product_desc
            save_fn(session)

        if not product_desc:
            product_desc = session.description or "premium product"

        brand = session.brand_profile() if session.onboarding_complete else {}

        # ── Step 2: Build img2img prompts ──────────────────────────────────
        send_fn(phone, {"kind": "text", "text": "✨ Crafting cinematic scene prompts..."})

        brand_colors = brand.get("brand_colors") or "rich, moody tones"
        brand_name   = brand.get("brand_name") or "the brand"
        ref_url      = session.reel_product_image_url   # permanent S3 URL — None for services
        is_service   = not ref_url

        if is_service:
            # ── Service mode: real people actively using/experiencing the service ──
            prompt_a = (
                f"Cinematic 9:16 scene of real people actively using and experiencing {product_desc}. "
                f"Show genuine human emotion — focus, joy, relief, or confidence — "
                f"captured in the middle of the service interaction. "
                f"Dramatic moody lighting, {brand_colors} color grade, "
                f"high-end commercial photography, photorealistic, 8K. "
                f"No text, no logos, no overlays."
            )
            prompt_b = (
                f"Cinematic 9:16 lifestyle scene showing the moment someone benefits from {product_desc}. "
                f"Authentic real-world environment, people engaged and satisfied, "
                f"warm natural light, shallow depth of field, {brand_colors} tones, "
                f"premium editorial photography, photorealistic, 8K. "
                f"No text, no logos, no overlays."
            )
        else:
            # ── Product mode: keep product identical, change background only ──
            prompt_a = (
                f"Keep the product from the input image exactly as it is — "
                f"same shape, same colors, same branding, same proportions, unchanged. "
                f"Place this exact product in a dramatic dark studio setting: "
                f"black background, single overhead spotlight, sharp light cone, "
                f"subtle floor reflection, {brand_colors} color grade, "
                f"commercial advertisement quality, photorealistic, 8K."
            )
            prompt_b = (
                f"Keep the product from the input image exactly as it is — "
                f"same shape, same colors, same branding, same proportions, unchanged. "
                f"Place this exact product on a clean marble surface with soft natural window light, "
                f"warm bokeh background, {brand_colors} accent tones, "
                f"premium lifestyle editorial photography, photorealistic, 8K."
            )

        # ── Step 3: Generate frames ───────────────────────────────────────
        send_fn(phone, {"kind": "text", "text": "🎨 Generating cinematic visuals (this takes ~2 min)..."})

        if is_service:
            logger.info("create_cinematic_reel_bg: service mode — text-only generation")

        img_result_a = image_gen.generate_image_with_reference(
            prompt=prompt_a,
            image_url=ref_url,
        )
        if not img_result_a.get("ok"):
            raise RuntimeError(f"Image A failed: {img_result_a.get('error')}")

        img_result_b = image_gen.generate_image_with_reference(
            prompt=prompt_b,
            image_url=ref_url,
        )
        if not img_result_b.get("ok"):
            raise RuntimeError(f"Image B failed: {img_result_b.get('error')}")

        # ── Crop SeedDream 2:3 output to 9:16, upload to S3 ─────────────
        img_url_a = _fetch_crop_upload(img_result_a["url"], f"{user_id}/reel_frames", user_id)
        img_url_b = _fetch_crop_upload(img_result_b["url"], f"{user_id}/reel_frames", user_id)

        # ── Step 4: Animate each image ────────────────────────────────────
        send_fn(phone, {"kind": "text", "text": "🎬 Animating clip 1 of 2..."})
        vid_a = video_gen.generate_video_from_image(
            image_url=img_url_a,
            prompt=f"Slow cinematic pan revealing {product_desc}, "
                   f"dramatic lighting, commercial ad quality, 4K",
            duration=5,
            aspect_ratio="2:3",
            resolution="720p",
            fps=24,
        )
        if not vid_a.get("ok"):
            raise RuntimeError(f"Video A failed: {vid_a.get('error')}")

        send_fn(phone, {"kind": "text", "text": "🎬 Animating clip 2 of 2..."})
        vid_b = video_gen.generate_video_from_image(
            image_url=img_url_b,
            prompt=f"Cinematic product reveal of {product_desc}, "
                   f"elegant motion, studio lighting, luxury commercial",
            duration=5,
            aspect_ratio="2:3",
            resolution="720p",
            fps=24,
        )
        if not vid_b.get("ok"):
            raise RuntimeError(f"Video B failed: {vid_b.get('error')}")

        # ── Step 5: Fetch trending music + merge clips ────────────────────
        send_fn(phone, {"kind": "text", "text": "🎵 Fetching trending music..."})
        music       = video_gen.get_trending_music_url()
        music_url   = music.get("audio_url") or None
        track_name  = music.get("name", "")
        track_artist = music.get("artist", "")

        send_fn(phone, {"kind": "text", "text": "🔗 Merging clips..."})
        merge_result = video_gen.merge_videos(
            video_bytes_list=[vid_a["bytes"], vid_b["bytes"]],
            music_url=music_url,
            output_fps=24,
        )
        if not merge_result.get("ok"):
            raise RuntimeError(f"Merge failed: {merge_result.get('error')}")

        # ── Step 6: profile badge intentionally NOT stamped on reels ─────
        # (badges are off by default; reels should be clean, unbranded video)
        final_video_bytes = merge_result["bytes"]

        # ── Step 7: Upload final video (public-read so Twilio can fetch it) ─
        send_fn(phone, {"kind": "text", "text": "☁️ Uploading your reel..."})
        upload = aws_storage.upload_bytes(
            final_video_bytes,
            content_type="video/mp4",
            extension="mp4",
            folder=f"{user_id}/reels",
            public=True,
        )
        if not upload.get("ok"):
            raise RuntimeError(f"Video upload failed: {upload.get('error')}")

        # Permanent public URL — used for both WhatsApp delivery and publishing
        session.reel_video_url = upload["permanent_url"]
        session.step = STEP_REEL_APPROVAL
        save_fn(session)

        # ── Deliver ──────────────────────────────────────────────────────
        preview_url = upload["permanent_url"]
        # Send video first, then a separate text so the approval prompt
        # is always visible (WhatsApp sometimes hides captions on video messages)
        music_line = f"\n🎵 *{track_name}* by {track_artist}" if track_name else ""
        send_fn(phone, {
            "kind": "video",
            "url":  preview_url,
            "caption": f"🎬 Your Cinematic Reel is ready!{music_line}",
        })
        send_fn(phone, {
            "kind": "text",
            "text": (
                "What would you like to do?\n\n"
                "✅ Reply *approve* to add a caption and publish\n"
                "🔄 Reply *regenerate* to create a new reel"
            ),
        })

    except Exception as exc:
        logger.error("create_cinematic_reel_bg failed for %s: %s", phone, exc)
        session.step = STEP_CHOOSE_CONTENT_TYPE
        save_fn(session)
        send_fn(phone, {
            "kind": "text",
            "text": (
                f"⚠️ Something went wrong creating your reel: {exc}\n\n"
                "Type *reel* to try again, or choose a different content type."
            ),
        })


def create_ugc_video_bg(
    session,
    phone: str,
    send_fn: Callable[[str, dict], None],
    save_fn: Callable,
) -> None:
    """
    Background worker — UGC Talking-Head Video.

    Steps:
    1. Generate a contextual portrait image (user in product environment) via GPT-image-2
    2. Synthesise the script to audio via inworld TTS
    3. Upload audio to S3
    4. Create lip-sync video via veed/fabric-1.0
    5. Upload video to S3 and send to user
    """
    from tools import aws_storage, image_gen, video_gen, voice as voice_tools, groq_ai

    user_id = session.verified_user_id or phone

    try:
        brand        = session.brand_profile() if session.onboarding_complete else {}
        script       = session.reel_script or ""
        product_desc = session.reel_product_description or session.description or "the product"
        user_photo_url = session.reel_user_photo_url   # None → use default avatar
        voice_gender   = session.reel_ugc_voice or "female"  # "male" or "female"

        # ── Step 1: Generate contextual portrait image via SeedDream ─────
        send_fn(phone, {"kind": "text", "text": "🖼️ Creating your UGC scene (this takes ~90 sec)..."})

        brand_name   = brand.get("brand_name") or "the brand"
        brand_colors = brand.get("brand_colors") or "vibrant"

        if user_photo_url:
            portrait_ref = user_photo_url
        else:
            portrait_ref = _get_avatar_url(voice_gender)  # reliable S3 URL, never imgur

        portrait_prompt = (
            f"The EXACT same person from the reference photo — "
            f"preserve their face, skin tone, eye color, hair color, hair style, "
            f"facial structure, and all distinguishing features with 100% accuracy. "
            f"Do NOT alter, beautify, or change the person's appearance in any way. "
            f"Place them in a real-world lifestyle setting relevant to {brand_name}, "
            f"naturally using or holding {product_desc}, "
            f"looking directly at camera with genuine enthusiasm, "
            f"natural light, warm {brand_colors} color tones, "
            f"9:16 portrait orientation, photorealistic, editorial photography quality."
        )

        img_result = image_gen.generate_image_with_reference(
            prompt=portrait_prompt,
            image_url=portrait_ref,
            aspect_ratio="2:3",
        )
        if not img_result.get("ok"):
            raise RuntimeError(f"Portrait image failed: {img_result.get('error')}")

        # Crop SeedDream 2:3 output to 9:16 and upload once to S3
        portrait_s3_url = _fetch_crop_upload(
            img_result["url"],
            f"{user_id}/reel_ugc_portrait",
            user_id,
        )

        # ── Step 2: Synthesise script to audio ───────────────────────────
        clone_voice    = session.reel_clone_voice
        voice_sample   = session.reel_voice_sample_url
        tts_voice      = "Paul" if voice_gender == "male" else "Rachel"

        send_fn(phone, {"kind": "text", "text": "🎙️ Generating voice-over..."})
        if clone_voice and voice_sample:
            audio_url = voice_tools.synthesize_and_upload(
                script,
                clone_reference_url=voice_sample,
            )
        else:
            audio_url = voice_tools.synthesize_and_upload(
                script,
                voice_id=tts_voice,
            )
        if not audio_url:
            raise RuntimeError("TTS synthesis failed")

        # ── Step 3: Lip-sync via veed/fabric-1.0 ─────────────────────────
        send_fn(phone, {"kind": "text", "text": "💬 Creating lip-sync video..."})
        lip_result = video_gen.generate_lipsync_video(
            image_url=portrait_s3_url,
            audio_url=audio_url,
            resolution="720p",
        )
        if not lip_result.get("ok"):
            raise RuntimeError(f"Lip-sync failed: {lip_result.get('error')}")

        # ── Step 4: Add TikTok-style captions ────────────────────────────
        send_fn(phone, {"kind": "text", "text": "📝 Adding captions..."})
        # Resolve brand hex colour for caption highlight
        _brand_colors_str = brand.get("brand_colors") or ""
        _hex_result = groq_ai.get_brand_hex_colors(_brand_colors_str) if _brand_colors_str else {}
        highlight_color = (
            _hex_result.get("primary")
            or _hex_result.get("hex")
            or "#FFFF00"
        )
        captioned_bytes = video_gen.add_tiktok_captions(
            video_bytes=lip_result["bytes"],
            highlight_color=highlight_color,
        )
        final_bytes = captioned_bytes if captioned_bytes else lip_result["bytes"]

        # ── Step 5: Upload final video (public-read so Twilio can fetch it) ─
        send_fn(phone, {"kind": "text", "text": "☁️ Uploading your UGC video..."})
        upload = aws_storage.upload_bytes(
            final_bytes,
            content_type="video/mp4",
            extension="mp4",
            folder=f"{user_id}/reels",
            public=True,
        )
        if not upload.get("ok"):
            raise RuntimeError(f"Video upload failed: {upload.get('error')}")

        session.reel_video_url = upload["permanent_url"]
        session.step = STEP_REEL_APPROVAL
        save_fn(session)

        # ── Deliver ──────────────────────────────────────────────────────
        send_fn(phone, {
            "kind": "video",
            "url":  upload["permanent_url"],
            "caption": f"🎬 Your UGC Video is ready!\n\n📝 Script: _{script}_",
        })
        send_fn(phone, {
            "kind": "text",
            "text": (
                "What would you like to do?\n\n"
                "✅ Reply *approve* to add a caption and publish\n"
                "🔄 Reply *regenerate* to create a new video"
            ),
        })

    except Exception as exc:
        logger.error("create_ugc_video_bg failed for %s: %s", phone, exc)
        session.step = STEP_CHOOSE_CONTENT_TYPE
        save_fn(session)
        send_fn(phone, {
            "kind": "text",
            "text": (
                f"⚠️ Something went wrong creating your UGC video: {exc}\n\n"
                "Type *reel* to try again, or choose a different content type."
            ),
        })


# ---------------------------------------------------------------------------
# Full Ad Reel
# ---------------------------------------------------------------------------

def create_ad_reel_bg(
    session,
    phone: str,
    send_fn: Callable[[str, dict], None],
    save_fn: Callable,
) -> None:
    """
    Full Ad Reel — 3 user images + 3 cinematic images → 5 p-video clips → lipsync → composite.
    Runs in a background thread.
    """
    from tools import image_gen, video_gen, aws_storage, voice as voice_tools, groq_ai

    phone   = session.phone_number
    user_id = session.verified_user_id or phone
    brand   = session.brand_profile()

    try:
        product_desc  = session.reel_product_description or ""
        script        = session.reel_script or ""
        voice_gender  = session.reel_ugc_voice or "female"
        tts_voice     = "Paul" if voice_gender == "male" else "Rachel"
        product_image = session.reel_product_image_url   # product/service reference photo
        user_photo    = session.reel_user_photo_url       # user selfie for lipsync face
        brand_name    = brand.get("brand_name") or "the brand"
        brand_colors  = brand.get("brand_colors") or "vibrant"

        # Reference for user-style images: user selfie > product image > default avatar
        user_ref = user_photo or product_image or _get_avatar_url(voice_gender)
        # Reference for cinematic images: product image only (no person)
        cine_ref = product_image  # None is fine — SeedDream handles text-only generation

        # ── Step 1: Generate 6 images via SeedDream ──────────────────────
        # 2 user-lifestyle + 1 close-up (for lipsync) + 3 cinematic product shots
        send_fn(phone, {"kind": "text", "text": "🎬 Creating Ad Reel — generating scenes (step 1/6)..."})

        user_prompts = [
            (
                f"The EXACT same person from the reference photo — preserve face, skin tone, "
                f"hair and all features precisely. Place them naturally holding {product_desc}, "
                f"genuine lifestyle setting for {brand_name}, warm {brand_colors} tones, "
                f"portrait 2:3, photorealistic, no text, no logos."
            ),
            (
                f"The EXACT same person from the reference photo — preserve all facial features. "
                f"Candid lifestyle scene: person enjoying {product_desc} outdoors or at home, "
                f"golden hour light, shallow depth of field, {brand_colors} accent, "
                f"portrait 2:3, photorealistic, no text."
            ),
            (
                f"The EXACT same person from the reference photo — preserve face and features 100%. "
                f"Close-up documentary portrait, direct eye contact with camera, confident expression, "
                f"natural skin texture, {brand_colors} accent lighting, portrait 2:3, photorealistic."
            ),
        ]

        cinematic_prompts = [
            (
                f"Cinematic product hero shot of {product_desc} on an elegant dark surface, "
                f"single overhead spotlight, sharp light cone, floor reflection, "
                f"brand colors {brand_colors}, hyper-detailed, portrait 2:3, no text, no people."
            ),
            (
                f"Macro close-up of {product_desc}, bokeh background, "
                f"{brand_colors} color palette, commercial photography quality, "
                f"portrait 2:3, no text, no people."
            ),
            (
                f"Wide aspirational scene: {product_desc} in premium lifestyle environment, "
                f"dramatic cinematic lighting, {brand_colors} tones, "
                f"high-end advertisement aesthetic, portrait 2:3, no text, no people."
            ),
        ]

        def _gen_and_upload(prompt: str, ref_url: str | None, folder_name: str, label: str) -> str:
            res = image_gen.generate_image_with_reference(
                prompt=prompt,
                image_url=ref_url,
                aspect_ratio="2:3",
            )
            if not res.get("ok"):
                raise RuntimeError(f"{label} failed: {res.get('error')}")
            # Crop SeedDream 2:3 → 9:16 and upload once to S3
            return _fetch_crop_upload(res["url"], f"{user_id}/reel_ad/{folder_name}", user_id)

        user_image_urls: list[str] = []
        for i, prompt in enumerate(user_prompts):
            url = _gen_and_upload(prompt, user_ref, "user_img", f"User image {i+1}")
            user_image_urls.append(url)

        cine_image_urls: list[str] = []
        for i, prompt in enumerate(cinematic_prompts):
            url = _gen_and_upload(prompt, cine_ref, "cine_img", f"Cinematic image {i+1}")
            cine_image_urls.append(url)

        # ── Step 2: p-video for user images (first 2) + cinematic (all 3) ─
        send_fn(phone, {"kind": "text", "text": "🎞️ Animating scenes (step 2/6)..."})

        all_video_bytes: list[bytes] = []
        # user clips: index 0 and 1 only (close-up at index 2 goes to lipsync)
        for i in range(2):
            vr = video_gen.generate_video_from_image(
                image_url=user_image_urls[i],
                prompt=f"Person naturally moving, subtle motion, {product_desc} lifestyle ad feel",
                duration=3,
            )
            if not vr.get("ok"):
                raise RuntimeError(f"User video {i+1} failed: {vr.get('error')}")
            all_video_bytes.append(vr["bytes"])  # index 0, 1

        for i in range(3):
            vr = video_gen.generate_video_from_image(
                image_url=cine_image_urls[i],
                prompt=f"Elegant product motion, camera slowly pushing in, cinematic ad quality",
                duration=3,
            )
            if not vr.get("ok"):
                raise RuntimeError(f"Cinematic video {i+1} failed: {vr.get('error')}")
            all_video_bytes.append(vr["bytes"])  # index 2, 3, 4

        # ── Step 3: TTS audio ─────────────────────────────────────────────
        send_fn(phone, {"kind": "text", "text": "🎙️ Generating voice-over (step 3/6)..."})
        clone_voice  = session.reel_clone_voice
        voice_sample = session.reel_voice_sample_url
        if clone_voice and voice_sample:
            audio_url = voice_tools.synthesize_and_upload(script, clone_reference_url=voice_sample)
        else:
            audio_url = voice_tools.synthesize_and_upload(script, voice_id=tts_voice)
        if not audio_url:
            raise RuntimeError("TTS synthesis failed")

        # ── Step 4: Lipsync using close-up image (user_image_urls[2]) ─────
        send_fn(phone, {"kind": "text", "text": "💬 Creating lip-sync (step 4/6)..."})
        # Upload close-up portrait to get permanent URL for fabric
        # user_image_urls[2] is already uploaded to S3 with a valid presigned URL
        closeup_url = user_image_urls[2]

        lip_result = video_gen.generate_lipsync_video(
            image_url=closeup_url,
            audio_url=audio_url,
            resolution="720p",
        )
        if not lip_result.get("ok"):
            raise RuntimeError(f"Lip-sync failed: {lip_result.get('error')}")

        # ── Step 5: TikTok captions on lipsync ───────────────────────────
        send_fn(phone, {"kind": "text", "text": "📝 Adding captions (step 5/6)..."})
        _brand_colors_str = brand.get("brand_colors") or ""
        _hex_result = groq_ai.get_brand_hex_colors(_brand_colors_str) if _brand_colors_str else {}
        highlight_color = _hex_result.get("primary") or _hex_result.get("hex") or "#FFFF00"
        captioned_bytes = video_gen.add_tiktok_captions(
            video_bytes=lip_result["bytes"],
            highlight_color=highlight_color,
        )
        lipsync_bytes = captioned_bytes if captioned_bytes else lip_result["bytes"]

        # Profile badge intentionally NOT stamped — reels stay clean/unbranded.

        # ── Step 6: Composite everything ─────────────────────────────────
        send_fn(phone, {"kind": "text", "text": "🎬 Compositing final Ad Reel (step 6/6)..."})
        composite_result = video_gen.composite_ad_video(
            lipsync_bytes=lipsync_bytes,
            user_vid1_bytes=all_video_bytes[0],
            user_vid2_bytes=all_video_bytes[1],
            cine_vid1_bytes=all_video_bytes[2],
            cine_vid2_bytes=all_video_bytes[3],
            cine_vid3_bytes=all_video_bytes[4],
        )
        if not composite_result.get("ok"):
            raise RuntimeError(f"Composite failed: {composite_result.get('error')}")
        final_bytes = composite_result["bytes"]

        # ── Upload & deliver ──────────────────────────────────────────────
        upload = aws_storage.upload_bytes(
            final_bytes,
            content_type="video/mp4",
            extension="mp4",
            folder=f"{user_id}/reels",
            public=True,
        )
        if not upload.get("ok"):
            raise RuntimeError(f"Video upload failed: {upload.get('error')}")

        session.reel_video_url = upload["permanent_url"]
        session.step = STEP_REEL_APPROVAL
        save_fn(session)

        send_fn(phone, {
            "kind": "video",
            "url":  upload["permanent_url"],
            "caption": f"🎬 Your Full Ad Reel is ready!\n\n📝 Script: _{script}_",
        })
        send_fn(phone, {
            "kind": "text",
            "text": (
                "What would you like to do?\n\n"
                "✅ Reply *approve* to add a caption and publish\n"
                "🔄 Reply *regenerate* to create a new ad reel"
            ),
        })

    except Exception as exc:
        logger.error("create_ad_reel_bg failed for %s: %s", phone, exc)
        session.step = STEP_CHOOSE_CONTENT_TYPE
        save_fn(session)
        send_fn(phone, {
            "kind": "text",
            "text": (
                f"⚠️ Something went wrong creating your Ad Reel: {exc}\n\n"
                "Type *reel* to try again, or choose a different content type."
            ),
        })
