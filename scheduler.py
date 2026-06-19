"""
Daily proactive content scheduler for BeeQ.

Every minute this module checks all active sessions. For any user whose local
time is 08:00–08:04 AM and who hasn't received a suggestion today, it picks the
content type from their 30-day calendar plan, generates the content, uploads to
S3, and sends it via WhatsApp.

Monthly limits  (resets each calendar month):
  image_post : 10
  carousel   :  8
  reel       : 12

Reel type distribution:
  cinematic  : 70 %
  ugc        : 20 %
  ad (full)  : 10 %

Key behaviours:
- Content always uploaded to S3 before sending (no Replicate TTL expiry)
- If previous day's suggestion is still pending approval, it is dismissed and
  a fresh suggestion is generated for today — no blocking.
- Calendar token is sha256(phone)[:16], exposed at /calendar/<token>
"""

from __future__ import annotations

import hashlib
import logging
import random
import threading
import time
from datetime import datetime, timedelta, timezone

import pytz

import config
import db
from session_store import MONTHLY_LIMITS, get_session, save_session

_logger = logging.getLogger(__name__)

_scheduler_started = False
_scheduler_lock = threading.Lock()

_REEL_WEIGHTS = [
    ("cinematic", 70),
    ("ugc",       20),
    ("ad",        10),
]


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def calendar_token(phone: str) -> str:
    """Deterministic public token for a phone number."""
    return hashlib.sha256(phone.encode()).hexdigest()[:16]


def calendar_url(phone: str) -> str:
    """Public URL for the user's content calendar (foundabee.com/calendar/{token})."""
    token = calendar_token(phone)
    return f"https://foundabee.com/calendar/{token}"


def _post_calendar_to_backend(phone: str, session, days: list[dict]) -> str:
    """
    POST the calendar to the Foundabee backend API.
    Returns the public calendar URL on success, falls back to local URL on failure.
    """
    import requests as _req
    backend_url = (config.FOUNDABEE_API_URL or "https://api.foundabee.com").rstrip("/")
    try:
        resp = _req.post(
            f"{backend_url}/v1/beeq/calendar",
            json={
                "phone_number": phone,
                "email":        session.verified_email or "",
                "brand_name":   session.brand_name or "Your Brand",
                "days":         days,
            },
            timeout=15,
        )
        resp.raise_for_status()
        url = calendar_url(phone)  # always use foundabee.com/calendar/{token}
        # Also cache locally so the bot can re-send without hitting the API
        db.save_content_calendar(
            phone_number=phone,
            token=calendar_token(phone),
            brand_name=session.brand_name or "Your Brand",
            days=days,
            calendar_url=url,
        )
        return url
    except Exception as exc:
        _logger.warning("Failed to POST calendar to backend: %s", exc)
        # Fallback: save locally and return local URL
        db.save_content_calendar(
            phone_number=phone,
            token=calendar_token(phone),
            brand_name=session.brand_name or "Your Brand",
            days=days,
        )
        return calendar_url(phone)


def generate_and_save_calendar(phone: str, session) -> str:
    """
    Generate a 30-day content calendar for this user, push to the Foundabee
    backend (so it's accessible at foundabee.com/calendar/{token}), and return
    the public URL.
    """
    from tools import groq_ai
    from datetime import datetime as _dt

    start_date = _dt.now(timezone.utc).strftime("%Y-%m-%d")
    brand = session.brand_profile()
    try:
        days = groq_ai.generate_30_day_calendar(brand, start_date)
    except Exception as exc:
        _logger.warning("generate_30_day_calendar failed: %s", exc)
        days = _fallback_calendar(start_date)

    return _post_calendar_to_backend(phone, session, days)


