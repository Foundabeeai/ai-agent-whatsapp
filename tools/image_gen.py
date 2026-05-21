"""Image generation via Replicate — openai/gpt-image-2 with async polling and retry."""

from __future__ import annotations

import logging
import os
import time

import replicate
from replicate.exceptions import ReplicateError

import config

logger = logging.getLogger(__name__)


os.environ.setdefault("REPLICATE_API_TOKEN", config.REPLICATE_API_TOKEN)

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
    Generate a single image with openai/gpt-image-2 via Replicate.
    Retries both prediction creation and polling on transient errors.
    Returns {"ok": True, "url": "..."} or {"ok": False, "error": "..."}.
    """
    if not config.REPLICATE_API_TOKEN:
        return {"ok": False, "error": "REPLICATE_API_TOKEN not set."}

    realistic_prompt = _make_realistic(prompt)

    # When a reference image is provided the model must keep the product unchanged.
    # The prompt describes ONLY the background/environment — never the product itself —
    # so the model has no reason to reimagine it.
    if reference_urls:
        ref_note = (
            " Use the reference image as the subject. "
            "Keep the subject 100% identical — same shape, colors, branding, texture, "
            "proportions. Replace ONLY the background and lighting with the scene described. "
            "Do not alter the subject in any way."
        )
        realistic_prompt = realistic_prompt.rstrip(" ,") + ref_note

    input_params = {
        "prompt": realistic_prompt,
        "quality": "auto",
        "background": "auto",
        "moderation": "low",
        "aspect_ratio": aspect_ratio,
        "output_format": "jpeg",   # jpeg = max WhatsApp/Twilio compatibility
        "number_of_images": 1,
        "output_compression": 80,
    }

    # Pass first reference image to the model if supported
    if reference_urls:
        input_params["image"] = reference_urls[0]

    # --- create prediction with retry ---
    prediction = None
    last_err: Exception | None = None
    for attempt in range(_CREATE_RETRIES):
        try:
            prediction = replicate.predictions.create(
                model="openai/gpt-image-2",
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
                        model="openai/gpt-image-2", input=safe_params
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
        "size": "2K",
        "sequential_image_generation": "disabled",
        "disable_safety_checker": False,
    }

    if image_url:
        # SeedDream accepts image_input as an array of URLs
        input_params["image_input"] = [image_url]
        logger.info("generate_image_with_reference: image_input=%s", image_url[:80])
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
