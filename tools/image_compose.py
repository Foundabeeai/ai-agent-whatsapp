"""
Poster compositor — takes a user's exact product photo and produces a
marketing-ready image with:
  • Dramatic cinematic treatment (contrast boost, vignette, colour grade)
  • AI-generated headline + subtext rendered on top
  • Brand-appropriate layout (gradient overlay + text on bottom third)
"""

from __future__ import annotations

import math
import os
import textwrap
from io import BytesIO

import requests
from PIL import (
    Image,
    ImageDraw,
    ImageEnhance,
    ImageFilter,
    ImageFont,
)

# ---------------------------------------------------------------------------
# Font discovery — tries bold/heavy system fonts in order
# ---------------------------------------------------------------------------
_BOLD_FONT_CANDIDATES = [
    # Linux (common on EC2/Ubuntu)
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
    # macOS
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/SFNSDisplay-Bold.otf",
    # Windows
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/impact.ttf",
]
_REGULAR_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
]

_OUTPUT_SIZE = 1080  # square 1080×1080 (Instagram standard)


def _find_font(candidates: list[str]) -> str | None:
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def _load_font(size: int, bold: bool = True) -> ImageFont.ImageFont:
    path = _find_font(_BOLD_FONT_CANDIDATES if bold else _REGULAR_FONT_CANDIDATES)
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def _crop_centre_square(img: Image.Image) -> Image.Image:
    """Crop to a centred square without distortion."""
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top  = (h - side) // 2
    return img.crop((left, top, left + side, top + side))


def _apply_dramatic_effects(img: Image.Image) -> Image.Image:
    """Boost contrast, deepen shadows, and add a cinematic vignette."""
    # Contrast + colour punch
    img = ImageEnhance.Contrast(img).enhance(1.35)
    img = ImageEnhance.Color(img).enhance(1.25)
    img = ImageEnhance.Brightness(img).enhance(0.92)

    # Vignette — radial dark overlay darkest at corners
    vignette = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(vignette)
    cx, cy = img.size[0] // 2, img.size[1] // 2
    max_r = math.hypot(cx, cy)
    steps = 60
    for i in range(steps, 0, -1):
        r = int(max_r * i / steps)
        alpha = int(130 * (1 - (i / steps) ** 1.6))
        draw.ellipse(
            (cx - r, cy - r, cx + r, cy + r),
            fill=(0, 0, 0, alpha),
        )
    img = img.convert("RGBA")
    img = Image.alpha_composite(img, vignette)
    return img.convert("RGB")


def _gradient_overlay(img: Image.Image, overlay_height_frac: float = 0.52) -> Image.Image:
    """Add a dark-to-transparent gradient at the bottom for text legibility."""
    w, h = img.size
    overlay_h = int(h * overlay_height_frac)
    gradient = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(gradient)
    for y in range(overlay_h):
        # Alpha goes from 0 (top of gradient) to 220 (bottom)
        t = y / overlay_h
        alpha = int(220 * t ** 1.4)
        draw.line([(0, h - overlay_h + y), (w, h - overlay_h + y)],
                  fill=(0, 0, 0, alpha))
    img = img.convert("RGBA")
    return Image.alpha_composite(img, gradient).convert("RGB")


