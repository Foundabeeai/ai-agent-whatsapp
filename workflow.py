"""
BeeQ WhatsApp Automation — full workflow orchestration.

Flow:
  awaiting_email
    → verifying_email  (async, background thread)
    → collect_instagram
    → onboarding_brand … onboarding_report_freq  (first-time users)
    → choose_content_type   [interactive buttons: Image / Carousel / Reel]
    → collect_description   (free text)
    → generating            (async: Groq prompts → Replicate images → S3 upload)
    → choose_caption        [interactive buttons: AI caption / Custom caption]
    → awaiting_custom_caption  (free text, only if custom chosen)
    → choose_publish_action [interactive buttons: Publish Now / Schedule]
    → awaiting_schedule_time   (free text datetime, only if schedule chosen)
    → publishing            (async: publish/schedule via connected account)
    → back to choose_content_type for next post
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from dateutil import parser as dateutil_parser
import pytz

import config
import db
from session_store import (
    UserSession,
    get_session,
    save_session,
    STEP_AWAITING_EMAIL,
    STEP_VERIFYING_EMAIL,
    STEP_COLLECT_INSTAGRAM,
    STEP_CHOOSE_CONTENT_TYPE,
    STEP_COLLECT_DESCRIPTION,
    STEP_GENERATING,
    STEP_CHOOSE_CAPTION,
    STEP_AWAITING_CUSTOM_CAPTION,
    STEP_CHOOSE_PUBLISH_ACTION,
    STEP_AWAITING_SCHEDULE_TIME,
    STEP_PUBLISHING,
    STEP_ONBOARDING_BRAND,
    STEP_ONBOARDING_GOAL,
    STEP_ONBOARDING_WEBSITE,
    STEP_ONBOARDING_VOICE,
    STEP_ONBOARDING_VOICE_CUSTOM,
    STEP_ONBOARDING_COLORS,
    STEP_ONBOARDING_REFERENCE,
    STEP_ONBOARDING_COMPETITORS,
    STEP_ONBOARDING_ASSETS,
    STEP_ONBOARDING_SCHEDULE,
    STEP_ONBOARDING_REPORT_FREQ,
    STEP_RECHECKING_PLAN,
    STEP_AWAITING_IMAGE_APPROVAL,
    STEP_COLLECT_PRODUCT_IMAGE,
    STEP_PUBLISH_FAILED,
    STEP_INITIAL_CONTENT_REVIEW,
    STEP_INITIAL_CONTENT_SCHEDULE,
    STEP_ONBOARDING_TIMEZONE,
    STEP_VOICE_CONFIRM,
    STEP_REEL_TYPE_SELECT,
    STEP_REEL_PRODUCT_IMAGE,
    STEP_REEL_DESCRIBE_PRODUCT,
    STEP_REEL_UGC_DESCRIBE,
    STEP_REEL_UGC_SCRIPT_REVIEW,
    STEP_REEL_USER_PHOTO,
    STEP_REEL_VOICE_SELECT,
    STEP_REEL_VOICE_CLONE,
    STEP_REEL_AD_PRODUCT_IMAGE,
    STEP_REEL_AD_DESCRIBE,
    STEP_REEL_AD_SCRIPT_REVIEW,
    STEP_REEL_AD_USER_PHOTO,
    STEP_REEL_APPROVAL,
    STEP_DAILY_SUGGESTION,
    STEP_DAILY_SUGGESTION_PUBLISH,
)
from tools import check_user, groq_ai, image_gen, aws_storage, zerini
from tools.beeq_voice import msg as beeq, dynamic as beeq_dyn
from tools import video_gen as video_gen_tools
from tools import reel_composer
from tools import voice as voice_tools
from tools.carousel_composer import make_research_carousel, stamp_post_image
from tools.whatsapp import (
    dispatch,
    send_text,
    send_image,
    send_content_type_menu,
    send_caption_choice_menu,
    send_publish_action_menu,
    send_image_count_menu,
)


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Per-phone send lock — ensures background threads never interleave messages
_send_locks: dict[str, threading.Lock] = {}
_send_locks_mutex = threading.Lock()

def _get_send_lock(phone: str) -> threading.Lock:
    with _send_locks_mutex:
        if phone not in _send_locks:
            _send_locks[phone] = threading.Lock()
        return _send_locks[phone]

# Voice mode tracking — phones currently in a voice conversation.
# Value is the epoch time when voice mode expires (60s window).
# When a phone is in voice mode, _send_async automatically fires TTS for every text message.
_voice_mode_phones: dict[str, float] = {}
_voice_mode_mutex = threading.Lock()

def _set_voice_mode(phone: str, duration: float = 60.0) -> None:
    """Mark `phone` as being in voice mode for `duration` seconds."""
    import time as _t
    with _voice_mode_mutex:
        _voice_mode_phones[phone] = _t.time() + duration

def _is_voice_mode(phone: str) -> bool:
    import time as _t
    with _voice_mode_mutex:
        expires = _voice_mode_phones.get(phone, 0)
        if _t.time() < expires:
            return True
        _voice_mode_phones.pop(phone, None)
        return False

# Fallback status messages shown when user messages during background work.
# Background workers overwrite session.bg_status with more specific text.
_DEFAULT_STATUS = {
    STEP_VERIFYING_EMAIL:        "🔍 Still verifying your account — hang tight!",
    STEP_RECHECKING_PLAN:        "🔍 Checking your account — one moment...",
    "resolving_instagram":       "🔍 Looking up your Instagram account — almost done!",
    STEP_GENERATING:             "🎨 Generating your images — still working on it!",
    STEP_PUBLISHING:             "📤 Publishing your post — almost there!",
    STEP_REEL_APPROVAL:          "🎬 Your reel is being created — this takes a few minutes. Hang tight!",
}

def _parse_user_time(text: str, user_timezone: str | None) -> datetime | None:
    """
    Parse a natural-language date/time string from the user.
    If `user_timezone` is a valid IANA string (e.g. "Asia/Kolkata"), the parsed
    time is treated as local to that zone then converted to UTC.
    Falls back to UTC if no timezone is set or parsing fails.
    Returns a timezone-aware UTC datetime, or None on failure.
    """
    try:
        dt = dateutil_parser.parse(text, fuzzy=True)
    except Exception:
        return None

    if dt.tzinfo is not None:
        # User explicitly specified a timezone offset — honour it, convert to UTC
        return dt.astimezone(pytz.utc)

    # No tz in the string — apply user's timezone if known
    if user_timezone:
        try:
            tz = pytz.timezone(user_timezone)
            dt = tz.localize(dt)
            return dt.astimezone(pytz.utc)
        except Exception:
            pass

    # Fallback: assume UTC
    return dt.replace(tzinfo=pytz.utc)


def _friendly_time(dt: datetime, user_timezone: str | None) -> str:
    """Format a UTC datetime as a friendly local time string for the user."""
    if user_timezone:
        try:
            tz = pytz.timezone(user_timezone)
            local_dt = dt.astimezone(tz)
            tz_abbr = local_dt.strftime("%Z")
            return local_dt.strftime(f"%B %d, %Y at %I:%M %p {tz_abbr}")
        except Exception:
            pass
    return dt.strftime("%B %d, %Y at %H:%M UTC")


# ---------------------------------------------------------------------------
# Voice helpers
# ---------------------------------------------------------------------------

def _is_audio_media(media_types: list[str]) -> bool:
    """Return True if any media item is an audio type (WhatsApp voice message)."""
    return any(
        mt.startswith("audio/") or "ogg" in mt or "mpeg" in mt or "mp4" in mt or "3gpp" in mt
        for mt in (media_types or [])
    )


def _first_audio_url(media_urls: list[str], media_types: list[str]) -> str | None:
    """Return the URL of the first audio media item."""
    for url, mt in zip(media_urls or [], media_types or []):
        if mt.startswith("audio/") or "ogg" in mt or "mpeg" in mt or "mp4" in mt or "3gpp" in mt:
            return url
    return None


# Fields the bot asks about, in rough onboarding order — used to tell Groq what to look for
_ONBOARDING_FIELD_ORDER = [
    "brand_name", "brand_description", "social_goal", "website_url",
    "brand_voice", "brand_colors", "competitor_handles", "posting_schedule",
    "report_frequency", "user_timezone",
]
_CONTENT_FIELDS = ["content_type", "description", "image_count", "publish_action", "scheduled_at"]
_ACCOUNT_FIELDS = ["instagram_username"]


def _pending_fields_for_step(step: str, session) -> list[str]:
    """
    Return the list of fields that are still unknown/needed for the current step
    and upcoming steps — so Groq can try to fill them all from one voice message.
    """
    fields: list[str] = []

    onboarding_steps = {
        STEP_ONBOARDING_BRAND:       ["brand_name", "brand_description"],
        STEP_ONBOARDING_GOAL:        ["social_goal"],
        STEP_ONBOARDING_WEBSITE:     ["website_url"],
        STEP_ONBOARDING_VOICE:       ["brand_voice"],
        STEP_ONBOARDING_VOICE_CUSTOM:["brand_voice"],
        STEP_ONBOARDING_COLORS:      ["brand_colors"],
        STEP_ONBOARDING_REFERENCE:   [],
        STEP_ONBOARDING_COMPETITORS: ["competitor_handles"],
        STEP_ONBOARDING_ASSETS:      [],
        STEP_ONBOARDING_SCHEDULE:    ["posting_schedule"],
        STEP_ONBOARDING_REPORT_FREQ: ["report_frequency"],
        STEP_ONBOARDING_TIMEZONE:    ["user_timezone"],
        STEP_COLLECT_INSTAGRAM:      ["instagram_username"],
        STEP_CHOOSE_CONTENT_TYPE:    ["content_type"],
        STEP_COLLECT_DESCRIPTION:    ["description", "image_count"],
        STEP_CHOOSE_PUBLISH_ACTION:  ["publish_action"],
        STEP_AWAITING_SCHEDULE_TIME: ["scheduled_at"],
        STEP_CHOOSE_CAPTION:         [],
    }

    # Always include the current step's fields
    fields.extend(onboarding_steps.get(step, []))

    # For onboarding, also include all subsequent empty onboarding fields so the
    # user can answer everything in one breath
    if step.startswith("onboarding_"):
        remaining_onboarding = [
            f for f in _ONBOARDING_FIELD_ORDER
            if not getattr(session, f, None)
        ]
        for f in remaining_onboarding:
            if f not in fields:
                fields.append(f)

    # Always try to grab content fields if we're past onboarding
    if session.onboarding_complete:
        for f in _CONTENT_FIELDS:
            if f not in fields and not getattr(session, f, None):
                fields.append(f)

    return fields


def _apply_voice_answers(session, answers: dict) -> list[str]:
    """
    Write extracted answers into session fields.
    Returns a list of field names that were successfully applied.
    """
    applied: list[str] = []

    # Fields that should only be set once (onboarding/account setup — never overwrite)
    once_fields = {
        "brand_name", "brand_description", "social_goal", "website_url",
        "brand_voice", "brand_colors", "posting_schedule", "report_frequency",
        "user_timezone", "instagram_username",
    }
    # Fields that should always be updated when voice provides them (content/publish fields)
    always_fields = {"description", "publish_action", "scheduled_at"}

    for f in once_fields:
        if f in answers and answers[f] and not getattr(session, f, None):
            setattr(session, f, str(answers[f]).strip())
            applied.append(f)

    for f in always_fields:
        if f in answers and answers[f]:
            setattr(session, f, str(answers[f]).strip())
            applied.append(f)

    # content_type — always override when voice explicitly specifies it.
    # Don't guard with "not session.content_type" — if the user says "carousel"
    # while reviewing an image post, they are requesting a type change.
    if "content_type" in answers:
        ct = str(answers["content_type"]).strip().lower()
        if ct in {"image_post", "carousel", "reel"} and ct != session.content_type:
            session.content_type = ct
            # Reset image_count when switching types so we ask / use correct defaults
            session.image_count = 1
            applied.append("content_type")

    # image_count — integer; apply for all types including carousel
    # (if user explicitly said "4 images" in voice, honour it and skip the count question)
    if "image_count" in answers and session.image_count == 1:
        try:
            n = max(2, min(10, int(answers["image_count"])))
            session.image_count = n
            applied.append("image_count")
        except (ValueError, TypeError):
            pass

    # reel_type — set when voice mentions "cinematic" or "ugc" in the context of reels
    if "reel_type" in answers and answers["reel_type"] in {"cinematic", "ugc"}:
        session.reel_type = answers["reel_type"]
        applied.append("reel_type")

    # competitor_handles — list
    if "competitor_handles" in answers and not session.competitor_handles:
        handles = answers["competitor_handles"]
        if isinstance(handles, list):
            session.competitor_handles = [h.lstrip("@").strip() for h in handles if h]
            if session.competitor_handles:
                applied.append("competitor_handles")

    return applied


def _advance_step_after_voice(session, applied: list[str]) -> None:
    """
    After applying voice answers, advance the session step as far as possible
    based on what was filled in.
    """
    # Map step → field(s) that must be set to consider the step complete
    step_completion: list[tuple[str, list[str]]] = [
        (STEP_ONBOARDING_BRAND,        ["brand_name"]),
        (STEP_ONBOARDING_GOAL,         ["social_goal"]),
        (STEP_ONBOARDING_WEBSITE,      ["website_url"]),
        (STEP_ONBOARDING_VOICE,        ["brand_voice"]),
        (STEP_ONBOARDING_VOICE_CUSTOM, ["brand_voice"]),
        (STEP_ONBOARDING_COLORS,       ["brand_colors"]),
        (STEP_ONBOARDING_COMPETITORS,  ["competitor_handles"]),
        (STEP_ONBOARDING_SCHEDULE,     ["posting_schedule"]),
        (STEP_ONBOARDING_REPORT_FREQ,  ["report_frequency"]),
        (STEP_ONBOARDING_TIMEZONE,     ["user_timezone"]),
        (STEP_COLLECT_INSTAGRAM,       ["instagram_username"]),
        (STEP_CHOOSE_CONTENT_TYPE,     ["content_type"]),
        (STEP_COLLECT_DESCRIPTION,     ["description"]),
        (STEP_CHOOSE_PUBLISH_ACTION,   ["publish_action"]),
        (STEP_AWAITING_SCHEDULE_TIME,  ["scheduled_at"]),
    ]
    # Advance through consecutive completed steps (single level per call;
    # caller loops until session.step stops changing)
    for step, required_fields in step_completion:
        if session.step == step and all(getattr(session, f, None) for f in required_fields):
            # Step is now complete — move to the natural next step
            next_step_map = {
                STEP_ONBOARDING_BRAND:        STEP_ONBOARDING_GOAL,
                STEP_ONBOARDING_GOAL:         STEP_ONBOARDING_WEBSITE,
                STEP_ONBOARDING_WEBSITE:      STEP_ONBOARDING_VOICE,
                STEP_ONBOARDING_VOICE:        STEP_ONBOARDING_COLORS,
                STEP_ONBOARDING_VOICE_CUSTOM: STEP_ONBOARDING_COLORS,
                STEP_ONBOARDING_COLORS:       STEP_ONBOARDING_REFERENCE,
                STEP_ONBOARDING_COMPETITORS:  STEP_ONBOARDING_ASSETS,
                STEP_ONBOARDING_SCHEDULE:     STEP_ONBOARDING_REPORT_FREQ,
                STEP_ONBOARDING_REPORT_FREQ:  STEP_ONBOARDING_TIMEZONE,
                STEP_ONBOARDING_TIMEZONE:     STEP_CHOOSE_CONTENT_TYPE,
                STEP_COLLECT_INSTAGRAM:       STEP_CHOOSE_CONTENT_TYPE,
                # Reel goes to type-select, not description
                STEP_CHOOSE_CONTENT_TYPE:     (
                    STEP_REEL_TYPE_SELECT if session.content_type == "reel"
                    else STEP_COLLECT_DESCRIPTION
                ),
                # Carousel always stops at confirm_carousel_count so user picks slide count
                STEP_COLLECT_DESCRIPTION:     "confirm_carousel_count" if session.content_type == "carousel" else STEP_CHOOSE_PUBLISH_ACTION,
                STEP_CHOOSE_PUBLISH_ACTION:   STEP_AWAITING_SCHEDULE_TIME if session.publish_action == "schedule" else STEP_PUBLISHING,
            }
            if step in next_step_map:
                session.step = next_step_map[step]
            return  # one level per call; caller loops


# Quick-reply button payload values (set in Twilio Content Templates)
BTN_IMAGE_POST = "image_post"
BTN_CAROUSEL   = "carousel"
BTN_REEL       = "reel"
BTN_AI_CAPTION     = "ai_caption"
BTN_CUSTOM_CAPTION = "custom_caption"
BTN_PUBLISH_NOW = "publish_now"
BTN_SCHEDULE    = "schedule"
BTN_IMG_3 = "3"
BTN_IMG_5 = "5"


def _extract_email(text: str) -> Optional[str]:
    m = EMAIL_RE.search(text or "")
    return m.group(0) if m else None


def _choice(body: str, button_payload: Optional[str]) -> str:
    """Prefer quick-reply ButtonPayload over typed body text."""
    p = (button_payload or "").strip().lower()
    return p if p else (body or "").strip().lower()


def _send_async(to: str, payload: dict, tts: bool = False) -> None:
    """
    Send a message, holding the per-phone lock so messages never interleave.

    tts=True  → also synthesize a single audio reply (Inworld TTS) and send it after the text.
               Only set this for the FINAL "action required" message in a flow step.
               Never set it for progress/status messages — user should get exactly one audio.
    tts=False → text only, no audio. Use for all intermediate status updates.
    """
    lock = _get_send_lock(to)
    with lock:
        try:
            result = dispatch(to, payload)
            if not result.get("ok"):
                send_text(to, f"⚠️ Could not deliver message: {result.get('error')}")
        except Exception as exc:
            send_text(to, f"⚠️ Unexpected error: {exc}")

        # Fire TTS only when explicitly requested AND user is in voice mode
        if tts and _is_voice_mode(to) and payload.get("kind") == "text":
            text = payload.get("text", "")
            if text:
                def _tts_fire(phone=to, msg=text):
                    try:
                        spoken = groq_ai.voice_reply_text(msg)
                        audio_url = voice_tools.synthesize_and_upload(spoken)
                        if audio_url:
                            lk = _get_send_lock(phone)
                            with lk:
                                dispatch(phone, {"kind": "media", "text": "", "media_url": audio_url})
                    except Exception:
                        pass
                threading.Thread(target=_tts_fire, daemon=True).start()


def _voice_reply(phone: str, text: str) -> dict:
    """
    Return a {"kind":"text"} payload AND fire a single TTS audio reply in the background
    (only when the phone is currently in voice mode).
    Use this for every direct `return` from handle_incoming_message that the user needs to hear.
    """
    if _is_voice_mode(phone):
        def _tts():
            try:
                spoken = groq_ai.voice_reply_text(text)
                audio_url = voice_tools.synthesize_and_upload(spoken)
                if audio_url:
                    lk = _get_send_lock(phone)
                    with lk:
                        dispatch(phone, {"kind": "media", "text": "", "media_url": audio_url})
            except Exception:
                pass
        threading.Thread(target=_tts, daemon=True).start()
    return {"kind": "text", "text": text}


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------

def _verify_email_bg(phone: str, email: str) -> None:
    """Run BeeQ user check and advance session state (background thread)."""
    import logging as _log
    _logger = _log.getLogger(__name__)
    try:
        _verify_email_bg_inner(phone, email)
    except Exception as exc:
        _logger.error("_verify_email_bg unhandled: %s", exc)
        session = get_session(phone)
        session.step = STEP_AWAITING_EMAIL
        session.bg_status = ""
        save_session(session)
        _send_async(phone, {"kind": "text",
                            "text": "❌ Verification error. Please send your email again."})


def _verify_email_bg_inner(phone: str, email: str) -> None:
    import logging as _log
    _logger = _log.getLogger(__name__)
    session = get_session(phone)
    email = email.strip().lower()

    # ── 0. Owner bypass — always verified ────────────────────────────────────
    if email in config.OWNER_EMAILS:
        _logger.info("_verify_email_bg: owner bypass for %s", email)
        verified_user_id = email  # use email as user_id placeholder
        # Fall through to the "Verified ✅" block below
        _do_verify(session, phone, email, verified_user_id)
        return

    # ── 1 & 2: Run DB check and API check in parallel (5s total budget) ────
    import concurrent.futures as _cf

    db_check = {"enterprise": False}
    api_result = {"ok": False, "registered": False, "enterprise": False}

    with _cf.ThreadPoolExecutor(max_workers=2) as pool:
        db_fut  = pool.submit(db.check_enterprise_in_foundabee_db, email)
        api_fut = pool.submit(check_user.check_user_registration, email)
        try:
            db_check = db_fut.result(timeout=6)
        except Exception as exc:
            _logger.warning("_verify_email_bg: db_check failed: %s", exc)
        try:
            api_result = api_fut.result(timeout=6)
        except Exception as exc:
            _logger.warning("_verify_email_bg: api_check failed: %s", exc)

    _logger.info("_verify_email_bg: db=%s api=%s", db_check, api_result)

    verified_user_id = ""
    if db_check.get("enterprise"):
        verified_user_id = db_check.get("user_id") or ""
    elif api_result.get("ok") and api_result.get("registered") and api_result.get("enterprise"):
        verified_user_id = str(api_result.get("user_id") or "")
    else:
        # ── 3. Last resort: previously verified session for this email ───────
        prior = db.find_verified_session_by_email(email)
        if prior and prior.get("verified_enterprise"):
            verified_user_id = prior.get("verified_user_id") or ""
        else:
            session.step = STEP_AWAITING_EMAIL
            save_session(session)
            if not api_result.get("registered") and db_check.get("found") is False:
                _send_async(phone, {"kind": "text",
                                    "text": beeq("email_not_found")})
            elif not api_result.get("ok") and not db_check.get("enterprise"):
                _send_async(phone, {"kind": "text",
                                    "text": "❌ Could not reach the verification service. "
                                            "Please try again in a moment."})
            else:
                _send_async(phone, {"kind": "text",
                                    "text": "⚠️ Your account isn't on an enterprise plan. "
                                            "Upgrade at foundabee.com to use this."})
            return

    # Verified ✅
    _do_verify(session, phone, email, verified_user_id)


def _do_verify(session, phone: str, email: str, verified_user_id: str) -> None:
    """Finalise a verified session and route user to correct next step."""
    session.verified_email = email
    session.verified_user_id = verified_user_id
    session.verified_enterprise = True
    session.set_verified_at_now()
    session.reset_flow()

    # ── If this phone has no onboarding data, check if another phone already
    #    completed onboarding for the same email and copy the brand profile over. ──
    if not session.onboarding_complete:
        prior = db.find_verified_session_by_email(email)
        if prior and prior.get("onboarding_complete") and prior.get("phone_number") != phone:
            _BRAND_FIELDS = [
                "onboarding_complete", "brand_name", "brand_description", "social_goal",
                "website_url", "brand_voice", "brand_colors", "reference_content_url",
                "competitor_handles", "brand_assets", "posting_schedule", "report_frequency",
                "user_timezone",
            ]
            for field_name in _BRAND_FIELDS:
                if prior.get(field_name) is not None:
                    setattr(session, field_name, prior[field_name])

    user_id_line = f"\nYour user ID: `{session.verified_user_id}`" if session.verified_user_id else ""

    if session.onboarding_complete and session.has_instagram_account():
        save_session(session)
        _send_async(phone, {"kind": "text",
                            "text": (f"Welcome back! Connected to @{session.instagram_username}.{user_id_line}\n\n"
                                     f"{beeq('ask_content_type')}\n"
                                     "_Tip: type *my posts* anytime to see your queue._")})
        time.sleep(0.8)
        send_content_type_menu(phone)

    elif session.onboarding_complete and not session.has_instagram_account():
        session.step = STEP_COLLECT_INSTAGRAM
        save_session(session)
        _send_async(phone, {"kind": "text",
                            "text": (f"✅ Verified! Welcome back, {session.brand_name or 'there'}.{user_id_line}\n\n"
                                     "I already have your brand profile 🐝\n"
                                     "Just link your Instagram and you're ready to go.\n\n"
                                     "What's your Instagram username? (e.g. @yourbrand)")})
    else:
        session.step = STEP_ONBOARDING_BRAND
        save_session(session)
        _send_async(phone, {"kind": "text",
                            "text": (f"✅ Verified! Welcome to Foundabee.{user_id_line}\n\n"
                                     "👋 I'm BeeQ, your AI social media manager. "
                                     "I'll handle your content, scheduling, and performance "
                                     "tracking so you can focus on running your business.\n\n"
                                     "Let me ask a few quick questions to get set up — "
                                     "most people finish in under 10 minutes.\n\n"
                                     "*What's your business/brand called, and what do you sell or offer?*\n"
                                     "(e.g. \"Sunny Skincare - organic face creams\")")})


def _recheck_plan_bg(phone: str, previous_step: str) -> None:
    """Silently re-verify enterprise status for a returning user (no email prompt)."""
    session = get_session(phone)
    email = session.verified_email
    if not email:
        session.step = STEP_AWAITING_EMAIL
        session.verified_enterprise = False
        save_session(session)
        _send_async(phone, {"kind": "text",
                            "text": "👋 Welcome back! Please share your Foundabee account email to continue."})
        return

    # Check DB first, then API
    db_check = db.check_enterprise_in_foundabee_db(email)
    still_active = db_check.get("enterprise", False)

    if not still_active:
        try:
            result = check_user.check_user_registration(email)
            still_active = result.get("ok") and result.get("enterprise")
        except Exception:
            # On error, give benefit of the doubt and let them continue
            still_active = True

    if not still_active:
        session.step = STEP_AWAITING_EMAIL
        session.verified_email = None
        session.verified_enterprise = False
        save_session(session)
        _send_async(phone, {"kind": "text",
                            "text": "⚠️ Your Foundabee enterprise plan is no longer active. "
                                    "Please share your email to re-verify."})
        return

    # Plan still active — refresh timestamp and resume
    session.verified_enterprise = True
    session.set_verified_at_now()
    session.step = previous_step
    save_session(session)

    # Nudge back into the flow
    if previous_step == STEP_CHOOSE_CONTENT_TYPE:
        _send_async(phone, {"kind": "text",
                            "text": beeq("ask_content_type")})
        time.sleep(0.5)
        send_content_type_menu(phone)


def _resolve_instagram_bg(phone: str, handle: str) -> None:
    """Look up the Instagram username in Zernio and store the account if found."""
    session = get_session(phone)
    result = zerini.find_instagram_account_by_username(handle)

    if not result.get("ok"):
        session.step = STEP_COLLECT_INSTAGRAM
        save_session(session)
        _send_async(phone, {"kind": "text",
                            "text": "❌ Could not look up that Instagram account. "
                                    "Please try again — what is your Instagram username?"})
        return

    if not result.get("found"):
        session.step = STEP_COLLECT_INSTAGRAM
        save_session(session)
        _send_async(phone, {
            "kind": "text",
            "text": (
                f"❌ No Instagram account '@{handle}' was found.\n\n"
                "Make sure the account is connected to your Foundabee workspace "
                "and the username matches exactly.\n\n"
                "Try a different username, or type the correct handle:"
            ),
        })
        return

    # Found — store it on the session
    username = result["username"]
    session.instagram_username = username
    session.zerini_account_id  = result["account_id"]
    session.zerini_profile_id  = result["profile_id"]

    if not session.onboarding_complete:
        session.step = STEP_ONBOARDING_SCHEDULE
        save_session(session)
        _send_async(phone, {"kind": "text",
                            "text": (
                                f"✅ Found Instagram @{username}! Almost there 🎉\n\n"
                                "When do you want posts going out?\n\n"
                                "1. 🌅 Morning (8–10 AM)\n"
                                "2. ☀️ Midday (12–2 PM)\n"
                                "3. 🌆 Evening (5–8 PM)"
                            )})
    else:
        session.step = STEP_CHOOSE_CONTENT_TYPE
        save_session(session)
        _send_async(phone, {"kind": "text",
                            "text": f"@{username} connected ✓\n\n{beeq('ask_content_type')}"})
        time.sleep(0.5)
        send_content_type_menu(phone)


def _generate_and_notify_bg(phone: str) -> None:
    """
    1. Use Groq to turn description → detailed image prompts
    2. Generate images via Replicate
    3. Upload to S3
    4. Send images back to user
    5. Advance to choose_caption step
    """
    session = get_session(phone)
    try:
        user_id = session.verified_user_id or phone
        count = session.image_count
        description = session.description or ""

        # ── PATH A: Product image uploaded → Replicate img2img ───────────────
        if session.reference_image_url:
            brand = session.brand_profile() if session.onboarding_complete else {}

            session.bg_status = "🎬 Art director is analyzing your product image..."
            save_session(session)
            _send_async(phone, {"kind": "text", "text": "🚀 Got it! Sending your image to the art director...\n"
                                                         "Analyzing the product and crafting the perfect scene 🎬"})

            # Art director: vision analysis → strategy → cinematic SeedDream prompt
            ad_result = groq_ai.art_director_analyze(
                image_url=session.reference_image_url,
                description=description,
                brand=brand,
            )
            poster_prompt = ad_result["prompt"]
            strategy = ad_result.get("strategy", "reimagine")
            camera = ad_result.get("camera_choice", "")
            camera_line = f"\n📷 *Camera:* {camera}" if camera else ""
            strategy_msg = (
                f"✨ *Reimagining the environment* around your product for maximum impact...{camera_line}"
                if strategy == "reimagine" else
                f"✨ *Enhancing in place* — upgrading lighting & cinematic quality...{camera_line}"
            )
            _send_async(phone, {"kind": "text", "text": strategy_msg})

            est_secs = count * 100
            wait_str = ("~90 seconds" if est_secs < 120
                        else f"~{est_secs // 60}–{est_secs // 60 + 1} minutes" if est_secs < 300
                        else f"~{est_secs // 60} minutes")

            session.bg_status = f"🎨 Generating {count} professional product post(s)... {wait_str} ☕"
            save_session(session)
            _send_async(phone, {"kind": "text", "text": session.bg_status})

            # Detect logo: use first brand asset that isn't the product reference image
            logo_url: str | None = None
            for asset in (session.brand_assets or []):
                if asset != session.reference_image_url:
                    logo_url = asset
                    break

            # SeedDream img2img — product preserved exactly, only environment changes
            gen_result = image_gen.generate_product_posts(
                prompts=[poster_prompt] * count,
                product_image_url=session.reference_image_url,
                logo_url=logo_url,
                aspect_ratio="1:1",
            )
            if not gen_result.get("ok"):
                raise RuntimeError(f"Image generation failed: {gen_result.get('error')}")

            session.bg_status = "☁️ Uploading to secure storage..."
            save_session(session)
            _send_async(phone, {"kind": "text", "text": session.bg_status})

            # Upload the composited bytes (product + logo) directly
            s3_urls = []
            for img_bytes in gen_result["bytes_list"]:
                up = aws_storage.upload_bytes(
                    img_bytes,
                    content_type="image/jpeg",
                    extension="jpg",
                    folder=f"{user_id}/posts",
                )
                if not up.get("ok"):
                    raise RuntimeError(f"S3 upload failed: {up.get('error')}")
                s3_urls.append(up["s3_url"])

            # Stamp profile badge on every image
            session.bg_status = "🏷️ Adding brand badge..."
            save_session(session)
            s3_urls = _stamp_s3_images(
                s3_urls,
                username=session.instagram_username or "yourbrand",
                brand_name=session.brand_name or "",
                avatar_url=session.brand_assets[0] if session.brand_assets else None,
                user_id=user_id,
            )
            session.image_prompts = [poster_prompt] * count

        # ── PATH B: No product image ──────────────────────────────────────────
        elif session.content_type == "carousel":
            # ── CAROUSEL: research-backed text slides rendered with Pillow ──
            brand = session.brand_profile() if session.onboarding_complete else {}
            slide_count = max(1, count - 1)  # hook slide + data slides = count

            session.bg_status = "📊 Researching data and insights for your carousel..."
            save_session(session)
            _send_async(phone, {"kind": "text", "text": "📊 Researching data and crafting your carousel slides..."})

            carousel_content = groq_ai.generate_research_carousel_content(
                topic=description, brand=brand, slide_count=slide_count
            )

            # Resolve brand colors for the compositor
            brand_hex = groq_ai.get_brand_hex_colors(session.brand_colors or "")

            # Generate background images: cover + extra for content slides
            # Formula: total images = max(1, total_slides // 2)
            total_slides = 1 + slide_count
            n_bg_images  = max(1, total_slides // 2)

            session.bg_status = f"🎨 Generating {n_bg_images} background image(s)..."
            save_session(session)
            _send_async(phone, {"kind": "text",
                                "text": f"🎨 Generating backgrounds and rendering {total_slides} slides..."})

            brand_name_hint = f" for {brand['brand_name']}" if brand.get("brand_name") else ""
            def _make_bg_prompt(i: int) -> str:
                base = (
                    f"Cinematic editorial photo{brand_name_hint}: {description}. "
                    f"Brand colors: {session.brand_colors or 'professional dark tones'}. "
                    "No text, no logos, dramatic commercial lighting, magazine quality."
                )
                return base

            import requests as _req
            hook_image_bytes: bytes | None = None
            extra_bg_bytes: list[bytes] = []

            for bi in range(n_bg_images):
                try:
                    prompt = _make_bg_prompt(bi)
                    ref = [session.brand_assets[0]] if session.brand_assets else None
                    gen = image_gen.generate_image(
                        prompt,
                        aspect_ratio="1:1",
                        reference_urls=ref,
                    )
                    if gen.get("ok"):
                        r = _req.get(gen["url"], timeout=30)
                        if r.ok:
                            if bi == 0:
                                hook_image_bytes = r.content
                            else:
                                extra_bg_bytes.append(r.content)
                except Exception:
                    pass

            session.bg_status = "🎨 Rendering carousel slides..."
            save_session(session)

            avatar_url = session.brand_logo_url or (session.brand_assets[0] if session.brand_assets else None)
            slide_bytes_list = make_research_carousel(
                carousel_content=carousel_content,
                username=session.instagram_username or "yourbrand",
                brand_name=session.brand_name or "Your Brand",
                avatar_url=avatar_url,
                brand_colors=brand_hex,
                hook_image_bytes=hook_image_bytes,
                extra_bg_bytes=extra_bg_bytes,
            )

            session.bg_status = "☁️ Uploading to secure storage..."
            save_session(session)
            _send_async(phone, {"kind": "text", "text": session.bg_status})

            import uuid as _uuid
            s3_urls: list[str] = []
            s3_client = aws_storage._s3_client()
            for slide_bytes in slide_bytes_list:
                from io import BytesIO as _BytesIO
                s3_key = f"{aws_storage.config.AWS_BASE_DIR}/{user_id}/post/{_uuid.uuid4().hex}.jpg"
                s3_client.upload_fileobj(
                    _BytesIO(slide_bytes),
                    aws_storage.config.AWS_BUCKET_NAME,
                    s3_key,
                    ExtraArgs={"ContentType": "image/jpeg"},
                )
                s3_urls.append(aws_storage._presign(s3_key))

            session.image_prompts = [carousel_content.get("hook", description)]

        else:
            # ── IMAGE POST / REEL: Replicate generation ──────────────────────
            brand = session.brand_profile() if session.onboarding_complete else {}
            session.bg_status = "🧠 Crafting your image prompts with AI..."
            save_session(session)
            _send_async(phone, {"kind": "text", "text": "🚀 Got it! Working on AI prompts now..."})

            # Pass full brand profile so prompts use real name, colors, voice, not invented ones
            prompts = groq_ai.generate_image_prompts(description, count=count, brand=brand)

            session.image_prompts = prompts
            save_session(session)

            est_secs = count * 90
            wait_str = ("~90 seconds" if est_secs < 120
                        else f"~{est_secs // 60}–{est_secs // 60 + 1} minutes" if est_secs < 300
                        else f"~{est_secs // 60} minutes")

            session.bg_status = f"🎨 Generating {count} image(s)... {wait_str} ☕"
            save_session(session)
            _send_async(phone, {"kind": "text", "text": session.bg_status})

            # Use first brand asset as style reference only when no product image was uploaded
            ref_urls = (session.brand_assets[:1]
                        if session.brand_assets and not session.reference_image_url
                        else None)
            gen_result = image_gen.generate_images(
                prompts,
                content_type=session.content_type or "image_post",
                reference_urls=ref_urls,
            )
            if not gen_result.get("ok"):
                raise RuntimeError(f"Image generation failed: {gen_result.get('error')}")

            session.bg_status = "☁️ Uploading to secure storage..."
            save_session(session)
            _send_async(phone, {"kind": "text", "text": session.bg_status})
            s3_result = aws_storage.upload_urls(gen_result["urls"], user_id, media_kind="post")
            if not s3_result.get("ok"):
                raise RuntimeError(f"S3 upload failed: {s3_result.get('error')}")
            s3_urls = s3_result["s3_urls"]

            # Stamp profile badge on every image
            session.bg_status = "🏷️ Adding brand badge..."
            save_session(session)
            s3_urls = _stamp_s3_images(
                s3_urls,
                username=session.instagram_username or "yourbrand",
                brand_name=session.brand_name or "",
                avatar_url=session.brand_assets[0] if session.brand_assets else None,
                user_id=user_id,
            )

        session.generated_image_urls = s3_urls
        session.bg_status = "📨 Sending your images..."
        save_session(session)

        # Step 4 — Send images to user (no TTS — just delivering the visuals)
        for i, url in enumerate(s3_urls):
            label = f"Slide {i + 1} of {len(s3_urls)}" if len(s3_urls) > 1 else "Here's your image 🎨"
            lock = _get_send_lock(phone)
            with lock:
                send_image(phone, label, url)
            time.sleep(1)  # give Twilio breathing room between images

        # Step 5 — Ask for approval (ONE audio message for voice users)
        session.step = STEP_AWAITING_IMAGE_APPROVAL
        session.bg_status = ""
        save_session(session)
        time.sleep(0.5)
        n = len(s3_urls)
        count_str = f"{n} image{'s' if n > 1 else ''}"
        approval_text = f"Here {'they are' if n > 1 else 'it is'} — {count_str} ready.\n\n{beeq('approve_or_regen')}"
        _send_async(phone, {"kind": "text", "text": approval_text}, tts=True)

    except Exception as exc:
        session.step = STEP_CHOOSE_CONTENT_TYPE
        session.bg_status = ""
        save_session(session)
        _send_async(phone, {"kind": "text",
                            "text": beeq("generation_error")}, tts=True)
        time.sleep(0.3)
        send_content_type_menu(phone)


def _publish_bg(phone: str) -> None:
    """Publish or schedule using the Instagram account already validated for this session."""
    session = get_session(phone)
    try:
        # Use the account resolved during collect_instagram step
        if not session.has_instagram_account():
            # Shouldn't normally happen, but handle gracefully
            session.step = STEP_COLLECT_INSTAGRAM
            save_session(session)
            _send_async(phone, {"kind": "text",
                                "text": "⚠️ No Instagram account linked yet. "
                                        "What is your Instagram username?"})
            return

        account_id = session.zerini_account_id
        username   = session.instagram_username or account_id

        session.bg_status = f"📲 Connecting to @{username}..."
        save_session(session)

        image_urls = session.generated_image_urls
        caption = session.caption or ""
        content_type = session.content_type or "image_post"

        # ── Suggest music based on content ──────────────────────────────────
        session.bg_status = "🎵 Finding a music suggestion..."
        save_session(session)
        music: dict | None = None
        try:
            music = groq_ai.suggest_music(
                description=session.description or caption,
                content_type=content_type,
                brand_voice=session.brand_voice or "",
            )
        except Exception:
            music = None

        session.bg_status = "📤 Submitting your post..."
        save_session(session)

        if session.publish_action == "schedule" and session.scheduled_at:
            try:
                scheduled_dt = dateutil_parser.parse(session.scheduled_at)
                if scheduled_dt.tzinfo is None:
                    scheduled_dt = scheduled_dt.replace(tzinfo=timezone.utc)
            except Exception:
                scheduled_dt = datetime.now(timezone.utc)

            result = zerini.schedule_post(
                account_id=account_id,
                image_urls=image_urls,
                caption=caption,
                scheduled_at=scheduled_dt,
                content_type=content_type,
                profile_id=session.zerini_profile_id,
                music=music,
            )
            action_word = f"scheduled for {_friendly_time(scheduled_dt, session.user_timezone)}"
        else:
            result = zerini.publish_now(
                account_id=account_id,
                image_urls=image_urls,
                caption=caption,
                content_type=content_type,
                profile_id=session.zerini_profile_id,
                music=music,
            )
            action_word = "published"

        if not result.get("ok"):
            raise RuntimeError(result.get("error"))

        post_id = result.get("post_id") or ""

        # Log post to MongoDB
        db.log_post(
            phone_number=phone,
            content_type=content_type,
            image_urls=image_urls,
            caption=caption,
            prompts=session.image_prompts,
            zerini_post_id=post_id,
            scheduled_at=dateutil_parser.parse(session.scheduled_at) if session.scheduled_at else None,
            status="scheduled" if session.publish_action == "schedule" else "published",
        )

        session.step = STEP_CHOOSE_CONTENT_TYPE
        session.bg_status = ""
        save_session(session)

        label = content_type.replace("_", " ")
        music_line = ""
        music_spoken = ""
        if music and music.get("name"):
            music_line = (
                f"\n\n🎵 *Suggested music:* {music['name']} — {music.get('artist', '')}\n"
                f"_Open the post in Instagram and tap 'Edit' → 'Add Music' to add it._"
            )
            music_spoken = f" I suggest pairing it with {music['name']} by {music.get('artist', '')} — you can add it directly in the Instagram app."

        if session.publish_action == "schedule":
            scheduled_friendly = _friendly_time(scheduled_dt, session.user_timezone) if "scheduled_dt" in dir() else action_word
            success_text = f"{beeq('scheduled', time=scheduled_friendly)} (@{username}){music_line}"
        else:
            success_text = f"{beeq('published')} (@{username}){music_line}"
        _send_async(phone, {"kind": "text", "text": success_text})

        # One spoken summary for voice users
        spoken_summary = f"Your {label} has been {action_word} to Instagram.{music_spoken} What would you like to create next?"
        _send_async(phone, {"kind": "text", "text": spoken_summary}, tts=True)

        time.sleep(0.5)
        summary = db.format_post_summary(phone)
        _send_async(phone, {"kind": "text", "text": summary})
        time.sleep(0.5)
        send_content_type_menu(phone)

    except Exception as exc:
        session.step = STEP_PUBLISH_FAILED
        session.bg_status = ""
        save_session(session)
        _send_async(phone, {"kind": "text",
                            "text": beeq("publish_error")})


def _generate_initial_content_bg(phone: str) -> None:
    """Generate 3 individual posts + 1 brand carousel as first-week content after onboarding."""
    session = get_session(phone)
    brand = session.brand_profile()
    description = session.brand_description or session.brand_name or "the brand"
    user_id = session.verified_user_id or phone

    brand_refs = session.brand_assets or []
    ref_kwargs = {"reference_urls": brand_refs} if brand_refs else {}
    # Avatar for badge = brand logo if detected, else first brand asset
    avatar_url = session.brand_logo_url or (brand_refs[0] if brand_refs else None)
    username = session.instagram_username or session.brand_name or "yourbrand"
    brand_name_str = session.brand_name or ""

    def _gen_and_stamp(replicate_urls: list[str], media_kind: str) -> list[str]:
        """Upload Replicate URLs to S3 then stamp profile badge. Returns final presigned URLs."""
        s3 = aws_storage.upload_urls(replicate_urls, user_id, media_kind=media_kind)
        if not s3.get("ok"):
            return []
        stamped = _stamp_s3_images(
            s3["s3_urls"],
            username=username,
            brand_name=brand_name_str,
            avatar_url=avatar_url,
            user_id=user_id,
        )
        return stamped

    queue: list[dict] = []

    # 3 individual posts
    for i in range(1, 4):
        try:
            _send_async(phone, {"kind": "text", "text": f"🎨 Creating post {i} of 3..."})
            prompts = groq_ai.generate_image_prompts(f"Post {i} for {description}", count=1, brand=brand)
            gen = image_gen.generate_images(prompts, content_type="image_post", **ref_kwargs)
            if not gen.get("ok"):
                _send_async(phone, {"kind": "text", "text": f"⚠️ Post {i} failed: {gen.get('error')}"})
                continue
            final_urls = _gen_and_stamp(gen["urls"], "initial_post")
            if not final_urls:
                continue
            caption = groq_ai.generate_caption(description, "image_post", website_url=session.website_url or "")
            db.log_post(phone_number=phone, content_type="image_post",
                        image_urls=final_urls, caption=caption, prompts=prompts, status="draft")
            queue.append({"content_type": "image_post", "image_urls": final_urls, "caption": caption})
        except Exception as exc:
            _send_async(phone, {"kind": "text", "text": f"⚠️ Post {i} error: {exc}"})
        time.sleep(1)

    # 1 carousel (3 images, brand-consistent)
    try:
        count = 3
        _send_async(phone, {"kind": "text",
                            "text": (
                                f"🎠 Creating your brand carousel ({count} slides)...\n"
                                f"⏱ Estimated time: ~{count * 2}–{count * 2 + 1} minutes\n"
                                "All slides will follow your brand colors and style."
                            )})
        prompts = groq_ai.generate_brand_consistent_prompts(description, count=count, brand=brand)
        gen = image_gen.generate_images(prompts, content_type="carousel", **ref_kwargs)
        if gen.get("ok"):
            slide_urls = _gen_and_stamp(gen["urls"], "initial_carousel")
            if slide_urls:
                caption = groq_ai.generate_caption(description, "carousel", website_url=session.website_url or "")
                db.log_post(phone_number=phone, content_type="carousel",
                            image_urls=slide_urls, caption=caption, prompts=prompts, status="draft")
                queue.append({"content_type": "carousel", "image_urls": slide_urls, "caption": caption})
        else:
            _send_async(phone, {"kind": "text", "text": f"⚠️ Carousel failed: {gen.get('error')}"})
    except Exception as exc:
        _send_async(phone, {"kind": "text", "text": f"⚠️ Carousel error: {exc}"})

    if not queue:
        # Nothing was generated successfully
        session = get_session(phone)
        session.step = STEP_CHOOSE_CONTENT_TYPE
        save_session(session)
        _send_async(phone, {"kind": "text", "text": "⚠️ Could not generate initial content. Let's create posts manually."})
        time.sleep(0.5)
        send_content_type_menu(phone)
        return

    # Store queue and start review
    session = get_session(phone)
    session.initial_content_queue = queue
    session.initial_content_index = 0
    session.step = STEP_INITIAL_CONTENT_REVIEW
    save_session(session)

    time.sleep(1)
    _send_async(phone, {"kind": "text",
                        "text": (
                            f"✅ Your first week of content is ready! ({len(queue)} pieces)\n\n"
                            "I'll show you each one — you decide whether to publish now, schedule it, or skip it. 🐝"
                        )})
    time.sleep(1)
    _send_initial_content_item(phone, session, 0)


def _generate_calendar_bg(phone: str) -> None:
    """Generate 30-day content calendar and send the link to the user."""
    import logging as _log
    _logger = _log.getLogger(__name__)
    try:
        import scheduler as _sched
        session = get_session(phone)
        url = _sched.generate_and_save_calendar(phone, session)
        time.sleep(3)  # let initial content message go first
        _send_async(phone, {"kind": "text",
                            "text": (
                                f"📅 *Your 30-Day Content Calendar is ready!*\n\n"
                                f"View and track all your planned content here:\n"
                                f"{url}\n\n"
                                f"_Bookmark this link — it updates live as posts are approved or skipped._"
                            )})
    except Exception as exc:
        _logger.warning("_generate_calendar_bg failed: %s", exc)


def _send_initial_content_item(phone: str, session, index: int) -> None:
    """Send one queued initial content item to the user for review."""
    queue = session.initial_content_queue
    if index >= len(queue):
        # All items reviewed
        session.step = STEP_CHOOSE_CONTENT_TYPE
        session.initial_content_queue = []
        session.initial_content_index = 0
        save_session(session)
        time.sleep(0.5)
        _send_async(phone, {"kind": "text",
                            "text": "🎉 All done! Want to create more content? 👇"})
        time.sleep(0.5)
        send_content_type_menu(phone)
        return

    item = queue[index]
    label = "📸 Image Post" if item["content_type"] == "image_post" else "🎠 Carousel"
    total = len(queue)

    # Send the image(s)
    for j, url in enumerate(item["image_urls"], 1):
        slide_label = (f"{label} — Slide {j} of {len(item['image_urls'])}"
                       if len(item["image_urls"]) > 1 else label)
        lock = _get_send_lock(phone)
        with lock:
            send_image(phone, slide_label, url)
        time.sleep(1)

    # Send caption + approval prompt
    _send_async(phone, {"kind": "text",
                        "text": (
                            f"*Content {index + 1} of {total} — {label}*\n\n"
                            f"📝 Caption:\n{item['caption']}\n\n"
                            "What would you like to do?\n"
                            "Reply *now* to publish immediately\n"
                            "Reply *schedule* to pick a date & time\n"
                            "Reply *skip* to move to the next one"
                        )})


def _start_bg(target, *args) -> None:
    threading.Thread(target=target, args=args, daemon=True).start()


def _stamp_s3_images(
    s3_urls: list[str],
    username: str,
    brand_name: str,
    avatar_url: str | None,
    user_id: str,
) -> list[str]:
    """
    Download each S3 image, stamp the profile badge, re-upload, return new URLs.
    Falls back to the original URL if stamping fails for any individual image.
    """
    import requests as _req
    from io import BytesIO as _BytesIO
    import uuid as _uuid

    stamped_urls: list[str] = []
    s3 = aws_storage._s3_client()

    for url in s3_urls:
        try:
            r = _req.get(url, timeout=30)
            r.raise_for_status()
            stamped_bytes = stamp_post_image(
                image_bytes=r.content,
                username=username,
                brand_name=brand_name,
                avatar_url=avatar_url,
            )
            s3_key = f"{aws_storage.config.AWS_BASE_DIR}/{user_id}/post/{_uuid.uuid4().hex}.jpg"
            s3.upload_fileobj(
                _BytesIO(stamped_bytes),
                aws_storage.config.AWS_BUCKET_NAME,
                s3_key,
                ExtraArgs={"ContentType": "image/jpeg"},
            )
            stamped_urls.append(aws_storage._presign(s3_key))
        except Exception:
            stamped_urls.append(url)  # keep original if stamping fails

    return stamped_urls


def _handle_description_ready(session: UserSession, phone: str, description: str, media_urls: list) -> dict:
    """
    Called once we have a description (typed or voice-extracted).
    Handles optional product image upload, then routes to generation.
    NOTE: reels are never routed here — they have their own flow via STEP_REEL_TYPE_SELECT.
    """
    # Safety guard — if somehow a reel lands here, redirect to the correct step
    if session.content_type == "reel":
        session.step = STEP_REEL_TYPE_SELECT
        save_session(session)
        return {
            "kind": "text",
            "text": (
                "🎬 *Choose your Reel type:*\n\n"
                "1️⃣ *Cinematic Product Reel* — animated product showcase with trending music\n\n"
                "2️⃣ *UGC Video* — talking-head video with your photo and AI voice\n\n"
                "3️⃣ *Full Ad* — professional multi-scene ad with lipsync, B-roll & cinematic cuts\n\n"
                "Reply *1*, *2*, or *3*."
            ),
        }
    session.description = description
    session.reference_image_url = getattr(session, "reference_image_url", None)  # preserve existing

    # If user attached a product image, upload it now
    if media_urls:
        upload = aws_storage.upload_from_url(
            media_urls[0],
            user_id=session.verified_user_id or phone,
            media_kind="product_ref",
        )
        if upload.get("ok"):
            session.reference_image_url = upload["s3_url"]

    save_session(session)

    # If they already have a product image (just uploaded or from before), skip the ask
    if session.reference_image_url:
        return _start_generation(session, phone)

    # Otherwise ask if they want to attach a product photo
    session.step = STEP_COLLECT_PRODUCT_IMAGE
    save_session(session)
    return {"kind": "text",
            "text": (
                "📸 Do you have a product image you'd like to use as the base?\n\n"
                "Send it now and I'll build the post around it.\n"
                "Or type *skip* to let AI generate from scratch."
            )}


def _start_generation(session: UserSession, phone: str) -> dict:
    """Common entry point for kicking off image generation after description + optional product image."""
    if session.content_type == "carousel":
        # If voice already specified image_count > 1, skip the count question
        if session.image_count and session.image_count > 1:
            session.step = STEP_GENERATING
            save_session(session)
            _start_bg(_generate_and_notify_bg, phone)
            return {"kind": "none"}
        session.step = "confirm_carousel_count"
        save_session(session)
        _start_bg(send_image_count_menu, phone)
        return {"kind": "none"}

    session.step = STEP_GENERATING
    save_session(session)
    _start_bg(_generate_and_notify_bg, phone)
    return {"kind": "none"}


# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------

def handle_incoming_message(
    phone: str,
    body: str,
    *,
    button_payload: Optional[str] = None,
    media_urls: Optional[list[str]] = None,
    media_types: Optional[list[str]] = None,
) -> dict:
    """
    Process one inbound WhatsApp message and return the immediate TwiML reply dict.
    Slow operations (API calls, image gen, publishing) run in background threads and
    send their replies via Twilio REST so the webhook returns fast.
    """
    session = get_session(phone)
    clean = (body or "").strip()
    choice = _choice(clean, button_payload)
    media_urls = media_urls or []
    media_types = media_types or []

    # ── Global reset command ───────────────────────────────────────────────
    # "reset", "restart", "start over" or "cancel" breaks out of ANY stuck state.
    # Only applies after the user is verified so it doesn't interfere with login.
    _reset_words = {"reset", "restart", "start over", "startover", "cancel", "start again"}
    if (session.is_verification_valid() and
            (choice in _reset_words or any(w in choice for w in _reset_words))):
        session.reset_flow()
        session.bg_status = ""
        save_session(session)
        from tools.whatsapp import send_content_type_menu as _ctm
        _start_bg(_ctm, phone)
        return {"kind": "text",
                "text": "🔄 No problem — let's start fresh! What would you like to create?"}

    # ── Voice message handling ─────────────────────────────────────────────
    # Step 1: Transcribe the audio.
    # Step 2: Show the transcript and ask the user to confirm or correct it.
    # Step 3 (next message): process the confirmed/corrected text normally.
    if _is_audio_media(media_types) and not clean:
        audio_url = _first_audio_url(media_urls, media_types)
        if audio_url:
            _set_voice_mode(phone, duration=300.0)   # 5-min window covers confirm + next steps
            _send_async(phone, {"kind": "text", "text": "🎙️ Transcribing your voice message..."})

            transcript = voice_tools.transcribe_audio_url(audio_url)
            if transcript:
                # Store transcript + current step, then wait for confirmation
                session.voice_pending_transcript = transcript
                session.voice_pre_step = session.step
                session.step = STEP_VOICE_CONFIRM
                save_session(session)

                confirm_text = (
                    f"📝 I heard:\n\n_{transcript}_\n\n"
                    "Is that correct?\n"
                    "• Reply *yes* to continue\n"
                    "• Or type any corrections and I'll use that instead"
                )
                _send_async(phone, {"kind": "text", "text": confirm_text}, tts=True)
                return {"kind": "none"}
            else:
                return _voice_reply(phone,
                    "I couldn't make out that voice message. Could you try again or type your request?")

    # ── Voice confirmation step ────────────────────────────────────────────
    # User either confirms ("yes") or types a correction.
    if session.step == STEP_VOICE_CONFIRM:
        transcript = session.voice_pending_transcript or ""
        pre_step   = session.voice_pre_step or STEP_CHOOSE_CONTENT_TYPE

        # Determine what text to actually process
        confirm_words = {"yes", "yep", "yeah", "correct", "right", "that's right",
                         "that is correct", "confirmed", "confirm", "ok", "okay", "yup"}
        if choice in confirm_words or any(w in choice for w in {"yes", "correct", "right", "that's it"}):
            # User confirmed — use the original transcript
            effective_text = transcript
        elif clean and len(clean) > 1 and choice not in confirm_words:
            # User typed a correction — use that instead
            effective_text = clean
            _send_async(phone, {"kind": "text", "text": f"✏️ Got it, using: _{effective_text}_"})
        else:
            return _voice_reply(phone,
                "Please reply yes to confirm, or type your correction.")

        # Clear voice confirm state, restore previous step
        session.voice_pending_transcript = None
        session.voice_pre_step = None
        session.step = pre_step
        # Now extract intent from the confirmed text and apply to session
        clean  = effective_text
        choice = effective_text.lower().strip()
        pending = _pending_fields_for_step(session.step, session)
        if pending:
            answers = groq_ai.extract_voice_answers(effective_text, pending)
            if answers:
                applied = _apply_voice_answers(session, answers)
                if applied:
                    for _ in range(10):
                        prev = session.step
                        _advance_step_after_voice(session, applied)
                        if session.step == prev:
                            break
        save_session(session)
        # Fall through to the normal step handlers below with effective_text as clean/choice

    # ── Smart intent intercept ─────────────────────────────────────────────
    # If the user is onboarded and sends a message (text and/or image) that
    # clearly expresses a creation intent, skip straight to the right step
    # instead of forcing them through a menu.
    _INTENT_ELIGIBLE_STEPS = {
        STEP_CHOOSE_CONTENT_TYPE,
        STEP_COLLECT_DESCRIPTION,
        STEP_REEL_TYPE_SELECT,
        STEP_REEL_PRODUCT_IMAGE,
        STEP_REEL_UGC_DESCRIBE,
    }
    if (session.onboarding_complete and
            session.step in _INTENT_ELIGIBLE_STEPS and
            (clean or media_urls)):
        fields_to_probe = ["content_type", "description", "reel_type"]
        intent_answers = groq_ai.extract_voice_answers(clean, fields_to_probe) if clean else {}

        # If user sent an image with a reel/video intent, pre-fill product image
        ct = intent_answers.get("content_type", "")
        rt = intent_answers.get("reel_type", "")
        desc = intent_answers.get("description", "").strip()

        if ct == "reel" or rt in ("cinematic", "ugc"):
            # User wants a reel — apply what we know and jump to reel flow
            if desc:
                session.description = desc
            if rt:
                session.reel_type = rt
            if media_urls and session.step != STEP_REEL_PRODUCT_IMAGE:
                # They attached an image — treat it as their product image
                session.step = STEP_REEL_PRODUCT_IMAGE
                save_session(session)
                # Fall through to STEP_REEL_PRODUCT_IMAGE handler which will process the image
            elif rt == "cinematic" and session.step not in (STEP_REEL_PRODUCT_IMAGE, STEP_REEL_TYPE_SELECT):
                session.step = STEP_REEL_TYPE_SELECT
                save_session(session)
            elif rt == "ugc" and session.step not in (STEP_REEL_UGC_DESCRIBE,):
                session.step = STEP_REEL_UGC_DESCRIBE
                save_session(session)
        elif ct in ("image_post", "carousel") and desc and session.step == STEP_CHOOSE_CONTENT_TYPE:
            # User described a post directly — set type + description and skip the menu
            session.content_type = ct
            session.description = desc
            session.image_count = 1 if ct == "image_post" else session.image_count
            session.step = STEP_COLLECT_DESCRIPTION
            save_session(session)
        # Fall through — the correct step handler below will now run

    # Steps where async work is running in the background — silently ignore any
    # user messages so we don't spam them with "still working" replies.
    # The background thread sends its own progress updates via Twilio REST.
    _SILENT_STEPS = {
        STEP_VERIFYING_EMAIL,
        STEP_RECHECKING_PLAN,
        "resolving_instagram",
        STEP_GENERATING,
        STEP_PUBLISHING,
    }
    if session.step in _SILENT_STEPS:
        status = session.bg_status or _DEFAULT_STATUS.get(session.step, "")
        if status:
            return {"kind": "text", "text": status}
        return {"kind": "none"}

    # ── Global: exit / restart ─────────────────────────────────────────────
    if choice in {"exit", "restart", "reset", "start over", "start again"}:
        email = session.verified_email
        user_id = session.verified_user_id
        # Preserve email/enterprise so they don't have to re-verify
        new_session = UserSession(phone_number=phone)
        new_session.verified_email = email
        new_session.verified_user_id = user_id
        new_session.verified_enterprise = bool(email)
        new_session.set_verified_at_now()
        save_session(new_session)
        if email:
            _start_bg(send_content_type_menu, phone)
            return {"kind": "text",
                    "text": "↩️ Restarted! What would you like to create? 👇"}
        return {"kind": "text",
                "text": "↩️ Restarted! Please share your Foundabee email to begin."}

    # Global shortcut — "my posts" / "status" works from any step
    _STATUS_TRIGGERS = {"my posts", "status", "posts", "queue", "pending", "scheduled"}
    if choice in _STATUS_TRIGGERS and session.verified_enterprise:
        summary = db.format_post_summary(phone)
        return {"kind": "text", "text": summary}

    # Re-verify enterprise plan status when session has expired.
    # If we have a saved email, silently recheck without asking for it again.
    if not session.is_verification_valid() and session.step not in _SILENT_STEPS:
        if session.verified_email:
            previous = session.step if session.step != STEP_AWAITING_EMAIL else STEP_CHOOSE_CONTENT_TYPE
            session.step = STEP_RECHECKING_PLAN
            save_session(session)
            _start_bg(_recheck_plan_bg, phone, previous)
            return {"kind": "none"}
        else:
            session.step = STEP_AWAITING_EMAIL
            session.verified_enterprise = False
            save_session(session)

    # ------------------------------------------------------------------
    # STEP: awaiting_email
    # ------------------------------------------------------------------
    if session.step == STEP_AWAITING_EMAIL:
        email = _extract_email(clean)
        if not email:
            return {"kind": "text", "text": beeq("welcome")}

        # Owner emails — verify instantly without any background thread
        if email.strip().lower() in config.OWNER_EMAILS:
            _do_verify(session, phone, email.strip().lower(), email.strip().lower())
            return {"kind": "none"}

        session.step = STEP_VERIFYING_EMAIL
        session.verified_email = email
        session.bg_status = datetime.now(timezone.utc).isoformat()  # reuse as step_start_time
        save_session(session)
        _start_bg(_verify_email_bg, phone, email)
        return {"kind": "text", "text": beeq("verifying")}

    # ------------------------------------------------------------------
    # STEP: verifying_email (async in progress)
    # ------------------------------------------------------------------
    if session.step == STEP_VERIFYING_EMAIL:
        # bg_status holds the ISO timestamp when verification started
        step_age = None
        try:
            started = datetime.fromisoformat(session.bg_status)
            step_age = (datetime.now(timezone.utc) - started).total_seconds()
        except Exception:
            pass
        # If stuck for more than 90 seconds, reset so they can try again
        if step_age is None or step_age > 90:
            session.step = STEP_AWAITING_EMAIL
            session.bg_status = ""
            save_session(session)
            return {"kind": "text",
                    "text": "⚠️ Verification timed out. Please send your email again."}
        return {"kind": "text", "text": beeq("still_verifying")}

    # ------------------------------------------------------------------
    # STEP: collect_instagram — ask for and validate Instagram username
    # ------------------------------------------------------------------
    if session.step == STEP_COLLECT_INSTAGRAM:
        handle = clean.lstrip("@").strip()
        if len(handle) < 2:
            return {"kind": "text", "text": beeq("ask_instagram")}
        session.step = "resolving_instagram"
        save_session(session)
        _start_bg(_resolve_instagram_bg, phone, handle)
        return {"kind": "text",
                "text": f"🔍 Looking up @{handle}..."}

    if session.step == "resolving_instagram":
        return {"kind": "text",
                "text": "⏳ Still looking up your Instagram account — almost done!"}

    # ------------------------------------------------------------------
    # ONBOARDING STEPS
    # ------------------------------------------------------------------

    # STEP: onboarding_brand
    if session.step == STEP_ONBOARDING_BRAND:
        if len(clean) < 3:
            return {"kind": "text", "text": beeq("onboarding_start")}
        # Use Groq to extract just the brand name from natural language like
        # "its called RealEstate NS" or "we are Nike, a shoe company"
        extracted = groq_ai.extract_voice_answers(clean, ["brand_name", "brand_description"])
        raw_name = extracted.get("brand_name", "").strip()
        if not raw_name:
            # Fallback: strip common filler prefixes
            import re as _re
            raw_name = _re.sub(
                r"^(it'?s?\s+called|we\s+are|i\s+am|my\s+brand\s+is|my\s+business\s+is|"
                r"the\s+brand\s+is|brand\s+name\s+is|called|name\s+is)\s+",
                "", clean, flags=_re.IGNORECASE
            ).split(" - ")[0].strip()
        session.brand_name = raw_name or clean.split(" - ")[0].strip()
        session.brand_description = extracted.get("brand_description", clean)
        session.step = STEP_ONBOARDING_GOAL
        save_session(session)
        return {"kind": "text", "text": beeq("onboarding_goal")}

    # STEP: onboarding_goal
    if session.step == STEP_ONBOARDING_GOAL:
        goal_map = {
            "1": "sales_leads", "sales": "sales_leads",
            "sales & leads": "sales_leads", "sales_leads": "sales_leads",
            "2": "grow_audience", "grow": "grow_audience", "audience": "grow_audience",
            "3": "brand_awareness", "brand": "brand_awareness", "awareness": "brand_awareness",
            "4": "all", "all": "all",
        }
        goal = goal_map.get(choice)
        if not goal:
            return {"kind": "text", "text": beeq("onboarding_goal")}
        session.social_goal = goal
        if goal in {"sales_leads", "all"}:
            session.step = STEP_ONBOARDING_WEBSITE
            save_session(session)
            return {"kind": "text", "text": beeq("onboarding_website")}
        else:
            session.step = STEP_ONBOARDING_VOICE
            save_session(session)
            return {"kind": "text", "text": beeq("onboarding_voice")}

    # STEP: onboarding_website
    if session.step == STEP_ONBOARDING_WEBSITE:
        if "skip" in choice or not clean:
            session.website_url = None
        else:
            session.website_url = clean
        session.step = STEP_ONBOARDING_VOICE
        save_session(session)
        return {"kind": "text", "text": beeq("onboarding_voice")}

    # STEP: onboarding_voice
    if session.step == STEP_ONBOARDING_VOICE:
        voice_map = {
            "1": "warm", "warm": "warm",
            "2": "bold", "bold": "bold",
            "3": "witty", "witty": "witty", "light": "witty", "funny": "witty",
            "4": "formal", "formal": "formal", "professional": "formal",
        }
        voice = voice_map.get(choice)
        if any(k in choice for k in ("5", "else", "other", "something")):
            session.step = STEP_ONBOARDING_VOICE_CUSTOM
            save_session(session)
            return {"kind": "text", "text": beeq("onboarding_voice_custom")}
        if voice:
            session.brand_voice = voice
            session.step = STEP_ONBOARDING_COLORS
            save_session(session)
            return {"kind": "text", "text": beeq("onboarding_colors")}
        # No match — resend menu
        return {"kind": "text", "text": beeq("onboarding_voice")}

    # STEP: onboarding_voice_custom
    if session.step == STEP_ONBOARDING_VOICE_CUSTOM:
        if len(clean) < 3:
            return {"kind": "text", "text": beeq("onboarding_voice_custom")}
        session.brand_voice = f"custom: {clean}"
        session.step = STEP_ONBOARDING_COLORS
        save_session(session)
        return {"kind": "text", "text": beeq("onboarding_colors")}

    # STEP: onboarding_colors
    if session.step == STEP_ONBOARDING_COLORS:
        session.brand_colors = clean if "not sure" not in clean.lower() else ""
        session.step = STEP_ONBOARDING_REFERENCE
        save_session(session)
        return {"kind": "text", "text": beeq("onboarding_reference")}

    # STEP: onboarding_reference
    if session.step == STEP_ONBOARDING_REFERENCE:
        if "skip" in choice:
            session.reference_content_url = None
        else:
            session.reference_content_url = clean
        session.step = STEP_ONBOARDING_COMPETITORS
        save_session(session)
        return {"kind": "text",
                "text": (
                    "Who are your top 2–3 competitors? Instagram handles or website URLs work.\n"
                    "(Separate with commas, or type *skip*)"
                )}

    # STEP: onboarding_competitors
    if session.step == STEP_ONBOARDING_COMPETITORS:
        if "skip" in choice:
            session.competitor_handles = []
        else:
            handles = [
                h.strip().lstrip("@")
                for h in re.split(r"[,\s]+", clean)
                if h.strip() and h.strip().lower() != "skip"
            ]
            session.competitor_handles = handles
        session.step = STEP_ONBOARDING_ASSETS
        save_session(session)
        return {"kind": "text",
                "text": (
                    "Send me 2–3 photos for your brand — product shots, logo, customer photos. "
                    "I'll use these as the visual foundation.\n"
                    "(Type *done* or *skip* when finished)"
                )}

    # STEP: onboarding_assets
    if session.step == STEP_ONBOARDING_ASSETS:
        has_media = bool(media_urls)
        done_words = {"done", "skip", "continue", "next", "finish", "finished"}

        if has_media:
            saved_this_batch = 0
            failed_this_batch = 0
            for url in media_urls:
                try:
                    upload = aws_storage.upload_from_url(
                        url,
                        user_id=session.verified_user_id or phone,
                        media_kind="brand_asset",
                    )
                    if upload.get("ok") and upload.get("s3_url"):
                        s3_url = upload["s3_url"]
                        session.brand_assets.append(s3_url)
                        saved_this_batch += 1
                        # Auto-detect logo using Groq vision (only store first one found)
                        if not session.brand_logo_url:
                            try:
                                if groq_ai.is_logo_image(s3_url):
                                    session.brand_logo_url = s3_url
                            except Exception:
                                pass
                    else:
                        failed_this_batch += 1
                except Exception:
                    failed_this_batch += 1
            save_session(session)

            total = len(session.brand_assets)
            if choice in done_words or total >= 3:
                pass  # fall through to move on
            else:
                note = f" ({failed_this_batch} failed to upload)" if failed_this_batch else ""
                return {"kind": "text",
                        "text": f"✅ {saved_this_batch} photo(s) saved{note}. "
                                f"{total} total so far. "
                                "Send more, or type *done* to continue."}
        elif choice not in done_words and not has_media:
            return {"kind": "text",
                    "text": "Send photos now, or type *done* to skip and continue."}

        # Move on to Instagram username collection
        session.step = STEP_COLLECT_INSTAGRAM
        save_session(session)
        return {"kind": "text", "text": beeq("ask_instagram")}

    # STEP: onboarding_schedule
    if session.step == STEP_ONBOARDING_SCHEDULE:
        schedule_map = {
            "1": "morning", "morning": "morning",
            "2": "midday", "midday": "midday", "noon": "midday", "lunch": "midday",
            "3": "evening", "evening": "evening", "night": "evening",
        }
        sched = schedule_map.get(choice)
        if not sched:
            return {"kind": "text", "text": beeq("onboarding_schedule")}
        session.posting_schedule = sched
        session.step = STEP_ONBOARDING_REPORT_FREQ
        save_session(session)
        return {"kind": "text", "text": beeq("onboarding_report")}

    # STEP: onboarding_report_freq
    if session.step == STEP_ONBOARDING_REPORT_FREQ:
        freq_map = {
            "1": "weekly", "weekly": "weekly", "week": "weekly",
            "2": "monthly", "monthly": "monthly", "month": "monthly",
            "3": "on_demand", "on demand": "on_demand", "on_demand": "on_demand",
            "only when i ask": "on_demand", "demand": "on_demand", "ask": "on_demand",
        }
        freq = freq_map.get(choice)
        if not freq:
            return {"kind": "text", "text": beeq("onboarding_report")}
        session.report_frequency = freq
        session.step = STEP_ONBOARDING_TIMEZONE
        save_session(session)
        return {"kind": "text", "text": beeq("onboarding_timezone")}

    if session.step == STEP_ONBOARDING_TIMEZONE:
        tz_str = groq_ai.get_timezone_for_location(body.strip())
        if tz_str:
            try:
                import pytz
                pytz.timezone(tz_str)   # validate
                session.user_timezone = tz_str
            except Exception:
                session.user_timezone = None
        else:
            session.user_timezone = None

        if not session.user_timezone:
            if body.strip().lower() not in {"skip", "idk", "i don't know", "unsure"}:
                return {"kind": "text",
                        "text": "Couldn't place that — try a city like *Dubai*, *Toronto*, or *Sydney*. Or type *skip* to use UTC."}

        session.onboarding_complete = True
        session.step = STEP_CHOOSE_CONTENT_TYPE
        save_session(session)

        tz_note = f"⏰ Timezone set to *{session.user_timezone}*\n" if session.user_timezone else ""
        competitors_str = (
            ", ".join(f"@{h}" for h in session.competitor_handles)
            if session.competitor_handles else "none specified"
        )

        summary = (
            f"✅ You're all set, *{session.brand_name}*! 🐝\n\n"
            f"🔍 Competitors noted: {competitors_str}\n"
            f"{tz_note}\n"
            f"📅 I'm building your *30-day content calendar* — you'll get the link in a moment.\n"
            f"Every morning at 8 AM I'll send you that day's content for approval.\n\n"
            f"You can also create content anytime below 👇"
        )
        _start_bg(_generate_calendar_bg, phone)
        return {"kind": "text", "text": summary}

    # ------------------------------------------------------------------
    # STEP: choose_content_type
    # ------------------------------------------------------------------
    if session.step == STEP_CHOOSE_CONTENT_TYPE:
        # Voice transcripts are long strings — detect intent via substring, not exact match
        def _has(keywords: set) -> bool:
            return choice in keywords or any(k in choice for k in keywords)

        # If voice extraction already set content_type, honour it directly
        if session.content_type in {"image_post", "carousel", "reel"}:
            ct = session.content_type
        elif _has({BTN_IMAGE_POST, "1", "image post", "photo", "single post", "single image"}):
            ct = "image_post"
        elif _has({BTN_CAROUSEL, "2", "carousel", "slide", "slides", "swipe"}):
            ct = "carousel"
        elif _has({BTN_REEL, "3", "reel", "video", "reels"}):
            ct = "reel"
        else:
            _start_bg(send_content_type_menu, phone)
            return {"kind": "text", "text": beeq("ask_content_type")}

        session.content_type = ct

        if ct == "image_post":
            session.image_count = 1
            session.step = STEP_COLLECT_DESCRIPTION
            save_session(session)
            # If description already extracted by voice, skip straight to generation
            if session.description:
                return _handle_description_ready(session, phone, clean, media_urls)
            return {"kind": "text", "text": beeq("ask_description")}

        if ct == "carousel":
            session.step = STEP_COLLECT_DESCRIPTION
            save_session(session)
            if session.description:
                return _handle_description_ready(session, phone, clean, media_urls)
            return {"kind": "text", "text": beeq("ask_description")}

        if ct == "reel":
            session.step = STEP_REEL_TYPE_SELECT
            save_session(session)
            return {
                "kind": "text",
                "text": (
                    "🎬 *Choose your Reel type:*\n\n"
                    "1️⃣ *Cinematic Product Reel* — dramatic product showcase with animated scenes & trending music\n\n"
                    "2️⃣ *UGC Video* — authentic talking-head video with your photo and AI voice\n\n"
                    "3️⃣ *Full Ad* — professional multi-scene ad with lipsync, B-roll & cinematic cuts\n\n"
                    "Reply *1*, *2*, or *3*."
                ),
            }

    # ------------------------------------------------------------------
    # STEP: reel_type_select
    # ------------------------------------------------------------------
    if session.step == STEP_REEL_TYPE_SELECT:
        def _reel_has(kws: set) -> bool:
            return choice in kws or any(k in choice for k in kws)

        if _reel_has({"1", "cinematic", "product reel", "product", "cinema"}):
            session.reel_type = "cinematic"
            session.step = STEP_REEL_PRODUCT_IMAGE
            save_session(session)
            return {
                "kind": "text",
                "text": (
                    "📦 *Cinematic Product Reel*\n\n"
                    "Send me a photo of your product (or type *skip* to generate from description only).\n\n"
                    "The clearer the product image, the better the result!"
                ),
            }

        if _reel_has({"2", "ugc", "talking", "talking head", "user generated", "me", "my photo", "selfie"}):
            session.reel_type = "ugc"
            session.step = STEP_REEL_UGC_DESCRIBE
            save_session(session)
            return {
                "kind": "text",
                "text": (
                    "🎙️ *UGC Video*\n\n"
                    "What product or service should the video be about?\n"
                    "Give me a brief description and I'll write the script!"
                ),
            }

        if _reel_has({"3", "ad", "full ad", "advertisement", "advert", "full"}):
            session.reel_type = "ad"
            session.step = STEP_REEL_AD_PRODUCT_IMAGE
            save_session(session)
            return {
                "kind": "text",
                "text": (
                    "🎬 *Full Ad Reel*\n\n"
                    "Send me a photo of your product or service "
                    "(used to place you in the scene).\n\n"
                    "Or type *skip* to generate from description only."
                ),
            }

        return {
            "kind": "text",
            "text": (
                "Please choose:\n"
                "Reply *1* or *cinematic* — Cinematic Product Reel\n"
                "Reply *2* or *ugc* — UGC Talking-Head Video\n"
                "Reply *3* or *ad* — Full Ad Reel"
            ),
        }

    # ------------------------------------------------------------------
    # STEP: reel_product_image  (Cinematic reel — upload product photo)
    # ------------------------------------------------------------------
    if session.step == STEP_REEL_PRODUCT_IMAGE:
        has_media = bool(media_urls)
        # Explicit skip words ONLY — must not advance on random text like "1" or "ok"
        skip_words = {"skip", "no image", "no photo", "without image", "generate without",
                      "scratch", "no picture", "without"}

        if has_media:
            # User sent an image — upload it
            upload = aws_storage.upload_from_url(
                media_urls[0],
                user_id=session.verified_user_id or phone,
                media_kind="reel_product",
            )
            if not upload.get("ok"):
                return {"kind": "text",
                        "text": f"⚠️ Couldn't upload that image ({upload.get('error')}). "
                                "Try again or type *skip* to continue without it."}
            # Store the 7-day presigned URL — Replicate can fetch this fine
            session.reel_product_image_url = upload.get("permanent_url") or upload["s3_url"]
            save_session(session)
            # Fall through to description step

        elif choice in skip_words or any(choice == w for w in skip_words):
            # Explicit skip — proceed without product image
            pass

        else:
            # Anything else (including duplicate webhook noise like "1") → hold here
            return {"kind": "text",
                    "text": (
                        "📦 *Send your product photo* so I can build the reel around it.\n\n"
                        "Just attach the image in this chat, or type *skip* to generate from description only."
                    )}

        # Either image uploaded or explicitly skipped — ask for description
        session.step = STEP_REEL_DESCRIBE_PRODUCT
        save_session(session)
        return {
            "kind": "text",
            "text": (
                beeq("ask_description") + "\n\n"
                "This helps me craft the perfect cinematic visuals."
            ),
        }

    # ------------------------------------------------------------------
    # STEP: reel_describe_product  (Cinematic reel — product description)
    # ------------------------------------------------------------------
    if session.step == STEP_REEL_DESCRIBE_PRODUCT:
        if len(clean) < 3:
            return {"kind": "text", "text": beeq("ask_description")}

        session.reel_product_description = clean
        save_session(session)

        # Launch background cinematic reel creation
        def _run_cinematic():
            reel_composer.create_cinematic_reel_bg(session, phone, _send_async, save_session)

        session.bg_status = "🎬 Creating your Cinematic Product Reel — sit tight..."
        save_session(session)
        _send_async(phone, {"kind": "text", "text": session.bg_status})
        threading.Thread(target=_run_cinematic, daemon=True).start()
        return {"kind": "none"}

    # ------------------------------------------------------------------
    # STEP: reel_ugc_describe  (UGC reel — product/service description)
    # ------------------------------------------------------------------
    if session.step == STEP_REEL_UGC_DESCRIBE:
        if len(clean) < 3:
            return {"kind": "text", "text": "Tell me a bit more about the product or service! 🎙️"}

        session.reel_product_description = clean
        brand = session.brand_profile() if session.onboarding_complete else {}

        _send_async(phone, {"kind": "text", "text": "✍️ " + beeq("generating")})
        script = groq_ai.generate_ugc_script(clean, brand)
        session.reel_script = script
        session.step = STEP_REEL_UGC_SCRIPT_REVIEW
        save_session(session)

        word_count = len(script.split())
        return {
            "kind": "text",
            "text": (
                f"📝 *Here's your UGC script* ({word_count} words):\n\n"
                f"{script}\n\n"
                "Reply *approve* to use this script, or type your own (at least 32 words):"
            ),
        }

    # ------------------------------------------------------------------
    # STEP: reel_ugc_script_review  (UGC reel — confirm/edit script)
    # ------------------------------------------------------------------
    if session.step == STEP_REEL_UGC_SCRIPT_REVIEW:
        approve_words = {"approve", "yes", "good", "perfect", "looks good", "use it", "ok", "okay"}
        is_approved = choice in approve_words or any(w in choice for w in approve_words)

        if not is_approved and len(clean) >= 3:
            # User typed their own script — accept any length
            words = clean.split()
            if len(words) > 32:
                _send_async(phone, {"kind": "text",
                                    "text": f"⚠️ Your script is {len(words)} words — that might make the video too long. Shorter is better for Reels, but we'll use it as-is!"})
            session.reel_script = clean

        session.step = STEP_REEL_USER_PHOTO
        save_session(session)
        return {
            "kind": "text",
            "text": (
                "📸 *Send your photo* for the video!\n\n"
                "I'll place you in an environment that matches your product and create the video.\n\n"
                "Or type *skip* to use a default avatar."
            ),
        }

    # ------------------------------------------------------------------
    # STEP: reel_user_photo  (UGC reel — upload selfie/portrait)
    # ------------------------------------------------------------------
    if session.step == STEP_REEL_USER_PHOTO:
        has_media = bool(media_urls)
        skip_words = {"skip", "default", "avatar", "no", "none"}

        if has_media:
            upload = aws_storage.upload_from_url(
                media_urls[0],
                user_id=session.verified_user_id or phone,
                media_kind="reel_user_photo",
            )
            if upload.get("ok"):
                session.reel_user_photo_url = upload.get("permanent_url") or upload["s3_url"]
                save_session(session)
            else:
                # Upload failed — continue without photo (use default avatar)
                logger.warning("reel_user_photo upload failed: %s — using default avatar", upload.get("error"))
                session.reel_user_photo_url = None
                save_session(session)
        elif not (choice in skip_words or any(w in choice for w in skip_words)):
            return {"kind": "text",
                    "text": "Send your photo, or type *skip* to use a default avatar:"}

        # Photo received or skipped — now ask for voice gender
        session.step = STEP_REEL_VOICE_SELECT
        save_session(session)
        return {
            "kind": "text",
            "text": (
                "🎙️ Choose a voice for your video:\n\n"
                "👩 Reply *female* for a female voice\n"
                "👨 Reply *male* for a male voice"
            ),
        }

    # ------------------------------------------------------------------
    # STEP: reel_voice_select  (UGC reel — pick male or female TTS voice)
    # ------------------------------------------------------------------
    if session.step == STEP_REEL_VOICE_SELECT:
        if "female" in choice or "woman" in choice or "girl" in choice:
            session.reel_ugc_voice = "female"
        elif "male" in choice or "man" in choice or "boy" in choice:
            session.reel_ugc_voice = "male"
        else:
            return {
                "kind": "text",
                "text": "Please reply *female* or *male* to choose the voice for your video:",
            }

        # Offer voice cloning if user uploaded photo OR it's an ad reel
        if session.reel_user_photo_url or session.reel_type == "ad":
            session.step = STEP_REEL_VOICE_CLONE
            session.reel_clone_awaiting_sample = False
            save_session(session)
            return {
                "kind": "text",
                "text": (
                    "🎤 Would you like to *clone your own voice* for the video?\n\n"
                    "Reply *yes* to clone your voice, or *skip* to use the default AI voice."
                ),
            }
        else:
            # No photo — skip straight to video creation
            session.reel_clone_voice = False
            save_session(session)
            def _run_ugc_direct():
                reel_composer.create_ugc_video_bg(session, phone, _send_async, save_session)
            session.bg_status = "🎙️ Creating your UGC video — this takes ~3 minutes..."
            save_session(session)
            _send_async(phone, {"kind": "text", "text": session.bg_status})
            threading.Thread(target=_run_ugc_direct, daemon=True).start()
            return {"kind": "none"}

    # ------------------------------------------------------------------
    # STEP: reel_voice_clone  (UGC reel — optional voice cloning)
    # ------------------------------------------------------------------
    _CLONE_SAMPLE_TEXT = (
        "The quick brown fox jumps over the lazy dog. "
        "She sells sea shells by the sea shore. "
        "How much wood would a woodchuck chuck."
    )

    if session.step == STEP_REEL_VOICE_CLONE:
        skip_words = {"skip", "no", "default", "nope", "nah"}
        yes_words  = {"yes", "yeah", "yep", "clone", "sure", "ok", "okay", "correct", "confirmed"}
        has_audio_media = bool(media_urls and _is_audio(body, media_urls))

        # ── State A: waiting for yes/no decision ─────────────────────────
        if not session.reel_clone_awaiting_sample and not session.reel_clone_awaiting_confirm:
            if choice in skip_words or any(w in choice for w in skip_words):
                session.reel_clone_voice = False
                save_session(session)
                # fall through to launch below
            elif choice in yes_words or any(w in choice for w in yes_words):
                session.reel_clone_awaiting_sample = True
                save_session(session)
                return {
                    "kind": "text",
                    "text": (
                        "🎙️ Please record a voice message reading this text clearly:\n\n"
                        f"_{_CLONE_SAMPLE_TEXT}_\n\n"
                        "Send the voice message when you're ready."
                    ),
                }
            else:
                return {
                    "kind": "text",
                    "text": "Reply *yes* to clone your voice, or *skip* to use the default AI voice:",
                }

        # ── State B: received voice sample — transcribe and ask to confirm ─
        elif session.reel_clone_awaiting_sample and not session.reel_clone_awaiting_confirm:
            if has_audio_media:
                _send_async(phone, {"kind": "text", "text": "🎙️ Transcribing your voice message..."})
                transcript = voice_tools.transcribe_audio_url(media_urls[0])

                if not transcript:
                    return {
                        "kind": "text",
                        "text": (
                            "⚠️ Couldn't transcribe that recording. "
                            "Please try again and read the sample text clearly."
                        ),
                    }

                # Store Twilio URL (no upload yet — wait for confirmation)
                session.reel_clone_pending_audio_url = media_urls[0]
                session.reel_clone_awaiting_sample   = False
                session.reel_clone_awaiting_confirm  = True
                save_session(session)
                return {
                    "kind": "text",
                    "text": (
                        f"📝 I heard:\n\n_{transcript}_\n\n"
                        "Is that correct?\n"
                        "• Reply *yes* to continue\n"
                        "• Or type *retry* to record again"
                    ),
                }
            elif choice in skip_words or any(w in choice for w in skip_words):
                session.reel_clone_voice = False
                session.reel_clone_awaiting_sample = False
                save_session(session)
                # fall through to launch
            else:
                return {
                    "kind": "text",
                    "text": (
                        "Please send a voice message reading the sample text, "
                        "or type *skip* to use the default AI voice."
                    ),
                }

        # ── State C: user confirmed transcription — upload and proceed ────
        elif session.reel_clone_awaiting_confirm:
            retry_words = {"retry", "redo", "again", "no", "wrong", "incorrect"}
            if choice in yes_words or any(w in choice for w in yes_words):
                # Upload the pending audio sample to S3
                pending_url = session.reel_clone_pending_audio_url
                if not pending_url:
                    session.reel_clone_awaiting_confirm = False
                    session.reel_clone_voice = False
                    save_session(session)
                else:
                    upload = aws_storage.upload_from_url(
                        pending_url,
                        user_id=session.verified_user_id or phone,
                        media_kind="reel_voice_sample",
                    )
                    if not upload.get("ok"):
                        return {"kind": "text",
                                "text": f"⚠️ Couldn't save voice sample ({upload.get('error')}). "
                                        "Type *retry* to record again or *skip* to use default voice."}
                    session.reel_clone_voice = True
                    session.reel_clone_awaiting_confirm  = False
                    session.reel_clone_pending_audio_url = None
                    session.reel_voice_sample_url = upload.get("permanent_url") or upload["s3_url"]
                    _send_async(phone, {"kind": "text", "text": "✅ Voice sample saved! Cloning your voice for the video..."})
                    save_session(session)
            elif choice in retry_words or any(w in choice for w in retry_words):
                session.reel_clone_awaiting_sample   = True
                session.reel_clone_awaiting_confirm  = False
                session.reel_clone_pending_audio_url = None
                save_session(session)
                return {
                    "kind": "text",
                    "text": (
                        "🎙️ No problem! Record a new voice message reading this clearly:\n\n"
                        f"_{_CLONE_SAMPLE_TEXT}_"
                    ),
                }
            elif choice in skip_words:
                session.reel_clone_voice = False
                session.reel_clone_awaiting_confirm  = False
                session.reel_clone_pending_audio_url = None
                save_session(session)
            else:
                return {
                    "kind": "text",
                    "text": "Reply *yes* if that's correct, *retry* to record again, or *skip* to use the default voice:",
                }

        # Launch correct background job based on reel type
        def _run_voice_reel():
            if session.reel_type == "ad":
                reel_composer.create_ad_reel_bg(session, phone, _send_async, save_session)
            else:
                reel_composer.create_ugc_video_bg(session, phone, _send_async, save_session)

        _bg_label = "🎬 Creating your Full Ad — this takes ~15 minutes..." \
                    if session.reel_type == "ad" else \
                    "🎙️ Creating your UGC video — this takes ~3 minutes..."
        session.bg_status = _bg_label
        save_session(session)
        _send_async(phone, {"kind": "text", "text": session.bg_status})
        threading.Thread(target=_run_voice_reel, daemon=True).start()
        return {"kind": "none"}

    # ------------------------------------------------------------------
    # STEP: reel_ad_product_image  (Full Ad — upload product/service photo)
    # ------------------------------------------------------------------
    if session.step == STEP_REEL_AD_PRODUCT_IMAGE:
        has_media = bool(media_urls)
        skip_words = {"skip", "no image", "no photo", "without", "scratch"}

        if has_media:
            upload = aws_storage.upload_from_url(
                media_urls[0],
                user_id=session.verified_user_id or phone,
                media_kind="reel_product",
            )
            if not upload.get("ok"):
                return {"kind": "text",
                        "text": f"⚠️ Upload failed ({upload.get('error')}). Try again or type *skip*."}
            session.reel_product_image_url = upload.get("permanent_url") or upload["s3_url"]
            save_session(session)
        elif not (choice in skip_words or any(w in choice for w in skip_words)):
            return {"kind": "text",
                    "text": "Send a photo of your product/service, or type *skip* to continue without one."}

        session.step = STEP_REEL_AD_DESCRIBE
        save_session(session)
        return {
            "kind": "text",
            "text": (
                "✍️ Describe your product or service in a few sentences.\n\n"
                "What does it do? Who is it for? What's the key benefit?"
            ),
        }

    # ------------------------------------------------------------------
    # STEP: reel_ad_describe  (Full Ad — describe product/service)
    # ------------------------------------------------------------------
    if session.step == STEP_REEL_AD_DESCRIBE:
        if len(clean) < 5:
            return {"kind": "text", "text": "Please give me a bit more detail about your product or service:"}

        session.reel_product_description = clean
        brand = session.brand_profile() if session.onboarding_complete else {}

        _send_async(phone, {"kind": "text", "text": "✍️ " + beeq("generating")})
        script = groq_ai.generate_ugc_script(clean, brand)
        session.reel_script = script
        session.step = STEP_REEL_AD_SCRIPT_REVIEW
        save_session(session)

        word_count = len(script.split())
        return {
            "kind": "text",
            "text": (
                f"📝 *Here's your ad script* ({word_count} words):\n\n"
                f"{script}\n\n"
                "Reply *approve* to use this script, or type your own version:"
            ),
        }

    # ------------------------------------------------------------------
    # STEP: reel_ad_script_review  (Full Ad — confirm/edit script)
    # ------------------------------------------------------------------
    if session.step == STEP_REEL_AD_SCRIPT_REVIEW:
        approve_words = {"approve", "yes", "good", "perfect", "looks good", "use it", "ok", "okay"}
        is_approved = choice in approve_words or any(w in choice for w in approve_words)

        if not is_approved and len(clean) >= 3:
            words = clean.split()
            if len(words) > 32:
                _send_async(phone, {"kind": "text",
                                    "text": f"⚠️ Your script is {len(words)} words — shorter is better for ads, but we'll use it."})
            session.reel_script = clean

        # Ask for user selfie before voice selection
        session.step = STEP_REEL_AD_USER_PHOTO
        save_session(session)
        return {
            "kind": "text",
            "text": (
                "📸 *Send your photo* to be the face of the ad!\n\n"
                "I'll generate a portrait scene from your photo and use it for the lip-sync.\n\n"
                "Or type *skip* to use a default avatar."
            ),
        }

    # ------------------------------------------------------------------
    # STEP: reel_ad_user_photo  (Full Ad — user selfie for lipsync face)
    # ------------------------------------------------------------------
    if session.step == STEP_REEL_AD_USER_PHOTO:
        has_media = bool(media_urls)
        skip_words = {"skip", "default", "avatar", "no", "none"}

        if has_media:
            upload = aws_storage.upload_from_url(
                media_urls[0],
                user_id=session.verified_user_id or phone,
                media_kind="reel_user_photo",
            )
            if upload.get("ok"):
                session.reel_user_photo_url = upload.get("permanent_url") or upload["s3_url"]
                save_session(session)
            else:
                logger.warning("reel_ad_user_photo upload failed: %s — using default avatar", upload.get("error"))
                session.reel_user_photo_url = None
                save_session(session)
        elif not (choice in skip_words or any(w in choice for w in skip_words)):
            return {"kind": "text",
                    "text": "Send your photo, or type *skip* to use a default avatar:"}

        # Advance to voice selection
        session.step = STEP_REEL_VOICE_SELECT
        save_session(session)
        return {
            "kind": "text",
            "text": (
                "🎙️ Choose a voice for your ad:\n\n"
                "👩 Reply *female* for a female voice\n"
                "👨 Reply *male* for a male voice"
            ),
        }

    # ------------------------------------------------------------------
    # STEP: reel_approval  (approve or regenerate the final reel)
    # ------------------------------------------------------------------
    if session.step == STEP_REEL_APPROVAL:
        approve_words = {"approve", "yes", "use it", "perfect", "looks good", "post it",
                         "publish", "go ahead", "let's go"}
        regen_words   = {"regenerate", "redo", "try again", "different", "new", "again", "no"}

        def _rin(kws): return choice in kws or any(w in choice for w in kws)

        if _rin(approve_words):
            # User approved — move to caption step with the video URL as context
            session.generated_image_urls = [session.reel_video_url] if session.reel_video_url else []
            session.step = STEP_CHOOSE_CAPTION
            save_session(session)
            _start_bg(send_caption_choice_menu, phone)
            return {
                "kind": "text",
                "text": "✅ Reel locked in — " + beeq("ask_caption"),
            }

        if _rin(regen_words):
            # Regenerate — restart from the appropriate entry point
            if session.reel_type == "cinematic":
                session.step = STEP_REEL_PRODUCT_IMAGE
                session.reel_video_url = None
                save_session(session)
                return {
                    "kind": "text",
                    "text": "🔄 " + beeq("ask_product_image"),
                }
            else:
                session.step = STEP_REEL_UGC_DESCRIBE
                session.reel_video_url = None
                session.reel_script = None
                save_session(session)
                return {
                    "kind": "text",
                    "text": "🔄 " + beeq("ask_description"),
                }

        return {
            "kind": "text",
            "text": beeq("approve_or_regen"),
        }

    # ------------------------------------------------------------------
    # STEP: collect_description
    # ------------------------------------------------------------------
    if session.step == STEP_COLLECT_DESCRIPTION:
        has_media = bool(media_urls)

        # If voice already filled description, go straight to generation
        if session.description and not has_media and len(clean) < 10:
            return _handle_description_ready(session, phone, session.description, media_urls)

        if not has_media and len(clean) < 5:
            return {"kind": "text",
                    "text": beeq("ask_description")}

        session.description = clean
        return _handle_description_ready(session, phone, clean, media_urls)

    # ------------------------------------------------------------------
    # STEP: collect_product_image
    # ------------------------------------------------------------------
    if session.step == STEP_COLLECT_PRODUCT_IMAGE:
        has_media = bool(media_urls)
        skip_words = {"skip", "no", "none", "nope", "generate", "scratch", "without"}

        if has_media:
            upload = aws_storage.upload_from_url(
                media_urls[0],
                user_id=session.verified_user_id or phone,
                media_kind="product_ref",
            )
            if upload.get("ok"):
                session.reference_image_url = upload["s3_url"]
            else:
                return {"kind": "text",
                        "text": beeq("upload_error")}
            save_session(session)
        elif choice not in skip_words:
            return {"kind": "text", "text": beeq("ask_product_image")}

        return _start_generation(session, phone)

    # ------------------------------------------------------------------
    # STEP: confirm_carousel_count  (carousel-only sub-step)
    # ------------------------------------------------------------------
    if session.step == "confirm_carousel_count":
        # Parse any spoken or typed number (2–10)
        _WORD_NUMS = {
            "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
            "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        }
        count: int | None = None
        # Try to find a digit in the message
        import re as _re
        digit_match = _re.search(r"\b(\d+)\b", clean)
        if digit_match:
            n = int(digit_match.group(1))
            if 2 <= n <= 10:
                count = n
        # Fall back to number words
        if count is None:
            for word, val in _WORD_NUMS.items():
                if word in choice:
                    count = val
                    break
        if count is None:
            return {"kind": "text",
                    "text": "Please reply with a number between *2 and 10* for how many slides you want. 🎠"}
        session.image_count = count
        session.step = STEP_GENERATING
        save_session(session)
        _start_bg(_generate_and_notify_bg, phone)
        return {"kind": "none"}  # bg thread sends the first message

    # ------------------------------------------------------------------
    # STEP: generating (async in progress)
    # ------------------------------------------------------------------
    if session.step == STEP_GENERATING:
        return {"kind": "text",
                "text": "🎨 Still generating your images — almost done! I'll send them here shortly."}

    # ------------------------------------------------------------------
    # STEP: awaiting_image_approval
    # ------------------------------------------------------------------
    if session.step == STEP_AWAITING_IMAGE_APPROVAL:
        # Substring match so voice transcripts like "yes those look great, approve them" work
        def _in(words: set) -> bool:
            return choice in words or any(w in choice for w in words)

        approve_words = {"approve", "yes", "looks good", "good", "ok", "okay",
                         "perfect", "great", "love it", "approved", "nice", "👍",
                         "let's go", "go ahead", "use it", "use them", "move on"}
        regen_words   = {"regenerate", "redo", "try again", "new ones", "make new",
                         "different", "remake", "start over", "not happy", "don't like"}

        if _in(approve_words):
            session.step = STEP_CHOOSE_CAPTION
            save_session(session)
            time.sleep(0.3)
            _send_async(phone, {"kind": "text",
                                "text": beeq("ask_caption")
                               }, tts=True)
            return {"kind": "none"}

        if _in(regen_words):
            session.step = STEP_GENERATING
            session.generated_image_urls = []
            save_session(session)
            _send_async(phone, {"kind": "text", "text": "🔄 " + beeq("generating")})
            _start_bg(_generate_and_notify_bg, phone)
            return {"kind": "none"}

        # Any other voice/text = a new request or change description — route properly
        if clean and len(clean) > 3:
            session.description = clean
        session.generated_image_urls = []
        save_session(session)
        _send_async(phone, {"kind": "text", "text": "🔄 Got it — " + beeq("generating")})
        # Use _start_generation so carousels go through slide-count confirmation
        return _start_generation(session, phone)

    # ------------------------------------------------------------------
    # STEP: choose_caption
    # ------------------------------------------------------------------
    if session.step == STEP_CHOOSE_CAPTION:
        def _cap_in(words: set) -> bool:
            return choice in words or any(w in choice for w in words)

        ai_words     = {"ai", "generate", "ai caption", "auto", "automatic", "write it for me",
                        "you write", "create caption", "generate caption", BTN_AI_CAPTION, "1"}
        custom_words = {"custom", "own", "write", "i'll write", "my own", "manual",
                        "i want to write", "type it", BTN_CUSTOM_CAPTION, "2"}

        if _cap_in(ai_words):
            session.step = STEP_CHOOSE_PUBLISH_ACTION
            save_session(session)
            _start_bg(_generate_caption_and_ask_publish_bg, phone)
            return {"kind": "text", "text": "✍️ Writing your caption..."}

        if _cap_in(custom_words):
            session.step = STEP_AWAITING_CUSTOM_CAPTION
            save_session(session)
            return _voice_reply(phone, beeq("ask_custom_caption"))

        return _voice_reply(phone, beeq("ask_caption"))

    # ------------------------------------------------------------------
    # STEP: awaiting_custom_caption
    # ------------------------------------------------------------------
    if session.step == STEP_AWAITING_CUSTOM_CAPTION:
        if len(clean) < 3:
            return {"kind": "text", "text": beeq("ask_custom_caption")}
        session.caption = clean
        session.step = STEP_CHOOSE_PUBLISH_ACTION
        save_session(session)
        _start_bg(send_publish_action_menu, phone, clean)
        return _voice_reply(phone, beeq("ask_publish"))

    # ------------------------------------------------------------------
    # STEP: choose_publish_action
    # ------------------------------------------------------------------
    if session.step == STEP_CHOOSE_PUBLISH_ACTION:
        def _pub_in(words: set) -> bool:
            return choice in words or any(w in choice for w in words)

        now_words      = {"now", "publish", "publish now", "post it", "post now",
                          "go live", "live", "immediately", "right now", BTN_PUBLISH_NOW, "1"}
        schedule_words = {"schedule", "later", "schedule for later", "set a time",
                          "pick a time", "not now", "plan it", BTN_SCHEDULE, "2"}

        if _pub_in(now_words):
            session.publish_action = "now"
            session.step = STEP_PUBLISHING
            save_session(session)
            _start_bg(_publish_bg, phone)
            return {"kind": "text", "text": "📤 " + beeq("publishing")}

        if _pub_in(schedule_words):
            session.publish_action = "schedule"
            session.step = STEP_AWAITING_SCHEDULE_TIME
            save_session(session)
            tz_hint = f" (your timezone: {session.user_timezone})" if session.user_timezone else ""
            return _voice_reply(phone, beeq("ask_schedule_time") + tz_hint)

        return _voice_reply(phone, beeq("ask_publish"))

    # ------------------------------------------------------------------
    # STEP: awaiting_schedule_time
    # ------------------------------------------------------------------
    if session.step == STEP_AWAITING_SCHEDULE_TIME:
        parsed_dt = _parse_user_time(clean, session.user_timezone)
        if not parsed_dt:
            tz_hint = f" (times in {session.user_timezone})" if session.user_timezone else " (times in UTC)"
            return {"kind": "text",
                    "text": beeq("ask_schedule_time") + tz_hint}

        session.scheduled_at = parsed_dt.isoformat()
        session.step = STEP_PUBLISHING
        save_session(session)
        _start_bg(_publish_bg, phone)
        friendly = _friendly_time(parsed_dt, session.user_timezone)
        return {"kind": "text",
                "text": beeq("scheduled", time=friendly)}

    if False and session.step == STEP_INITIAL_CONTENT_REVIEW:  # removed — calendar replaces this
        idx = session.initial_content_index
        queue = session.initial_content_queue

        def _icr_in(words: set) -> bool:
            return choice in words or any(w in choice for w in words)

        skip_words    = {"skip", "next", "pass", "skip this", "move on", "not this one", "s"}
        publish_words = {"now", "publish", "publish now", "yes", "upload", "post it",
                         "go ahead", "approve", "approved", "looks good", "perfect", "1"}
        sched_words   = {"schedule", "later", "set a time", "not now", "plan it", "sched", "2"}

        if _icr_in(skip_words):
            session.initial_content_index = idx + 1
            save_session(session)
            _send_initial_content_item(phone, session, idx + 1)
            return {"kind": "none"}

        if _icr_in(publish_words):
            item = queue[idx]
            _send_async(phone, {"kind": "text", "text": beeq("publishing")})
            try:
                item_music = groq_ai.suggest_music(
                    description=item["caption"],
                    content_type=item["content_type"],
                    brand_voice=session.brand_voice or "",
                )
            except Exception:
                item_music = None
            try:
                result = zerini.publish_now(
                    account_id=session.zerini_account_id,
                    image_urls=item["image_urls"],
                    caption=item["caption"],
                    content_type=item["content_type"],
                    profile_id=session.zerini_profile_id,
                    music=item_music,
                )
                if result.get("ok"):
                    music_line = (
                        f"\n\n🎵 *Suggested music:* {item_music['name']} — {item_music.get('artist','')}\n"
                        "_Open the post in Instagram and tap 'Edit' → 'Add Music' to add it._"
                        if item_music and item_music.get("name") else ""
                    )
                    _send_async(phone, {"kind": "text",
                                        "text": f"{beeq('published')} (@{session.instagram_username}){music_line}"}, tts=True)
                else:
                    _send_async(phone, {"kind": "text",
                                        "text": beeq("publish_error")}, tts=True)
            except Exception as exc:
                _send_async(phone, {"kind": "text", "text": beeq("publish_error")}, tts=True)
            time.sleep(0.5)
            session.initial_content_index = idx + 1
            save_session(session)
            _send_initial_content_item(phone, session, idx + 1)
            return {"kind": "none"}

        if _icr_in(sched_words):
            session.step = STEP_INITIAL_CONTENT_SCHEDULE
            save_session(session)
            tz_hint = f" (your timezone: {session.user_timezone})" if session.user_timezone else ""
            return _voice_reply(phone, beeq("ask_schedule_time") + tz_hint)

        return _voice_reply(phone, "Say *approve* to post now, *schedule* to pick a time, or *skip* for the next one.")

    # ------------------------------------------------------------------
    if False and session.step == STEP_INITIAL_CONTENT_SCHEDULE:  # removed — calendar replaces this
        idx = session.initial_content_index
        queue = session.initial_content_queue
        item = queue[idx] if idx < len(queue) else None

        parsed_dt = _parse_user_time(body, session.user_timezone)
        if not parsed_dt or not item:
            tz_hint = f" (times in {session.user_timezone})" if session.user_timezone else ""
            return {"kind": "text",
                    "text": beeq("ask_schedule_time") + tz_hint}

        _send_async(phone, {"kind": "text", "text": "🎵 Finding a music match and scheduling..."})
        try:
            sched_music = groq_ai.suggest_music(
                description=item["caption"],
                content_type=item["content_type"],
                brand_voice=session.brand_voice or "",
            )
        except Exception:
            sched_music = None
        try:
            result = zerini.schedule_post(
                account_id=session.zerini_account_id,
                image_urls=item["image_urls"],
                caption=item["caption"],
                scheduled_at=parsed_dt,
                content_type=item["content_type"],
                profile_id=session.zerini_profile_id,
                music=sched_music,
            )
            friendly = _friendly_time(parsed_dt, session.user_timezone)
            if result.get("ok"):
                music_line = (
                    f"\n\n🎵 *Suggested music:* {sched_music['name']} — {sched_music.get('artist','')}\n"
                    "_Open the post in Instagram and tap 'Edit' → 'Add Music' to add it._"
                    if sched_music and sched_music.get("name") else ""
                )
                _send_async(phone, {"kind": "text",
                                    "text": f"{beeq('scheduled', time=friendly)} (@{session.instagram_username}){music_line}"})
            else:
                _send_async(phone, {"kind": "text",
                                    "text": beeq("publish_error")})
        except Exception as exc:
            _send_async(phone, {"kind": "text", "text": beeq("publish_error")})

        time.sleep(0.5)
        session.step = STEP_INITIAL_CONTENT_REVIEW
        session.initial_content_index = idx + 1
        save_session(session)
        _send_initial_content_item(phone, session, idx + 1)
        return {"kind": "none"}

    # ------------------------------------------------------------------
    # STEP: publishing (async in progress)
    # ------------------------------------------------------------------
    if session.step == STEP_PUBLISHING:
        return {"kind": "text",
                "text": "📤 " + beeq("publishing")}

    # ------------------------------------------------------------------
    # STEP: publish failed — let user choose retry or new post
    # ------------------------------------------------------------------
    if session.step == STEP_PUBLISH_FAILED:
        choice = body.strip().lower()
        if choice in {"retry", "try again", "publish again", "republish", "1"}:
            session.step = STEP_PUBLISHING
            save_session(session)
            _start_bg(_publish_bg, phone)
            return {"kind": "text", "text": "🔄 " + beeq("publishing")}
        if choice in {"new", "new post", "create", "create new", "start over", "2"}:
            session.step = STEP_CHOOSE_CONTENT_TYPE
            save_session(session)
            send_content_type_menu(phone)
            return {"kind": "none"}
        # Unrecognised — re-prompt
        return {"kind": "text", "text": beeq("publish_error")}

    # ------------------------------------------------------------------
    # STEP: daily_suggestion — user reviewing today's proactive content
    # ------------------------------------------------------------------
    if session.step == STEP_DAILY_SUGGESTION:
        choice = _choice(body, button_payload).lower()
        suggestion = session.daily_suggestion or {}
        content_type = suggestion.get("content_type", "image_post")
        post_id = suggestion.get("post_id")
        reel_type = suggestion.get("reel_type")

        # ── Skip ──
        if any(w in choice for w in ("skip", "dismiss", "no", "next", "later", "pass")):
            import scheduler as _sched
            _sched._mark_calendar_day(phone, "skipped")
            session.step = STEP_CHOOSE_CONTENT_TYPE
            session.daily_suggestion = None
            save_session(session)
            send_content_type_menu(phone)
            return {"kind": "text", "text": "No problem! Type *create* anytime to make something. 🐝"}

        # ── Reel: make it ──
        if content_type == "reel" and any(w in choice for w in ("make", "yes", "start", "create", "go", "ok", "okay", "sure", "post now", "post")):
            # Pre-fill reel type and route to reel flow
            session.reel_type = reel_type or "cinematic"
            session.content_type = "reel"
            if reel_type in ("ugc", "ad"):
                session.step = STEP_REEL_UGC_DESCRIBE if reel_type == "ugc" else STEP_REEL_AD_PRODUCT_IMAGE
            else:
                session.step = STEP_REEL_PRODUCT_IMAGE
            session.daily_suggestion = None
            save_session(session)
            reel_label = {"cinematic": "Cinematic", "ugc": "UGC", "ad": "Ad"}.get(reel_type or "cinematic", "Cinematic")
            return {"kind": "text", "text": f"🎬 Let's make your {reel_label} reel!\n\n"
                                            f"{'Upload a product photo to get started 📸' if reel_type != 'ugc' else 'Tell me about the product or service you want to feature:'}"}

        # ── Post Now ──
        if any(w in choice for w in ("post now", "post it", "publish", "yes", "approve", "post", "✅", "go", "ok", "okay", "sure")):
            image_urls = suggestion.get("image_urls", [])
            caption = suggestion.get("caption", "")
            if not image_urls:
                session.step = STEP_CHOOSE_CONTENT_TYPE
                session.daily_suggestion = None
                save_session(session)
                send_content_type_menu(phone)
                return {"kind": "none"}
            # Pre-fill session for publishing
            session.content_type = content_type
            session.generated_image_urls = image_urls
            session.caption = caption
            session.publish_action = "now"
            session.step = STEP_PUBLISHING
            session.daily_suggestion = None
            save_session(session)
            import scheduler as _sched
            _sched._mark_calendar_day(phone, "approved")
            _start_bg(_publish_bg, phone)
            return {"kind": "text", "text": "📤 " + beeq("publishing")}

        # ── Schedule ──
        if any(w in choice for w in ("schedule", "later", "time", "⏰")):
            session.step = STEP_DAILY_SUGGESTION_PUBLISH
            save_session(session)
            return {"kind": "text", "text": "⏰ When should I post this?\n"
                                            "_(e.g. \"tomorrow at 9am\", \"Friday 6pm\")_"}

        # Unrecognised — re-prompt
        return {"kind": "text", "text": "Reply *post now*, *schedule*, or *skip* 🐝"}

    # ------------------------------------------------------------------
    # STEP: daily_suggestion_publish — awaiting schedule time
    # ------------------------------------------------------------------
    if session.step == STEP_DAILY_SUGGESTION_PUBLISH:
        suggestion = session.daily_suggestion or {}
        image_urls = suggestion.get("image_urls", [])
        caption = suggestion.get("caption", "")
        content_type = suggestion.get("content_type", "image_post")

        scheduled_dt = _parse_user_time(body, session.user_timezone)
        if not scheduled_dt or scheduled_dt <= datetime.now(timezone.utc):
            return {"kind": "text", "text": "⚠️ I didn't catch that. Try something like \"tomorrow at 9am\" or \"Friday 6pm\":"}

        session.content_type = content_type
        session.generated_image_urls = image_urls
        session.caption = caption
        session.publish_action = "schedule"
        session.scheduled_at = scheduled_dt.isoformat()
        session.step = STEP_PUBLISHING
        session.daily_suggestion = None
        save_session(session)
        _start_bg(_publish_bg, phone)
        friendly = _friendly_time(scheduled_dt, session.user_timezone)
        return {"kind": "text", "text": f"⏰ Scheduled for {friendly}! 🐝"}

    # Fallback — shouldn't normally be reached
    session.step = STEP_AWAITING_EMAIL
    save_session(session)
    return {"kind": "text",
            "text": beeq("welcome")}


# ---------------------------------------------------------------------------
# Helper background function for AI caption → publish menu
# ---------------------------------------------------------------------------

def _generate_caption_and_ask_publish_bg(phone: str) -> None:
    session = get_session(phone)
    try:
        caption = groq_ai.generate_caption(
            session.description or "",
            session.content_type or "image_post",
            website_url=session.website_url or "",
        )
        session.caption = caption
        save_session(session)
        send_publish_action_menu(phone, caption)
        # One TTS audio summarising the caption and asking how to publish
        _send_async(phone, {
            "kind": "text",
            "text": beeq("ask_publish")
        }, tts=True)
    except Exception as exc:
        session.step = STEP_CHOOSE_CAPTION
        save_session(session)
        _send_async(phone, {"kind": "text",
                            "text": beeq("generation_error")}, tts=True)
        time.sleep(0.3)
        send_caption_choice_menu(phone)
