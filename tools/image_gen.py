"""Image generation via Replicate — bytedance/seedream-4.5 with async polling and retry."""

from __future__ import annotations

import io
import logging
import os
import time

import replicate
import requests as _requests
from replicate.exceptions import ReplicateError

import config

logger = logging.getLogger(__name__)

os.environ.setdefault("REPLICATE_API_TOKEN", config.REPLICATE_API_TOKEN)


def _to_replicate_url(url: str) -> str:
    """
    Upload a presigned S3 URL to Replicate's file store and return a clean
    https://replicate.delivery/... URL that SeedDream can fetch reliably.
    Falls back to the original URL on any error.
    """
    try:
        resp = _requests.get(url, timeout=30)
        resp.raise_for_status()
        image_bytes = resp.content
        content_type = resp.headers.get("Content-Type", "image/jpeg")
        replicate_file = replicate.files.create(
            io.BytesIO(image_bytes),
            filename="ref.jpg",
            content_type=content_type,
        )
        clean_url = str(replicate_file.urls.get("get", url)) if hasattr(replicate_file, "urls") else str(replicate_file)
        logger.info("_to_replicate_url: uploaded %d bytes → %s", len(image_bytes), clean_url[:80])
        return clean_url
    except Exception as exc:
        logger.warning("_to_replicate_url failed, using original URL: %s", exc)
        return url

_ASPECT_RATIOS = {
    "image_post": "1:1",
    "carousel":   "1:1",
    "reel":       "2:3",
}

_POLL_INTERVAL   = 10   # seconds between status checks
_MAX_WAIT        = 480  # 8 min hard cap per image
_CREATE_RETRIES  = 4    # retry prediction creation on 5xx / timeout errors
_RETRY_BACKOFF   = [5, 15, 30, 60]
_RELOAD_RETRIES  = 5    # consecutive reload failures before giving up

# Appended to every prompt to bias the model toward photorealistic output.
# Keep it positive — negative keywords ("no X") can trigger content filters.
_REALISM_SUFFIX = (
    ", photorealistic, ultra-realistic photograph, shot on Sony A7R V, "
    "85mm f/1.4 lens, natural lighting, 8K resolution, sharp focus, "
    "professional commercial photography, clean studio or lifestyle setting"
)


def _is_e005(exc: Exception) -> bool:
    """True when Replicate returns a content-policy E005 flag."""
    return "e005" in str(exc).lower() or "flagged as sensitive" in str(exc).lower()


def _safe_fallback_prompt(original: str) -> str:
    """Strip style modifiers and return a minimal safe prompt."""
    # Keep only the first sentence / subject description, append safe style tags
    first = original.split(".")[0].strip()
    return (
        f"{first}, clean white studio background, professional product photography, "
        "soft diffused lighting, sharp focus, 8K resolution"
    )


def _is_retryable(exc: Exception) -> bool:
    """True for transient network/server errors worth retrying."""
    msg = str(exc).lower()
    if isinstance(exc, ReplicateError):
        status = getattr(exc, "status", None)
        if status in (502, 503, 504):
            return True
    return any(token in msg for token in (
        "502", "503", "504",
        "bad gateway", "service unavailable", "gateway timeout",
        "connection", "timeout", "timed out", "read timeout",
        "connect timeout", "connectionreset", "remotedisconnected",
    ))


def _make_realistic(prompt: str) -> str:
    """Append realism keywords unless the prompt already requests a specific style."""
    lower = prompt.lower()
    style_words = ("illustration", "cartoon", "watercolor", "oil painting",
                   "vector", "anime", "sketch", "3d render", "digital art")
    if any(w in lower for w in style_words):
        return prompt  # respect an explicitly non-photo style
    return prompt.rstrip(" ,") + _REALISM_SUFFIX


