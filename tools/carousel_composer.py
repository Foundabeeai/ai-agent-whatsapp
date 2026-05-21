"""
Research-backed Instagram carousel composer.

Renders text-based slides with stats, data points, and insights
in a high-impact editorial style — matching the visual language of
viral data carousels (bold stats, alternating colour schemes,
profile badge on every slide).

Slide structure:
  Slide 1      — Hook (dark bg or Replicate image, big headline)
  Middle slides — Stage / stat / headline / body (alternating brand colour schemes)
  Last slide   — Finale (brand primary colour, CTA)
"""

from __future__ import annotations

import os
from io import BytesIO
from typing import Optional

import requests
from PIL import Image, ImageDraw, ImageFont

# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────

_W = _H = 1080
_PAD = 80
_BADGE_TOP = 68

# Default colour schemes (overridden by brand colors at runtime)
_DEFAULT_SCHEMES = [
    # 0  Hook — near black
    {"bg": "#111111", "stage": "#FFFFFF", "stat": "#FFFFFF", "hl": "#FFFFFF",
     "body": "#BBBBBB", "cta": "#888888", "counter_bg": "#2A2A2A", "counter_fg": "#FFFFFF"},
    # 1  Cream — warm light
    {"bg": "#F4EFE6", "stage": "#C0392B", "stat": "#111111", "hl": "#111111",
     "body": "#444444", "cta": "#999999", "counter_bg": "#111111", "counter_fg": "#FFFFFF"},
    # 2  Charcoal
    {"bg": "#1C1C1C", "stage": "#D4A017", "stat": "#FFFFFF", "hl": "#FFFFFF",
     "body": "#CCCCCC", "cta": "#888888", "counter_bg": "#333333", "counter_fg": "#FFFFFF"},
    # 3  Scarlet finale
    {"bg": "#C0392B", "stage": "#F9C74F", "stat": "#FFFFFF", "hl": "#FFFFFF",
     "body": "#FFD0CC", "cta": "#FFD0CC", "counter_bg": "#A93226", "counter_fg": "#FFFFFF"},
]

# ─────────────────────────────────────────────────────────────
# Font helpers
# ─────────────────────────────────────────────────────────────

_HN = "/System/Library/Fonts/HelveticaNeue.ttc"
_ARIAL_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
_ARIAL = "/System/Library/Fonts/Supplemental/Arial.ttf"
_LIB_BOLD = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
_LIB = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"
_DEJA_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_DEJA = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_WIN_BOLD = "C:/Windows/Fonts/arialbd.ttf"
_WIN = "C:/Windows/Fonts/arial.ttf"


def _fnt(size: int, weight: str = "bold") -> ImageFont.FreeTypeFont:
    hn_index = {"black": 9, "bold": 1, "regular": 0, "light": 7}.get(weight, 1)
    if os.path.exists(_HN):
        try:
            return ImageFont.truetype(_HN, size, index=hn_index)
        except Exception:
            try:
                return ImageFont.truetype(_HN, size, index=1)
            except Exception:
                pass
    is_bold = weight in ("bold", "black")
    for path in ([_ARIAL_BOLD, _LIB_BOLD, _DEJA_BOLD, _WIN_BOLD]
                 if is_bold else [_ARIAL, _LIB, _DEJA, _WIN]):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


# ─────────────────────────────────────────────────────────────
# Color helpers
# ─────────────────────────────────────────────────────────────

def _rgb(hex_str: str) -> tuple:
    h = hex_str.lstrip("#")
    if len(h) == 3:
        h = h[0]*2 + h[1]*2 + h[2]*2
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def _is_dark(hex_str: str) -> bool:
    r, g, b = _rgb(hex_str)
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    return luminance < 128


def _darken(hex_str: str, factor: float = 0.5) -> str:
    r, g, b = _rgb(hex_str)
    return "#{:02x}{:02x}{:02x}".format(int(r * factor), int(g * factor), int(b * factor))


