"""MongoDB connection and message logging for WhatsApp conversations."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from pymongo import MongoClient, ReturnDocument
from pymongo.database import Database

import config


_client: MongoClient | None = None
_db: Database | None = None

# Separate client for the Foundabee app DB (may live on a different server)
_foundabee_client: MongoClient | None = None

# In-memory session cache — primary store so sessions survive MongoDB outages.
# MongoDB is the persistence layer (written through on every save).
_session_cache: dict[str, dict] = {}

_CONTENT_LABELS = {
    "image_post": "post",
    "carousel":   "carousel",
    "reel":       "reel",
}


def get_db() -> Database:
    global _client, _db
    if _db is None:
        _client = MongoClient(config.MONGO_URI, serverSelectionTimeoutMS=5000)
        _db = _client[config.MONGO_DB_NAME]
        _ensure_indexes(_db)
    return _db


def _get_foundabee_db() -> Database:
    """Return the Foundabee app database, using a dedicated connection if the URI differs."""
    global _foundabee_client
    if config.FOUNDABEE_MONGO_URI == config.MONGO_URI:
        return get_db().client[config.FOUNDABEE_DB_NAME]
    if _foundabee_client is None:
        _foundabee_client = MongoClient(
            config.FOUNDABEE_MONGO_URI,
            serverSelectionTimeoutMS=4000,
            connectTimeoutMS=4000,
            socketTimeoutMS=5000,
        )
    return _foundabee_client[config.FOUNDABEE_DB_NAME]


def _ensure_indexes(db: Database) -> None:
    db.messages.create_index([("phone_number", 1), ("created_at", -1)])
    db.sessions.create_index([("phone_number", 1)], unique=True)
    db.sessions.create_index([("verified_email", 1)])
    db.posts.create_index([("phone_number", 1), ("created_at", -1)])
    db.posts.create_index([("phone_number", 1), ("status", 1), ("content_type", 1)])
    db.content_calendars.create_index([("phone_number", 1)], unique=True)
    db.content_calendars.create_index([("token", 1)], unique=True)


def log_inbound(phone_number: str, text: str, media_urls: list[str] | None = None) -> None:
    """Persist an inbound WhatsApp message."""
    try:
        db = get_db()
        db.messages.insert_one({
            "phone_number": phone_number,
            "direction": "inbound",
            "text": text,
            "media_urls": media_urls or [],
            "created_at": datetime.now(timezone.utc),
        })
    except Exception:
        pass  # never let logging break the main flow


def log_outbound(phone_number: str, payload: dict) -> None:
    """Persist an outbound WhatsApp message payload."""
    try:
        db = get_db()
        db.messages.insert_one({
            "phone_number": phone_number,
            "direction": "outbound",
            "payload": payload,
            "created_at": datetime.now(timezone.utc),
        })
    except Exception:
        pass


def log_post(
    *,
    phone_number: str,
    content_type: str,
    image_urls: list[str],
    caption: str,
    prompts: list[str],
    zerini_post_id: str | None = None,
    scheduled_at: datetime | None = None,
    status: str = "published",
) -> str | None:
    """Persist a created/published post record. Returns inserted document _id as str."""
    try:
        db = get_db()
        result = db.posts.insert_one({
            "phone_number": phone_number,
            "content_type": content_type,
            "image_urls": image_urls,
            "caption": caption,
            "prompts": prompts,
            "zerini_post_id": zerini_post_id,
            "scheduled_at": scheduled_at,
            "status": status,
            "created_at": datetime.now(timezone.utc),
        })
        return str(result.inserted_id)
    except Exception:
        return None


def check_enterprise_in_foundabee_db(email: str) -> dict:
    """
    Directly query the Foundabee MongoDB database to check if an email has an
    active enterprise subscription — mirrors the mongosh command:

      db.subscription_plans.aggregate([
        { $match: { status: "ACTIVE", "subscription_plan.product.name": /enterprise/i } },
        { $lookup: { from: "users", localField: "user", foreignField: "uuid", as: "u" } },
        { $unwind: "$u" },
        { $match: { "u.email": /<email>/i } }
      ])

    Returns:
      {"found": True,  "enterprise": True,  "user_id": "...", "plan": "..."}
      {"found": False, "enterprise": False}
      {"found": False, "enterprise": False, "error": "..."}  — on DB error
    """
    email = (email or "").strip()
    if not email:
        return {"found": False, "enterprise": False, "error": "No email provided."}

    try:
        foundabee_db = _get_foundabee_db()

        # Case-insensitive exact match on email — handles mixed-case stored values
        email_regex = re.compile(f"^{re.escape(email)}$", re.IGNORECASE)

        import logging as _logging
        _log = _logging.getLogger(__name__)

        # ── Step 1: find the user by email ──────────────────────────────────
        user_doc = foundabee_db.users.find_one(
            {"email": {"$regex": email_regex}},
            {"uuid": 1, "email": 1, "_id": 1},
        )
        _log.info("check_enterprise: user_doc for %s → %s", email, user_doc)

        if not user_doc:
            return {"found": False, "enterprise": False}

        user_uuid = user_doc.get("uuid") or str(user_doc.get("_id") or "")

        # ── Step 2: find ANY subscription for this user ──────────────────────
        # Look up by uuid OR _id to handle schema variations
        sub_query = {
            "$or": [
                {"user": user_uuid},
                {"user": user_doc.get("_id")},
                {"userId": user_uuid},
                {"userId": user_doc.get("_id")},
            ]
        }
        all_subs = list(foundabee_db.subscription_plans.find(sub_query, {"status": 1, "subscription_plan": 1}).limit(10))
        _log.info("check_enterprise: all_subs for uuid=%s → %s", user_uuid, all_subs)

        # ── Step 3: match enterprise + active (flexible status check) ────────
        ACTIVE_STATUSES = {"active", "active_recurring", "trialing", "paid", "current"}
        for sub in all_subs:
            status = str(sub.get("status") or "").lower()
            plan_name = str(
                (sub.get("subscription_plan") or {}).get("product", {}).get("name") or ""
            ).lower()
            _log.info("check_enterprise: sub status=%r plan_name=%r", status, plan_name)
            is_active = status in ACTIVE_STATUSES or status.startswith("active")
            is_enterprise = "enterprise" in plan_name or "pro" in plan_name
            if is_active and is_enterprise:
                return {
                    "found": True,
                    "enterprise": True,
                    "user_id": user_uuid,
                    "plan": plan_name,
                }

        # No active enterprise sub found — but user exists
        # Log what statuses/plans we did find so we can diagnose
        _log.warning(
            "check_enterprise: user %s found but no active enterprise sub. subs=%s",
            email,
            [(s.get("status"), (s.get("subscription_plan") or {}).get("product", {}).get("name")) for s in all_subs],
        )
        return {"found": True, "enterprise": False}
    except Exception as exc:
        return {"found": False, "enterprise": False, "error": str(exc)}


def find_verified_session_by_email(email: str) -> dict | None:
    """
    Look up any session that was previously verified as enterprise for this email.
    Used as a fallback when the Foundabee API is unreachable or returns not-found.
    Returns the session dict or None.
    """
    try:
        db = get_db()
        doc = db.sessions.find_one({
            "verified_email": email.strip().lower(),
            "verified_enterprise": True,
        })
        if doc:
            doc.pop("_id", None)
        return doc
    except Exception:
        return None


def get_message_history(phone_number: str, limit: int = 20) -> list[dict]:
    """Return last N messages for a phone number (newest first)."""
    try:
        db = get_db()
        cursor = db.messages.find(
            {"phone_number": phone_number},
            sort=[("created_at", -1)],
            limit=limit,
        )
        return list(cursor)
    except Exception:
        return []


def promote_overdue_scheduled(phone_number: str) -> int:
    """
    Mark any scheduled posts whose scheduled_at has passed as 'published'.
    Returns the number of posts promoted.
    """
    try:
        db = get_db()
        now = datetime.now(timezone.utc)
        result = db.posts.update_many(
            {
                "phone_number": phone_number,
                "status": "scheduled",
                "scheduled_at": {"$lte": now},
            },
            {"$set": {"status": "published"}},
        )
        return result.modified_count
    except Exception:
        return 0


def get_post_counts(phone_number: str) -> dict:
    """
    Return post counts broken down by content_type and status.

    Shape:
    {
      "published": {"image_post": 3, "carousel": 1, "reel": 0},
      "scheduled": {"image_post": 2, "carousel": 0, "reel": 1},
      "draft":     {"image_post": 1, "carousel": 0, "reel": 0},
    }
    """
    empty = lambda: {"image_post": 0, "carousel": 0, "reel": 0}
    counts: dict[str, dict] = {"published": empty(), "scheduled": empty(), "draft": empty()}
    try:
        db = get_db()
        pipeline = [
            {"$match": {"phone_number": phone_number}},
            {"$group": {"_id": {"status": "$status", "content_type": "$content_type"}, "n": {"$sum": 1}}},
        ]
        for row in db.posts.aggregate(pipeline):
            status = row["_id"].get("status", "draft")
            ctype  = row["_id"].get("content_type", "image_post")
            if status not in counts:
                counts[status] = empty()
            if ctype in counts[status]:
                counts[status][ctype] = row["n"]
    except Exception:
        pass
    return counts


def get_scheduled_posts(phone_number: str) -> list[dict]:
    """Return all scheduled (not yet published) posts, oldest first."""
    try:
        db = get_db()
        now = datetime.now(timezone.utc)
        cursor = db.posts.find(
            {"phone_number": phone_number, "status": "scheduled", "scheduled_at": {"$gt": now}},
            sort=[("scheduled_at", 1)],
        )
        return list(cursor)
    except Exception:
        return []


def get_monthly_counts(phone_number: str, year: int, month: int) -> dict:
    """
    Return post counts for a specific calendar month, grouped by content_type.
    Counts ALL statuses (published, scheduled, draft).
    Shape: {"image_post": N, "carousel": N, "reel": N}
    """
    counts = {"image_post": 0, "carousel": 0, "reel": 0}
    try:
        from calendar import monthrange
        db = get_db()
        month_start = datetime(year, month, 1, tzinfo=timezone.utc)
        last_day = monthrange(year, month)[1]
        month_end = datetime(year, month, last_day, 23, 59, 59, tzinfo=timezone.utc)
        pipeline = [
            {"$match": {
                "phone_number": phone_number,
                "created_at": {"$gte": month_start, "$lte": month_end},
            }},
            {"$group": {"_id": "$content_type", "n": {"$sum": 1}}},
        ]
        for row in db.posts.aggregate(pipeline):
            ctype = row["_id"] or "image_post"
            if ctype in counts:
                counts[ctype] = row["n"]
    except Exception:
        pass
    return counts


def format_post_summary(phone_number: str) -> str:
    """
    Build a human-readable summary of the user's post queue.
    Promotes overdue scheduled posts first, then reads counts.
    """
    promote_overdue_scheduled(phone_number)
    counts = get_post_counts(phone_number)
    scheduled_docs = get_scheduled_posts(phone_number)

    pub  = counts["published"]
    sched = counts["scheduled"]
    draft = counts["draft"]

    total_published  = sum(pub.values())
    total_scheduled  = sum(sched.values())
    total_draft      = sum(draft.values())

    lines = ["📊 *Your content summary*\n"]

    # Published
    if total_published:
        parts = []
        if pub["image_post"]: parts.append(f"{pub['image_post']} post{'s' if pub['image_post']>1 else ''}")
        if pub["carousel"]:   parts.append(f"{pub['carousel']} carousel{'s' if pub['carousel']>1 else ''}")
        if pub["reel"]:       parts.append(f"{pub['reel']} reel{'s' if pub['reel']>1 else ''}")
        lines.append(f"✅ *Published:* {', '.join(parts)}")
    else:
        lines.append("✅ *Published:* none yet")

    # Scheduled (with times)
    if total_scheduled and scheduled_docs:
        lines.append(f"\n⏰ *Scheduled ({total_scheduled}):*")
        for doc in scheduled_docs[:5]:
            ct    = _CONTENT_LABELS.get(doc.get("content_type", ""), "post")
            sat   = doc.get("scheduled_at")
            tstr  = sat.strftime("%b %d at %H:%M UTC") if isinstance(sat, datetime) else "unknown time"
            lines.append(f"  • {ct.capitalize()} — {tstr}")
        if len(scheduled_docs) > 5:
            lines.append(f"  • ...and {len(scheduled_docs) - 5} more")
        lines.append("\n_Scheduled posts go live automatically at their set time._")
    elif total_scheduled:
        lines.append(f"\n⏰ *Scheduled:* {total_scheduled} pending")

    # Drafts
    if total_draft:
        parts = []
        if draft["image_post"]: parts.append(f"{draft['image_post']} post{'s' if draft['image_post']>1 else ''}")
        if draft["carousel"]:   parts.append(f"{draft['carousel']} carousel{'s' if draft['carousel']>1 else ''}")
        if draft["reel"]:       parts.append(f"{draft['reel']} reel{'s' if draft['reel']>1 else ''}")
        lines.append(f"\n📝 *Drafts:* {', '.join(parts)}")

    if total_published == 0 and total_scheduled == 0 and total_draft == 0:
        lines.append("\nNo posts created yet. Let's make something! 🐝")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Content Calendar
# ---------------------------------------------------------------------------

def save_content_calendar(phone_number: str, token: str, brand_name: str, days: list) -> None:
    """Upsert a 30-day content calendar for a user."""
    try:
        db = get_db()
        db.content_calendars.update_one(
            {"phone_number": phone_number},
            {"$set": {
                "phone_number": phone_number,
                "token": token,
                "brand_name": brand_name,
                "days": days,
                "updated_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
    except Exception:
        pass


def get_content_calendar_by_token(token: str) -> dict | None:
    """Load a calendar by its public token."""
    try:
        db = get_db()
        doc = db.content_calendars.find_one({"token": token})
        if doc:
            doc.pop("_id", None)
        return doc
    except Exception:
        return None


def get_content_calendar(phone_number: str) -> dict | None:
    """Load a calendar by phone number."""
    try:
        db = get_db()
        doc = db.content_calendars.find_one({"phone_number": phone_number})
        if doc:
            doc.pop("_id", None)
        return doc
    except Exception:
        return None


def update_calendar_day_status(phone_number: str, day_index: int, status: str) -> None:
    """Update the status of a specific calendar day (pending/approved/skipped)."""
    try:
        db = get_db()
        db.content_calendars.update_one(
            {"phone_number": phone_number},
            {"$set": {f"days.{day_index}.status": status}},
        )
    except Exception:
        pass