def generate_image(prompt: str, aspect_ratio: str = "1:1", reference_urls: list[str] | None = None) -> dict:
    """
    Generate a single image with bytedance/seedream-4.5 via Replicate.
    Retries both prediction creation and polling on transient errors.
    Returns {"ok": True, "url": "..."} or {"ok": False, "error": "..."}.
    """
    if not config.REPLICATE_API_TOKEN:
        return {"ok": False, "error": "REPLICATE_API_TOKEN not set."}

    # SeedDream only accepts "1:1", "3:2", "2:3" — remap anything else
    if aspect_ratio not in ("1:1", "3:2", "2:3"):
        aspect_ratio = "1:1"

    realistic_prompt = _make_realistic(prompt)

    if reference_urls:
        ref_note = (
            " CRITICAL: The product/subject from the reference image must appear IDENTICALLY — "
            "same shape, colors, branding, texture, proportions. "
            "Replace ONLY the background and lighting with the scene described. "
            "Do not alter the subject in any way."
        )
        realistic_prompt = realistic_prompt.rstrip(" ,") + ref_note

    input_params = {
        "prompt": realistic_prompt,
        "aspect_ratio": aspect_ratio,
    }

    if reference_urls:
        # SeedDream expects image_input as an array; re-host presigned URLs via Replicate
        clean_url = _to_replicate_url(reference_urls[0])
        input_params["image_input"] = [clean_url]

    # --- create prediction with retry ---
    prediction = None
    last_err: Exception | None = None
    for attempt in range(_CREATE_RETRIES):
        try:
            prediction = replicate.predictions.create(
                model="bytedance/seedream-4.5",
                input=input_params,
            )
            break
        except Exception as exc:
            last_err = exc
            if not _is_retryable(exc) or attempt == _CREATE_RETRIES - 1:
                return {"ok": False, "error": f"Could not start prediction: {exc}"}
            time.sleep(_RETRY_BACKOFF[attempt])

    if prediction is None:
        return {"ok": False, "error": f"Prediction creation failed after {_CREATE_RETRIES} attempts: {last_err}"}

    # --- poll until done, retrying transient reload failures ---
    elapsed = 0
    consecutive_failures = 0
    while elapsed < _MAX_WAIT:
        try:
            prediction.reload()
            consecutive_failures = 0  # reset on success
        except Exception as exc:
            if _is_retryable(exc):
                consecutive_failures += 1
                if consecutive_failures <= _RELOAD_RETRIES:
                    time.sleep(_POLL_INTERVAL)
                    elapsed += _POLL_INTERVAL
                    continue
                return {"ok": False, "error": f"Too many transient errors while polling: {exc}"}
            return {"ok": False, "error": f"Polling error: {exc}"}

        status = prediction.status

        if status == "succeeded":
            output = prediction.output
            if not output:
                return {"ok": False, "error": "Model returned empty output."}
            item = output[0]
            url = str(item.url) if hasattr(item, "url") else str(item)
            return {"ok": True, "url": url}

        if status in {"failed", "canceled"}:
            err = getattr(prediction, "error", None) or f"Prediction {status}"
            err_str = str(err)
            # E005 content filter — retry once with a stripped-down safe prompt
            if _is_e005(Exception(err_str)):
                safe = _safe_fallback_prompt(prompt)
                safe_params = dict(input_params)
                safe_params["prompt"] = safe
                # Keep the image reference if one was provided — only simplify the prompt
                try:
                    pred2 = replicate.predictions.create(
                        model="bytedance/seedream-4.5", input=safe_params
                    )
                    elapsed2 = 0
                    while elapsed2 < _MAX_WAIT:
                        pred2.reload()
                        if pred2.status == "succeeded":
                            out = pred2.output
                            if out:
                                item = out[0]
                                url = str(item.url) if hasattr(item, "url") else str(item)
                                return {"ok": True, "url": url}
                        if pred2.status in {"failed", "canceled"}:
                            break
                        time.sleep(_POLL_INTERVAL)
                        elapsed2 += _POLL_INTERVAL
                except Exception:
                    pass
            return {"ok": False, "error": f"Prediction failed: {err_str}"}

        time.sleep(_POLL_INTERVAL)
        elapsed += _POLL_INTERVAL

    try:
        prediction.cancel()
    except Exception:
        pass
    return {"ok": False, "error": f"Image generation timed out after {_MAX_WAIT}s."}


