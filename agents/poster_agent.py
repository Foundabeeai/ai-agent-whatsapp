"""
Detailed poster / flyer sub-agent.

Produces rich marketing flyers (real-estate / product) like a designed poster —
price, feature checklist, appliance/extras box, the property photo, the agent's
cut-out photo + contact block, and brand logos — using openai/gpt-image-2.

Flow (intent["_sub_step"]):
  awaiting_property    — need the listing link or a property/product photo
  awaiting_agent_photo — need the user's (agent) headshot
  generating           — poster is rendering
Then it hands off to image_post_agent's normal review/publish loop.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

import db
from session_store import UserSession, save_session, STEP_AGENT_IMAGE_POST
from tools import aws_storage, groq_ai, detailed_poster


def _card_has_contact(card: dict) -> bool:
    return bool(card and (card.get("email") or card.get("mobile") or card.get("website")))


def _card_summary(card: dict) -> str:
    rows = [
        ("Name", card.get("name")), ("Title", card.get("role")),
        ("Email", card.get("email")), ("Mobile", card.get("mobile")),
        ("Website", card.get("website")), ("Company", card.get("company")),
    ]
    return "\n".join(f"• {k}: {v}" for k, v in rows if v) or "• (nothing saved yet)"

logger = logging.getLogger(__name__)

_POSTER_KEYWORDS = ("poster", "flyer", "detailed", "brochure", "listing sheet",
                    "real estate flyer", "property flyer", "for sale flyer")


def _send(phone: str, payload: dict, tts: bool = False) -> None:
    from workflow import _send_async
    _send_async(phone, payload, tts=tts)


def wants_detailed_poster(intent: dict) -> bool:
    if intent.get("_post_style") == "minimal":
        return False
    if intent.get("_post_style") == "detailed":
        return True
    txt = f"{intent.get('description','')} {intent.get('style_notes','')}".lower()
    if any(k in txt for k in _POSTER_KEYWORDS):
        return True
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


def _refresh_details(intent: dict, session: UserSession) -> None:
    """(Re)extract poster fields from the message + whatever scraped context we have."""
    brand = session.brand_profile() if session.onboarding_complete else {}
    scraped_ctx = "\n".join(intent.get("_scraped_summaries") or [])
    intent["_poster_details"] = groq_ai.extract_poster_details(
        intent.get("description", "") or "", scraped_ctx, brand)


def start(phone: str, session: UserSession, intent: dict) -> dict:
    intent["_post_style"] = "detailed"
    intent["_poster_logo"] = session.brand_assets[0] if session.brand_assets else None

    # Pick up anything already provided (scraped listing photos, uploaded images)
    scraped_imgs = intent.get("_scraped_image_urls") or []
    media_urls = intent.get("_media_urls") or []
    media_types = intent.get("_media_types") or []
    uploaded = [u for u, t in zip(media_urls, media_types) if t.startswith("image/")]

    if scraped_imgs:
        intent["_poster_prop_img"] = _to_s3(scraped_imgs[0], phone, "poster_property")
        if uploaded:  # an uploaded image alongside a listing is likely the headshot
            intent["_poster_agent_photo"] = _to_s3(uploaded[0], phone, "poster_agent")
    elif uploaded:
        intent["_poster_prop_img"] = _to_s3(uploaded[0], phone, "poster_property")
        if len(uploaded) > 1:
            intent["_poster_agent_photo"] = _to_s3(uploaded[1], phone, "poster_agent")

    _refresh_details(intent, session)
    # Load any saved contact card for this phone (name/role/email/mobile/website/company)
    intent["_contact_card"] = db.get_contact_card(phone) or {}
    return _advance(phone, session, intent)


def _advance(phone: str, session: UserSession, intent: dict) -> dict:
    """Ask for whatever's still missing, in order, then render."""
    if not intent.get("_poster_prop_img") and not intent.get("_poster_prop_skipped"):
        intent["_sub_step"] = "awaiting_property"
        session.agent_intent = intent
        session.step = STEP_AGENT_IMAGE_POST
        save_session(session)
        return {"kind": "text",
                "text": ("🖼 *Detailed poster* — let's build your flyer!\n\n"
                         "🏠 Paste your *Zillow / listing link* (I'll pull the price, specs & photo), "
                         "or send a *property photo*.\n_(Reply *skip* to design without a property photo.)_")}

    # ── Contact details: confirm saved card, or collect once ──
    if not intent.get("_contact_done"):
        card = intent.get("_contact_card") or {}
        if _card_has_contact(card):
            intent["_sub_step"] = "awaiting_contact_confirm"
            session.agent_intent = intent
            session.step = STEP_AGENT_IMAGE_POST
            save_session(session)
            return {"kind": "text",
                    "text": ("📇 I'll use your saved contact details:\n\n" + _card_summary(card) +
                             "\n\nReply *yes* to use these, or send any *updates* "
                             "(e.g. _new mobile 902-555-1234_).")}
        intent["_sub_step"] = "awaiting_contact"
        session.agent_intent = intent
        session.step = STEP_AGENT_IMAGE_POST
        save_session(session)
        return {"kind": "text",
                "text": ("📇 A few details for your flyer's contact block. Send in one message:\n"
                         "• Your *name* & *title* (e.g. Realtor®)\n• *Email*\n• *Mobile number*\n"
                         "• *Website* (if any)\n• *Brokerage/company* (if any)")}

    if not intent.get("_poster_agent_photo") and not intent.get("_poster_photo_skipped"):
        intent["_sub_step"] = "awaiting_agent_photo"
        session.agent_intent = intent
        session.step = STEP_AGENT_IMAGE_POST
        save_session(session)
        return {"kind": "text",
                "text": ("📸 Now send a clear *headshot of yourself* — it goes on the flyer with your contact info.\n"
                         "_(Or reply *skip* to leave your photo off.)_")}

    return _kickoff(phone, session, intent)