def _fallback_calendar(start_date: str) -> list[dict]:
    """Simple alternating calendar if Groq fails."""
    from datetime import datetime as _dt, timedelta as _td
    types = (["image_post"] * 10 + ["carousel"] * 8 + ["reel"] * 12)
    random.shuffle(types)
    start = _dt.strptime(start_date, "%Y-%m-%d")
    reel_types = ["cinematic"] * 8 + ["ugc"] * 3 + ["ad"] * 1
    random.shuffle(reel_types)
    ri = 0
    days = []
    for i, ct in enumerate(types):
        rt = None
        if ct == "reel":
            rt = reel_types[ri % len(reel_types)]
            ri += 1
        days.append({
            "day": i + 1,
            "date": (start + _td(days=i)).strftime("%Y-%m-%d"),
            "content_type": ct,
            "reel_type": rt,
            "topic": f"Day {i+1} — {ct.replace('_',' ').title()} content",
            "caption_idea": "",
            "status": "pending",
        })
    return days


# ---------------------------------------------------------------------------
# Scheduler loop
# ---------------------------------------------------------------------------

def start() -> None:
    """Start the scheduler background thread (idempotent)."""
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler_started = True
    t = threading.Thread(target=_scheduler_loop, daemon=True, name="beeq-scheduler")
    t.start()
    _logger.info("BeeQ daily scheduler started")


def _scheduler_loop() -> None:
    while True:
        try:
            _tick()
        except Exception as exc:
            _logger.error("scheduler tick error: %s", exc)
        time.sleep(60)


def _tick() -> None:
    from db import _session_cache
    now_utc = datetime.now(timezone.utc)
    for phone, session_dict in list(_session_cache.items()):
        try:
            _maybe_send_suggestion(phone, session_dict, now_utc)
        except Exception as exc:
            _logger.warning("scheduler: error for %s: %s", phone, exc)


def _maybe_send_suggestion(phone: str, session_dict: dict, now_utc: datetime) -> None:
    if not session_dict.get("onboarding_complete"):
        return
    if not session_dict.get("verified_enterprise"):
        return

    tz_str = session_dict.get("user_timezone")
    if not tz_str:
        return

    try:
        tz = pytz.timezone(tz_str)
        local_now = now_utc.astimezone(tz)
    except Exception:
        return

    if local_now.hour != 8 or local_now.minute > 4:
        return

    today_str = local_now.strftime("%Y-%m-%d")
    if session_dict.get("last_daily_suggestion_date") == today_str:
        return

    # ── Dismiss any stale pending suggestion from a previous day ──
    session = get_session(phone)
    if session.daily_suggestion and session.last_daily_suggestion_date != today_str:
        _logger.info("scheduler: clearing stale suggestion for %s", phone)
        # Mark the previous day's draft as skipped in DB if it has a post_id
        post_id = (session.daily_suggestion or {}).get("post_id")
        if post_id:
            try:
                db.get_db().posts.update_one(
                    {"_id": __import__("bson").ObjectId(post_id)},
                    {"$set": {"status": "skipped"}},
                )
            except Exception:
                pass
        session.daily_suggestion = None
        # Don't reset step if user is mid-flow; only reset if stuck on daily_suggestion
        from session_store import STEP_DAILY_SUGGESTION, STEP_DAILY_SUGGESTION_PUBLISH, STEP_CHOOSE_CONTENT_TYPE
        if session.step in (STEP_DAILY_SUGGESTION, STEP_DAILY_SUGGESTION_PUBLISH):
            session.step = STEP_CHOOSE_CONTENT_TYPE

    # ── Pick today's content from calendar or quota ──
    content_type, reel_type = _pick_from_calendar(phone, today_str, local_now)

    if content_type is None:
        _logger.info("scheduler: %s — quota exhausted, skipping", phone)
        return

    session.last_daily_suggestion_date = today_str
    save_session(session)

    _logger.info("scheduler: firing daily %s for %s", content_type, phone)
    threading.Thread(
        target=_generate_and_send_suggestion,
        args=(phone, content_type, reel_type),
        daemon=True,
        name=f"daily-{phone[-6:]}",
    ).start()


