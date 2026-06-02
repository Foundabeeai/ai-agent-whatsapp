"""Foundabee WhatsApp Automation — Flask webhook entrypoint."""

import json
import logging
import os
import threading
import urllib.error
import urllib.request

from dotenv import load_dotenv
from flask import Flask, request, Response
from twilio.twiml.messaging_response import MessagingResponse

load_dotenv()

from workflow import handle_incoming_message
import db
import scheduler

app = Flask(__name__)

# Start the daily content scheduler in the background
scheduler.start()
logger = logging.getLogger(__name__)

# ── Twilio duplicate-webhook deduplication ─────────────────────────────────
# Twilio retries the webhook if it doesn't get a 2xx within ~15 s.
# We track the last N MessageSids per phone so a retry never double-processes.
import collections, time as _time
_seen_sids: dict[str, tuple[str, float]] = {}   # phone → (last_sid, timestamp)
_DEDUP_WINDOW = 30.0   # seconds — ignore duplicate SID within this window

def _is_duplicate(phone: str, sid: str) -> bool:
    if not sid:
        return False
    entry = _seen_sids.get(phone)
    if entry and entry[0] == sid and (_time.time() - entry[1]) < _DEDUP_WINDOW:
        return True
    _seen_sids[phone] = (sid, _time.time())
    return False


def _bg(target, *args, **kwargs) -> None:
    threading.Thread(target=target, args=args, kwargs=kwargs, daemon=True).start()


def _try_print_ngrok_public_url(port: int) -> None:
    """If ngrok is running locally, print the HTTPS URL Twilio must use (path /webhook)."""
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:4040/api/tunnels",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        print(
            "\n  (ngrok API not reachable — start ngrok in another terminal, e.g. "
            f"  ngrok http {port})\n"
        )
        return
    https_url = None
    for t in data.get("tunnels") or []:
        if (t.get("proto") or "").lower() == "https":
            https_url = (t.get("public_url") or "").rstrip("/")
            break
    if not https_url:
        print("\n  (ngrok is running but no HTTPS tunnel found in /api/tunnels)\n")
        return
    webhook = f"{https_url}/webhook"
    print("\n  === Twilio \"When a message comes in\" (HTTP POST) ===")
    print(f"  {webhook}")
    print("  Copy exactly into Twilio → WhatsApp sandbox (or your number) and Save.\n")


def _twilio_param(*names: str) -> str:
    """Twilio sends application/x-www-form-urlencoded; some proxies/tests use JSON."""
    for mapping in (request.form, request.args):
        for name in names:
            val = mapping.get(name)
            if val is not None and str(val).strip():
                return str(val).strip()
    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        for name in names:
            val = payload.get(name)
            if val is not None and str(val).strip():
                return str(val).strip()
    return ""


def _extract_media() -> tuple[list[str], list[str]]:
    urls, types = [], []
    try:
        count = int(_twilio_param("NumMedia", "num_media") or "0")
    except ValueError:
        count = 0
    for i in range(count):
        url = _twilio_param(f"MediaUrl{i}", f"media_url_{i}")
        mt = (_twilio_param(f"MediaContentType{i}", f"media_content_type_{i}") or "").lower()
        if url:
            urls.append(url)
            types.append(mt)
    return urls, types


def _twiml_text(text: str) -> Response:
    twiml = MessagingResponse()
    twiml.message(text[:4096])
    return Response(str(twiml), mimetype="text/xml")


@app.route("/webhook", methods=["POST"], strict_slashes=False)
def whatsapp_webhook():
    body = _twilio_param("Body", "body")
    from_number = _twilio_param("From", "from")
    button_raw = _twilio_param("ButtonPayload", "button_payload")
    button_payload = button_raw or None
    media_urls, media_types = _extract_media()
    message_sid = _twilio_param("MessageSid", "message_sid")

    # Deduplicate Twilio retries — same SID within 30 s → silent ack
    if _is_duplicate(from_number, message_sid):
        logger.info("webhook duplicate SID=%s from=%s — ignored", message_sid, from_number)
        return Response(str(MessagingResponse()), mimetype="text/xml; charset=utf-8")

    # Never block TwiML on MongoDB — log in background
    _bg(db.log_inbound, from_number, body, media_urls)

    ct = (request.content_type or "").split(";")[0].strip()
    logger.info(
        "webhook POST From=%r Body_len=%d NumMedia_keys=%s content_type=%s",
        from_number,
        len(body),
        [k for k in request.form if k.startswith("MediaUrl")][:3],
        ct,
    )

    if not body and not media_urls and not button_payload:
        logger.warning(
            "webhook empty payload (no Body/media/button) — form keys=%s",
            list(request.form.keys()),
        )
        return Response(str(MessagingResponse()), mimetype="text/xml; charset=utf-8")

    try:
        payload = handle_incoming_message(
            from_number,
            body,
            button_payload=button_payload,
            media_urls=media_urls,
            media_types=media_types,
        )
    except Exception as exc:
        payload = {"kind": "text",
                   "text": f"🐝 Bee here — something went wrong on my end. Please try again. ({exc})"}

    _bg(db.log_outbound, from_number, payload)

    # Build TwiML response
    kind = payload.get("kind", "text")

    # "none" = background work in progress; silently ack so Twilio is happy
    # but the user gets no reply (background thread sends its own updates)
    if kind == "none":
        return Response(str(MessagingResponse()), mimetype="text/xml; charset=utf-8")

    twiml = MessagingResponse()

    if kind == "text":
        twiml.message(str(payload.get("text") or ""))

    elif kind == "media":
        msg = twiml.message(str(payload.get("text") or ""))
        if payload.get("media_url"):
            msg.media(str(payload["media_url"]))

    # interactive / content_template → empty TwiML; REST already sent in background
    return Response(str(twiml), mimetype="text/xml; charset=utf-8")


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "service": "foundabee-whatsapp-automation"}, 200


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    print(f"\n  Listening on 0.0.0.0:{port}  local: http://127.0.0.1:{port}/webhook\n")
    _try_print_ngrok_public_url(port)
    print(
        "  After you send \"hi\" on WhatsApp, you should see a log line "
        "\"webhook POST\" above. If you see nothing, Twilio is not reaching this PC "
        "(wrong webhook URL, ngrok stopped, or Messaging Service overrides the number).\n"
    )
    app.run(host="0.0.0.0", port=port, debug=debug)
