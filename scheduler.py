"""
Daily proactive content scheduler for BeeQ.

Every minute this module checks all active sessions. For any user whose local
time is 08:00–08:04 AM and who hasn't received a suggestion today, it picks a
content type based on their remaining monthly quota, generates the content in a
background thread, and sends it via WhatsApp.

Monthly limits  (resets each calendar month):
  image_post : 10
  carousel   :  8
  reel       : 12

Reel type distribution:
  cinematic  : 70 %
  ugc        :  2 0 %
  ad (full)  : 10 %
"""

from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime, timezone

import pytz

import db
from session_store import MONTHLY_LIMITS, get_session, save_session

_logger = logging.getLogger(__name__)

# Avoid importing workflow at module level (circular import risk).
# Instead, import lazily inside functions.

_scheduler_started = False
_scheduler_lock = threading.Lock()

# Reel type weights (must sum to 100)
_REEL_WEIGHTS = [
    ("cinematic", 70),
    ("ugc",       20),
    ("ad",        10),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start() -> None:
    """Start the scheduler background thread (idempotent — safe to call multiple times)."""
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler_started = True
    t = threading.Thread(target=_scheduler_loop, daemon=True, name="beeq-scheduler")
    t.start()
    _logger.info("BeeQ daily scheduler started")


# ---------------------------------------------------------------------------
# Internal loop
# ---------------------------------------------------------------------------

def _scheduler_loop() -> None:
    """Run forever; wake every 60 s and check all active sessions."""
    while True:
        try:
            _tick()
        except Exception as exc:
            _logger.error("scheduler tick error: %s", exc)
        time.sleep(60)


def _tick() -> None:
    """Single tick — iterate all cached sessions and fire daily suggestions."""
    from db import _session_cache  # in-memory cache of all sessions
    now_utc = datetime.now(timezone.utc)

    for phone, session_dict in list(_session_cache.items()):
        try:
            _maybe_send_suggestion(phone, session_dict, now_utc)
        except Exception as exc:
            _logger.warning("scheduler: error for %s: %s", phone, exc)


def _maybe_send_suggestion(phone: str, session_dict: dict, now_utc: datetime) -> None:
    """Check one session and fire a suggestion if conditions are met."""
    # Must be fully onboarded + verified
    if not session_dict.get("onboarding_complete"):
        return
    if not session_dict.get("verified_enterprise"):
        return

    # Must have a timezone set
    tz_str = session_dict.get("user_timezone")
    if not tz_str:
        return

    # Check local time is 08:00–08:04
    try:
        tz = pytz.timezone(tz_str)
        local_now = now_utc.astimezone(tz)
    except Exception:
        return

    if local_now.hour != 8 or local_now.minute > 4:
        return

    # Haven't already sent one today
    today_str = local_now.strftime("%Y-%m-%d")
    if session_dict.get("last_daily_suggestion_date") == today_str:
        return

    # Pick content type based on remaining monthly quota
    content_type, reel_type = _pick_content_type(phone, local_now.year, local_now.month)
    if content_type is None:
        _logger.info("scheduler: %s — monthly quota exhausted, skipping", phone)
        return

    # Mark sent today immediately so a second tick in the same window doesn't fire again
    session = get_session(phone)
    session.last_daily_suggestion_date = today_str
    save_session(session)

    _logger.info("scheduler: sending daily %s suggestion to %s", content_type, phone)
    threading.Thread(
        target=_generate_and_send_suggestion,
        args=(phone, content_type, reel_type),
        daemon=True,
        name=f"daily-{phone[-6:]}",
    ).start()


def _pick_content_type(phone: str, year: int, month: int) -> tuple[str | None, str | None]:
    """
    Choose a content type weighted by remaining monthly quota.
    Returns (content_type, reel_type) — reel_type is None for non-reels.
    """
    monthly = db.get_monthly_counts(phone, year, month)
    remaining = {
        ct: max(0, MONTHLY_LIMITS[ct] - monthly.get(ct, 0))
        for ct in MONTHLY_LIMITS
    }

    # Build weighted pool based on remaining capacity
    pool: list[tuple[str, str | None, int]] = []
    if remaining["image_post"] > 0:
        pool.append(("image_post", None, remaining["image_post"]))
    if remaining["carousel"] > 0:
        pool.append(("carousel", None, remaining["carousel"]))
    if remaining["reel"] > 0:
        for reel_type, weight in _REEL_WEIGHTS:
            pool.append(("reel", reel_type, weight))

    if not pool:
        return None, None

    # Weighted random pick
    total = sum(w for _, _, w in pool)
    r = random.uniform(0, total)
    cumulative = 0
    for content_type, reel_type, w in pool:
        cumulative += w
        if r <= cumulative:
            return content_type, reel_type

    # Fallback
    ct, rt, _ = pool[-1]
    return ct, rt


# ---------------------------------------------------------------------------
# Content generation + send
# ---------------------------------------------------------------------------

def _generate_and_send_suggestion(phone: str, content_type: str, reel_type: str | None) -> None:
    """Generate content and send the daily suggestion message."""
    import workflow as wf  # lazy import to avoid circular

    session = get_session(phone)
    brand = session.brand_profile()
    brand_name = session.brand_name or "your brand"

    try:
        if content_type == "image_post":
            _send_post_suggestion(phone, session, brand, brand_name, wf)
        elif content_type == "carousel":
            _send_carousel_suggestion(phone, session, brand, brand_name, wf)
        elif content_type == "reel":
            _send_reel_suggestion(phone, session, brand, brand_name, reel_type, wf)
    except Exception as exc:
        _logger.error("daily suggestion generation failed for %s: %s", phone, exc)
        wf._send_async(phone, {"kind": "text",
                               "text": "🐝 Good morning! I tried to prepare today's content but hit a snag. "
                                       "Type *create* whenever you're ready and I'll get started!"})


def _send_post_suggestion(phone, session, brand, brand_name, wf) -> None:
    from tools import groq_ai, image_gen, aws_storage
    from tools.carousel_composer import stamp_post_image

    # Generate a post idea using brand description as description
    description = (
        f"Daily social media post for {brand.get('brand_name', 'our brand')}. "
        f"{brand.get('brand_description', '')}. Goal: {brand.get('social_goal', 'engagement')}."
    )
    prompts = groq_ai.generate_image_prompts(description, count=1, brand=brand)
    prompt_text = prompts[0] if prompts else description
    image_url_raw = image_gen.generate_image(prompt_text, aspect_ratio="1:1")
    if not image_url_raw:
        raise RuntimeError("image generation returned None")

    # Stamp badge
    logo_url = session.brand_logo_url
    try:
        import requests as _req
        img_bytes = _req.get(image_url_raw, timeout=15).content
        stamped = stamp_post_image(img_bytes, brand.get("brand_name", ""), brand.get("brand_name", ""), avatar_url=logo_url)
        result = aws_storage.upload_bytes(stamped, content_type="image/jpeg", extension="jpg", folder="daily")
        s3_url = result.get("s3_url") or image_url_raw
    except Exception:
        s3_url = image_url_raw

    caption = groq_ai.generate_caption(description, "image_post", brand.get("website_url", ""))

    # Save as draft
    post_id = db.log_post(
        phone_number=phone,
        content_type="image_post",
        image_urls=[s3_url],
        caption=caption,
        prompts=[prompt_text],
        status="draft",
    )

    # Store in session for approval flow
    session = get_session(phone)
    from session_store import STEP_DAILY_SUGGESTION
    session.daily_suggestion = {
        "content_type": "image_post",
        "image_urls": [s3_url],
        "caption": caption,
        "reel_type": None,
        "post_id": post_id,
    }
    session.step = STEP_DAILY_SUGGESTION
    save_session(session)

    wf._send_async(phone, {"kind": "text",
                           "text": f"🌅 *Good morning, {brand_name}!*\n\n"
                                   f"Here's your post for today 👇"})
    time.sleep(1)
    wf._send_async(phone, {"kind": "media", "text": f"_{caption}_", "media_url": s3_url})
    time.sleep(0.8)
    wf._send_async(phone, {"kind": "text",
                           "text": "Reply:\n✅ *post now* — publish immediately\n"
                                   "⏰ *schedule* — pick a time\n"
                                   "⏭ *skip* — dismiss for today"})


def _send_carousel_suggestion(phone, session, brand, brand_name, wf) -> None:
    from tools import groq_ai, aws_storage
    from tools.carousel_composer import make_research_carousel

    # Generate a relevant topic for this brand
    topic = (
        f"{brand.get('social_goal', 'industry insights')} for "
        f"{brand.get('brand_name', 'our brand')}"
    )
    slides_data = groq_ai.generate_research_carousel_content(topic, brand, slide_count=4)
    from tools import groq_ai as _groq
    brand_colors_hex = _groq.get_brand_hex_colors(brand.get("brand_colors", ""))
    slides_list = make_research_carousel(
        slides_data,
        username=brand.get("brand_name", ""),
        brand_name=brand.get("brand_name", ""),
        avatar_url=session.brand_logo_url,
        brand_colors=brand_colors_hex,
    )
    # Take the first slide (cover) as the preview image
    carousel_bytes = slides_list[0] if slides_list else b""
    result = aws_storage.upload_bytes(carousel_bytes, content_type="image/jpeg", extension="jpg", folder="daily")
    s3_url = result.get("s3_url") or ""
    caption = groq_ai.generate_caption(topic, "carousel", brand.get("website_url", ""))

    post_id = db.log_post(
        phone_number=phone,
        content_type="carousel",
        image_urls=[s3_url],
        caption=caption,
        prompts=[topic],
        status="draft",
    )

    session = get_session(phone)
    from session_store import STEP_DAILY_SUGGESTION
    session.daily_suggestion = {
        "content_type": "carousel",
        "image_urls": [s3_url],
        "caption": caption,
        "reel_type": None,
        "post_id": post_id,
    }
    session.step = STEP_DAILY_SUGGESTION
    save_session(session)

    wf._send_async(phone, {"kind": "text",
                           "text": f"🌅 *Good morning, {brand_name}!*\n\n"
                                   f"Here's your carousel for today 👇"})
    time.sleep(1)
    wf._send_async(phone, {"kind": "media", "text": f"_{caption}_", "media_url": s3_url})
    time.sleep(0.8)
    wf._send_async(phone, {"kind": "text",
                           "text": "Reply:\n✅ *post now* — publish immediately\n"
                                   "⏰ *schedule* — pick a time\n"
                                   "⏭ *skip* — dismiss for today"})


def _send_reel_suggestion(phone, session, brand, brand_name, reel_type, wf) -> None:
    """For reels, we can't auto-generate the full reel without user assets.
    Instead, notify the user and pre-fill the reel flow."""
    from session_store import STEP_DAILY_SUGGESTION

    reel_label = {"cinematic": "cinematic product reel", "ugc": "UGC-style reel",
                  "ad": "full ad reel"}.get(reel_type or "cinematic", "reel")

    # Save placeholder in session
    session = get_session(phone)
    session.daily_suggestion = {
        "content_type": "reel",
        "image_urls": [],
        "caption": "",
        "reel_type": reel_type,
        "post_id": None,
    }
    session.step = STEP_DAILY_SUGGESTION
    save_session(session)

    wf._send_async(phone, {"kind": "text",
                           "text": f"🌅 *Good morning, {brand_name}!*\n\n"
                                   f"Today's content pick: a *{reel_label}* 🎬\n\n"
                                   f"Reply:\n"
                                   f"✅ *make it* — start the reel now\n"
                                   f"⏭ *skip* — dismiss for today"})
