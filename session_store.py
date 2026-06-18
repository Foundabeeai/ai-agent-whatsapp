"""MongoDB-backed session store for per-user WhatsApp conversation state."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from db import get_db


VERIFICATION_TTL = timedelta(days=30)  # kept for legacy compat but not used for expiry check

# Workflow steps (in order)
STEP_AWAITING_EMAIL = "awaiting_email"
STEP_VERIFYING_EMAIL = "verifying_email"
STEP_COLLECT_INSTAGRAM = "collect_instagram"
STEP_CHOOSE_CONTENT_TYPE = "choose_content_type"
STEP_COLLECT_DESCRIPTION = "collect_description"
STEP_GENERATING = "generating"
STEP_CHOOSE_CAPTION = "choose_caption"
STEP_AWAITING_CUSTOM_CAPTION = "awaiting_custom_caption"
STEP_CHOOSE_PUBLISH_ACTION = "choose_publish_action"
STEP_AWAITING_SCHEDULE_TIME = "awaiting_schedule_time"
STEP_PUBLISHING = "publishing"

# Onboarding steps
STEP_ONBOARDING_BRAND = "onboarding_brand"
STEP_ONBOARDING_GOAL = "onboarding_goal"
STEP_ONBOARDING_WEBSITE = "onboarding_website"
STEP_ONBOARDING_VOICE = "onboarding_voice"
STEP_ONBOARDING_VOICE_CUSTOM = "onboarding_voice_custom"
STEP_ONBOARDING_COLORS = "onboarding_colors"
STEP_ONBOARDING_REFERENCE = "onboarding_reference"
STEP_ONBOARDING_REFERENCE_SELECT = "onboarding_reference_select"  # user picks from scraped images
STEP_ONBOARDING_COMPETITORS = "onboarding_competitors"
STEP_ONBOARDING_ASSETS = "onboarding_assets"
STEP_ONBOARDING_SCHEDULE = "onboarding_schedule"
STEP_ONBOARDING_REPORT_FREQ = "onboarding_report_freq"
STEP_ONBOARDING_TIMEZONE          = "onboarding_timezone"
STEP_RECHECKING_PLAN              = "rechecking_plan"
STEP_AWAITING_IMAGE_APPROVAL      = "awaiting_image_approval"
STEP_COLLECT_PRODUCT_IMAGE        = "collect_product_image"
STEP_PUBLISH_FAILED               = "publish_failed"
STEP_INITIAL_CONTENT_REVIEW       = "initial_content_review"       # reviewing one queued initial post
STEP_INITIAL_CONTENT_SCHEDULE     = "initial_content_schedule"     # awaiting schedule time for that post
STEP_VOICE_CONFIRM                = "voice_confirm"                 # waiting for user to confirm/correct transcription

# Reel creation steps
STEP_REEL_TYPE_SELECT         = "reel_type_select"          # choose Cinematic or UGC
STEP_REEL_PRODUCT_IMAGE       = "reel_product_image"        # upload product image (Cinematic)
STEP_REEL_DESCRIBE_PRODUCT    = "reel_describe_product"     # describe product in words
STEP_REEL_UGC_DESCRIBE        = "reel_ugc_describe"         # describe product/service (UGC)
STEP_REEL_UGC_SCRIPT_REVIEW   = "reel_ugc_script_review"   # confirm AI-written script
STEP_REEL_USER_PHOTO          = "reel_user_photo"           # upload user photo (UGC)
STEP_REEL_VOICE_SELECT        = "reel_voice_select"         # choose male or female voice (UGC/Ad)
STEP_REEL_VOICE_CLONE         = "reel_voice_clone"          # ask if user wants voice cloning (UGC/Ad)
STEP_REEL_AD_PRODUCT_IMAGE    = "reel_ad_product_image"     # upload product/service photo (Ad)
STEP_REEL_AD_DESCRIBE         = "reel_ad_describe"          # describe product/service (Ad)
STEP_REEL_AD_SCRIPT_REVIEW    = "reel_ad_script_review"     # confirm AI script (Ad)
STEP_REEL_AD_USER_PHOTO       = "reel_ad_user_photo"        # upload user selfie / avatar (Ad)
STEP_REEL_APPROVAL            = "reel_approval"             # approve / regenerate final reel

# Daily proactive suggestion steps
STEP_DAILY_SUGGESTION         = "daily_suggestion"          # user reviewing today's auto-generated content
STEP_DAILY_SUGGESTION_PUBLISH = "daily_suggestion_publish"  # awaiting schedule time after approval

# Harness + sub-agent steps
STEP_AGENT_COLLECTING         = "agent_collecting"          # harness waiting for one missing field
STEP_AGENT_IMAGE_POST         = "agent_image_post"          # inside image post sub-agent
STEP_AGENT_CAROUSEL           = "agent_carousel"            # inside carousel sub-agent
STEP_AGENT_REEL               = "agent_reel"                # inside reel sub-agent

# Monthly content limits
MONTHLY_LIMITS = {"image_post": 10, "carousel": 8, "reel": 12}


@dataclass
class UserSession:
    phone_number: str
    step: str = STEP_AWAITING_EMAIL

    # Verification
    verified_email: Optional[str] = None
    verified_user_id: Optional[str] = None
    verified_enterprise: bool = False
    verified_at: Optional[str] = None  # ISO string for MongoDB compat

    # Zernio / Instagram account (resolved once after verification)
    instagram_username: Optional[str] = None   # e.g. "mybrand"
    zerini_account_id: Optional[str] = None    # Zernio account _id for this IG handle
    zerini_profile_id: Optional[str] = None    # Zernio profileId that owns the account

    # Onboarding fields
    onboarding_complete: bool = False
    brand_name: Optional[str] = None
    brand_description: Optional[str] = None
    social_goal: Optional[str] = None
    website_url: Optional[str] = None
    brand_voice: Optional[str] = None
    brand_colors: Optional[str] = None
    reference_content_url: Optional[str] = None
    competitor_handles: list[str] = field(default_factory=list)
    brand_assets: list[str] = field(default_factory=list)
    brand_logo_url: Optional[str] = None   # S3 URL of detected brand logo
    posting_schedule: Optional[str] = None
    report_frequency: Optional[str] = None

    # Timezone (IANA string, e.g. "Asia/Kolkata") — set during onboarding
    user_timezone: Optional[str] = None

    # Live status shown to user when they message during background work
    bg_status: str = ""

    # Content creation
    content_type: Optional[str] = None       # image_post | carousel | reel
    reference_image_url: Optional[str] = None  # S3 URL of product image uploaded by user
    scraped_reference_images: list = field(default_factory=list)  # S3 URLs from URL scrape
    post_style_skill: Optional[dict] = None  # VLM-derived style fingerprint from reference image

    # Harness / sub-agent state
    agent_intent: Optional[dict] = None        # full extracted intent dict while in a sub-agent
    agent_missing_field: Optional[str] = None  # field harness is currently asking about
    description: Optional[str] = None
    image_count: int = 1
    image_prompts: list[str] = field(default_factory=list)
    generated_image_urls: list[str] = field(default_factory=list)
    caption: Optional[str] = None

    # Publish
    publish_action: Optional[str] = None  # now | schedule
    scheduled_at: Optional[str] = None   # ISO string

    # Initial content review queue — list of dicts:
    # {"content_type": str, "image_urls": [str], "caption": str}
    initial_content_queue: list = field(default_factory=list)
    initial_content_index: int = 0   # which item in queue we're currently reviewing

    # Voice confirmation — stored while waiting for user to confirm transcription
    voice_pending_transcript: Optional[str] = None  # what Whisper heard
    voice_pre_step: Optional[str] = None            # step to restore after confirmation

    # Reel creation fields
    reel_type: Optional[str] = None                # "cinematic" | "ugc"
    reel_product_image_url: Optional[str] = None   # S3 URL of uploaded product image
    reel_product_description: Optional[str] = None # GPT-vision or user-typed description
    reel_script: Optional[str] = None              # UGC script (max 32 words)
    reel_user_photo_url: Optional[str] = None      # S3 URL of user selfie (UGC)
    reel_ugc_voice: Optional[str] = None           # "male" or "female" chosen for UGC TTS
    reel_clone_voice: bool = False                 # True if user wants voice cloning
    reel_voice_sample_url: Optional[str] = None   # S3 URL of voice sample for cloning
    reel_clone_awaiting_sample: bool = False       # True while waiting for voice sample recording
    reel_clone_awaiting_confirm: bool = False      # True while waiting for transcript confirmation
    reel_clone_pending_audio_url: Optional[str] = None  # Twilio URL of recorded sample (pre-upload)
    reel_video_url: Optional[str] = None           # final output video S3 URL

    # Daily proactive suggestion
    last_daily_suggestion_date: Optional[str] = None  # ISO date "YYYY-MM-DD" last sent
    daily_suggestion: Optional[dict] = None           # {content_type, image_urls, caption, reel_type, post_id}

    def is_verification_valid(self) -> bool:
        """Once a phone number is verified as enterprise, it stays verified permanently."""
        return bool(self.verified_enterprise)

    def set_verified_at_now(self) -> None:
        self.verified_at = datetime.now(timezone.utc).isoformat()

    def has_instagram_account(self) -> bool:
        return bool(self.zerini_account_id and self.zerini_profile_id)

    def brand_profile(self) -> dict:
        return {
            "brand_name": self.brand_name or "",
            "brand_description": self.brand_description or "",
            "social_goal": self.social_goal or "",
            "brand_voice": self.brand_voice or "",
            "brand_colors": self.brand_colors or "",
            "reference_url": self.reference_content_url or "",
            "competitors": self.competitor_handles or [],
        }

    def reset_flow(self) -> None:
        """Reset only content creation fields; onboarding fields are preserved."""
        self.step = STEP_CHOOSE_CONTENT_TYPE
        self.content_type = None
        self.description = None
        self.reference_image_url = None
        self.image_count = 1
        self.image_prompts = []
        self.generated_image_urls = []
        self.caption = None
        self.publish_action = None
        self.scheduled_at = None
        self.initial_content_queue = []
        self.initial_content_index = 0
        self.reel_type = None
        self.reel_product_image_url = None
        self.reel_product_description = None
        self.reel_script = None
        self.reel_user_photo_url = None
        self.reel_ugc_voice = None
        self.reel_clone_voice = False
        self.reel_voice_sample_url = None
        self.reel_clone_awaiting_sample = False
        self.reel_clone_awaiting_confirm = False
        self.reel_clone_pending_audio_url = None
        self.reel_video_url = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "UserSession":
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in allowed}
        return cls(**filtered)


def get_session(phone_number: str) -> UserSession:
    """
    Load session from in-memory cache (primary) or MongoDB (fallback).
    Always returns a valid UserSession — never raises.
    """
    from db import _session_cache
    key = (phone_number or "unknown").strip()

    # 1. In-memory cache — fastest, always available
    if key in _session_cache:
        try:
            return UserSession.from_dict(_session_cache[key])
        except Exception:
            pass

    # 2. MongoDB — persistent across restarts
    try:
        db = get_db()
        doc = db.sessions.find_one({"phone_number": key})
        if doc:
            doc.pop("_id", None)
            _session_cache[key] = doc  # warm the cache
            return UserSession.from_dict(doc)
    except Exception:
        pass

    return UserSession(phone_number=key)


def save_session(session: UserSession) -> None:
    """
    Write session to in-memory cache immediately, then persist to MongoDB.
    The cache write never fails so the session is always available within
    the same process even if MongoDB is unreachable.
    """
    from db import _session_cache
    data = session.to_dict()

    # Always update in-memory cache first
    _session_cache[session.phone_number] = data

    # Best-effort MongoDB persist
    try:
        db = get_db()
        db.sessions.update_one(
            {"phone_number": session.phone_number},
            {"$set": data},
            upsert=True,
        )
    except Exception:
        pass
