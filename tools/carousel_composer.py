"""
Instagram carousel composer — editorial magazine style.

Slide layout (matching viral research carousels):
  • Cover   — Full AI photo bg + dark gradient + category pill + big headline
  • Content — Alternating cream/dark bg, stage label + "||" bars + headline + body + swipe hint
  • Finale  — Brand accent bg, big CTA

Image distribution (AI background photos):
  3 slides → 1 image   (cover only)
  4 slides → 2 images  (cover + 1 content)
  5 slides → 2 images
  6 slides → 3 images
  n slides → max(1, n//2) images, distributed evenly

Profile badge: white pill, 76px avatar, name + @handle + blue checkmark — top-left every slide
Slide counter: dark rounded pill — top-right every slide
Text colors: auto-complement background luminance
"""

from __future__ import annotations

import os
from io import BytesIO
from typing import Optional

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ─────────────────────────────────────────────────
# Canvas dimensions (defaults — overridden by style compositor)
# ─────────────────────────────────────────────────
_W = _H = 1080
_PAD = 72          # horizontal padding
_BADGE_Y = 56      # top of profile badge
_CONTENT_TOP = 220 # y where content starts on data slides


def _apply_compositor(c: dict) -> dict:
    """Resolve a compositor dict, filling missing keys with defaults."""
    defaults = {
        "canvas_w": 1080, "canvas_h": 1080, "pad_x": 72,
        "overlay_top_alpha": 20, "overlay_bot_alpha": 200,
        "badge_present": True, "badge_position": "top-left",
        "badge_avatar_px": 72, "badge_offset_x": 72, "badge_offset_y": 56,
        "badge_shape": "pill", "badge_bg_rgba": [255, 255, 255, 235],
        "badge_border_color": None, "badge_border_px": 0,
        "headline_size_px": 78, "headline_weight": "black",
        "headline_y_pct": 0.56, "headline_color": "#ffffff",
        "body_size_px": 26, "body_color": "#cccccc",
        "content_top_y": 220, "slide_counter_present": True,
        "slide_counter_position": "top-right",
        "accent_color": None, "bg_primary": "#111111", "bg_secondary": "#ede8df",
        "text_uppercase": False, "letter_spacing_px": 2,
    }
    return {**defaults, **(c or {})}

# ─────────────────────────────────────────────────
# Font loader — tries system fonts in priority order
# ─────────────────────────────────────────────────
_FONT_CANDIDATES = {
    "black": [
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "C:/Windows/Fonts/arialbd.ttf",
    ],
    "bold": [
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "C:/Windows/Fonts/arialbd.ttf",
    ],
    "regular": [
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "C:/Windows/Fonts/arial.ttf",
    ],
}
_HN = "/System/Library/Fonts/HelveticaNeue.ttc"
_HN_INDEX = {"black": 9, "bold": 1, "regular": 0}


def _fnt(size: int, weight: str = "bold") -> ImageFont.FreeTypeFont:
    if os.path.exists(_HN):
        try:
            return ImageFont.truetype(_HN, size, index=_HN_INDEX.get(weight, 1))
        except Exception:
            pass
    for path in _FONT_CANDIDATES.get(weight, _FONT_CANDIDATES["bold"]):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


# ─────────────────────────────────────────────────
# Color utilities
# ─────────────────────────────────────────────────

def _rgb(h: str) -> tuple:
    h = h.lstrip("#")
    if len(h) == 3:
        h = h[0]*2 + h[1]*2 + h[2]*2
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def _luminance(h: str) -> float:
    r, g, b = _rgb(h)
    return 0.299*r + 0.587*g + 0.114*b


def _is_dark(h: str) -> bool:
    return _luminance(h) < 140


def _darken(h: str, f: float = 0.5) -> str:
    r, g, b = _rgb(h)
    return "#{:02x}{:02x}{:02x}".format(int(r*f), int(g*f), int(b*f))


def _lighten(h: str, f: float = 1.5) -> str:
    r, g, b = _rgb(h)
    return "#{:02x}{:02x}{:02x}".format(min(255, int(r*f)), min(255, int(g*f)), min(255, int(b*f)))


def _on(bg: str) -> str:
    """Return a text color that contrasts with bg."""
    return "#FFFFFF" if _is_dark(bg) else "#111111"


def _body_on(bg: str) -> str:
    return "#CCCCCC" if _is_dark(bg) else "#555555"


def _cta_on(bg: str) -> str:
    return "#888888" if _is_dark(bg) else "#999999"


