#!/usr/bin/env python3
"""
Verify the Apify Zillow actor returns photos + facts for a real listing.

Usage (from automation-foundabee/, with APIFY_API_TOKEN set in .env):
  python scripts/test_zillow.py "https://www.zillow.com/homedetails/...."

Prints: how many photos were found, the first few photo URLs, and the
key-facts summary — so you can confirm the actor works before relying on it
in WhatsApp.
"""

import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import config
from tools import apify_scraper


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/test_zillow.py <zillow_url>")
        sys.exit(1)

    url = sys.argv[1].strip()

    print(f"\nActor : {config.APIFY_ZILLOW_ACTOR}")
    print(f"Token : {'set' if config.APIFY_API_TOKEN else 'MISSING — set APIFY_API_TOKEN in .env'}")
    print(f"URL   : {url}")
    print("─" * 60)

    if not config.APIFY_API_TOKEN:
        print("❌ No APIFY_API_TOKEN — cannot run.")
        sys.exit(1)

    result = apify_scraper.scrape_zillow(url)
    if not result:
        print("❌ Actor returned nothing. Either the run failed, the URL is wrong,")
        print("   or this actor doesn't support detail URLs. Check the Apify run log.")
        sys.exit(1)

    imgs = result["image_urls"]
    print(f"✅ Photos found: {len(imgs)}")
    for u in imgs[:5]:
        print(f"   • {u[:110]}")
    if len(imgs) > 5:
        print(f"   … and {len(imgs) - 5} more")

    print("\n── Summary (key facts) ──")
    print(result["summary"][:1200] or "(empty)")

    print("\n── Raw record keys (for field mapping) ──")
    print(", ".join(sorted(result["raw"].keys()))[:1500])


if __name__ == "__main__":
    main()