_SEEDREAM_MODEL = "bytedance/seedream-4.5"

def generate_image_with_reference(
    prompt: str,
    image_url: str | None,
    aspect_ratio: str = "2:3",
) -> dict:
    """
    Reel frame generation using bytedance/seedream-4.5.
    Valid aspect_ratio values: "1:1", "3:2", "2:3" (portrait = "2:3").
    Returns {"ok": True, "url": "..."} or {"ok": False, "error": "..."}.
    """
    if not config.REPLICATE_API_TOKEN:
        return {"ok": False, "error": "REPLICATE_API_TOKEN not set."}

    # SeedDream only accepts "1:1", "3:2", "2:3" — map any 9:16 callers to "2:3"
    if aspect_ratio == "9:16":
        aspect_ratio = "2:3"

    input_params: dict = {
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
    }

    if image_url:
        # Re-host presigned S3 URL → clean Replicate URL; pass as array
        clean_url = _to_replicate_url(image_url)
        input_params["image_input"] = [clean_url]
        logger.info("generate_image_with_reference: image_input=%s", clean_url[:80])
    else:
        logger.warning("generate_image_with_reference: no image_url — text-only generation")

    for attempt in range(_CREATE_RETRIES):
        try:
            prediction = replicate.predictions.create(
                model=_SEEDREAM_MODEL,
                input=input_params,
            )
            break
        except Exception as exc:
            if not _is_retryable(exc) or attempt == _CREATE_RETRIES - 1:
                return {"ok": False, "error": f"Could not start prediction: {exc}"}
            time.sleep(_RETRY_BACKOFF[attempt])
    else:
        return {"ok": False, "error": "Prediction creation failed"}

    # Poll until done
    elapsed = 0
    consecutive_failures = 0
    while elapsed < _MAX_WAIT:
        try:
            prediction.reload()
            consecutive_failures = 0
        except Exception:
            consecutive_failures += 1
            if consecutive_failures >= 3:
                return {"ok": False, "error": "Lost connection to Replicate"}
            time.sleep(_POLL_INTERVAL)
            elapsed += _POLL_INTERVAL
            continue

        if prediction.status == "succeeded":
            output = prediction.output
            url = None
            if isinstance(output, list) and output:
                raw = output[0]
                url = raw.url if hasattr(raw, "url") else str(raw)
            elif isinstance(output, str):
                url = output
            elif hasattr(output, "url"):
                url = output.url
            if url:
                return {"ok": True, "url": url}
            return {"ok": False, "error": f"Unexpected output shape: {output!r}"}

        if prediction.status in ("failed", "canceled"):
            return {"ok": False, "error": prediction.error or prediction.status}

        time.sleep(_POLL_INTERVAL)
        elapsed += _POLL_INTERVAL

    try:
        prediction.cancel()
    except Exception:
        pass
    return {"ok": False, "error": f"Timed out after {_MAX_WAIT}s"}


def generate_greenscreen_portrait(
    person_image_url: str,
    clothes_prompt: str = "",
) -> dict:
    """
    Put the SAME person from person_image_url on a solid chroma-key GREEN background,
    framed as a portrait (2:3) suitable for a talking-head overlay.
    Optionally restyle their clothing via clothes_prompt; otherwise keep their outfit.
    Returns {"ok": True, "url": "..."} or {"ok": False, "error": "..."}.
    """
    outfit = (
        f"Change their outfit to: {clothes_prompt}. " if clothes_prompt.strip()
        else "Keep their exact same outfit, hairstyle and appearance. "
    )
    prompt = (
        "Studio portrait of the SAME person from the reference image, head and upper "
        "body visible, centered, looking at the camera as if presenting to it. "
        + outfit +
        "Preserve their face, identity and proportions EXACTLY — do not change who they are. "
        "Place them against a perfectly uniform solid chroma-key GREEN screen background "
        "(#00b140 green), evenly lit, no shadows on the background, clean edges around hair "
        "and shoulders for easy background removal. Professional, sharp, well-lit, photorealistic."
    )
    return generate_image_with_reference(prompt, person_image_url, aspect_ratio="2:3")


