"""
Detailed poster / flyer generator (openai/gpt-image-2 via Replicate).

For "detailed" posts — rich real-estate / product marketing flyers with a price,
feature checklist, appliance/extras box, a property photo, the agent's cut-out
photo and contact block, and brand logos. gpt-image-2 renders legible on-image
text, so we drive it with a structured layout prompt + reference images
(property photo, agent headshot, brand logo).

Public entry:
    generate_detailed_poster(details, property_image_url, agent_photo_url,
                             logo_url, brand) -> {"ok", "url", "bytes"} | {"ok": False, "error"}
"""

from __future__ import annotations

import logging
import time

import replicate

import config
from tools.tracing import traceable
from tools.replicate_queue import gated as _gated

logger = logging.getLogger(__name__)

_MODEL = "openai/gpt-image-2"
_MAX_WAIT = 300
_POLL = 5


def _line(label: str, value) -> str:
    return f"- {label}: {value}\n" if value else ""


def _build_prompt(details: dict, brand: dict, has_property: bool, has_agent: bool, has_logo: bool) -> str:
    d = details or {}
    features = d.get("features") or []
    extras = d.get("extras") or []
    contact = d.get("contact") or {}
    colors = (brand.get("brand_colors") or d.get("colors")
              or "deep teal green, warm gold, and clean white")

    feat_str = "; ".join(str(f) for f in features) if features else ""
    extras_title = d.get("extras_title") or "INCLUDED"
    extras_str = "; ".join(str(e) for e in extras) if extras else ""

    ref_notes = []
    if has_property:
        ref_notes.append("the FIRST reference image is the real property/product photo — feature it prominently and keep it unchanged")
    if has_agent:
        ref_notes.append("the agent headshot reference image must appear as a clean cut-out of the SAME person in the contact area")
    if has_logo:
        ref_notes.append("use the brand logo reference for the top logo area")
    ref_block = (" Reference images: " + "; ".join(ref_notes) + ".") if ref_notes else ""

    prompt = (
        "Design a professional, print-quality 1:1 square real-estate / product MARKETING FLYER "
        "(Instagram post). Modern, clean, high-contrast layout with crisp, perfectly legible typography. "
        f"Use a brand color palette of {colors} as accents.\n\n"
        "Include these elements, arranged tastefully (you may reorder for the best composition):\n"
        f"{_line('Top brand/logo', brand.get('brand_name') or d.get('brand_name'))}"
        f"{_line('Offer banner', d.get('offer'))}"
        f"{_line('Big price', d.get('price'))}"
        f"{_line('Headline / product name', d.get('title'))}"
        f"{_line('Tagline', d.get('tagline'))}"
        f"{_line('Key features (as a checklist with check icons)', feat_str)}"
        f"{_line(f'A boxed section titled {extras_title!r} listing', extras_str)}"
        f"{_line('Highlight callout badge', d.get('callout'))}"
        f"{_line('A large photo of the property/product', 'from the reference image')}"
        "Bottom contact bar with the agent's cut-out photo and:\n"
        f"{_line('  Name', contact.get('name'))}"
        f"{_line('  Title/role', contact.get('role'))}"
        f"{_line('  Phone', contact.get('phone'))}"
        f"{_line('  Email', contact.get('email'))}"
        f"{_line('  Brokerage/company', contact.get('company'))}"
        "\nRules: render ALL text exactly as given, spelled correctly, sharp and readable. "
        "Balanced professional composition, generous whitespace, real-estate-flyer aesthetic. "
        "No lorem ipsum, no placeholder text, no watermarks." + ref_block
    )
    return prompt


@traceable(run_type="tool", name="generate_detailed_poster")
@_gated("image")
def generate_detailed_poster(
    details: dict,
    property_image_url: str | None = None,
    agent_photo_url: str | None = None,
    logo_url: str | None = None,
    brand: dict | None = None,
    aspect_ratio: str = "1:1",
) -> dict:
    """Generate a detailed marketing-flyer poster with gpt-image-2."""
    if not config.REPLICATE_API_TOKEN:
        return {"ok": False, "error": "REPLICATE_API_TOKEN not set"}
    brand = brand or {}

    # Re-host presigned S3 URLs → clean Replicate URLs (same trick SeedDream uses)
    from tools.image_gen import _to_replicate_url  # reuse helper

    input_images: list[str] = []
    for u in (property_image_url, agent_photo_url, logo_url):
        if u:
            try:
                input_images.append(_to_replicate_url(u))
            except Exception:
                input_images.append(u)

    prompt = _build_prompt(details, brand,
                           has_property=bool(property_image_url),
                           has_agent=bool(agent_photo_url),
                           has_logo=bool(logo_url))

    inp: dict = {
        "prompt": prompt,
        "aspect_ratio": aspect_ratio if aspect_ratio in ("1:1", "4:3", "3:4", "2:3", "3:2") else "1:1",
        "quality": "high",
        "output_format": "png",
        "number_of_images": 1,
    }
    if input_images:
        inp["input_images"] = input_images

    try:
        prediction = replicate.predictions.create(model=_MODEL, input=inp)
    except Exception as exc:
        logger.error("gpt-image-2 create failed: %s", exc)
        return {"ok": False, "error": f"Could not start poster: {exc}"}

    elapsed = 0
    while elapsed < _MAX_WAIT:
        try:
            prediction.reload()
        except Exception:
            time.sleep(_POLL); elapsed += _POLL; continue
        if prediction.status == "succeeded":
            out = prediction.output
            url = None
            if isinstance(out, list) and out:
                raw = out[0]
                url = raw.url if hasattr(raw, "url") else str(raw)
            elif isinstance(out, str):
                url = out
            elif hasattr(out, "url"):
                url = out.url
            if not url:
                return {"ok": False, "error": f"unexpected output: {out!r}"}
            data = None
            try:
                import requests as _req
                r = _req.get(url, timeout=60)
                if r.ok:
                    data = r.content
            except Exception:
                pass
            return {"ok": True, "url": url, "bytes": data}
        if prediction.status in ("failed", "canceled"):
            return {"ok": False, "error": prediction.error or prediction.status}
        time.sleep(_POLL); elapsed += _POLL

    try:
        prediction.cancel()
    except Exception:
        pass
    return {"ok": False, "error": f"poster timed out after {_MAX_WAIT}s"}