def _build_schemes(brand_colors: dict | None) -> list[dict]:
    """Build colour schemes, overriding defaults with brand colors if provided."""
    if not brand_colors:
        return _DEFAULT_SCHEMES

    primary = brand_colors.get("primary", "#111111")
    secondary = brand_colors.get("secondary", "#F4EFE6")
    accent = brand_colors.get("accent", "#C0392B")

    # Ensure hook slide bg is dark enough for white text
    hook_bg = primary if _is_dark(primary) else _darken(primary, 0.55)
    primary_text = "#FFFFFF" if _is_dark(primary) else "#111111"
    secondary_text = "#FFFFFF" if _is_dark(secondary) else "#111111"
    secondary_body = "#CCCCCC" if _is_dark(secondary) else "#444444"
    primary_body = "#CCCCCC" if _is_dark(primary) else "#666666"

    # Counter pill: dark on light bg, light on dark bg
    def _counter_bg(bg):
        return "#FFFFFF" if _is_dark(bg) else "#111111"
    def _counter_fg(bg):
        return "#111111" if _is_dark(bg) else "#FFFFFF"

    return [
        # 0 Hook
        {"bg": hook_bg, "stage": "#FFFFFF", "stat": "#FFFFFF", "hl": "#FFFFFF",
         "body": "#BBBBBB", "cta": "#888888",
         "counter_bg": _counter_bg(hook_bg), "counter_fg": _counter_fg(hook_bg)},
        # 1 Secondary (light/neutral)
        {"bg": secondary, "stage": accent, "stat": secondary_text, "hl": secondary_text,
         "body": secondary_body, "cta": "#999999" if not _is_dark(secondary) else "#BBBBBB",
         "counter_bg": _counter_bg(secondary), "counter_fg": _counter_fg(secondary)},
        # 2 Primary (dark/brand)
        {"bg": primary, "stage": accent, "stat": primary_text, "hl": primary_text,
         "body": primary_body, "cta": "#888888" if _is_dark(primary) else "#777777",
         "counter_bg": _counter_bg(primary), "counter_fg": _counter_fg(primary)},
        # 3 Finale — accent bg
        {"bg": accent, "stage": secondary if _is_dark(accent) else primary,
         "stat": "#FFFFFF" if _is_dark(accent) else "#111111",
         "hl": "#FFFFFF" if _is_dark(accent) else "#111111",
         "body": "#EEE" if _is_dark(accent) else "#555",
         "cta": "#EEE" if _is_dark(accent) else "#555",
         "counter_bg": _counter_bg(accent), "counter_fg": _counter_fg(accent)},
    ]


# ─────────────────────────────────────────────────────────────
# Drawing helpers
# ─────────────────────────────────────────────────────────────

def _rounded_rect(draw: ImageDraw.ImageDraw, xy, r: int, fill, alpha: int = 255) -> None:
    x1, y1, x2, y2 = xy
    if x2 <= x1 or y2 <= y1:
        return
    c = (*fill[:3], alpha)
    draw.rectangle([x1 + r, y1, x2 - r, y2], fill=c)
    draw.rectangle([x1, y1 + r, x2, y2 - r], fill=c)
    for cx, cy in [(x1, y1), (x2 - 2*r, y1), (x1, y2 - 2*r), (x2 - 2*r, y2 - 2*r)]:
        draw.ellipse([cx, cy, cx + 2*r, cy + 2*r], fill=c)


def _measure(text: str, font) -> tuple[int, int]:
    """Return (width, height) of text string, accounting for bbox offset."""
    dummy = Image.new("RGB", (1, 1))
    d = ImageDraw.Draw(dummy)
    bb = d.textbbox((0, 0), text, font=font)
    return bb[2] - bb[0], bb[3] - bb[1]


def _draw_text(draw: ImageDraw.ImageDraw, pos: tuple, text: str, font, fill) -> int:
    """Draw text at pos, compensating for bbox origin. Returns actual rendered height."""
    bb = draw.textbbox((0, 0), text, font=font)
    ox, oy = bb[0], bb[1]
    draw.text((pos[0] - ox, pos[1] - oy), text, font=font, fill=fill)
    return bb[3] - bb[1]