def _build_schemes(brand_colors: dict | None) -> list[dict]:
    """
    Build 4 colour schemes for the carousel slides.
    [0] = hook/cover (always dark for text legibility on photo)
    [1] = cream/light (warm neutral)
    [2] = dark/brand
    [3] = finale (accent)
    """
    if not brand_colors:
        primary   = "#111111"
        secondary = "#EDE8DF"
        accent    = "#C0392B"
    else:
        primary   = brand_colors.get("primary",   "#111111")
        secondary = brand_colors.get("secondary", "#EDE8DF")
        accent    = brand_colors.get("accent",    "#C0392B")

    # Hook is always photo-bg, overlay handles legibility — scheme just for elements
    hook_bg = "#0D0D0D"

    # Cream slide — secondary or warm neutral
    cream_bg = secondary if not _is_dark(secondary) else "#EDE8DF"

    # Dark slide — primary or near-black
    dark_bg = primary if _is_dark(primary) else _darken(primary, 0.3)
    if _luminance(dark_bg) > 80:   # force dark enough
        dark_bg = "#1A1A1A"

    # Finale — accent
    fin_bg = accent

    def _scheme(bg, stage_color, pill_color=None):
        return {
            "bg":          bg,
            "stage":       stage_color,               # label + bars
            "pill":        pill_color or stage_color, # category pill on cover
            "hl":          _on(bg),                   # headline
            "body":        _body_on(bg),              # body text
            "cta":         _cta_on(bg),               # swipe hint
            "counter_bg":  "#1A1A1A" if not _is_dark(bg) else "#FFFFFF",
            "counter_fg":  "#FFFFFF" if not _is_dark(bg) else "#111111",
            "source":      stage_color,               # source attribution
        }

    return [
        _scheme(hook_bg,  "#FFFFFF", accent),
        _scheme(cream_bg, accent),
        _scheme(dark_bg,  accent),
        _scheme(fin_bg,   _on(fin_bg), _on(fin_bg)),
    ]


# ─────────────────────────────────────────────────
# Drawing primitives
# ─────────────────────────────────────────────────