def _pick_from_calendar(phone: str, today_str: str, local_now) -> tuple[str | None, str | None]:
    """
    Try to get today's content type from the 30-day calendar.
    Falls back to quota-weighted random pick.
    """
    cal = db.get_content_calendar(phone)
    if cal:
        for day in cal.get("days", []):
            if day.get("date") == today_str and day.get("status") == "pending":
                return day.get("content_type"), day.get("reel_type")

    # Fallback: quota-based random
    return _pick_content_type(phone, local_now.year, local_now.month)


def _pick_content_type(phone: str, year: int, month: int) -> tuple[str | None, str | None]:
    monthly = db.get_monthly_counts(phone, year, month)
    remaining = {
        ct: max(0, MONTHLY_LIMITS[ct] - monthly.get(ct, 0))
        for ct in MONTHLY_LIMITS
    }
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

    total = sum(w for _, _, w in pool)
    r = random.uniform(0, total)
    cumulative = 0
    for content_type, reel_type, w in pool:
        cumulative += w
        if r <= cumulative:
            return content_type, reel_type
    ct, rt, _ = pool[-1]
    return ct, rt


# ---------------------------------------------------------------------------
# Content generation + send
# ---------------------------------------------------------------------------

def _generate_and_send_suggestion(phone: str, content_type: str, reel_type: str | None) -> None:
    import workflow as wf
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
        _logger.error("daily suggestion failed for %s: %s", phone, exc)
        wf._send_async(phone, {"kind": "text",
                               "text": "🐝 Good morning! I tried to prepare today's content but hit a snag. "
                                       "Type *create* whenever you're ready!"})


def _upload_to_s3(image_url_raw: str, folder: str = "daily") -> str:
    """
    Download from any URL (including Replicate) and upload to S3.
    Returns the permanent S3 presigned URL, or the original URL on failure.
    """
    try:
        import requests as _req
        from tools import aws_storage
        resp = _req.get(image_url_raw, timeout=30)
        resp.raise_for_status()
        result = aws_storage.upload_bytes(
            resp.content,
            content_type="image/jpeg",
            extension="jpg",
            folder=folder,
        )
        s3_url = result.get("s3_url") or ""
        if s3_url:
            return s3_url
    except Exception as exc:
        _logger.warning("_upload_to_s3 failed: %s", exc)
    return image_url_raw