def handle_step(phone: str, session: UserSession, clean: str, button_payload: Optional[str],
                media_urls: list[str], media_types: list[str], voice_confirmed: bool) -> dict:
    intent = session.agent_intent or {}
    sub_step = intent.get("_sub_step", "")
    low = (button_payload or clean or "").strip().lower()
    img = next((u for u, t in zip(media_urls, media_types) if t.startswith("image/")), None)

    if sub_step == "awaiting_property":
        # a listing link?
        try:
            from tools.url_context import find_all_urls, scrape_url as _scrape_url
            urls = find_all_urls(clean or "")
        except Exception:
            urls = []
        if urls:
            _send(phone, {"kind": "text", "text": "🔍 Found a link — pulling the photos and details…"})
            ctx = _scrape_url(urls[0], phone)
            if ctx.get("ok") and ctx.get("image_urls"):
                intent["_scraped_image_urls"] = ctx["image_urls"]
                intent["_scraped_summaries"] = [ctx.get("summary", "")]
                intent["_poster_prop_img"] = _to_s3(ctx["image_urls"][0], phone, "poster_property")
                _refresh_details(intent, session)
            else:
                _send(phone, {"kind": "text", "text": "⚠️ Couldn't pull photos from that link — send a property photo, or reply *skip*."})
                return _advance(phone, session, intent)
            return _advance(phone, session, intent)
        if img:
            intent["_poster_prop_img"] = _to_s3(img, phone, "poster_property")
            return _advance(phone, session, intent)
        if low in ("skip", "no", "none"):
            intent["_poster_prop_skipped"] = True
            return _advance(phone, session, intent)
        return {"kind": "text", "text": "🏠 Paste your Zillow/listing link or send a property photo, or reply *skip*."}

    if sub_step == "awaiting_contact_confirm":
        card = intent.get("_contact_card") or {}
        if low in ("yes", "y", "correct", "yep", "use these", "use them", "looks good", "confirm", "ok", "okay"):
            intent["_contact_done"] = True
            db.save_contact_card(phone, card)
            return _advance(phone, session, intent)
        # otherwise treat the message as an update
        card = groq_ai.parse_contact_details(clean, card)
        intent["_contact_card"] = card
        intent["_contact_done"] = True
        db.save_contact_card(phone, card)
        _send(phone, {"kind": "text", "text": "✅ Updated your details."})
        return _advance(phone, session, intent)

    if sub_step == "awaiting_contact":
        if low in ("skip", "none", "no"):
            intent["_contact_done"] = True
            return _advance(phone, session, intent)
        card = groq_ai.parse_contact_details(clean, intent.get("_contact_card") or {})
        intent["_contact_card"] = card
        if _card_has_contact(card):
            intent["_contact_done"] = True
            db.save_contact_card(phone, card)
            _send(phone, {"kind": "text", "text": "✅ Saved your contact details."})
            return _advance(phone, session, intent)
        return {"kind": "text",
                "text": "📇 I still need at least an *email* or *mobile number* — please send them (or reply *skip*)."}

    if sub_step == "awaiting_agent_photo":
        if img:
            intent["_poster_agent_photo"] = _to_s3(img, phone, "poster_agent")
            return _advance(phone, session, intent)
        if low in ("skip", "no", "none", "without"):
            intent["_poster_photo_skipped"] = True
            return _advance(phone, session, intent)
        return {"kind": "text", "text": "📸 Send a headshot photo, or reply *skip* to leave it off."}

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

        # Merge the saved/collected contact card into the poster's contact block
        card = intent.get("_contact_card") or {}
        contact = details.setdefault("contact", {})
        for card_key, det_key in (("name", "name"), ("role", "role"), ("email", "email"),
                                  ("mobile", "phone"), ("website", "website"), ("company", "company")):
            if card.get(card_key):
                contact[det_key] = card[card_key]
        website = card.get("website") or session.website_url or ""

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
            website_url=website, style_skill=None)

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