def _rounded_rect(draw: ImageDraw.ImageDraw, xy, r: int, fill, alpha: int = 255):
    x1, y1, x2, y2 = xy
    if x2 <= x1 or y2 <= y1:
        return
    # Clamp radius so rectangles never get inverted coordinates
    r = min(r, (x2 - x1) // 2, (y2 - y1) // 2)
    if r < 0:
        r = 0
    c = (*fill[:3], alpha)
    draw.rectangle([x1+r, y1, x2-r, y2], fill=c)
    draw.rectangle([x1, y1+r, x2, y2-r], fill=c)
    for cx, cy in [(x1,y1),(x2-2*r,y1),(x1,y2-2*r),(x2-2*r,y2-2*r)]:
        draw.ellipse([cx, cy, cx+2*r, cy+2*r], fill=c)


def _measure(text: str, font) -> tuple[int, int]:
    bb = ImageDraw.Draw(Image.new("RGB",(1,1))).textbbox((0,0), text, font=font)
    return bb[2]-bb[0], bb[3]-bb[1]


def _draw_text(draw: ImageDraw.ImageDraw, pos, text: str, font, fill) -> int:
    bb = draw.textbbox((0,0), text, font=font)
    draw.text((pos[0]-bb[0], pos[1]-bb[1]), text, font=font, fill=fill)
    return bb[3]-bb[1]


def _wrap(text: str, font, max_w: int) -> list[str]:
    words = text.split()
    lines, cur = [], ""
    d = ImageDraw.Draw(Image.new("RGB",(1,1)))
    for w in words:
        test = f"{cur} {w}".strip()
        if d.textbbox((0,0), test, font=font)[2] > max_w and cur:
            lines.append(cur); cur = w
        else:
            cur = test
    if cur:
        lines.append(cur)
    return lines


def _letter_spaced(draw, x, y, text, font, fill, spacing=3):
    """Draw text with extra letter spacing."""
    cx = x
    for ch in text:
        w = _draw_text(draw, (cx, y), ch, font, fill)
        cw, _ = _measure(ch, font)
        cx += cw + spacing
    return cx - x


def _spaced_width(text, font, spacing=3):
    total = 0
    for ch in text:
        cw, _ = _measure(ch, font)
        total += cw + spacing
    return total - spacing


# ─────────────────────────────────────────────────
# Profile badge
# ─────────────────────────────────────────────────

def _avatar_circle(size: int, name: str, source: Optional[Image.Image] = None) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0,0,0,0))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0,0,size,size), fill=255)
    if source:
        av = source.convert("RGBA").resize((size, size), Image.LANCZOS)
        img.paste(av, (0,0))
    else:
        palette = [(70,130,180),(200,60,60),(50,160,100),(170,90,200)]
        col = palette[abs(hash(name)) % len(palette)]
        ImageDraw.Draw(img).ellipse((0,0,size,size), fill=(*col,255))
        f = _fnt(size//2, "bold")
        letter = (name[0] if name else "B").upper()
        lw, lh = _measure(letter, f)
        _draw_text(ImageDraw.Draw(img), ((size-lw)//2,(size-lh)//2), letter, f, (255,255,255,255))
    img.putalpha(mask)
    return img


def _profile_badge(canvas: Image.Image, username: str, brand_name: str,
                   avatar: Optional[Image.Image] = None,
                   comp: Optional[dict] = None) -> None:
    """Draw profile badge using compositor style parameters."""
    if comp and not comp.get("badge_present", True):
        return  # user's style has no badge

    c = comp or {}
    AV  = int(c.get("badge_avatar_px", 72))
    PX  = 10
    PY  = 10
    GAP = 14

    off_x = int(c.get("badge_offset_x", _PAD))
    off_y = int(c.get("badge_offset_y", _BADGE_Y))
    shape = c.get("badge_shape", "pill")  # pill | circle | square
    bg_rgba_raw = c.get("badge_bg_rgba", [255, 255, 255, 235])
    bg_rgba = tuple(int(v) for v in bg_rgba_raw[:4]) if bg_rgba_raw else (255, 255, 255, 235)
    border_color = c.get("badge_border_color")
    border_px    = int(c.get("badge_border_px", 0))
    shows_handle = c.get("shows_handle", True)
    badge_pos    = c.get("badge_position", "top-left")

    name_f   = _fnt(20, "bold")
    handle_f = _fnt(16, "regular")
    check_f  = _fnt(18, "bold")

    nw, nh = _measure(brand_name, name_f)
    hw, hh = _measure(f"@{username}", handle_f) if shows_handle else (0, 0)
    cw, _  = _measure(" ✓", check_f)

    text_w  = max(nw + cw + 6, hw) if shows_handle else nw + cw + 6
    pill_w  = PX + AV + GAP + text_w + PX + 8
    pill_h  = AV + PY*2
    radius  = pill_h // 2 if shape == "pill" else (8 if shape == "square" else pill_h // 2)

    pill = Image.new("RGBA", (pill_w, pill_h), (0, 0, 0, 0))
    pd   = ImageDraw.Draw(pill)
    _rounded_rect(pd, (0, 0, pill_w, pill_h), r=radius, fill=bg_rgba[:3], alpha=bg_rgba[3])

    # Optional border ring
    if border_color and border_px > 0:
        border_rgb = _rgb(border_color)
        pd.rounded_rectangle(
            [border_px//2, border_px//2, pill_w-border_px//2, pill_h-border_px//2],
            radius=radius, outline=(*border_rgb, 255), width=border_px,
        )

    # Drop shadow
    shadow = Image.new("RGBA", (pill_w+8, pill_h+8), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    _rounded_rect(sd, (4, 4, pill_w+4, pill_h+4), r=(pill_h+8)//2, fill=(0, 0, 0), alpha=40)
    shadow = shadow.filter(ImageFilter.GaussianBlur(4))

    # Compute paste position based on badge_position
    W, H = canvas.size
    if badge_pos == "top-right":
        paste_x = W - off_x - pill_w
        paste_y = off_y
    elif badge_pos == "bottom-left":
        paste_x = off_x
        paste_y = H - off_y - pill_h
    elif badge_pos == "bottom-right":
        paste_x = W - off_x - pill_w
        paste_y = H - off_y - pill_h
    else:  # top-left (default)
        paste_x = off_x
        paste_y = off_y

    canvas.paste(shadow, (paste_x - 4, paste_y - 4), shadow)

    av_img = _avatar_circle(AV, brand_name, avatar)
    pill.paste(av_img, (PX, PY), av_img)

    tx = PX + AV + GAP
    total_th = nh + 4 + hh if shows_handle else nh
    ty = (pill_h - total_th) // 2

    text_col = (18, 18, 18, 255) if bg_rgba[0] > 128 else (240, 240, 240, 255)
    _draw_text(pd, (tx, ty),       brand_name,     name_f,   text_col)
    _draw_text(pd, (tx+nw+4, ty),  "✓",            check_f,  (29, 155, 240, 255))
    if shows_handle:
        sub_col = (100, 100, 100, 255) if bg_rgba[0] > 128 else (180, 180, 180, 255)
        _draw_text(pd, (tx, ty+nh+4), f"@{username}", handle_f, sub_col)

    canvas.paste(pill, (paste_x, paste_y), pill)


# ─────────────────────────────────────────────────
# Slide counter
# ─────────────────────────────────────────────────

def _slide_counter(canvas: Image.Image, current: int, total: int, scheme: dict,
                   comp: Optional[dict] = None) -> None:
    c = comp or {}
    if not c.get("slide_counter_present", True):
        return

    pos = c.get("slide_counter_position", "top-right")
    off_x = int(c.get("badge_offset_x", _PAD))
    off_y = int(c.get("badge_offset_y", _BADGE_Y))

    text = f"{current}/{total}"
    f    = _fnt(22, "bold")
    tw, th = _measure(text, f)
    px, py = 18, 10

    pill_w = tw + px*2
    pill_h = th + py*2
    pill   = Image.new("RGBA", (pill_w, pill_h), (0,0,0,0))
    pd     = ImageDraw.Draw(pill)
    bg_rgb = _rgb(scheme["counter_bg"])
    _rounded_rect(pd, (0,0,pill_w,pill_h), r=pill_h//2, fill=bg_rgb, alpha=210)
    _draw_text(pd, (px, py), text, f, (*_rgb(scheme["counter_fg"]), 255))

    W, H = canvas.size
    if pos == "top-left":
        paste_x, paste_y = off_x, off_y + 8
    elif pos == "bottom-right":
        paste_x, paste_y = W - off_x - pill_w, H - off_y - pill_h
    elif pos == "bottom-left":
        paste_x, paste_y = off_x, H - off_y - pill_h
    else:  # top-right (default)
        paste_x, paste_y = W - off_x - pill_w, off_y + 8

    canvas.paste(pill, (paste_x, paste_y), pill)


# ─────────────────────────────────────────────────
# Category pill (cover only)
# ─────────────────────────────────────────────────

def _category_pill(draw: ImageDraw.ImageDraw, canvas: Image.Image,
                   text: str, x: int, y: int, color: str) -> int:
    """Draw a small rounded pill badge. Returns pill height."""
    f    = _fnt(22, "bold")
    tw, th = _measure(text, f)
    px, py = 22, 10
    pw = tw + px*2
    ph = th + py*2

    pill = Image.new("RGBA", (pw, ph), (0,0,0,0))
    pd   = ImageDraw.Draw(pill)
    _rounded_rect(pd, (0,0,pw,ph), r=ph//2, fill=_rgb(color), alpha=255)
    _draw_text(pd, (px, py), text, f, (255,255,255,255))
    canvas.paste(pill, (x, y), pill)
    return ph


# ─────────────────────────────────────────────────
# Gradient overlay helper
# ─────────────────────────────────────────────────

def _gradient_overlay(w: int, h: int, top_alpha: int = 20, bot_alpha: int = 200) -> Image.Image:
    overlay = Image.new("RGBA", (w, h), (0,0,0,0))
    od = ImageDraw.Draw(overlay)
    for y in range(h):
        t = y / h
        alpha = int(top_alpha + (bot_alpha - top_alpha) * (t**1.5))
        od.line([(0,y),(w,y)], fill=(0,0,0,alpha))
    return overlay


# ─────────────────────────────────────────────────
# SLIDE 1 — Cover / Hook
# ─────────────────────────────────────────────────

def _cover_slide(hook_text: str, category: str, total: int, scheme: dict,
                 username: str, brand_name: str, avatar: Optional[Image.Image],
                 bg_bytes: Optional[bytes],
                 comp: Optional[dict] = None) -> Image.Image:
    c   = comp or {}
    W   = int(c.get("canvas_w", _W))
    H   = int(c.get("canvas_h", _H))
    PAD = int(c.get("pad_x", _PAD))
    hl_size     = int(c.get("headline_size_px", 78))
    hl_weight   = c.get("headline_weight", "black")
    hl_y_pct    = float(c.get("headline_y_pct", 0.56))
    hl_color_s  = c.get("headline_color", "#ffffff")
    hl_color    = (*_rgb(hl_color_s), 255)
    top_alpha   = int(c.get("overlay_top_alpha", 30))
    bot_alpha   = int(c.get("overlay_bot_alpha", 210))
    uppercase   = c.get("text_uppercase", False)
    lspacing    = int(c.get("letter_spacing_px", 2))

    # Background
    photo_cover = False
    if bg_bytes:
        bg = Image.open(BytesIO(bg_bytes)).convert("RGB")
        bw, bh = bg.size
        side = min(bw, bh)
        bg = bg.crop(((bw-side)//2, (bh-side)//2, (bw+side)//2, (bh+side)//2))
        bg = bg.resize((W, H), Image.LANCZOS)
        img = bg.convert("RGBA")
        photo_cover = True
    else:
        img = Image.new("RGBA", (W, H), (*_rgb(scheme["bg"]), 255))

    if photo_cover:
        # Readable veil + gradient so the cover hook is legible on any photo
        img = Image.alpha_composite(img, Image.new("RGBA", (W, H), (0, 0, 0, 90)))
        img = Image.alpha_composite(img, _gradient_overlay(W, H, top_alpha=40, bot_alpha=235))
        hl_color = (255, 255, 255, 255)
    else:
        img = Image.alpha_composite(img, _gradient_overlay(W, H, top_alpha=top_alpha, bot_alpha=bot_alpha))
    draw = ImageDraw.Draw(img)

    # Category pill
    cat_text = (category or brand_name or "").upper()
    pill_y = int(H * hl_y_pct)
    pill_h = _category_pill(draw, img, cat_text, PAD, pill_y, scheme["pill"])

    # Headline
    hl_f    = _fnt(hl_size, hl_weight if hl_weight in ("black", "bold", "regular") else "black")
    max_w   = W - PAD*2
    hook_t  = hook_text.upper() if uppercase else hook_text
    lines   = _wrap(hook_t, hl_f, max_w)
    lh      = int(hl_size * 1.15)
    start_y = pill_y + pill_h + 24

    for i, line in enumerate(lines):
        if lspacing > 2:
            _letter_spaced(draw, PAD, start_y + i*lh, line, hl_f, hl_color, spacing=lspacing)
        else:
            _draw_text(draw, (PAD, start_y + i*lh), line, hl_f, hl_color)

    end_y = start_y + len(lines)*lh + 18
    sub_f = _fnt(26, "regular")
    _draw_text(draw, (PAD, end_y), "Swipe to read →", sub_f, (200,200,200,200))

    _profile_badge(img, username, brand_name, avatar, comp=c)
    _slide_counter(img, 1, total, scheme, comp=c)
    return img.convert("RGB")


# ─────────────────────────────────────────────────
# Content slides (text with optional photo bg)
# ─────────────────────────────────────────────────

def _content_slide(slide: dict, slide_num: int, total: int, scheme: dict,
                   username: str, brand_name: str, avatar: Optional[Image.Image],
                   is_last: bool, bg_bytes: Optional[bytes] = None,
                   comp: Optional[dict] = None) -> Image.Image:

    c       = comp or {}
    W       = int(c.get("canvas_w", _W))
    H       = int(c.get("canvas_h", _H))
    PAD     = int(c.get("pad_x", _PAD))
    CT      = int(c.get("content_top_y", _CONTENT_TOP))
    hl_size = int(c.get("headline_size_px", 52))
    body_sz = int(c.get("body_size_px", 26))
    hl_wt   = c.get("headline_weight", "black")
    hl_wt   = hl_wt if hl_wt in ("black", "bold", "regular") else "black"
    hl_col_s   = c.get("headline_color", None)
    body_col_s = c.get("body_color", None)
    uppercase  = c.get("text_uppercase", False)
    lspacing   = int(c.get("letter_spacing_px", 2))
    ta         = int(c.get("overlay_top_alpha", 50))
    ba         = int(c.get("overlay_bot_alpha", 240))

    if bg_bytes:
        bg = Image.open(BytesIO(bg_bytes)).convert("RGB")
        bw, bh = bg.size
        side = min(bw, bh)
        bg = bg.crop(((bw-side)//2, (bh-side)//2, (bw+side)//2, (bh+side)//2))
        bg = bg.resize((W, H), Image.LANCZOS)
        img = bg.convert("RGBA")
        # Guaranteed-readable treatment over ANY photo: a uniform dark veil so
        # text near the top is legible, plus a stronger bottom gradient. Custom
        # style colours are ignored here because they may clash with the photo.
        veil = Image.new("RGBA", (W, H), (0, 0, 0, 95))
        img  = Image.alpha_composite(img, veil)
        img  = Image.alpha_composite(img, _gradient_overlay(W, H, top_alpha=40, bot_alpha=235))
        text_col  = (255, 255, 255, 255)   # white headline — always readable on the veil
        body_col  = (236, 236, 236, 255)
        stage_col = _rgb(scheme["pill"] if "pill" in scheme else scheme["stage"])
        cta_col   = (215, 215, 215, 230)
    else:
        img       = Image.new("RGBA", (W, H), (*_rgb(scheme["bg"]), 255))
        text_col  = (*_rgb(hl_col_s), 255) if hl_col_s else (*_rgb(scheme["hl"]), 255)
        body_col  = (*_rgb(body_col_s), 255) if body_col_s else (*_rgb(scheme["body"]), 255)
        stage_col = _rgb(scheme["stage"])
        cta_col   = (*_rgb(scheme["cta"]), 255)

    draw  = ImageDraw.Draw(img)
    max_w = W - PAD*2
    y     = CT

    # ── MINIMAL / HOOK mode ────────────────────────────────────────────────
    # New carousels carry just a short headline (+ optional tiny subtext). Render
    # it large and centered for a clean, scroll-stopping "hook over photo" look.
    stage_raw = (slide.get("stage") or "").strip()
    stat_raw  = (slide.get("stat") or "").strip()
    if not stage_raw and not stat_raw:
        headline = (slide.get("headline") or "").strip()
        subtext  = (slide.get("body") or "").strip()
        hook_f   = _fnt(max(hl_size, 64), "black")
        sub_f    = _fnt(max(body_sz, 30), "regular")

        hl_lines = _wrap(headline.upper() if uppercase else headline, hook_f, max_w)
        sub_lines = _wrap(subtext, sub_f, max_w) if subtext else []

        line_h    = int(max(hl_size, 64) * 1.12)
        sub_line_h = int(max(body_sz, 30) * 1.3)
        block_h   = len(hl_lines) * line_h + (len(sub_lines) * sub_line_h + 24 if sub_lines else 0)
        cy        = (H - block_h) // 2  # vertically centered

        for line in hl_lines:
            lw, _ = _measure(line, hook_f)
            _draw_text(draw, ((W - lw)//2, cy), line, hook_f, text_col)
            cy += line_h
        if sub_lines:
            cy += 24
            for line in sub_lines:
                lw, _ = _measure(line, sub_f)
                _draw_text(draw, ((W - lw)//2, cy), line, sub_f, body_col)
                cy += sub_line_h

        # Bottom swipe / CTA hint — centered
        if is_last:
            cta_text = (slide.get("cta") or "FOLLOW FOR MORE").upper()
        else:
            cta_text = (slide.get("swipe") or "SWIPE →").upper()
        cta_f = _fnt(22, "bold")
        cw    = _spaced_width(cta_text, cta_f, spacing=lspacing)
        _letter_spaced(draw, (W - cw)//2, H - PAD - 30, cta_text, cta_f, cta_col, spacing=lspacing)

        _profile_badge(img, username, brand_name, avatar, comp=c)
        _slide_counter(img, slide_num, total, scheme, comp=c)
        return img.convert("RGB")

    # Stage label
    stage = (slide.get("stage") or "").upper()
    if stage:
        sf  = _fnt(24, "bold")
        _letter_spaced(draw, PAD, y, stage, sf, (*stage_col, 255), spacing=lspacing)
        _, sh = _measure(stage, sf)
        y += sh + 14

        bar_f = _fnt(22, "bold")
        _draw_text(draw, (PAD, y), "||", bar_f, (*stage_col, 255))
        _, bh2 = _measure("||", bar_f)
        y += bh2 + 22

    # Big stat (if present)
    stat = slide.get("stat") or ""
    if stat:
        stf = _fnt(130, "black")
        _draw_text(draw, (PAD, y), stat, stf, text_col)
        y += int(130 * 1.1)

        stat_label = (slide.get("stat_label") or "").upper()
        if stat_label:
            slf = _fnt(20, "regular")
            _letter_spaced(draw, PAD, y, stat_label, slf, body_col, spacing=lspacing)
            _, slh = _measure(stat_label, slf)
            y += slh + 28

    # Headline
    headline = slide.get("headline") or ""
    if headline:
        hlf   = _fnt(hl_size, hl_wt)
        ht    = headline.upper() if uppercase else headline
        lines = _wrap(ht, hlf, max_w)
        for line in lines:
            if lspacing > 2:
                lw = _letter_spaced(draw, PAD, y, line, hlf, text_col, spacing=lspacing)
                _, lh2 = _measure(line, hlf)
                y += lh2 + 6
            else:
                h_drawn = _draw_text(draw, (PAD, y), line, hlf, text_col)
                y += h_drawn + 6
        y += 20

    # Body — support **bold** markdown-style
    body = slide.get("body") or ""
    if body:
        bf    = _fnt(body_sz, "regular")
        bf_b  = _fnt(body_sz, "bold")
        import re as _re
        tokens = _re.split(r'(\*\*[^*]+\*\*)', body)
        words_rich = []
        for tok in tokens:
            if tok.startswith("**") and tok.endswith("**"):
                for w in tok[2:-2].split():
                    words_rich.append((w, True))
            else:
                for w in tok.split():
                    words_rich.append((w, False))

        lines_rich = []
        cur_line: list = []
        cur_w = 0
        SPACE_W, _ = _measure(" ", bf)
        for word, bold in words_rich:
            f = bf_b if bold else bf
            ww, _ = _measure(word, f)
            if cur_line and cur_w + SPACE_W + ww > max_w:
                lines_rich.append(cur_line)
                cur_line = [(word, bold)]
                cur_w = ww
            else:
                cur_line.append((word, bold))
                cur_w += (SPACE_W if cur_line else 0) + ww
        if cur_line:
            lines_rich.append(cur_line)

        for line_words in lines_rich[:6]:
            cx = PAD
            line_h = 0
            for word, bold in line_words:
                f = bf_b if bold else bf
                sw, sh = _measure(word, f)
                _draw_text(draw, (cx, y), word, f, text_col if bold else body_col)
                cx += sw + SPACE_W
                line_h = max(line_h, sh)
            y += line_h + 10
        y += 8

    # Source attribution
    source = (slide.get("source") or slide.get("stat_label") or "")
    if source and not slide.get("stat"):
        src_text = f"SOURCE: {source.upper()}"
        srcf = _fnt(20, "bold")
        _letter_spaced(draw, PAD, y, src_text, srcf,
                       (*_rgb(scheme.get("source", scheme["stage"])), 255), spacing=lspacing)
        _, srch = _measure(src_text, srcf)
        y += srch + 10

    # Bottom swipe / CTA hint — centered
    if is_last:
        cta_text = (slide.get("cta") or "FOLLOW FOR DAILY INSIGHTS").upper()
    else:
        swipe = (slide.get("swipe") or f"SWIPE FOR SLIDE {slide_num + 1}").upper()
        cta_text = f"→  {swipe}"

    cta_f = _fnt(22, "bold")
    cw    = _spaced_width(cta_text, cta_f, spacing=lspacing)
    cta_x = (W - cw) // 2
    cta_y = H - PAD - 30
    _letter_spaced(draw, cta_x, cta_y, cta_text, cta_f, cta_col, spacing=lspacing)

    sep_y = cta_y - 16
    draw.line([((W-200)//2, sep_y), ((W+200)//2, sep_y)],
              fill=(*_rgb(scheme["cta"]), 80), width=1)

    _profile_badge(img, username, brand_name, avatar, comp=c)
    _slide_counter(img, slide_num, total, scheme, comp=c)
    return img.convert("RGB")


# ─────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────

def make_research_carousel(
    carousel_content: dict,
    username: str,
    brand_name: str,
    avatar_url: Optional[str] = None,
    brand_colors: Optional[dict] = None,
    hook_image_bytes: Optional[bytes] = None,
    extra_bg_bytes: Optional[list[bytes]] = None,
    style_compositor: Optional[dict] = None,
) -> list[bytes]:
    """
    Render a full research carousel. Returns list of JPEG bytes (one per slide).

    carousel_content  : { "hook": str, "slides": [...] }
    brand_colors      : { "primary": "#hex", "secondary": "#hex", "accent": "#hex" }
    hook_image_bytes  : cover slide background image bytes
    extra_bg_bytes    : additional photo-bg images for content slides
    style_compositor  : user's VLM-derived pixel-level style parameters (from db.get_post_style_compositor)
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

    comp    = _apply_compositor(style_compositor)
    # Override brand_colors with compositor accent if present and no explicit brand_colors given
    if not brand_colors and comp.get("accent_color"):
        brand_colors = {
            "primary":   comp.get("bg_primary", "#111111"),
            "secondary": comp.get("bg_secondary", "#ede8df"),
            "accent":    comp["accent_color"],
        }

    schemes = _build_schemes(brand_colors)
    slides  = carousel_content.get("slides") or []
    total   = 1 + len(slides)

    # Determine which content slide indices get a photo background
    # Formula: total_images = max(1, total // 2); first image = cover
    # extra images go to content slides spaced evenly
    n_extra_imgs = max(0, (total // 2) - 1)  # images beyond the cover
    extra_bg_list = list(extra_bg_bytes or [])[:n_extra_imgs]

    # Which content slide indices (0-based) get a photo bg?
    img_slide_indices: set[int] = set()
    if extra_bg_list:
        step = max(1, len(slides) // len(extra_bg_list))
        for k in range(len(extra_bg_list)):
            idx = min(k * step, len(slides) - 1)
            img_slide_indices.add(idx)

    result: list[bytes] = []

    # ── Cover slide ──────────────────────────────
    cover = _cover_slide(
        hook_text   = carousel_content.get("hook", ""),
        category    = (brand_colors or {}).get("category", ""),
        total       = total,
        scheme      = schemes[0],
        username    = username,
        brand_name  = brand_name,
        avatar      = avatar,
        bg_bytes    = hook_image_bytes,
        comp        = comp,
    )
    buf = BytesIO(); cover.save(buf, "JPEG", quality=92, optimize=True)
    result.append(buf.getvalue())

    # ── Content slides ───────────────────────────
    scheme_cycle = [schemes[1], schemes[2]]  # alternate cream ↔ dark
    extra_used   = 0

    for i, slide in enumerate(slides):
        scheme  = scheme_cycle[i % 2]
        is_last = (i == len(slides) - 1)

        # Photo bg for this slide?
        bg_for_slide: Optional[bytes] = None
        if i in img_slide_indices and extra_used < len(extra_bg_list):
            bg_for_slide = extra_bg_list[extra_used]
            extra_used  += 1
            # When photo bg is used, treat it like a cover scheme
            scheme = schemes[0]

        slide_img = _content_slide(
            slide      = slide,
            slide_num  = i + 2,
            total      = total,
            scheme     = scheme,
            username   = username,
            brand_name = brand_name,
            avatar     = avatar,
            is_last    = is_last,
            bg_bytes   = bg_for_slide,
            comp       = comp,
        )
        buf = BytesIO(); slide_img.save(buf, "JPEG", quality=92, optimize=True)
        result.append(buf.getvalue())

    return result


# ─────────────────────────────────────────────────
# Profile badge stamp (used on image posts & reels)
# ─────────────────────────────────────────────────

def stamp_post_image(
    image_bytes: bytes,
    username: str,
    brand_name: str,
    avatar_url: Optional[str] = None,
    style_compositor: Optional[dict] = None,
) -> bytes:
    comp = _apply_compositor(style_compositor)
    W    = int(comp.get("canvas_w", _W))
    H    = int(comp.get("canvas_h", _H))

    try:
        img = Image.open(BytesIO(image_bytes)).convert("RGBA")
    except Exception:
        return image_bytes

    if img.size != (W, H):
        ratio = max(W/img.width, H/img.height)
        nw, nh = int(img.width*ratio), int(img.height*ratio)
        img = img.resize((nw, nh), Image.LANCZOS)
        img = img.crop(((nw-W)//2, (nh-H)//2, (nw-W)//2+W, (nh-H)//2+H))

    avatar: Optional[Image.Image] = None
    if avatar_url:
        try:
            r = requests.get(avatar_url, timeout=10)
            if r.ok:
                avatar = Image.open(BytesIO(r.content)).convert("RGBA")
        except Exception:
            pass

    _profile_badge(img, username, brand_name, avatar, comp=comp)
    buf = BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=92, optimize=True)
    return buf.getvalue()


def render_badge_png(username: str, brand_name: str,
                     avatar_url: Optional[str] = None) -> bytes:
    avatar: Optional[Image.Image] = None
    if avatar_url:
        try:
            r = requests.get(avatar_url, timeout=10)
            if r.ok:
                avatar = Image.open(BytesIO(r.content)).convert("RGBA")
        except Exception:
            pass
    canvas = Image.new("RGBA", (800, 200), (0,0,0,0))
    _profile_badge(canvas, username, brand_name, avatar)
    bbox = canvas.getbbox()
    if bbox:
        canvas = canvas.crop(bbox)
    buf = BytesIO()
    canvas.save(buf, "PNG")
    return buf.getvalue()