def _send_post_suggestion(phone, session, brand, brand_name, wf) -> None:
    from tools import groq_ai, image_gen, aws_storage
    from tools.carousel_composer import stamp_post_image

    description = (
        f"Daily social media post for {brand.get('brand_name', 'our brand')}. "
        f"{brand.get('brand_description', '')}. Goal: {brand.get('social_goal', 'engagement')}."
    )
    # Check calendar for today's topic
    _enrich_description_from_calendar(phone, description)

    prompts = groq_ai.generate_image_prompts(description, count=1, brand=brand)
    prompt_text = prompts[0] if prompts else description
    image_url_raw = image_gen.generate_image(prompt_text, aspect_ratio="1:1")
    if not image_url_raw:
        raise RuntimeError("image generation returned None")

    # Always upload to S3 (never rely on Replicate URL)
    logo_url = session.brand_logo_url
    try:
        import requests as _req
        img_bytes = _req.get(image_url_raw, timeout=30).content
        stamped = stamp_post_image(
            img_bytes,
            brand.get("brand_name", ""),
            brand.get("brand_name", ""),
            avatar_url=logo_url,
        )
        result = aws_storage.upload_bytes(stamped, content_type="image/jpeg", extension="jpg", folder="daily")
        s3_url = result.get("s3_url") or _upload_to_s3(image_url_raw)
    except Exception:
        s3_url = _upload_to_s3(image_url_raw)

    caption = groq_ai.generate_caption(description, "image_post", brand.get("website_url", ""))

    post_id = db.log_post(
        phone_number=phone,
        content_type="image_post",
        image_urls=[s3_url],
        caption=caption,
        prompts=[prompt_text],
        status="draft",
    )

    # Update calendar day status
    _mark_calendar_day(phone, "pending")

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
                           "text": f"🌅 *Good morning, {brand_name}!*\n\nHere's your post for today 👇"})
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

    topic = _get_calendar_topic(phone) or (
        f"{brand.get('social_goal', 'industry insights')} for {brand.get('brand_name', 'our brand')}"
    )
    slides_data = groq_ai.generate_research_carousel_content(topic, brand, slide_count=4)
    brand_colors_hex = groq_ai.get_brand_hex_colors(brand.get("brand_colors", ""))
    slides_list = make_research_carousel(
        slides_data,
        username=brand.get("brand_name", ""),
        brand_name=brand.get("brand_name", ""),
        avatar_url=session.brand_logo_url,
        brand_colors=brand_colors_hex,
    )
    # Upload all slides to S3 and use the cover as preview
    s3_urls = []
    for slide_bytes in slides_list:
        result = aws_storage.upload_bytes(slide_bytes, content_type="image/jpeg", extension="jpg", folder="daily")
        url = result.get("s3_url") or ""
        if url:
            s3_urls.append(url)

    s3_url = s3_urls[0] if s3_urls else ""
    if not s3_url:
        raise RuntimeError("carousel upload failed")

    caption = groq_ai.generate_caption(topic, "carousel", brand.get("website_url", ""))

    post_id = db.log_post(
        phone_number=phone,
        content_type="carousel",
        image_urls=s3_urls,
        caption=caption,
        prompts=[topic],
        status="draft",
    )

    _mark_calendar_day(phone, "pending")

    session = get_session(phone)
    from session_store import STEP_DAILY_SUGGESTION
    session.daily_suggestion = {
        "content_type": "carousel",
        "image_urls": s3_urls,
        "caption": caption,
        "reel_type": None,
        "post_id": post_id,
    }
    session.step = STEP_DAILY_SUGGESTION
    save_session(session)

    wf._send_async(phone, {"kind": "text",
                           "text": f"🌅 *Good morning, {brand_name}!*\n\nHere's your carousel for today 👇"})
    time.sleep(1)
    wf._send_async(phone, {"kind": "media", "text": f"_{caption}_", "media_url": s3_url})
    time.sleep(0.8)
    wf._send_async(phone, {"kind": "text",
                           "text": "Reply:\n✅ *post now* — publish immediately\n"
                                   "⏰ *schedule* — pick a time\n"
                                   "⏭ *skip* — dismiss for today"})


def _send_reel_suggestion(phone, session, brand, brand_name, reel_type, wf) -> None:
    from session_store import STEP_DAILY_SUGGESTION

    reel_label = {"cinematic": "cinematic product reel", "ugc": "UGC-style reel",
                  "ad": "full ad reel"}.get(reel_type or "cinematic", "reel")

    topic = _get_calendar_topic(phone) or ""
    topic_line = f"\n📋 *Today's topic:* _{topic}_" if topic else ""

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
                                   f"Today's content pick: a *{reel_label}* 🎬{topic_line}\n\n"
                                   f"Reply:\n"
                                   f"✅ *make it* — start the reel now\n"
                                   f"⏭ *skip* — dismiss for today"})


# ---------------------------------------------------------------------------
# Calendar helpers
# ---------------------------------------------------------------------------

def _get_calendar_topic(phone: str) -> str | None:
    """Return today's topic from the content calendar if available."""
    from datetime import datetime as _dt
    today_str = _dt.now(timezone.utc).strftime("%Y-%m-%d")
    cal = db.get_content_calendar(phone)
    if not cal:
        return None
    for day in cal.get("days", []):
        if day.get("date") == today_str:
            return day.get("topic") or None
    return None


def _enrich_description_from_calendar(phone: str, default: str) -> str:
    topic = _get_calendar_topic(phone)
    return topic if topic else default


def _mark_calendar_day(phone: str, status: str) -> None:
    """Mark today's calendar day with the given status."""
    from datetime import datetime as _dt
    today_str = _dt.now(timezone.utc).strftime("%Y-%m-%d")
    cal = db.get_content_calendar(phone)
    if not cal:
        return
    for i, day in enumerate(cal.get("days", [])):
        if day.get("date") == today_str:
            db.update_calendar_day_status(phone, i, status)
            break
