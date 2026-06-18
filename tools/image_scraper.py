"""
Scrape images from public URLs (social media, real estate sites, brand sites).

Strategy:
  1. Fast path: requests + BeautifulSoup (works for most static/SSR pages)
  2. Fallback: Playwright (for JS-heavy SPAs like Instagram, Zillow, etc.)

Returns a list of absolute image URLs (max MAX_IMAGES), already filtered for
minimum size and skipping icons/logos.
"""

from __future__ import annotations

import io
import logging
import re
import urllib.parse
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

MAX_IMAGES = 8          # scrape up to this many per page
MIN_DIMENSION = 200     # skip images smaller than this (pixels) when we can tell
_TIMEOUT = 15           # seconds for requests
_PLAYWRIGHT_TIMEOUT = 20_000  # ms


_SKIP_PATTERNS = re.compile(
    r"(icon|logo|avatar|sprite|pixel|badge|button|emoji|thumb(?:nail)?|\.gif)"
    r"|1x1|tracking|analytics",
    re.IGNORECASE,
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ── helpers ────────────────────────────────────────────────────────────────

def _abs(url: str, base: str) -> str:
    return urllib.parse.urljoin(base, url)


def _clean(url: str) -> str:
    """Strip query strings that are just cache-busters, keep CDN tokens."""
    parsed = urllib.parse.urlparse(url)
    # keep the path-only clean version if the query is just width/height params
    qs = parsed.query
    if re.match(r"^(w|h|width|height|q|quality|auto|fit|format)=", qs):
        return urllib.parse.urlunparse(parsed._replace(query=""))
    return url


def _is_likely_image_url(url: str) -> bool:
    path = urllib.parse.urlparse(url).path.lower()
    return bool(re.search(r"\.(jpe?g|png|webp|avif)(\?|$)", path))


def _filter(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for url in urls:
        url = _clean(url)
        if url in seen:
            continue
        seen.add(url)
        if _SKIP_PATTERNS.search(url):
            continue
        result.append(url)
        if len(result) >= MAX_IMAGES:
            break
    return result


def _extract_from_soup(soup: BeautifulSoup, base_url: str) -> list[str]:
    candidates: list[str] = []

    # <meta property="og:image"> first — best quality image on the page
    for tag in soup.find_all("meta", property=re.compile(r"og:image|twitter:image")):
        content = tag.get("content") or ""
        if content:
            candidates.append(_abs(content, base_url))

    # <img> tags — prefer large ones (srcset / data-src)
    for img in soup.find_all("img"):
        src = (
            img.get("data-src")
            or img.get("data-lazy-src")
            or img.get("src")
            or ""
        )
        srcset = img.get("srcset") or ""
        if srcset:
            # pick the largest descriptor
            parts = [p.strip().split() for p in srcset.split(",") if p.strip()]
            if parts:
                largest = max(parts, key=lambda p: _descriptor_weight(p))
                src = largest[0]
        if src:
            candidates.append(_abs(src, base_url))

    # <source> inside <picture>
    for source in soup.find_all("source"):
        srcset = source.get("srcset") or ""
        parts = [p.strip().split() for p in srcset.split(",") if p.strip()]
        if parts:
            largest = max(parts, key=lambda p: _descriptor_weight(p))
            candidates.append(_abs(largest[0], base_url))

    # JSON-LD — sometimes contains image URLs
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            import json
            data = json.loads(script.string or "")
            for img_url in _jsonld_images(data):
                candidates.append(_abs(img_url, base_url))
        except Exception:
            pass

    # Background images in style attributes
    for tag in soup.find_all(style=True):
        for m in re.finditer(r'url\(["\']?([^"\')\s]+)["\']?\)', tag["style"]):
            u = m.group(1)
            if _is_likely_image_url(u):
                candidates.append(_abs(u, base_url))

    return _filter(candidates)


def _descriptor_weight(parts: list[str]) -> int:
    if len(parts) >= 2:
        d = parts[1].lower()
        m = re.match(r"(\d+)(w|x)", d)
        if m:
            val = int(m.group(1))
            return val * (1 if m.group(2) == "x" else 1)
    return 0


def _jsonld_images(data) -> list[str]:
    results: list[str] = []
    if isinstance(data, dict):
        for k, v in data.items():
            if k.lower() in ("image", "thumbnail", "photo", "logo"):
                if isinstance(v, str):
                    results.append(v)
                elif isinstance(v, list):
                    results.extend(x for x in v if isinstance(x, str))
                elif isinstance(v, dict):
                    url = v.get("url") or v.get("contentUrl") or ""
                    if url:
                        results.append(url)
            else:
                results.extend(_jsonld_images(v))
    elif isinstance(data, list):
        for item in data:
            results.extend(_jsonld_images(item))
    return results


# ── fast path ──────────────────────────────────────────────────────────────

def _scrape_requests(url: str) -> list[str]:
    resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT, allow_redirects=True)
    resp.raise_for_status()
    ct = resp.headers.get("content-type", "")
    if "html" not in ct:
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    return _extract_from_soup(soup, resp.url)


# ── playwright fallback ────────────────────────────────────────────────────

def _scrape_playwright(url: str) -> list[str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("playwright not installed — skipping JS fallback")
        return []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(
                user_agent=_HEADERS["User-Agent"],
                viewport={"width": 1280, "height": 900},
            )
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=_PLAYWRIGHT_TIMEOUT)
            # give JS a moment to render images
            page.wait_for_timeout(3000)
            content = page.content()
            final_url = page.url
        finally:
            browser.close()

    soup = BeautifulSoup(content, "html.parser")
    return _extract_from_soup(soup, final_url)


# ── platform-specific extractors ──────────────────────────────────────────

def _is_instagram(url: str) -> bool:
    return "instagram.com" in url.lower()


def _is_tiktok(url: str) -> bool:
    return "tiktok.com" in url.lower()


def _scrape_instagram(url: str) -> list[str]:
    """
    Instagram blocks all scrapers. We use Playwright + wait for img tags.
    Works for public posts/profiles. Private accounts → empty list.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(user_agent=_HEADERS["User-Agent"])
            page = ctx.new_page()
            page.goto(url, wait_until="networkidle", timeout=_PLAYWRIGHT_TIMEOUT)
            page.wait_for_timeout(4000)
            # collect all img src whose path looks like /v/ or /t51.
            imgs = page.eval_on_selector_all(
                "img",
                """els => els.map(el => el.src || el.getAttribute('data-src') || '').filter(Boolean)""",
            )
        finally:
            browser.close()

    return _filter([u for u in imgs if _is_likely_image_url(u) and ("cdninstagram" in u or "fbcdn" in u)])


# ── public API ────────────────────────────────────────────────────────────

def scrape_images(url: str) -> list[str]:
    """
    Return up to MAX_IMAGES image URLs from the given page.
    Tries fast path first, falls back to Playwright for JS-heavy pages.
    Returns [] on failure (never raises).
    """
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        if _is_instagram(url):
            imgs = _scrape_instagram(url)
            if imgs:
                return imgs
            # Instagram likely private/blocked — return empty
            return []

        # fast path
        try:
            imgs = _scrape_requests(url)
            if imgs:
                return imgs
        except Exception as e:
            logger.info("requests scrape failed (%s), trying playwright: %s", type(e).__name__, e)

        # Playwright fallback
        imgs = _scrape_playwright(url)
        return imgs

    except Exception as e:
        logger.warning("scrape_images failed for %s: %s", url, e)
        return []