def generate_images(
    prompts: list[str],
    content_type: str = "image_post",
    reference_urls: list[str] | None = None,
) -> dict:
    """
    Generate one image per prompt. Returns {"ok": True, "urls": [...]} or error.
    reference_urls — brand asset S3 URLs used as visual style reference.
    """
    aspect_ratio = _ASPECT_RATIOS.get(content_type, "1:1")
    urls: list[str] = []
    for i, prompt in enumerate(prompts):
        result = generate_image(prompt, aspect_ratio=aspect_ratio, reference_urls=reference_urls)
        if not result.get("ok"):
            return result
        urls.append(result["url"])
        if i < len(prompts) - 1:
            time.sleep(2)
    return {"ok": True, "urls": urls}


# ---------------------------------------------------------------------------
# SeedDream product post generation (strict product preservation)
# ---------------------------------------------------------------------------

def generate_product_post(
    prompt: str,
    product_image_url: str,
    aspect_ratio: str = "1:1",
    preserve_subject: bool = False,
) -> dict:
    """
    Generate a professional product post using SeedDream 4.5.
    The product from product_image_url is kept 100% identical —
    only the environment, lighting, and background change.

    preserve_subject=True → STRICT mode: the ENTIRE subject AND scene (a real
    property/product/service photo) must stay structurally identical. Only camera
    angle, lighting, time of day and photographic quality may change. Used for
    real-estate and any actual subject scraped from a link.

    Returns {"ok": True, "url": "...", "bytes": b"..."} or {"ok": False, "error": "..."}.
    """
    if not config.REPLICATE_API_TOKEN:
        return {"ok": False, "error": "REPLICATE_API_TOKEN not set."}

    # SeedDream 1:1 is valid; remap any stray values
    valid_ratios = {"1:1", "3:2", "2:3"}
    if aspect_ratio not in valid_ratios:
        aspect_ratio = "1:1"

    if preserve_subject:
        # Strict: keep the whole real subject/scene; only restage the shot.
        lock_prefix = (
            "CRITICAL: The reference image is a REAL photograph of the actual subject "
            "(a specific property, product or scene). The output MUST be the SAME EXACT "
            "subject — identical architecture, structure, layout, shape, materials, colours "
            "and identity. Do NOT invent a different building, room, product or scene. Do NOT "
            "add, remove or restyle structural features (windows, doors, walls, rooms, roofline). "
            "ONLY change: camera angle and lens, lighting quality and direction, time of day, "
            "weather/sky for exteriors, colour grade, clarity and tasteful cleanup. "
            "It must remain instantly recognisable as the same real subject. "
        )
    else:
        # Product-on-background: lock the product, free to build a scene around it.
        lock_prefix = (
            "CRITICAL: The product in the reference image must appear IDENTICALLY in the output. "
            "Do NOT change its shape, color, packaging design, label text, logo, branding marks, "
            "materials, or proportions in ANY way. Treat the product as a locked element. "
            "ONLY change: the background environment, surface it rests on, ambient lighting, "
            "and atmospheric effects around it. "
            "The product must be the clear hero, sharply in focus, centered or prominently placed. "
        )
    full_prompt = lock_prefix + prompt

    clean_product_url = _to_replicate_url(product_image_url)
    input_params: dict = {
        "prompt": full_prompt,
        "aspect_ratio": aspect_ratio,
        "image_input": [clean_product_url],
    }

    for attempt in range(_CREATE_RETRIES):
        try:
            prediction = replicate.predictions.create(
                model=_SEEDREAM_MODEL,
                input=input_params,
            )
            break
        except Exception as exc:
            if not _is_retryable(exc) or attempt == _CREATE_RETRIES - 1:
                return {"ok": False, "error": f"Could not start prediction: {exc}"}
            time.sleep(_RETRY_BACKOFF[attempt])
    else:
        return {"ok": False, "error": "Prediction creation failed"}

    import requests as _req
    elapsed = 0
    while elapsed < _MAX_WAIT:
        try:
            prediction.reload()
        except Exception:
            time.sleep(_POLL_INTERVAL)
            elapsed += _POLL_INTERVAL
            continue

        if prediction.status == "succeeded":
            output = prediction.output
            url = None
            if isinstance(output, list) and output:
                raw = output[0]
                url = raw.url if hasattr(raw, "url") else str(raw)
            elif isinstance(output, str):
                url = output
            elif hasattr(output, "url"):
                url = output.url
            if url:
                try:
                    img_bytes = _req.get(url, timeout=60).content
                except Exception as exc:
                    return {"ok": False, "error": f"Failed to download result: {exc}"}
                return {"ok": True, "url": url, "bytes": img_bytes}
            return {"ok": False, "error": f"Unexpected output: {output!r}"}

        if prediction.status in ("failed", "canceled"):
            return {"ok": False, "error": prediction.error or prediction.status}

        time.sleep(_POLL_INTERVAL)
        elapsed += _POLL_INTERVAL

    try:
        prediction.cancel()
    except Exception:
        pass
    return {"ok": False, "error": f"Timed out after {_MAX_WAIT}s"}


