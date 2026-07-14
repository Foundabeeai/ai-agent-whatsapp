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
from tools.tracing import init_tracing

# Wire LangSmith tracing (no-op if disabled / not configured)
init_tracing()

app = Flask(__name__)

# Start the daily content scheduler in the background
scheduler.start()
logger = logging.getLogger(__name__)

# ── Twilio duplicate-webhook deduplication ─────────────────────────────────
# Twilio retries the webhook if it doesn't get a 2xx within ~15 s.
# Single instance → in-memory. Multi-instance (SHARED_STATE) → MongoDB, so a retry
# that lands on a DIFFERENT instance is still caught fleet-wide (otherwise the same
# message gets processed on several instances and the conversation state thrashes).
import collections, time as _time
import config as _config
_seen_sids: dict[str, tuple[str, float]] = {}   # phone → (last_sid, timestamp)
_DEDUP_WINDOW = 60.0   # seconds — ignore duplicate SID within this window

import threading as _threading
_proc_locks: dict[str, _threading.Lock] = {}
_proc_locks_mutex = _threading.Lock()

def _get_proc_lock(phone: str):
    """
    Per-user PROCESSING lock so a user's messages are handled strictly one-at-a-time.
    Fleet-wide (MongoDB) in SHARED_STATE so rapid messages landing on different
    instances can't read/advance the same session simultaneously and scramble context.
    """
    if getattr(_config, "SHARED_STATE", False):
        return db.MongoLock(f"proc:{phone}", ttl=45.0, wait_timeout=40.0)
    with _proc_locks_mutex:
        if phone not in _proc_locks:
            _proc_locks[phone] = _threading.Lock()
        return _proc_locks[phone]


def _is_duplicate(phone: str, sid: str) -> bool:
    if not sid:
        return False
    if getattr(_config, "SHARED_STATE", False):
        # Distributed dedup — atomic claim in Mongo across all instances
        return db.claim_message_sid(sid, ttl=_DEDUP_WINDOW)
    # Single-instance in-memory path
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
        # Serialize this user's messages (fleet-wide) so rapid-fire messages on
        # different instances can't scramble the conversation state.
        with _get_proc_lock(from_number):
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


@app.route("/internal/calendar-approved", methods=["POST"])
def internal_calendar_approved():
    """
    Called by the Foundabee backend when a user approves a calendar day via the web UI.
    Triggers content generation and schedules via zerini for that date.
    """
    import json as _json
    try:
        payload = request.get_json(silent=True) or {}
        phone   = payload.get("phone", "")
        date    = payload.get("date", "")
        day     = payload.get("day", {})
        if not phone or not date or not day:
            return {"ok": False, "error": "missing phone/date/day"}, 400
        _bg(scheduler.trigger_approved_post, phone, date, day)
        return {"ok": True}, 200
    except Exception as exc:
        logger.exception("internal_calendar_approved error: %s", exc)
        return {"ok": False, "error": str(exc)}, 500


@app.route("/calendar/<token>", methods=["GET"])
def content_calendar(token: str):
    """Serve a user's 30-day content calendar as a styled HTML page."""
    cal = db.get_content_calendar_by_token(token)
    if not cal:
        return "<h2 style='font-family:sans-serif;text-align:center;margin-top:4rem'>Calendar not found.</h2>", 404

    brand_name = cal.get("brand_name", "Your Brand")
    days = cal.get("days", [])
    updated = cal.get("updated_at", "")

    TYPE_EMOJI = {"image_post": "🖼️", "carousel": "📑", "reel": "🎬"}
    TYPE_COLOR = {"image_post": "#3b82f6", "carousel": "#8b5cf6", "reel": "#ec4899"}
    STATUS_COLOR = {"pending": "#f59e0b", "approved": "#22c55e", "skipped": "#9ca3af", "published": "#22c55e"}

    rows = ""
    for d in days:
        ct = d.get("content_type", "image_post")
        rt = d.get("reel_type") or ""
        status = d.get("status", "pending")
        reel_label = f" ({rt})" if rt and ct == "reel" else ""
        rows += f"""
        <tr>
          <td style="padding:10px 14px;font-weight:600;color:#374151">{d.get('date','')}</td>
          <td style="padding:10px 14px">
            <span style="background:{TYPE_COLOR.get(ct,'#6b7280')};color:#fff;padding:3px 10px;border-radius:99px;font-size:12px;font-weight:600">
              {TYPE_EMOJI.get(ct,'')} {ct.replace('_',' ').title()}{reel_label}
            </span>
          </td>
          <td style="padding:10px 14px;color:#1f2937;max-width:280px">{d.get('topic','')}</td>
          <td style="padding:10px 14px;color:#6b7280;font-size:13px;max-width:260px">{d.get('caption_idea','')}</td>
          <td style="padding:10px 14px">
            <span style="background:{STATUS_COLOR.get(status,'#9ca3af')}22;color:{STATUS_COLOR.get(status,'#9ca3af')};padding:3px 10px;border-radius:99px;font-size:12px;font-weight:600;text-transform:capitalize">
              {status}
            </span>
          </td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{brand_name} — 30-Day Content Calendar</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f9fafb;color:#111827}}
  .header{{background:linear-gradient(135deg,#fbbf24,#f97316);padding:36px 40px;color:#fff}}
  .header h1{{font-size:26px;font-weight:700;letter-spacing:-0.5px}}
  .header p{{opacity:.85;margin-top:6px;font-size:14px}}
  .legend{{display:flex;gap:16px;padding:18px 40px;background:#fff;border-bottom:1px solid #e5e7eb;flex-wrap:wrap}}
  .pill{{padding:4px 14px;border-radius:99px;font-size:12px;font-weight:600;color:#fff}}
  .wrap{{padding:24px 40px 60px}}
  table{{width:100%;border-collapse:collapse;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.07)}}
  thead{{background:#f3f4f6}}
  th{{padding:11px 14px;text-align:left;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#6b7280}}
  tr:not(:last-child){{border-bottom:1px solid #f3f4f6}}
  tr:hover{{background:#fafafa}}
  @media(max-width:700px){{.wrap{{padding:12px}}.header{{padding:24px 16px}}.legend{{padding:12px 16px}}td:nth-child(4){{display:none}}th:nth-child(4){{display:none}}}}
</style>
</head>
<body>
<div class="header">
  <h1>🐝 {brand_name} — 30-Day Content Calendar</h1>
  <p>Your automated content plan · Generated by BeeQ · Last updated: {str(updated)[:10]}</p>
</div>
<div class="legend">
  <span class="pill" style="background:#3b82f6">🖼️ Image Post</span>
  <span class="pill" style="background:#8b5cf6">📑 Carousel</span>
  <span class="pill" style="background:#ec4899">🎬 Reel</span>
  <span style="font-size:13px;color:#6b7280;align-self:center">Status: <b style="color:#f59e0b">pending</b> · <b style="color:#22c55e">approved/published</b> · <b style="color:#9ca3af">skipped</b></span>
</div>
<div class="wrap">
<table>
  <thead><tr>
    <th>Date</th><th>Content Type</th><th>Topic</th><th>Caption Idea</th><th>Status</th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table>
</div>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


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
