"""
Apify-backed scraping for bot-protected sites (primarily Zillow).

Zillow blocks normal scrapers (PerimeterX). Apify's Zillow Detail Scraper actor
runs a real browser farm that gets past the protection and returns structured
property data + photo URLs.

Requires:
  APIFY_API_TOKEN     — from https://console.apify.com/account/integrations
  APIFY_ZILLOW_ACTOR  — actor id (default: maxcopell~zillow-detail-scraper)

Public API:
  scrape_zillow(url) -> dict | None
    {
      "summary":    str,          # human-readable key facts
      "image_urls": list[str],    # high-res photo URLs (NOT yet uploaded to S3)
      "raw":        dict,         # full property record
    }
"""

from __future__ import annotations

import logging

import requests

import config

logger = logging.getLogger(__name__)

_RUN_TIMEOUT = 120  # seconds — Apify run-sync can take a while for Zillow


def is_zillow(url: str) -> bool:
    return "zillow.com" in (url or "").lower()


def _run_actor(actor: str, payload: dict) -> list[dict]:
    """Run an Apify actor synchronously and return its dataset items."""
    token = config.APIFY_API_TOKEN
    if not token:
        raise RuntimeError("APIFY_API_TOKEN not configured")

    endpoint = (
        f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
        f"?token={token}"
    )
    resp = requests.post(endpoint, json=payload, timeout=_RUN_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):  # some actors wrap items
        data = data.get("items", [])
    return data or []


def _extract_photos(item: dict) -> list[str]:
    """Pull high-res photo URLs from a Zillow property record (schema varies)."""
    urls: list[str] = []

    # Common shapes returned by Zillow actors
    photos = item.get("photos") or item.get("responsivePhotos") or item.get("images") or []
    for p in photos:
        if isinstance(p, str):
            urls.append(p)
        elif isinstance(p, dict):
            # responsivePhotos → {"mixedSources": {"jpeg": [{"url": ...}]}}
            mixed = p.get("mixedSources") or {}
            jpegs = mixed.get("jpeg") or mixed.get("webp") or []
            if jpegs:
                # last entry is usually the highest resolution
                best = jpegs[-1]
                if isinstance(best, dict) and best.get("url"):
                    urls.append(best["url"])
            elif p.get("url"):
                urls.append(p["url"])

    # Fallback single hero image
    for key in ("imgSrc", "image", "hiResImageLink", "desktopWebHdpImageLink"):
        v = item.get(key)
        if isinstance(v, str) and v.startswith("http"):
            urls.append(v)

    # Dedupe, keep order
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u and u.startswith("http") and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _build_summary(item: dict) -> str:
    """Compose a concise key-facts string from a Zillow record."""
    parts: list[str] = []

    addr = item.get("address")
    if isinstance(addr, dict):
        addr = ", ".join(
            str(addr[k]) for k in ("streetAddress", "city", "state", "zipcode")
            if addr.get(k)
        )
    if addr:
        parts.append(f"Address: {addr}")

    price = item.get("price") or item.get("unformattedPrice")
    if price:
        parts.append(f"Price: {price}")

    for label, key in (("Beds", "bedrooms"), ("Baths", "bathrooms"),
                       ("Area", "livingArea"), ("Type", "homeType"),
                       ("Year built", "yearBuilt"), ("Lot", "lotAreaValue")):
        v = item.get(key)
        if v:
            parts.append(f"{label}: {v}")

    desc = item.get("description") or ""
    if desc:
        parts.append(f"Description: {desc[:600]}")

    return "\n".join(parts)


def scrape_zillow(url: str) -> dict | None:
    """
    Scrape a Zillow listing via Apify. Returns dict with summary + image_urls,
    or None if Apify isn't configured or the run yields nothing.
    """
    if not config.APIFY_API_TOKEN:
        logger.info("apify: no token configured, skipping Zillow scrape")
        return None

    try:
        items = _run_actor(config.APIFY_ZILLOW_ACTOR, {
            "startUrls": [{"url": url}],
            # common alt input keys different actors accept — harmless extras
            "propertyUrls": [{"url": url}],
            "extractionMethod": "PAGINATION_WITH_ZOOM_IN",
            "maxItems": 1,
        })
    except Exception as exc:
        logger.warning("apify: Zillow scrape failed for %s: %s", url, exc)
        return None

    if not items:
        logger.warning("apify: Zillow scrape returned no items for %s", url)
        return None

    item = items[0]
    image_urls = _extract_photos(item)
    summary    = _build_summary(item)
    logger.info("apify: Zillow scrape ok — %d photos, summary len=%d",
                len(image_urls), len(summary))
    return {"summary": summary, "image_urls": image_urls, "raw": item}
