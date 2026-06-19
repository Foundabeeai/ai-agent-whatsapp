#!/usr/bin/env python3
"""
Delete a WhatsApp user completely so they re-onboard from scratch.

Usage (run from automation-foundabee/):
  python scripts/delete_user.py +918590679130

What gets wiped:
  Bot DB   (foundabee_whatsapp):  sessions, messages, posts, content_calendars, post_style_skills
  Backend DB (demo-foundabee):    beeq_calendars

After running, restart the bot so the in-memory session cache is cleared:
  sudo systemctl restart beeq
"""

import sys
import os

# Allow running from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import db

def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/delete_user.py <phone>")
        print("Example: python scripts/delete_user.py +918590679130")
        sys.exit(1)

    phone = sys.argv[1].strip()
    print(f"\nDeleting user: {phone}")
    print("─" * 40)

    result = db.delete_user(phone)

    if "error" in result:
        print(f"❌ Error: {result['error']}")
        sys.exit(1)

    for collection, count in result.items():
        print(f"  {collection:<28} {count} deleted")

    print("─" * 40)
    print("✅ Done. Restart the bot to clear the in-memory cache:")
    print("   sudo systemctl restart beeq\n")

if __name__ == "__main__":
    main()
