"""
Voice I/O for BeeQ.

STT : Groq Whisper (whisper-large-v3-turbo) — transcribes WhatsApp OGG voice notes.
TTS : Replicate inworld/realtime-tts-2       — synthesizes bot replies to MP3.

WhatsApp via Twilio delivers voice messages as audio/ogg;codecs=opus.
Groq Whisper accepts OGG natively — no conversion needed.

Replicate inworld/realtime-tts-2 input schema:
  text           str   – text to synthesize (max 2000 chars)
  voiceId        str   – preset voice name (e.g. "Ashley", "Dennis", "Alex", "Darlene")
  modelId        str   – must be "inworld-tts-2"
  audioEncoding  str   – MP3 | OGG_OPUS | WAV | FLAC | LINEAR16 (default MP3)
  speakingRate   float – 0.5–1.5 (default 1.0)
  deliveryMode   str   – STABLE | BALANCED | CREATIVE

Output: the model returns a FileOutput / URL pointing to the audio file.
"""

from __future__ import annotations

import base64
import io
import logging
import os

import requests

import config
from tools.replicate_queue import gated as _gated

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TTS config — ElevenLabs Flash v2.5 via Replicate
# ---------------------------------------------------------------------------
_TTS_MODEL         = "elevenlabs/flash-v2.5"
_TTS_VOICE_FEMALE  = "Rachel"   # warm, clear female voice
_TTS_VOICE_MALE    = "Paul"     # natural male voice
_CLONE_MODEL       = "chenxwh/openvoice:d548923c9d7fc9330a3b7c7f9e2f91b2ee90c83311a351dfcd32af353799223d"


# ---------------------------------------------------------------------------
# STT — transcribe Twilio audio URL with Groq Whisper
# ---------------------------------------------------------------------------

def transcribe_audio_url(media_url: str) -> str | None:
    """
    Download audio from a Twilio media URL and transcribe with Groq Whisper.
    Returns the transcript string, or None on failure.

    Twilio media URLs require HTTP Basic auth (account SID + auth token).
    WhatsApp voice messages arrive as audio/ogg;codecs=opus which Whisper handles natively.
    """
    if not config.GROQ_API_KEY:
        logger.warning("voice.transcribe: GROQ_API_KEY not set")
        return None
    if not config.TWILIO_ACCOUNT_SID or not config.TWILIO_AUTH_TOKEN:
        logger.warning("voice.transcribe: Twilio credentials not set")
        return None

    # Download audio bytes from Twilio
    try:
        resp = requests.get(
            media_url,
            auth=(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN),
            timeout=30,
        )
        resp.raise_for_status()
        audio_bytes = resp.content
    except Exception as exc:
        logger.error("voice.transcribe: download failed: %s", exc)
        return None

    # Send to Groq Whisper
    try:
        from groq import Groq
        client = Groq(api_key=config.GROQ_API_KEY)
        transcription = client.audio.transcriptions.create(
            file=("audio.ogg", io.BytesIO(audio_bytes), "audio/ogg"),
            model="whisper-large-v3-turbo",
            response_format="text",
            language="en",
        )
        text = str(transcription).strip()
        logger.info("voice.transcribe: got %d chars", len(text))
        return text if text else None
    except Exception as exc:
        logger.error("voice.transcribe: Groq STT failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# TTS — Replicate inworld/realtime-tts-2 → S3 → presigned URL
# ---------------------------------------------------------------------------

@_gated("tts")
def synthesize_and_upload(
    text: str,
    voice_id: str | None = None,
    clone_reference_url: str | None = None,
) -> str | None:
    """
    Convert text to speech and upload to S3. Returns presigned URL or None.

    - clone_reference_url set → uses chenxwh/openvoice for voice cloning
    - otherwise              → uses elevenlabs/flash-v2.5 with Paul or Rachel
    """
    if not config.REPLICATE_API_TOKEN:
        logger.warning("voice.tts: REPLICATE_API_TOKEN not set")
        return None

    speak_text = text[:1500].strip()
    if not speak_text:
        return None

    import replicate as _replicate
    os.environ.setdefault("REPLICATE_API_TOKEN", config.REPLICATE_API_TOKEN)

    try:
        if clone_reference_url:
            # ── Voice cloning via OpenVoice ───────────────────────────────
            logger.info("voice.tts: cloning voice from %s", clone_reference_url[:60])
            output = _replicate.run(
                _CLONE_MODEL,
                input={
                    "text":     speak_text,
                    "audio":    clone_reference_url,
                    "speed":    1,
                    "language": "EN_NEWEST",
                },
            )
        else:
            # ── ElevenLabs Flash v2.5 ─────────────────────────────────────
            chosen_voice = voice_id or _TTS_VOICE_FEMALE
            logger.info("voice.tts: elevenlabs voice=%s", chosen_voice)
            output = _replicate.run(
                _TTS_MODEL,
                input={
                    "prompt":           speak_text,
                    "voice":            chosen_voice,
                    "stability":        0.5,
                    "similarity_boost": 0.75,
                    "style":            0,
                    "speed":            1.0,
                    "language_code":    "en",
                },
            )
        logger.info("voice.tts: output type=%s", type(output))
    except Exception as exc:
        logger.error("voice.tts: Replicate call failed: %s", exc)
        return None

    # Resolve output → bytes (handles FileOutput, URL string, list, dict)
    audio_bytes: bytes | None = None
    try:
        if hasattr(output, "read"):
            audio_bytes = output.read()
        elif hasattr(output, "url"):
            audio_bytes = _fetch_url(str(output.url))
        elif isinstance(output, str) and output.startswith("http"):
            audio_bytes = _fetch_url(output)
        elif isinstance(output, list) and output:
            first = output[0]
            if hasattr(first, "read"):
                audio_bytes = first.read()
            elif hasattr(first, "url"):
                audio_bytes = _fetch_url(str(first.url))
            elif isinstance(first, str) and first.startswith("http"):
                audio_bytes = _fetch_url(first)
        elif isinstance(output, dict):
            for k in ("url", "audio", "output"):
                if k in output:
                    audio_bytes = _fetch_url(output[k])
                    break
    except Exception as exc:
        logger.error("voice.tts: output parsing failed: %s", exc)
        return None

    if not audio_bytes:
        logger.error("voice.tts: empty audio from output=%r", output)
        return None

    logger.info("voice.tts: got %d bytes", len(audio_bytes))

    try:
        from tools.aws_storage import upload_bytes
        result = upload_bytes(
            data=audio_bytes,
            content_type="audio/mpeg",
            extension="mp3",
            folder="voice",
        )
        if result.get("ok"):
            url = result.get("s3_url") or result.get("permanent_url")
            logger.info("voice.tts: uploaded → %s", url)
            return url
        logger.error("voice.tts: S3 upload failed: %s", result.get("error"))
        return None
    except Exception as exc:
        logger.error("voice.tts: upload exception: %s", exc)
        return None


def _fetch_url(url: str, timeout: int = 30) -> bytes | None:
    """Download bytes from a URL. Returns None on failure."""
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.content
    except Exception as exc:
        logger.error("voice._fetch_url: %s → %s", url, exc)
        return None