def _wrap(text: str, font, max_w: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    dummy = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    for word in words:
        test = f"{cur} {word}".strip()
        w, _ = _measure(test, font)
        if w > max_w and cur:
            lines.append(cur)
            cur = word
        else:
            cur = test
    if cur:
        lines.append(cur)
    return lines


# ─────────────────────────────────────────────────────────────
# Profile badge
# ─────────────────────────────────────────────────────────────

def _avatar_placeholder(size: int, name: str) -> Image.Image:
    palette = [(70, 130, 180), (200, 60, 60), (50, 160, 100), (170, 90, 200)]
    colour = palette[abs(hash(name)) % len(palette)]
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((0, 0, size, size), fill=(*colour, 255))
    initial = (name[0] if name else "B").upper()
    f = _fnt(size // 2, "bold")
    iw, ih = _measure(initial, f)
    _draw_text(d, ((size - iw) // 2, (size - ih) // 2), initial, f, (255, 255, 255, 255))
    return img


def _profile_badge(canvas: Image.Image, username: str, brand_name: str,
                   avatar: Optional[Image.Image]) -> None:
    av_size = 58
    pad_x, pad_y = 10, 9
    gap = 12

    name_f = _fnt(22, "bold")
    handle_f = _fnt(18, "regular")
    check_f = _fnt(20, "bold")

    nw, nh = _measure(brand_name, name_f)
    hw, hh = _measure(f"@{username}", handle_f)
    cw, _ = _measure(" ✓", check_f)

    text_w = max(nw + cw + 6, hw)
    pill_w = pad_x + av_size + gap + text_w + pad_x + 10
    pill_h = av_size + pad_y * 2

    pill = Image.new("RGBA", (pill_w, pill_h), (0, 0, 0, 0))
    pd = ImageDraw.Draw(pill)
    _rounded_rect(pd, (0, 0, pill_w, pill_h), r=pill_h // 2,
                  fill=(255, 255, 255), alpha=230)

    # Avatar
    av_img = (avatar.copy().resize((av_size, av_size)).convert("RGBA")
              if avatar else _avatar_placeholder(av_size, brand_name))
    mask = Image.new("L", (av_size, av_size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, av_size, av_size), fill=255)
    av_img.putalpha(mask)
    pill.paste(av_img, (pad_x, pad_y), av_img)

    tx = pad_x + av_size + gap
    total_th = nh + 5 + hh
    ty = (pill_h - total_th) // 2

    # Name + checkmark
    _draw_text(pd, (tx, ty), brand_name, name_f, (20, 20, 20, 255))
    _draw_text(pd, (tx + nw + 4, ty), "✓", check_f, (29, 155, 240, 255))
    # Handle
    _draw_text(pd, (tx, ty + nh + 5), f"@{username}", handle_f, (100, 100, 100, 255))

    canvas.paste(pill, (_PAD, _BADGE_TOP), pill)


# ─────────────────────────────────────────────────────────────
# Slide counter  (fixed: text perfectly centred in pill)
# ─────────────────────────────────────────────────────────────

def _slide_counter(canvas: Image.Image, current: int, total: int, scheme: dict) -> None:
    text = f"{current}/{total}"
    f = _fnt(24, "bold")
    tw, th = _measure(text, f)
    px, py = 16, 9

    pill_w = tw + px * 2
    pill_h = th + py * 2

    pill = Image.new("RGBA", (pill_w, pill_h), (0, 0, 0, 0))
    pd = ImageDraw.Draw(pill)
    bg = _rgb(scheme["counter_bg"])
    _rounded_rect(pd, (0, 0, pill_w, pill_h), r=pill_h // 2, fill=bg, alpha=220)
    # Centre text in pill
    _draw_text(pd, (px, py), text, f, (*_rgb(scheme["counter_fg"]), 255))

    rx = _W - _PAD - pill_w
    canvas.paste(pill, (rx, _BADGE_TOP), pill)


# ─────────────────────────────────────────────────────────────
# Slide renderers
# ─────────────────────────────────────────────────────────────

def _hook_slide(hook_text: str, total: int, scheme: dict,
                username: str, brand_name: str,
                avatar: Optional[Image.Image],
                hook_image_bytes: Optional[bytes] = None) -> Image.Image:

    if hook_image_bytes:
        # Use AI-generated image as background
        bg_img = Image.open(BytesIO(hook_image_bytes)).convert("RGB")
        # Crop to square
        w, h = bg_img.size
        side = min(w, h)
        bg_img = bg_img.crop(((w - side) // 2, (h - side) // 2,
                               (w + side) // 2, (h + side) // 2))
        bg_img = bg_img.resize((_W, _H), Image.LANCZOS)
        img = bg_img.convert("RGBA")
    else:
        img = Image.new("RGBA", (_W, _H), (*_rgb(scheme["bg"]), 255))

    # Dark overlay — stronger at bottom for text legibility
    overlay = Image.new("RGBA", (_W, _H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for y in range(_H):
        t = y / _H
        # Top: 30% opacity, bottom: 75% opacity
        alpha = int(255 * (0.30 + 0.45 * t))
        od.line([(0, y), (_W, y)], fill=(0, 0, 0, alpha))
    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)

    # Hook headline — bottom third, left aligned
    hl_f = _fnt(76, "black")
    max_w = _W - _PAD * 2
    lines = _wrap(hook_text, hl_f, max_w)
    lh = 88
    block_h = len(lines) * lh
    start_y = _H - block_h - 130

    for i, line in enumerate(lines):
        _draw_text(draw, (_PAD, start_y + i * lh), line, hl_f,
                   (*_rgb(scheme["hl"]), 255))

    swipe_f = _fnt(30, "regular")
    _draw_text(draw, (_PAD, start_y + block_h + 20), "Swipe  →",
               swipe_f, (*_rgb(scheme["cta"]), 255))

    _profile_badge(img, username, brand_name, avatar)
    _slide_counter(img, 1, total, scheme)
    return img.convert("RGB")


def _data_slide(slide: dict, slide_num: int, total: int, scheme: dict,
                username: str, brand_name: str,
                avatar: Optional[Image.Image], is_last: bool = False) -> Image.Image:

    img = Image.new("RGBA", (_W, _H), (*_rgb(scheme["bg"]), 255))
    draw = ImageDraw.Draw(img)

    max_w = _W - _PAD * 2
    y = _BADGE_TOP + 96

    # Stage label
    stage = (slide.get("stage") or "").upper()
    if stage:
        sf = _fnt(26, "bold")
        _draw_text(draw, (_PAD, y), stage, sf, (*_rgb(scheme["stage"]), 255))
        _, sh = _measure(stage, sf)
        y += sh + 20

    # Big stat
    stat = slide.get("stat") or ""
    if stat:
        stf = _fnt(118, "black")
        _draw_text(draw, (_PAD, y), stat, stf, (*_rgb(scheme["stat"]), 255))
        y += int(118 * 1.15)

    # Stat label
    stat_label = (slide.get("stat_label") or "").upper()
    if stat_label:
        slf = _fnt(22, "regular")
        _draw_text(draw, (_PAD, y), stat_label, slf, (*_rgb(scheme["body"]), 255))
        _, slh = _measure(stat_label, slf)
        y += slh + 28

    # Headline
    headline = slide.get("headline") or ""
    if headline:
        hlf = _fnt(52, "bold")
        lines = _wrap(headline, hlf, max_w)
        for line in lines:
            _, lh = _measure(line, hlf)
            _draw_text(draw, (_PAD, y), line, hlf, (*_rgb(scheme["hl"]), 255))
            y += lh + 8
        y += 16

    # Body text
    body = slide.get("body") or ""
    if body:
        bf = _fnt(28, "regular")
        lines = _wrap(body, bf, max_w)
        for line in lines[:5]:
            _, lh = _measure(line, bf)
            _draw_text(draw, (_PAD, y), line, bf, (*_rgb(scheme["body"]), 255))
            y += lh + 10

    # Footer CTA
    if is_last:
        cta_text = (slide.get("cta") or "FOLLOW ALONG — POST DAILY").upper()
    else:
        swipe = (slide.get("swipe") or f"SWIPE FOR SLIDE {slide_num + 1}").upper()
        cta_text = f"→  {swipe}"

    cta_f = _fnt(24, "bold")
    _, cta_h = _measure(cta_text, cta_f)
    cta_y = _H - _PAD - cta_h - 4
    _draw_text(draw, (_PAD, cta_y), cta_text, cta_f, (*_rgb(scheme["cta"]), 255))

    _profile_badge(img, username, brand_name, avatar)
    _slide_counter(img, slide_num, total, scheme)
    return img.convert("RGB")


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

def make_research_carousel(
    carousel_content: dict,
    username: str,
    brand_name: str,
    avatar_url: Optional[str] = None,
    brand_colors: Optional[dict] = None,
    hook_image_bytes: Optional[bytes] = None,
) -> list[bytes]:
    """
    Render a research carousel. Returns list of JPEG bytes (one per slide).

    carousel_content: { "hook": str, "slides": [ {stage, stat, stat_label, headline, body, swipe/cta}, ... ] }
    brand_colors:     { "primary": "#hex", "secondary": "#hex", "accent": "#hex" }
    hook_image_bytes: JPEG/PNG bytes of a Replicate-generated image for slide 1 background
    """
    # Load avatar
    avatar: Optional[Image.Image] = None
    if avatar_url:
        try:
            r = requests.get(avatar_url, timeout=20)
            r.raise_for_status()
            avatar = Image.open(BytesIO(r.content)).convert("RGBA")
        except Exception:
            pass

    schemes = _build_schemes(brand_colors)
    slides = carousel_content.get("slides") or []
    total = 1 + len(slides)
    result: list[bytes] = []

    # Slide 1 — Hook
    hook_img = _hook_slide(
        hook_text=carousel_content.get("hook", ""),
        total=total,
        scheme=schemes[0],
        username=username,
        brand_name=brand_name,
        avatar=avatar,
        hook_image_bytes=hook_image_bytes,
    )
    buf = BytesIO()
    hook_img.save(buf, "JPEG", quality=90, optimize=True)
    result.append(buf.getvalue())

    # Data slides — cycle through schemes 1, 2, 3
    for i, slide in enumerate(slides):
        scheme = schemes[(i % (len(schemes) - 1)) + 1]
        is_last = (i == len(slides) - 1)
        slide_img = _data_slide(
            slide=slide, slide_num=i + 2, total=total,
            scheme=scheme, username=username, brand_name=brand_name,
            avatar=avatar, is_last=is_last,
        )
        buf = BytesIO()
        slide_img.save(buf, "JPEG", quality=90, optimize=True)
        result.append(buf.getvalue())

    return result


def stamp_post_image(
    image_bytes: bytes,
    username: str,
    brand_name: str,
    avatar_url: Optional[str] = None,
) -> bytes:
    """
    Stamp the Instagram-style profile badge (brand name + @handle + avatar) onto
    any post image (JPEG or PNG bytes). Returns stamped JPEG bytes.

    Used for image posts and reels so every piece of content carries the brand identity,
    matching the badge already applied to carousel slides.
    """
    # Load the source image and normalise to 1080×1080 RGBA
    try:
        img = Image.open(BytesIO(image_bytes)).convert("RGBA")
    except Exception:
        return image_bytes  # if PIL can't open it, return original unchanged

    # Resize to square 1080×1080 if needed (crop to centre)
    if img.size != (_W, _H):
        # Scale so the shorter side fills 1080, then centre-crop
        ratio = max(_W / img.width, _H / img.height)
        new_w = int(img.width * ratio)
        new_h = int(img.height * ratio)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        left = (new_w - _W) // 2
        top  = (new_h - _H) // 2
        img  = img.crop((left, top, left + _W, top + _H))

    # Load avatar (same logic as carousel)
    avatar: Optional[Image.Image] = None
    if avatar_url:
        try:
            r = requests.get(avatar_url, timeout=10)
            if r.ok:
                avatar = Image.open(BytesIO(r.content)).convert("RGBA")
        except Exception:
            pass

    # Stamp the badge
    _profile_badge(img, username, brand_name, avatar)

    # Return as JPEG bytes
    buf = BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=92, optimize=True)
    return buf.getvalue()


def render_badge_png(
    username: str,
    brand_name: str,
    avatar_url: Optional[str] = None,
) -> bytes:
    """
    Render just the profile badge as a standalone transparent PNG (no background canvas).
    Used to overlay the badge on a video after compositing.
    Returns RGBA PNG bytes.
    """
    # Load avatar if provided
    avatar: Optional[Image.Image] = None
    if avatar_url:
        try:
            r = requests.get(avatar_url, timeout=10)
            if r.ok:
                avatar = Image.open(BytesIO(r.content)).convert("RGBA")
        except Exception:
            pass

    # Build a temporary 1×1 canvas just to call _profile_badge which draws the pill
    # We use a large transparent canvas, then crop to the pill bounds
    canvas = Image.new("RGBA", (800, 200), (0, 0, 0, 0))
    _profile_badge(canvas, username, brand_name, avatar)

    # Crop to bounding box of non-transparent pixels
    bbox = canvas.getbbox()
    if bbox:
        canvas = canvas.crop(bbox)

    buf = BytesIO()
    canvas.save(buf, "PNG")
    return buf.getvalue()
