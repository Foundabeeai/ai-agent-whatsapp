"""
BeeQ Admin Utilities — for testing and maintenance.

Usage (from project root):
    python admin.py reset +919188621183
    python admin.py reset_all
    python admin.py list
"""

from __future__ import annotations

import sys

from db import get_db, _session_cache


def reset_user(phone: str) -> None:
    """
    Delete a user's session by phone number.
    Accepts formats: +919188621183  /  919188621183  /  whatsapp:+919188621183
    """
    # Normalise to whatsapp:+E164
    phone = phone.strip()
    if not phone.startswith("whatsapp:"):
        if not phone.startswith("+"):
            phone = "+" + phone
        phone = "whatsapp:" + phone

    db = get_db()
    result = db.sessions.delete_one({"phone_number": phone})
    _session_cache.pop(phone, None)

    if result.deleted_count:
        print(f"✅ Deleted session for {phone}")
    else:
        print(f"⚠️  No session found for {phone}")


def reset_by_email(email: str) -> None:
    """Delete ALL sessions associated with a given email address."""
    email = email.strip().lower()
    db = get_db()
    docs = list(db.sessions.find({"verified_email": {"$regex": f"^{email}$", "$options": "i"}},
                                  {"phone_number": 1}))
    if not docs:
        print(f"⚠️  No sessions found for email: {email}")
        return
    for doc in docs:
        phone = doc["phone_number"]
        db.sessions.delete_one({"phone_number": phone})
        _session_cache.pop(phone, None)
        print(f"✅ Deleted session for {phone}")


def reset_all() -> None:
    """Wipe every session — use with care."""
    db = get_db()
    count = db.sessions.count_documents({})
    db.sessions.delete_many({})
    _session_cache.clear()
    print(f"✅ Deleted all {count} session(s).")


def list_users() -> None:
    """Print all sessions with their current step and email."""
    db = get_db()
    docs = list(db.sessions.find({}, {"phone_number": 1, "step": 1, "verified_email": 1,
                                       "onboarding_complete": 1, "_id": 0}))
    if not docs:
        print("No sessions in database.")
        return
    print(f"{'Phone':<35} {'Email':<35} {'Step':<30} {'Onboarded'}")
    print("-" * 110)
    for d in docs:
        print(f"{d.get('phone_number',''):<35} "
              f"{d.get('verified_email','—'):<35} "
              f"{d.get('step',''):<30} "
              f"{'✅' if d.get('onboarding_complete') else '❌'}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print("Commands: reset <phone>  |  reset_email <email>  |  reset_all  |  list")
        sys.exit(0)

    cmd = args[0].lower()
    if cmd == "reset" and len(args) >= 2:
        reset_user(args[1])
    elif cmd == "reset_email" and len(args) >= 2:
        reset_by_email(args[1])
    elif cmd == "reset_all":
        confirm = input("Type YES to wipe all sessions: ")
        if confirm.strip() == "YES":
            reset_all()
        else:
            print("Aborted.")
    elif cmd == "list":
        list_users()
    else:
        print("Commands: reset <phone>  |  reset_email <email>  |  reset_all  |  list")
