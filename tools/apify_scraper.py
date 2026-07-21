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
import re

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
    if resp.status_code >= 400:
        # Surface WHY (402 = actor needs paid rental/credits; 404 = wrong actor id)
        body = (resp.text or "")[:300]
        logger.warning("apify: actor %s HTTP %s — %s", actor, resp.status_code, body)
        resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):  # some actors wrap items
        data = data.get("items", [])
    logger.info("apify: actor %s returned %d items", actor, len(data or []))
    return data or []


_IMG_RE = re.compile(r"https?://[^\s\"'<>]+?\.(?:jpg|jpeg|png|webp)", re.IGNORECASE)
# Zillow photo URLs look like .../fp/<hash>-cc_ft_768.jpg — the size token at the
# end varies per resolution of the SAME photo. Strip it to group variants.
_SIZE_TOKEN_RE = re.compile(r"[-_](?:cc_ft_|p_|e_)?\d{2,4}(?=\.(?:jpg|jpeg|png|webp))", re.IGNORECASE)


_ZILLOW_HASH_RE = re.compile(r"/fp/([0-9a-f]{16,})", re.IGNORECASE)


def _photo_key(url: str) -> str:
    """
    Collapse all size/format variants of the same Zillow photo to one key.
    Zillow URLs are .../fp/<hash>-<sizetoken>.jpg — the <hash> identifies the photo,
    so group by it. (cc_ft_1536, p_d, p_e etc. are all the same photo.)
    """
    m = _ZILLOW_HASH_RE.search(url)
    if m:
        return m.group(1)
    # Non-Zillow: strip trailing numeric size token
    return _SIZE_TOKEN_RE.sub("", url.split("?")[0])


def _walk_image_urls(obj) -> list[str]:
    """Recursively collect every image URL anywhere in the record."""
    found: list[str] = []
    if isinstance(obj, str):
        if obj.startswith("http") and _IMG_RE.match(obj):
            found.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            found.extend(_walk_image_urls(v))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(_walk_image_urls(v))
    return found


def _extract_photos(item: dict, limit: int = 8) -> list[str]:
    """
    Pull DISTINCT high-res photo URLs from a Zillow record. Schema varies wildly
    across actors, so we collect every image URL in the record, group the
    resolution-variants of each photo, and keep the largest variant per photo.
    """
    # 1. Structured arrays (best res per photo) — common key names
    structured: list[str] = []
    for key in ("photos", "responsivePhotos", "originalPhotos", "hugePhotos",
                "carouselPhotos", "images", "galleryPhotos"):
        arr = item.get(key)
        if not isinstance(arr, list):
            continue
        for p in arr:
            if isinstance(p, str) and p.startswith("http"):
                structured.append(p)
            elif isinstance(p, dict):
                mixed = p.get("mixedSources") or {}
                jpegs = mixed.get("jpeg") or mixed.get("webp") or []
                if jpegs and isinstance(jpegs, list):
                    best = jpegs[-1]  # last = highest res
                    if isinstance(best, dict) and best.get("url"):
                        structured.append(best["url"])
                for k in ("url", "src", "href", "image"):
                    v = p.get(k)
                    if isinstance(v, str) and v.startswith("http"):
                        structured.append(v)

    # 2. Deep scan as a safety net (catches any nesting we didn't name)
    deep = _walk_image_urls(item)

    # 3. Group by photo identity, keep the LARGEST variant of each distinct photo
    best_by_photo: dict[str, str] = {}
    def _size_hint(u: str) -> int:
        m = re.search(r"(\d{2,4})(?=\.(?:jpg|jpeg|png|webp))", u, re.IGNORECASE)
        return int(m.group(1)) if m else 0

    for u in structured + deep:
        # skip obvious non-photo assets
        low = u.lower()
        if any(b in low for b in ("logo", "icon", "sprite", "badge", "pixel",
                                  "map", "static-maps", "googleapis", "streetview")):
            continue
        key = _photo_key(u)
        if key not in best_by_photo or _size_hint(u) > _size_hint(best_by_photo[key]):
            best_by_photo[key] = u

    photos = list(best_by_photo.values())

    # 4. Fallback single hero image if nothing else
    if not photos:
        for key in ("imgSrc", "image", "hiResImageLink", "desktopWebHdpImageLink"):
            v = item.get(key)
            if isinstance(v, str) and v.startswith("http"):
                photos.append(v)

    return photos[:limit]


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
    if not image_urls:
        import json as _json
        rp = item.get("responsivePhotos") if isinstance(item, dict) else None
        sample = _json.dumps(rp[0])[:600] if isinstance(rp, list) and rp else f"responsivePhotos={type(rp).__name__} len={len(rp) if isinstance(rp, list) else 'n/a'}"
        logger.warning("apify: Zillow item had NO photos. status=%s sample=%s",
                       item.get("homeStatus"), sample)
    logger.info("apify: Zillow scrape ok — %d photos, summary len=%d",
                len(image_urls), len(summary))
    return {"summary": summary, "image_urls": image_urls, "raw": item}
