"""
Scrape a URL for text content + images and return structured context
that can be injected into agent intent.

Used by the harness when a user includes a link in their message — e.g.
"create a carousel for my property listing www.zillow.com/..."

Returns:
  {
    "url":          str,
    "title":        str,
    "summary":      str,   # LLM-condensed key facts (≤300 words)
    "raw_text":     str,   # full scraped text (truncated to 4000 chars)
    "image_urls":   list[str],   # S3 presigned URLs of scraped images
    "ok":           bool,
    "error":        str | None,
  }
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_TIMEOUT = 20
_MAX_TEXT = 4000
_MAX_IMAGES = 6

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_SKIP_IMG = re.compile(
    r"(icon|logo|avatar|sprite|pixel|badge|button|emoji|thumb(?:nail)?|\.gif|1x1|tracking)",
    re.IGNORECASE,
)


def _abs(url: str, base: str) -> str:
    return urllib.parse.urljoin(base, url)


def _fetch_html(url: str) -> tuple[str, str]:
    """Returns (html, final_url). Falls back to Playwright for JS-heavy pages."""
    try:
        r = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT, allow_redirects=True)
        r.raise_for_status()
        return r.text, r.url
    except Exception:
        pass

    # Playwright fallback for JS-rendered pages (Zillow, Instagram, etc.)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_extra_http_headers(_HEADERS)
            page.goto(url, timeout=25_000, wait_until="networkidle")
            html = page.content()
            final_url = page.url
            browser.close()
            return html, final_url
    except Exception as exc:
        raise RuntimeError(f"Both requests and Playwright failed: {exc}") from exc


def _extract_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()

    # Prefer article/main/section content
    container = soup.find("article") or soup.find("main") or soup.find("body") or soup
    lines = []
    for el in container.find_all(["h1", "h2", "h3", "h4", "p", "li", "td", "span"]):
        t = el.get_text(" ", strip=True)
        if t and len(t) > 20:
            lines.append(t)

    text = "\n".join(lines)
    return text[:_MAX_TEXT]


def _extract_images(soup: BeautifulSoup, base_url: str, html: str = "") -> list[str]:
    candidates: list[str] = []

    # 1. og:image / twitter:image meta tags
    for meta in soup.find_all("meta", property=re.compile(r"^og:image")):
        c = meta.get("content", "")
        if c:
            candidates.append(_abs(c, base_url))
    for meta in soup.find_all("meta", attrs={"name": re.compile(r"twitter:image")}):
        c = meta.get("content", "")
        if c:
            candidates.append(_abs(c, base_url))

    # 2. <img> tags
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
        if not src:
            srcset = img.get("srcset", "")
            if srcset:
                src = srcset.split(",")[-1].strip().split(" ")[0]  # last = highest res
        if not src:
            continue
        abs_src = _abs(src, base_url)
        if _SKIP_IMG.search(abs_src):
            continue
        candidates.append(abs_src)

    # 3. JSON blobs in the raw HTML — JS-heavy sites (Zillow, Airbnb, etc.) embed
    #    their photo CDN URLs inside <script> JSON (__NEXT_DATA__, JSON-LD, etc.).
    #    Grep for high-res image URLs directly.
    blob = html or str(soup)
    for m in re.findall(r'https?:\\?/\\?/[^\s"\'<>]+?\.(?:jpg|jpeg|png|webp)', blob, re.IGNORECASE):
        clean_url = m.replace("\\/", "/").replace("\\u002F", "/")
        if _SKIP_IMG.search(clean_url):
            continue
        candidates.append(clean_url)

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for u in candidates:
        if u not in seen and u.startswith("http"):
            seen.add(u)
            unique.append(u)

    return unique[:_MAX_IMAGES * 4]  # over-collect; quality filter trims later


# Quality thresholds for scraped images
_MIN_LONG_SIDE  = 700      # px — reject anything smaller (thumbnails, icons)
_MIN_BYTES      = 25_000   # ~25KB — reject tiny/placeholder images
_MAX_ASPECT     = 3.0      # reject extreme banners/strips


def _upload_images(image_urls: list[str], phone: str) -> list[str]:
    """
    Download each candidate, keep only HIGH-QUALITY images (large enough, real photos),
    rank by resolution, upload the best ones to S3. Returns presigned S3 URLs.
    """
    from io import BytesIO as _BytesIO
    from PIL import Image as _Image
    from tools.aws_storage import upload_bytes

    scored: list[tuple[int, bytes, str]] = []  # (pixel_area, bytes, ext)
    for url in image_urls:
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
            if not resp.ok:
                continue
            data = resp.content
            if len(data) < _MIN_BYTES:
                continue
            try:
                im = _Image.open(_BytesIO(data))
                w, h = im.size
            except Exception:
                continue
            long_side  = max(w, h)
            short_side = max(1, min(w, h))
            if long_side < _MIN_LONG_SIDE:
                continue
            if long_side / short_side > _MAX_ASPECT:
                continue
            ext = (im.format or "JPEG").lower().replace("jpeg", "jpg")
            scored.append((w * h, data, ext))
        except Exception as exc:
            logger.debug("Failed to fetch/validate scraped image %s: %s", url, exc)

    # Highest resolution first
    scored.sort(key=lambda t: t[0], reverse=True)

    s3_urls: list[str] = []
    for _area, data, ext in scored[:_MAX_IMAGES]:
        try:
            ct = "image/png" if ext == "png" else "image/jpeg"
            up = upload_bytes(data, content_type=ct, extension=ext,
                              folder=f"{phone}/scraped")
            if up.get("ok"):
                s3_urls.append(up["s3_url"])
        except Exception as exc:
            logger.debug("Failed to upload scraped image: %s", exc)

    logger.info("scraped %d candidate images → %d high-quality uploaded",
                len(image_urls), len(s3_urls))
    return s3_urls


def _summarize_text(title: str, raw_text: str, url: str) -> str:
    """Use Groq to condense scraped text into key facts (≤200 words)."""
    try:
        from tools.groq_ai import _chat
        prompt = (
            f"URL: {url}\nPage title: {title}\n\n"
            f"Scraped text:\n{raw_text[:3000]}\n\n"
            "Extract key facts from this page in ≤200 words. "
            "Focus on: what is being sold/promoted, price, location, key features, "
            "any unique selling points. Be specific. No fluff."
        )
        resp = _chat([{"role": "user", "content": prompt}], max_tokens=300, temperature=0.0)
        return resp.strip()
    except Exception:
        return raw_text[:500]


def _scrape_via_apify(url: str, phone: str) -> dict | None:
    """Use Apify for bot-protected sites (Zillow). Returns context dict or None."""
    try:
        from tools import apify_scraper
    except Exception:
        return None
    if not apify_scraper.is_zillow(url):
        return None
    result = apify_scraper.scrape_zillow(url)
    if not result:
        return None
    s3_image_urls = _upload_images(result["image_urls"], phone)
    return {
        "url": url,
        "title": "",
        "summary": result["summary"],
        "raw_text": result["summary"],
        "image_urls": s3_image_urls,
        "ok": True,
        "error": None,
    }


def scrape_url(url: str, phone: str) -> dict:
    """
    Full pipeline: fetch page → extract text + images → upload images to S3
    → summarize text with LLM. Returns structured context dict.

    Bot-protected sites (Zillow) are routed through Apify first.
    """
    # Robust path for sites that block normal scrapers
    apify_ctx = _scrape_via_apify(url, phone)
    if apify_ctx and (apify_ctx["image_urls"] or apify_ctx["summary"]):
        return apify_ctx

    try:
        html, final_url = _fetch_html(url)
    except Exception as exc:
        return {"url": url, "ok": False, "error": str(exc),
                "title": "", "summary": "", "raw_text": "", "image_urls": []}

    soup = BeautifulSoup(html, "html.parser")

    title = ""
    title_tag = soup.find("title")
    if title_tag:
        title = title_tag.get_text(strip=True)
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = og_title["content"]

    raw_text = _extract_text(soup)

    # Detect bot-blocked / access-denied pages (Zillow, LinkedIn, etc. use PerimeterX
    # and friends). These return a tiny denial page with no usable content.
    _blocked_markers = (
        "access to this page has been denied", "access denied",
        "are you a robot", "verify you are human", "captcha",
        "enable javascript and cookies", "request unsuccessful",
    )
    combined_low = (title + " " + raw_text).lower()
    is_blocked = (
        any(m in combined_low for m in _blocked_markers)
        or len(raw_text.strip()) < 80
    )
    if is_blocked:
        logger.warning("url_context: %s appears bot-blocked or empty (text len=%d)",
                       final_url, len(raw_text.strip()))
        return {
            "url": final_url, "ok": False, "error": "blocked",
            "title": title, "summary": "", "raw_text": raw_text, "image_urls": [],
        }

    raw_image_urls = _extract_images(soup, final_url, html=html)
    summary = _summarize_text(title, raw_text, final_url)
    s3_image_urls = _upload_images(raw_image_urls, phone)

    return {
        "url":        final_url,
        "title":      title,
        "summary":    summary,
        "raw_text":   raw_text,
        "image_urls": s3_image_urls,
        "ok":         True,
        "error":      None,
    }


def extract_urls(text: str) -> list[str]:
    """Pull all http/https URLs out of a text string."""
    return re.findall(r"https?://[^\s\)\]\>\"\']+", text)


def extract_bare_urls(text: str) -> list[str]:
    """
    Also catch bare domain URLs like www.zillow.com/... that lack http://.
    Returns them with https:// prepended.
    """
    bare = re.findall(r"\bwww\.[^\s\)\]\>\"\']+", text)
    return [f"https://{u}" for u in bare]


def find_all_urls(text: str) -> list[str]:
    """Find all URLs (http/https + bare www.) in text, deduplicated."""
    all_urls = extract_urls(text) + extract_bare_urls(text)
    seen: set[str] = set()
    unique: list[str] = []
    for u in all_urls:
        clean = u.rstrip(".,;!?)")
        if clean not in seen:
            seen.add(clean)
            unique.append(clean)
    return unique
