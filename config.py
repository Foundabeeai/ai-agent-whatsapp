"""Central config — reads all env vars with safe defaults."""

import os
from dotenv import load_dotenv

load_dotenv()


def _get(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


def _get_int(key: str, default: int) -> int:
    try:
        return int(_get(key, str(default)))
    except ValueError:
        return default


def _get_float(key: str, default: float) -> float:
    try:
        return float(_get(key, str(default)))
    except ValueError:
        return default


# Twilio
TWILIO_ACCOUNT_SID = _get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = _get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = _get("TWILIO_WHATSAPP_NUMBER")

# Twilio Content Template SIDs (set in Twilio Console → Content)
WA_SID_CONTENT_TYPE = _get("WA_SID_CONTENT_TYPE")       # Image / Carousel / Reel buttons
WA_SID_CAPTION_CHOICE = _get("WA_SID_CAPTION_CHOICE")   # Generate / Custom caption buttons
WA_SID_PUBLISH_ACTION = _get("WA_SID_PUBLISH_ACTION")   # Publish Now / Schedule buttons
WA_SID_IMAGE_COUNT = _get("WA_SID_IMAGE_COUNT")         # 1 / 3 / 5 images (carousel)

# Groq
GROQ_API_KEY = _get("GROQ_API_KEY")
GROQ_MODEL = _get("GROQ_MODEL", "openai/gpt-oss-120b")

# Replicate
REPLICATE_API_TOKEN = _get("REPLICATE_API_TOKEN")
REPLICATE_IMAGE_MODEL = _get("REPLICATE_IMAGE_MODEL", "openai/gpt-image-2")

# AWS
AWS_ACCESS_KEY_ID = _get("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = _get("AWS_SECRET_ACCESS_KEY")
AWS_REGION = _get("AWS_REGION", "ca-central-1")
AWS_BUCKET_NAME = _get("AWS_BUCKET_NAME", "foundabee-temp")
AWS_BASE_DIR = _get("AWS_BASE_DIR", "foundabee_whatsapp_posts")

# MongoDB
MONGO_URI = _get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME = _get("MONGO_DB_NAME", "foundabee_whatsapp")
# The Foundabee app database on the same MongoDB — used to verify enterprise plans directly
FOUNDABEE_DB_NAME = _get("FOUNDABEE_DB_NAME", "demo-foundabee")
# Separate MongoDB URI for the Foundabee app DB (may be a remote AWS server)
FOUNDABEE_MONGO_URI = _get("FOUNDABEE_MONGO_URI", MONGO_URI)

# Owner / admin emails — bypass enterprise check (comma-separated in .env)
OWNER_EMAILS: set[str] = {
    e.strip().lower()
    for e in _get("OWNER_EMAILS", "amay0varghese@gmail.com").split(",")
    if e.strip()
}

# Foundabee user check
CHECK_USER_BASE_URL = _get("CHECK_USER_BASE_URL", "http://3.97.167.111:8002/check/api/user")
INTEGRATION_LOOKUP_API_KEY = _get("INTEGRATION_LOOKUP_API_KEY", "amay-test")
CHECK_USER_TIMEOUT = _get_float("CHECK_USER_TIMEOUT_SECONDS", 5.0)
CHECK_USER_CONNECT_TIMEOUT = _get_float("CHECK_USER_CONNECT_TIMEOUT_SECONDS", 4.0)
CHECK_USER_MAX_ATTEMPTS = _get_int("CHECK_USER_MAX_ATTEMPTS", 1)

# Zerini (social media scheduling)
ZERINI_API_KEY = _get("ZERINI_API_KEY")
ZERINI_PROFILE_ID = _get("ZERINI_PROFILE_ID")  # from zernio.com/dashboard — scopes all API calls

# Last.fm
LASTFM_API_KEY = _get("LASTFM_API_KEY")
LASTFM_FALLBACK_AUDIO_URL = _get("LASTFM_FALLBACK_AUDIO_URL",
    "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3")

# Reel avatars — presigned or public S3 URLs for Maya (female) and George (male)
# Upload the avatar images to S3 and set these env vars, or they fall back to the
# inline base64 stubs defined in tools/reel_composer.py.
# Strip presigned query params — AI models need clean permanent URLs
AVATAR_MAYA_URL   = _get("AVATAR_MAYA_URL",   "").split("?")[0]
AVATAR_GEORGE_URL = _get("AVATAR_GEORGE_URL", "").split("?")[0]

# Groq vision model for product image analysis
GROQ_VISION_MODEL = _get("GROQ_VISION_MODEL", "qwen/qwen3.6-27b")

# App
PORT = _get_int("PORT", 5000)
FLASK_DEBUG = _get("FLASK_DEBUG", "0") == "1"

# Public base URL for calendar links (no trailing slash)
# Set PUBLIC_HOST in .env, e.g. http://3.97.167.111:5000
PUBLIC_HOST = _get("PUBLIC_HOST", "")