def overlay_logo(
    image_bytes: bytes,
    logo_url: str,
    position: str = "bottom_right",
    size_pct: float = 0.18,
    padding_pct: float = 0.03,
) -> bytes:
    """
    Overlay the brand logo onto the generated image.
    size_pct   — logo width as fraction of image width (default 18%)
    padding_pct — margin from edges as fraction of image width (default 3%)
    Returns composited JPEG bytes.
    """
    import io as _io
    import requests as _req
    from PIL import Image

    try:
        base = Image.open(_io.BytesIO(image_bytes)).convert("RGBA")
        logo_data = _req.get(logo_url, timeout=30).content
        logo = Image.open(_io.BytesIO(logo_data)).convert("RGBA")

        bw, bh = base.size
        logo_w = int(bw * size_pct)
        ratio = logo_w / logo.width
        logo_h = int(logo.height * ratio)
        logo = logo.resize((logo_w, logo_h), Image.LANCZOS)

        pad = int(bw * padding_pct)
        positions = {
            "bottom_right": (bw - logo_w - pad, bh - logo_h - pad),
            "bottom_left":  (pad, bh - logo_h - pad),
            "top_right":    (bw - logo_w - pad, pad),
            "top_left":     (pad, pad),
        }
        x, y = positions.get(position, positions["bottom_right"])

        # Semi-transparent backing so logo is always readable
        backing = Image.new("RGBA", (logo_w + pad, logo_h + pad), (255, 255, 255, 140))
        base.paste(backing, (x - pad // 2, y - pad // 2), backing)
        base.paste(logo, (x, y), logo)

        out = base.convert("RGB")
        buf = _io.BytesIO()
        out.save(buf, format="JPEG", quality=92)
        return buf.getvalue()
    except Exception as exc:
        logger.warning("overlay_logo failed: %s — returning original image", exc)
        return image_bytes


def generate_product_posts(
    prompts: list[str],
    product_image_url: str,
    logo_url: str | None = None,
    aspect_ratio: str = "1:1",
    preserve_subject: bool = False,
) -> dict:
    """
    Generate product posts with SeedDream (strict product preservation) + optional logo overlay.
    preserve_subject=True keeps the whole real subject/scene intact (see generate_product_post).
    Returns {"ok": True, "bytes_list": [b"..."], "urls": ["..."]} or {"ok": False, "error": "..."}.
    """
    bytes_list: list[bytes] = []
    urls: list[str] = []
    for i, prompt in enumerate(prompts):
        result = generate_product_post(prompt, product_image_url, aspect_ratio=aspect_ratio,
                                       preserve_subject=preserve_subject)
        if not result.get("ok"):
            return result
        img_bytes = result["bytes"]
        if logo_url:
            img_bytes = overlay_logo(img_bytes, logo_url)
        bytes_list.append(img_bytes)
        urls.append(result["url"])
        if i < len(prompts) - 1:
            time.sleep(2)
    return {"ok": True, "bytes_list": bytes_list, "urls": urls}