def _draw_text_with_shadow(
    draw: ImageDraw.ImageDraw,
    pos: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple,
    shadow_offset: int = 4,
    shadow_blur: int = 8,
) -> None:
    """Draw text with a blurred drop shadow for depth."""
    sx, sy = pos[0] + shadow_offset, pos[1] + shadow_offset
    # Shadow layer
    shadow_img = Image.new("RGBA", draw.im.size, (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow_img)
    sdraw.text((sx, sy), text, font=font, fill=(0, 0, 0, 200))
    shadow_img = shadow_img.filter(ImageFilter.GaussianBlur(radius=shadow_blur))
    # Composite shadow onto base
    base = draw._image  # type: ignore[attr-defined]
    base.paste(Image.alpha_composite(base.convert("RGBA"), shadow_img).convert("RGB"))
    # Draw actual text
    draw.text(pos, text, font=font, fill=fill)


def _wrap_text(text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    """Word-wrap text to fit within max_width pixels."""
    words = text.split()
    lines: list[str] = []
    current = ""
    dummy = Image.new("RGB", (1, 1))
    d = ImageDraw.Draw(dummy)
    for word in words:
        test = f"{current} {word}".strip()
        bbox = d.textbbox((0, 0), test, font=font)
        if bbox[2] > max_width and current:
            lines.append(current)
            current = word
        else:
            current = test
    if current:
        lines.append(current)
    return lines


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def make_poster(
    product_image_url: str,
    headline: str,
    subtext: str = "",
    cta: str = "",
) -> bytes:
    """
    Download the exact product image, apply dramatic effects, render
    marketing text on top, and return JPEG bytes ready for S3 upload.

    Args:
        product_image_url: S3 presigned URL or any public image URL
        headline:  Short punchy marketing headline (≤ 6 words ideal)
        subtext:   Supporting line (optional)
        cta:       Call-to-action (optional, e.g. "Shop now →")

    Returns: JPEG bytes
    """
    # ── Download ─────────────────────────────────────────────────────────────
    resp = requests.get(product_image_url, timeout=60)
    resp.raise_for_status()
    img = Image.open(BytesIO(resp.content)).convert("RGB")

    # ── Crop + resize ────────────────────────────────────────────────────────
    img = _crop_centre_square(img)
    img = img.resize((_OUTPUT_SIZE, _OUTPUT_SIZE), Image.LANCZOS)

    # ── Dramatic treatment ───────────────────────────────────────────────────
    img = _apply_dramatic_effects(img)
    img = _gradient_overlay(img)

    # ── Text rendering ───────────────────────────────────────────────────────
    padding  = 54
    max_text_w = _OUTPUT_SIZE - padding * 2

    headline_font = _load_font(80, bold=True)
    subtext_font  = _load_font(42, bold=False)
    cta_font      = _load_font(38, bold=True)

    # Work out total text block height so we can pin it to just above bottom
    headline_lines = _wrap_text(headline.upper(), headline_font, max_text_w)
    subtext_lines  = _wrap_text(subtext, subtext_font, max_text_w) if subtext else []

    line_h_h = 88   # headline line height
    line_h_s = 52   # subtext line height
    line_h_c = 56   # cta line height

    block_h = (
        len(headline_lines) * line_h_h
        + (len(subtext_lines) * line_h_s + 20 if subtext_lines else 0)
        + (line_h_c + 18 if cta else 0)
    )

    # Pin text block 60 px from the bottom
    text_y = _OUTPUT_SIZE - block_h - 60

    # We need an RGBA canvas to use alpha_composite for the shadow trick
    canvas = img.convert("RGBA")
    draw = ImageDraw.Draw(canvas)

    def _centred_x(text: str, font: ImageFont.ImageFont) -> int:
        bbox = draw.textbbox((0, 0), text, font=font)
        return (_OUTPUT_SIZE - (bbox[2] - bbox[0])) // 2

    # Headline — white, large, bold
    y = text_y
    for line in headline_lines:
        x = _centred_x(line, headline_font)
        # shadow
        draw.text((x + 4, y + 4), line, font=headline_font, fill=(0, 0, 0, 160))
        draw.text((x, y), line, font=headline_font, fill=(255, 255, 255, 255))
        y += line_h_h

    # Subtext — softer white
    if subtext_lines:
        y += 20
        for line in subtext_lines:
            x = _centred_x(line, subtext_font)
            draw.text((x + 2, y + 2), line, font=subtext_font, fill=(0, 0, 0, 130))
            draw.text((x, y), line, font=subtext_font, fill=(220, 220, 220, 240))
            y += line_h_s

    # CTA — gold/amber accent
    if cta:
        y += 18
        x = _centred_x(cta, cta_font)
        draw.text((x + 2, y + 2), cta, font=cta_font, fill=(0, 0, 0, 130))
        draw.text((x, y), cta, font=cta_font, fill=(255, 210, 60, 255))

    # ── Output ───────────────────────────────────────────────────────────────
    final = canvas.convert("RGB")
    buf = BytesIO()
    final.save(buf, "JPEG", quality=88, optimize=True)
    return buf.getvalue()
