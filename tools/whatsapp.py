"""Twilio WhatsApp outbound — text, media, and interactive button messages."""

from __future__ import annotations

import json
from typing import Any

from twilio.rest import Client

import config


def _client() -> Client:
    if not config.TWILIO_ACCOUNT_SID or not config.TWILIO_AUTH_TOKEN:
        raise RuntimeError("Twilio credentials missing (TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN).")
    return Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)


def _wa(number: str) -> str:
    """Normalize to whatsapp:+E164 format."""
    number = (number or "").strip()
    if number.lower().startswith("whatsapp:"):
        return number
    if number.startswith("+"):
        return f"whatsapp:{number}"
    return number


def _sender() -> str:
    sender = config.TWILIO_WHATSAPP_NUMBER
    if not sender:
        raise RuntimeError("TWILIO_WHATSAPP_NUMBER not set.")
    return _wa(sender)


# ---------------------------------------------------------------------------
# Low-level senders
# ---------------------------------------------------------------------------

def send_text(to: str, body: str) -> dict:
    """Send a plain text WhatsApp message."""
    try:
        msg = _client().messages.create(body=body[:4096], from_=_sender(), to=_wa(to))
        return {"ok": True, "sid": msg.sid}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def send_media(to: str, body: str, media_url: str) -> dict:
    """Send a WhatsApp message with a media attachment."""
    try:
        msg = _client().messages.create(
            body=body[:4096], from_=_sender(), to=_wa(to), media_url=[media_url]
        )
        return {"ok": True, "sid": msg.sid}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def send_image(to: str, caption: str, media_url: str) -> dict:
    """
    Send an image to WhatsApp. Tries the proper media message first.
    If that fails, falls back to sending the direct URL as plain text
    so the user can always tap and view the image.
    """
    result = send_media(to, caption, media_url)
    if result.get("ok"):
        return result
    # Fallback — send URL as text so the image is never lost
    fallback_text = f"{caption}\n\n🖼️ View image: {media_url}"
    return send_text(to, fallback_text)


def send_content_template(to: str, content_sid: str, variables: dict | None = None) -> dict:
    """Send a Twilio Content API template (WhatsApp quick-reply buttons etc.)."""
    sid = (content_sid or "").strip()
    if not sid:
        return {"ok": False, "error": "content_sid is empty."}
    try:
        kwargs: dict[str, Any] = {"from_": _sender(), "to": _wa(to), "content_sid": sid}
        if variables:
            kwargs["content_variables"] = json.dumps(variables)
        msg = _client().messages.create(**kwargs)
        return {"ok": True, "sid": msg.sid}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# High-level interactive button helpers
# (Uses Content Template SID if configured, else numbered-text fallback)
# ---------------------------------------------------------------------------

def _send_buttons(
    to: str,
    body: str,
    options: list[tuple[str, str]],   # (id/value, label)
    content_sid: str,
) -> dict:
    if content_sid:
        return send_content_template(to, content_sid)
    # Numbered text fallback
    numbered = "\n".join(f"{i + 1}. {label}" for i, (_, label) in enumerate(options))
    return send_text(to, f"{body}\n\n{numbered}\n\nReply with the option number.")


def send_content_type_menu(to: str) -> dict:
    return _send_buttons(
        to,
        body="What would you like to create today?",
        options=[
            ("image_post", "📸 Image Post"),
            ("carousel",   "🎠 Carousel (3 images)"),
            ("reel",       "🎬 Reel (video slideshow)"),
        ],
        content_sid=config.WA_SID_CONTENT_TYPE,
    )


def send_caption_choice_menu(to: str) -> dict:
    return _send_buttons(
        to,
        body="Your images are ready! How would you like to caption this post?",
        options=[
            ("ai_caption",     "✨ Generate AI caption"),
            ("custom_caption", "✏️ I'll write my own"),
        ],
        content_sid=config.WA_SID_CAPTION_CHOICE,
    )


def send_publish_action_menu(to: str, caption: str) -> dict:
    body = f"Caption ready:\n\n{caption}\n\nHow would you like to publish?"
    return _send_buttons(
        to,
        body=body,
        options=[
            ("publish_now", "📤 Publish Now"),
            ("schedule",    "📅 Schedule for Later"),
        ],
        content_sid=config.WA_SID_PUBLISH_ACTION,
    )


def send_image_count_menu(to: str) -> dict:
    return send_text(
        to,
        "🎠 How many slides do you want in your carousel?\n\n"
        "Reply with any number between *2 and 10*.\n"
        "_(e.g. reply *4* or say \"four slides\" by voice)_"
    )


# ---------------------------------------------------------------------------
# Dispatcher — routes payload dict → correct Twilio call
# ---------------------------------------------------------------------------

def dispatch(to: str, payload: dict) -> dict:
    """Send any outbound payload dict. Returns {ok, sid} or {ok: False, error}."""
    kind = payload.get("kind", "text")
    if kind == "text":
        return send_text(to, str(payload.get("text") or ""))
    if kind == "media":
        result = send_image(to, str(payload.get("text") or ""), str(payload.get("media_url") or ""))
        if not result.get("ok"):
            return result
        follow = payload.get("follow_up")
        if isinstance(follow, dict):
            return dispatch(to, follow)
        return result
    if kind == "video":
        video_url = str(payload.get("url") or "")
        caption   = str(payload.get("caption") or "")
        result = send_media(to, caption, video_url)
        if result.get("ok"):
            return result
        # Fallback — send the URL as plain text so the video is never lost
        fallback = f"{caption}\n\n🎬 Watch your reel: {video_url}"
        return send_text(to, fallback)
    if kind == "content_template":
        return send_content_template(to, str(payload.get("content_sid") or ""),
                                     payload.get("variables"))
    return {"ok": False, "error": f"Unknown payload kind: {kind!r}"}
