"""
Detailed poster / flyer sub-agent.

Produces rich marketing flyers (real-estate / product) like a designed poster —
price, feature checklist, appliance/extras box, the property photo, the agent's
cut-out photo + contact block, and brand logos — using openai/gpt-image-2.

Triggered from image_post_agent when the user asks for a "detailed"/"poster"/
"flyer" style (or a listing link is provided). Details come from the user's
message + any scraped listing context; the agent's headshot is requested.

Sub-steps (intent["_sub_step"]):
  awaiting_agent_photo — waiting for the user's (agent) headshot
  generating           — poster is rendering
Then it hands off to image_post_agent's normal review/publish loop.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from session_store import UserSession, save_session, STEP_AGENT_IMAGE_POST
from tools import aws_storage, groq_ai, detailed_poster

logger = logging.getLogger(__name__)

_POSTER_KEYWORDS = ("poster", "flyer", "detailed", "brochure", "listing sheet",
                    "real estate flyer", "property flyer", "for sale flyer")


def _send(phone: str, payload: dict, tts: bool = False) -> None:
    from workflow import _send_async
    _send_async(phone, payload, tts=tts)


def wants_detailed_poster(intent: dict) -> bool:
    if intent.get("_post_style") == "detailed":
        return True
    txt = f"{intent.get('description','')} {intent.get('style_notes','')}".lower()
    if any(k in txt for k in _POSTER_KEYWORDS):
        return True
    # A shared listing link (Zillow etc.) with scraped photos → build the flyer.
    if intent.get("_url_provided") and (intent.get("_scraped_image_urls") or []):
        return True
    return False


def _to_s3(url: str, phone: str, kind: str) -> Optional[str]:
    """Ensure a media URL is an S3 URL gpt-image-2 can read (Twilio URLs need re-hosting)."""
    if not url:
        return None
    if "amazonaws.com" in url or "s3." in url:
        return url
    try:
        up = aws_storage.upload_from_url(url, user_id=phone, media_kind=kind)
        return up.get("s3_url") if up.get("ok") else url
    except Exception:
        return url


def start(phone: str, session: UserSession, intent: dict) -> dict:
    intent["reel_type"] = intent.get("reel_type")  # no-op, keep shape
    brand = session.brand_profile() if session.onboarding_complete else {}
    description = intent.get("description", "") or ""
    scraped_ctx = "\n".join(intent.get("_scraped_summaries") or [])
    scraped_imgs = intent.get("_scraped_image_urls") or []
    media_urls = intent.get("_media_urls") or []
    media_types = intent.get("_media_types") or []
    uploaded_imgs = [u for u, t in zip(media_urls, media_types) if t.startswith("image/")]

    # Property/product image: scraped listing photo first, else an uploaded image
    prop_img = scraped_imgs[0] if scraped_imgs else (uploaded_imgs[0] if uploaded_imgs else None)
    # An agent headshot: an uploaded image that ISN'T the property image
    agent_photo = None
    if scraped_imgs and uploaded_imgs:
        agent_photo = uploaded_imgs[0]
    elif len(uploaded_imgs) > 1:
        agent_photo = uploaded_imgs[1]

    # Extract structured poster fields from the message + scraped listing facts
    details = groq_ai.extract_poster_details(description, scraped_ctx, brand)

    intent["_poster_details"] = details
    intent["_poster_prop_img"] = _to_s3(prop_img, phone, "poster_property") if prop_img else None
    intent["_poster_logo"] = session.brand_assets[0] if session.brand_assets else None

    if not agent_photo:
        intent["_sub_step"] = "awaiting_agent_photo"
        session.agent_intent = intent
        session.step = STEP_AGENT_IMAGE_POST
        save_session(session)
        return {"kind": "text",
                "text": ("🖼 *Detailed poster* — this style features YOUR photo on the flyer.\n\n"
                         "📸 Send a clear headshot of yourself and I'll build it.\n"
                         "_(Or reply *skip* to make it without your photo.)_")}

    intent["_poster_agent_photo"] = _to_s3(agent_photo, phone, "poster_agent")
    return _kickoff(phone, session, intent)


def handle_step(phone: str, session: UserSession, clean: str, button_payload: Optional[str],
                media_urls: list[str], media_types: list[str], voice_confirmed: bool) -> dict:
    intent = session.agent_intent or {}
    sub_step = intent.get("_sub_step", "")
    low = (button_payload or clean or "").strip().lower()

    if sub_step == "awaiting_agent_photo":
        img = next((u for u, t in zip(media_urls, media_types) if t.startswith("image/")), None)
        if img:
            intent["_poster_agent_photo"] = _to_s3(img, phone, "poster_agent")
            return _kickoff(phone, session, intent)
        if low in ("skip", "no", "none", "without"):
            intent["_poster_agent_photo"] = None
            return _kickoff(phone, session, intent)
        return {"kind": "text", "text": "📸 Please send a headshot photo, or reply *skip* to build it without your photo."}

    if sub_step == "generating":
        return {"kind": "text", "text": "⏳ Still designing your poster — hang tight!"}

    return start(phone, session, intent)


def _kickoff(phone: str, session: UserSession, intent: dict) -> dict:
    intent["_sub_step"] = "generating"
    session.agent_intent = intent
    session.step = STEP_AGENT_IMAGE_POST
    save_session(session)
    _send(phone, {"kind": "text", "text": "🎨 Designing your detailed poster (this takes ~1 minute)…"})
    threading.Thread(target=_generate_bg, args=(phone, session, intent), daemon=True).start()
    return {"kind": "none"}


def _generate_bg(phone: str, session: UserSession, intent: dict) -> None:
    from agents.image_post_agent import _finish_generation
    try:
        brand = session.brand_profile() if session.onboarding_complete else {}
        details = intent.get("_poster_details") or {}

        out = detailed_poster.generate_detailed_poster(
            details=details,
            property_image_url=intent.get("_poster_prop_img"),
            agent_photo_url=intent.get("_poster_agent_photo"),
            logo_url=intent.get("_poster_logo"),
            brand=brand,
        )
        if not out.get("ok") or not (out.get("bytes") or out.get("url")):
            raise RuntimeError(out.get("error") or "poster generation returned nothing")

        user_id = session.verified_user_id or phone
        if out.get("bytes"):
            up = aws_storage.upload_bytes(out["bytes"], content_type="image/png",
                                          extension="png", folder=f"{user_id}/posts")
            poster_url = up.get("s3_url")
        else:
            up = aws_storage.upload_from_url(out["url"], user_id=user_id, media_kind="poster")
            poster_url = up.get("s3_url") if up.get("ok") else out["url"]

        caption = groq_ai.generate_caption_with_style(
            intent.get("description", ""), "image_post",
            website_url=session.website_url or "", style_skill=None)

        intent["_sub_step"] = ""
        session.agent_intent = intent
        save_session(session)
        _finish_generation(phone, session, intent, [poster_url], caption, [])
    except Exception as exc:
        logger.exception("poster _generate_bg failed: %s", exc)
        intent["_sub_step"] = "awaiting_agent_photo"
        session.agent_intent = intent
        save_session(session)
        _send(phone, {"kind": "text",
                      "text": f"😕 Couldn't build the poster: {exc}\nSend your photo again or reply *skip*."})
