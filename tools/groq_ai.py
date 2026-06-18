"""Groq Cloud — image prompt generation and social media caption writing."""

from __future__ import annotations

from groq import Groq

import config


def _client() -> Groq:
    return Groq(api_key=config.GROQ_API_KEY)


def _chat(system: str, user: str, temperature: float = 1.0, max_tokens: int = 8192) -> str:
    resp = _client().chat.completions.create(
        model=config.GROQ_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        temperature=temperature,
        max_completion_tokens=max_tokens,
        top_p=1,
        reasoning_effort="medium",
        stop=None,
    )
    return (resp.choices[0].message.content or "").strip()


def generate_image_prompts(description: str, count: int = 1, brand: dict | None = None) -> list[str]:
    """
    Turn a user's plain-language description into `count` detailed image prompts.
    When `brand` is provided (brand_name, brand_colors, brand_voice, social_goal, etc.),
    the prompts are grounded in those real details — no invented names or random aesthetics.
    """
    brand = brand or {}
    brand_name    = brand.get("brand_name", "")
    brand_colors  = brand.get("brand_colors", "")
    brand_voice   = brand.get("brand_voice", "")
    location_hint = ""  # will be extracted from description if present

    brand_context = ""
    if brand_name:
        brand_context += f"Brand: {brand_name}. "
    if brand_colors:
        brand_context += f"Brand colors: {brand_colors}. "
    if brand_voice:
        brand_context += f"Visual tone: {brand_voice}. "

    system = (
        "You are an expert commercial photography and AI image prompt engineer. "
        "Your job is to create highly detailed, realistic image prompts for a real business. "
        "\n\nCRITICAL RULES:\n"
        "- Use ONLY the real brand name, location, and details provided. NEVER invent names, logos, or places.\n"
        "- Ground every prompt in the actual business context (barbershop, café, gym, etc.).\n"
        "- Describe the physical space, lighting, people, atmosphere realistically.\n"
        "- Include specific colors from the brand palette.\n"
        "- Style: photorealistic commercial photography, not illustration.\n"
        "- Each prompt on its own line. No numbering. Output prompts ONLY."
    )
    user = (
        f"{brand_context}\n"
        f"Content to visualise: {description}\n\n"
        f"Generate {count} distinct photorealistic image prompt(s) that accurately represent this business."
    )
    raw = _chat(system, user, temperature=0.7)
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    while len(lines) < count:
        lines.append(lines[0] if lines else description)
    return lines[:count]


def generate_brand_consistent_prompts(description: str, count: int, brand: dict) -> list[str]:
    """
    Generate `count` carousel image prompts that form a visual story arc AND share
    a unified brand aesthetic (same colors, lighting, style throughout).

    Story structure:
      Slide 1   — Hook / problem / opening scene
      Middle slides — Journey, details, transformation steps
      Last slide — Resolution, CTA, payoff

    Every slide uses identical color palette, lighting mood, and compositional style
    so the carousel feels like one cohesive campaign, not random images.
    """
    positions = _story_positions(count)

    system = (
        "You are an expert AI image prompt engineer for branded Instagram carousels. "
        "You create carousel sets where:\n"
        "1. VISUAL CONSISTENCY — every slide shares the SAME color palette, lighting style, "
        "background treatment, and compositional approach. Someone scrolling should instantly "
        "recognise all slides as belonging to the same brand campaign.\n"
        "2. NARRATIVE ARC — the slides tell a story in sequence. "
        "Slide 1 hooks with a problem or bold opening. "
        "Middle slides show the journey or key details. "
        "The final slide delivers the resolution or call to action.\n"
        "Output ONLY the prompts, one per line, in story order. Do NOT number them."
    )

    brand_ctx = (
        f"Brand: {brand.get('brand_name') or 'the brand'}\n"
        f"Tone/voice: {brand.get('brand_voice') or 'professional'}\n"
        f"Brand colors: {brand.get('brand_colors') or 'derive elegant colors from context'}\n"
        f"Brand description: {brand.get('brand_description') or ''}\n"
    )

    positions_text = "\n".join(f"  Slide {i+1}: {p}" for i, p in enumerate(positions))

    user = (
        f"{brand_ctx}\n"
        f"Carousel topic: {description}\n\n"
        f"Generate exactly {count} image prompts following this story arc:\n"
        f"{positions_text}\n\n"
        f"Rules for every prompt:\n"
        f"- Use the exact same brand colors throughout\n"
        f"- Same lighting mood and background style on every slide\n"
        f"- Same compositional framing (e.g. always centered subject, always left-aligned text space)\n"
        f"- Include photographic/artistic style details (lighting, depth of field, mood)\n"
        f"- Make each slide visually advance the story"
    )

    raw = _chat(system, user, temperature=0.72, max_tokens=1200)
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    while len(lines) < count:
        lines.append(lines[0] if lines else description)
    return lines[:count]


def _story_positions(count: int) -> list[str]:
    """Return story-arc role labels for each slide position."""
    if count == 1:
        return ["Single impactful scene — problem + solution in one frame"]
    if count == 2:
        return ["Hook — bold opening scene that presents the problem or desire",
                "Resolution — satisfying payoff or call to action"]
    if count == 3:
        return ["Hook — bold opening scene that presents the problem or desire",
                "Journey — the key detail, transformation, or 'how'",
                "Resolution — satisfying payoff or call to action"]
    if count == 5:
        return [
            "Hook — bold opening that grabs attention and presents the core problem",
            "Context — establishing the world or setting of the story",
            "Tension / key detail — the most important insight or turning point",
            "Transformation — showing change, progress, or the solution in action",
            "Resolution + CTA — satisfying conclusion with a clear next step",
        ]
    # Generic for any other count
    positions = ["Hook — bold opening that grabs attention and presents the core problem"]
    middle = count - 2
    for i in range(middle):
        positions.append(f"Development step {i+1} — advance the story, add detail or tension")
    positions.append("Resolution + CTA — satisfying conclusion with a clear next step")
    return positions


def generate_caption(description: str, content_type: str, website_url: str = "") -> str:
    """Generate an engaging Instagram caption with max 5 hashtags."""
    link_instruction = (
        f"\nInclude this link naturally near the end: {website_url}"
        if website_url else ""
    )
    system = (
        "You are a social media expert who writes engaging Instagram captions. "
        "Write a compelling caption that hooks the audience, tells a story, "
        "includes a call to action."
        + link_instruction +
        "\nEnd with MAXIMUM 5 highly relevant hashtags — no more. "
        "Output ONLY the caption text, nothing else."
    )
    user = (
        f"Content type: {content_type}\n"
        f"Content description: {description}\n"
        "Write an Instagram caption for this post."
    )
    return _chat(system, user, temperature=0.75)


def generate_product_poster_prompt(description: str, brand: dict) -> str:
    """
    Generate a SeedDream img2img environment/lighting prompt for a product post.
    The product itself is locked by a hard prefix in image_gen.generate_product_post —
    this prompt ONLY describes the environment, lighting, and mood around the product.
    Strict rules prevent hallucination of new product details.
    """
    brand_name   = brand.get("brand_name") or "the brand"
    brand_colors = brand.get("brand_colors") or "neutral professional tones"
    brand_voice  = brand.get("brand_voice") or "premium and clean"

    system = (
        "You are an expert commercial photography art director. "
        "The product image is FIXED — it will be injected into the scene as-is. "
        "Your ONLY job is to describe the ENVIRONMENT and LIGHTING around the product. "
        "\n\nSTRICT RULES — follow every one:\n"
        "1. NEVER describe the product itself — no colors, shapes, labels, packaging.\n"
        "2. NEVER invent or mention any text, logos, or brand marks in the scene.\n"
        "3. ONLY describe: surface the product sits on, background, lighting setup, atmosphere, mood.\n"
        "4. Lighting must be: professional studio or natural lifestyle — soft, directional, with subtle shadows.\n"
        "5. Background must be: clean, uncluttered, blurred bokeh or elegant gradient.\n"
        "6. The scene must feel like a premium commercial photo shoot.\n"
        "7. Square 1:1 composition — leave breathing room around the product.\n"
        "8. End every prompt with: photorealistic, sharp product focus, 8K, commercial photography quality.\n"
        "9. Output ONLY the environment/lighting prompt — one concise paragraph, no headers."
    )
    user = (
        f"Brand: {brand_name}\n"
        f"Brand colors to use in the environment: {brand_colors}\n"
        f"Brand visual tone: {brand_voice}\n"
        f"Content idea / occasion: {description}\n\n"
        "Write the environment and lighting prompt (NOT the product — that is already in the reference image)."
    )
    return _chat(system, user, temperature=0.65, max_tokens=280)


def generate_poster_text(description: str, brand: dict) -> dict:
    """
    Generate dramatic marketing copy for a product poster overlay.

    Returns:
        {
          "headline": "SHORT PUNCHY LINE",
          "subtext":  "Supporting benefit or emotion sentence.",
          "cta":      "Shop now →",
        }
    """
    import json as _json
    import re as _re

    system = (
        "You are a world-class advertising copywriter. "
        "Generate dramatic, emotional marketing text for a product poster image. "
        "Rules:\n"
        "- headline: maximum 5 words, punchy and bold, Title Case\n"
        "- subtext: one short sentence (max 12 words) highlighting a key benefit or emotion\n"
        "- cta: short call to action max 4 words e.g. 'Shop Now →' or 'Try It Today →'\n"
        "Output ONLY valid JSON with keys headline, subtext, cta. No markdown."
    )
    brand_ctx = (
        f"Brand: {brand.get('brand_name') or 'the brand'}\n"
        f"Voice: {brand.get('brand_voice') or 'bold'}\n"
        f"Goal: {brand.get('social_goal') or 'awareness'}\n"
    )
    raw = _chat(system, f"{brand_ctx}\nProduct/content: {description}",
                temperature=0.82, max_tokens=200)
    try:
        clean = _re.sub(r"```[a-z]*", "", raw).strip().strip("`")
        data = _json.loads(clean)
        return {
            "headline": str(data.get("headline") or description[:30]).strip(),
            "subtext":  str(data.get("subtext") or "").strip(),
            "cta":      str(data.get("cta") or "Learn More →").strip(),
        }
    except Exception:
        words = description.split()
        return {
            "headline": " ".join(words[:5]).title(),
            "subtext":  description,
            "cta":      "Learn More →",
        }


def generate_research_carousel_content(topic: str, brand: dict, slide_count: int = 3) -> dict:
    """
    Generate a research-backed carousel script.

    Returns:
        {
          "hook":   "Opening hook headline for slide 1",
          "slides": [
            {
              "stage":      "STAGE 01 — <name>",
              "stat":       "XX%",
              "stat_label": "SHORT LABEL",
              "headline":   "Bold statement.",
              "body":       "1-2 sentence supporting insight.",
              "swipe":      "SWIPE FOR STAGE 2",
            },
            ...
          ]
        }
    """
    import json as _json
    import re as _re

    system = (
        "You are a world-class research analyst and viral content strategist. "
        "You create Instagram carousel scripts that feel like peer-reviewed research translated for the real world — "
        "authoritative, specific, and impossible to scroll past.\n\n"
        "Rules for every carousel:\n"
        "- Hook: One sentence that creates massive curiosity or challenges a belief. "
        "Cite a real institution, study, or data source. Max 15 words.\n"
        "- Each data slide must use REAL statistics from credible sources "
        "(McKinsey, Harvard, MIT, CB Insights, Statista, Forbes, peer-reviewed journals). "
        "If citing a stat, name the source in the stat_label or body.\n"
        "- Stage names should be evocative: not just 'STAGE 01' but 'STAGE 01 — THE SILENT KILLER' etc.\n"
        "- Headline: Max 10 words. Should feel like a punch to the gut — bold, counterintuitive, or alarming.\n"
        "- Body: 2 short sentences max. Every word must earn its place. "
        "Bold claims, specific numbers, zero filler.\n"
        "- The last slide should leave the reader with one powerful takeaway and a strong CTA.\n"
        "- Output ONLY valid JSON matching the schema exactly. No markdown, no extra keys."
    )

    brand_ctx = (
        f"Brand: {brand.get('brand_name') or 'the brand'}\n"
        f"Industry/niche: {brand.get('brand_description') or ''}\n"
        f"Tone: {brand.get('brand_voice') or 'authoritative and insightful'}\n"
    )

    user = (
        f"{brand_ctx}\n"
        f"Carousel topic: {topic}\n"
        f"Number of data slides (not including hook): {slide_count}\n\n"
        f"Return JSON in this exact shape:\n"
        f'{{"hook": "...", "slides": [{{'
        f'"stage": "STAGE 01 — NAME", "stat": "XX%", "stat_label": "SHORT LABEL", '
        f'"headline": "Bold statement.", "body": "Supporting insight.", "swipe": "SWIPE FOR STAGE 2"'
        f'}}, ...]}}'
    )

    raw = _chat(system, user, temperature=0.65, max_tokens=1200)
    try:
        clean = _re.sub(r"```[a-z]*", "", raw).strip().strip("`")
        data = _json.loads(clean)
        # Ensure required keys exist
        if "hook" not in data or "slides" not in data:
            raise ValueError("Missing keys")
        return data
    except Exception:
        # Fallback: build a minimal valid structure
        return {
            "hook": f"What nobody tells you about {topic}.",
            "slides": [
                {
                    "stage": f"STAGE 0{i+1} — KEY INSIGHT",
                    "stat": f"{(i+1)*20}%",
                    "stat_label": "OF BUSINESSES MISS THIS",
                    "headline": f"Key insight {i+1} about {topic}.",
                    "body": "Research consistently shows that the most successful brands prioritise this.",
                    "swipe": f"SWIPE FOR STAGE {i+2}" if i < slide_count - 1 else None,
                    "cta": "FOLLOW FOR DAILY INSIGHTS" if i == slide_count - 1 else None,
                }
                for i in range(slide_count)
            ],
        }


def suggest_music(description: str, content_type: str, brand_voice: str = "") -> dict:
    """
    Suggest a real, licensable song that matches the mood of the content.

    Returns:
        {"name": "Song Title", "artist": "Artist Name", "mood": "upbeat / emotional / ..."}
    """
    import json as _json, re as _re

    system = (
        "You are a music supervisor for social media content. "
        "Suggest ONE real, well-known song that perfectly matches the mood and vibe of the described content. "
        "Prioritise songs that are commonly used on Instagram/TikTok and are likely available in Instagram's music library. "
        "Rules:\n"
        "- Pick a REAL song by a REAL artist — no made-up titles.\n"
        "- Match the energy: upbeat content → energetic track, inspirational → motivational anthem, "
        "emotional → moving ballad, professional/corporate → clean instrumental.\n"
        "- Avoid explicit songs for business/brand content.\n"
        "Output ONLY valid JSON with keys: name, artist, mood. No markdown."
    )
    user = (
        f"Content type: {content_type}\n"
        f"Brand voice / tone: {brand_voice or 'professional'}\n"
        f"Content description: {description}\n"
        "Suggest the best matching song."
    )
    raw = _chat(system, user, temperature=0.7, max_tokens=120).strip()
    try:
        clean = _re.sub(r"```[a-z]*", "", raw).strip().strip("`")
        data = _json.loads(clean)
        return {
            "name":   str(data.get("name") or "").strip(),
            "artist": str(data.get("artist") or "").strip(),
            "mood":   str(data.get("mood") or "").strip(),
        }
    except Exception:
        return {"name": "Good Days", "artist": "SZA", "mood": "uplifting"}


def get_brand_hex_colors(brand_colors: str) -> dict:
    """
    Convert a brand color description (e.g. "Navy blue and gold" or "#1A2B3C")
    to a dict of hex codes: {"primary": "#...", "secondary": "#...", "accent": "#..."}
    Returns sensible defaults if parsing fails.
    """
    import json as _json, re as _re
    if not brand_colors:
        return {"primary": "#111111", "secondary": "#F4EFE6", "accent": "#C0392B"}

    system = (
        "You are a color expert. Convert a brand color description to exact hex codes. "
        "Output ONLY valid JSON with keys: primary, secondary, accent. "
        "primary = the main/dominant brand color, "
        "secondary = a complementary lighter or neutral color, "
        "accent = a highlight or contrast color. "
        "No markdown, no explanation."
    )
    raw = _chat(system, f"Brand colors: {brand_colors}", temperature=0.0, max_tokens=80).strip()
    try:
        clean = _re.sub(r"```[a-z]*", "", raw).strip().strip("`")
        data = _json.loads(clean)
        # Validate — each value should be a hex string
        result = {}
        for key in ("primary", "secondary", "accent"):
            val = str(data.get(key, "")).strip()
            if not val.startswith("#"):
                val = "#" + val
            if len(val) in (4, 7):
                result[key] = val
        if len(result) == 3:
            return result
    except Exception:
        pass
    return {"primary": "#111111", "secondary": "#F4EFE6", "accent": "#C0392B"}


def get_timezone_for_location(location: str) -> str | None:
    """
    Given a city, country, or region string return the best-matching IANA timezone
    (e.g. "Asia/Kolkata", "America/New_York"). Returns None if unrecognisable.
    """
    system = (
        "You are a timezone lookup service. "
        "The user will give you a city, country, or region name. "
        "Reply with ONLY the IANA timezone string (e.g. Asia/Kolkata, America/New_York, Europe/London). "
        "If you cannot determine a timezone, reply with the single word: UNKNOWN. "
        "No explanation, no punctuation, just the timezone string or UNKNOWN."
    )
    raw = _chat(system, location, temperature=0.0, max_tokens=32).strip()
    if raw.upper() == "UNKNOWN" or "/" not in raw:
        return None
    return raw


def understand_intent(user_message: str, context: str = "") -> str:
    """
    Use Groq to understand free-form user messages and normalize intent.
    Returns a clean, structured summary of what the user wants.
    """
    system = (
        "You are a helpful assistant for a WhatsApp social media automation bot. "
        "Your job is to understand what the user wants to create and summarize it clearly. "
        "Be concise and specific."
    )
    user = f"Context: {context}\nUser message: {user_message}"
    return _chat(system, user, temperature=0.3, max_tokens=256)


def extract_voice_answers(transcript: str, pending_fields: list[str]) -> dict:
    """
    Given a voice transcript and a list of pending fields the bot still needs,
    extract as many answers as possible from the transcript.

    `pending_fields` is a list of field names the bot is currently asking about.
    Supported field names (same as UserSession attributes / onboarding steps):
        brand_name, brand_description, social_goal, website_url, brand_voice,
        brand_colors, competitor_handles, posting_schedule, report_frequency,
        user_timezone, content_type, description, image_count, instagram_username,
        publish_action, scheduled_at

    Returns a dict of {field: value} for every field that could be extracted.
    Values are strings (or lists for competitor_handles).
    Missing/unclear fields are omitted entirely.

    Example return:
        {
            "brand_name": "Zara",
            "social_goal": "brand awareness",
            "website_url": "zara.com",
            "posting_schedule": "3 times a week",
            "brand_voice": "aspirational and elegant"
        }
    """
    if not pending_fields or not transcript.strip():
        return {}

    fields_desc = "\n".join(f"- {f}" for f in pending_fields)

    system = """You are an AI assistant helping a WhatsApp social media bot extract structured answers from voice transcripts.

The bot is onboarding/assisting a user and needs specific information. The user may answer multiple questions in one voice message, or make a request like "create me an Instagram post about X".

Your task: read the transcript and extract values for the requested fields. Return ONLY a valid JSON object. Omit any field you are not confident about — never guess. If a field is clearly mentioned or strongly implied, include it.

CRITICAL RULES:
1. For `description`: Extract WHAT the user wants to post about. Strip out the request framing ("can you create a post", "make me a reel", "I want an Instagram post"). Keep only the SUBJECT MATTER. E.g. "can you create me an Instagram post of my newly uploaded game on Google Play Store it's called Yo Yo E" → description: "newly launched mobile game called Yo Yo E now available on Google Play Store".
2. For `content_type`: If user says "post" or "image" → "image_post". "carousel" or "slides" → "carousel". "reel" or "video" → "reel". Default to "image_post" if they just say "post".
3. For proper nouns (brand names, app names, game names): preserve the capitalisation/spelling from the transcript as-is. If they say "it's called Yo Yo E", use "Yo Yo E".
4. For `publish_action`: "now" / "right now" / "immediately" / "yes publish it" → "now". "schedule" / "later" / "tomorrow" → "schedule".

Field definitions:
- brand_name: the name of their brand/business
- brand_description: a short description of what the brand does / sells
- social_goal: their main goal on Instagram (e.g. "increase sales", "brand awareness", "grow followers")
- website_url: their website URL (include https:// if they give a domain)
- brand_voice: their brand's tone/personality (e.g. "professional", "playful and fun", "luxury and elegant")
- brand_colors: description of their brand colors (e.g. "deep navy and gold", "black and white minimal")
- competitor_handles: list of Instagram handles they consider competitors (JSON array of strings, no @ symbols)
- posting_schedule: how often they want to post (e.g. "3 times a week", "daily")
- report_frequency: how often they want performance reports (e.g. "weekly", "monthly")
- user_timezone: their city or timezone (e.g. "Dubai", "New York", "Asia/Kolkata")
- content_type: "image_post", "carousel", or "reel" — infer from their words
- description: the SUBJECT of what they want to post about (cleaned of request framing)
- image_count: number of images they want (integer, usually 1-5)
- instagram_username: their Instagram handle (no @ symbol)
- publish_action: "now" or "schedule"
- scheduled_at: when to post as natural language (e.g. "tomorrow at 9am")
- reel_type: "cinematic" (if user says "cinematic", "product reel", "product video") or "ugc" (if "ugc", "talking head", "my video", "my face", "selfie video")

Return ONLY valid JSON. Example:
{"content_type": "image_post", "description": "newly launched mobile game Yo Yo E on Google Play Store"}"""

    user = f"Requested fields:\n{fields_desc}\n\nTranscript:\n{transcript}"

    try:
        raw = _chat(system, user, temperature=0.1, max_tokens=512)
        # Strip markdown code fences if present
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        import json as _json
        result = _json.loads(raw.strip())
        if isinstance(result, dict):
            return result
    except Exception:
        pass
    return {}


def analyze_product_image(image_url: str) -> str:
    """
    Use Groq vision to produce a concise product description from an image URL.
    Returns a short English description (2-4 sentences) suitable for prompt engineering.
    Falls back to a generic description if vision fails.
    """
    try:
        resp = _client().chat.completions.create(
            model=config.GROQ_VISION_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": image_url},
                        },
                        {
                            "type": "text",
                            "text": (
                                "Describe this product image concisely for use in AI image generation prompts. "
                                "Include: product type, shape, color, material, any visible branding or text. "
                                "2-4 sentences max. Be specific and factual."
                            ),
                        },
                    ],
                }
            ],
            temperature=0.6,
            max_completion_tokens=512,
            top_p=0.95,
            reasoning_effort="default",
            stop=None,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        return "a product photographed against a plain background"


def is_logo_image(image_url: str) -> bool:
    """
    Use Groq vision to detect whether an image is a brand logo/badge
    (as opposed to a product photo or lifestyle photo).
    Returns True if the image appears to be a logo.
    """
    try:
        resp = _client().chat.completions.create(
            model=config.GROQ_VISION_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": (
                        "Is this image a brand logo, icon, or badge? "
                        "Answer with only 'yes' or 'no'."
                    )},
                ],
            }],
            temperature=0.6,
            max_completion_tokens=16,
            top_p=0.95,
            reasoning_effort="default",
            stop=None,
        )
        answer = (resp.choices[0].message.content or "").strip().lower()
        return answer.startswith("yes")
    except Exception:
        return False


def generate_cinematic_product_prompts(
    product_description: str,
    brand: dict,
) -> tuple[str, str]:
    """
    Return two distinct cinematic product-shoot prompts for a Replicate image model.
    Prompt 1 — dramatic studio hero shot.
    Prompt 2 — lifestyle / in-the-wild contextual shot.
    Both stay faithful to the real product and brand colors.
    """
    brand_ctx = (
        f"Brand: {brand.get('brand_name') or 'the brand'}\n"
        f"Brand colors: {brand.get('brand_colors') or 'professional'}\n"
        f"Brand voice: {brand.get('brand_voice') or 'bold and premium'}\n"
    )
    system = (
        "You are a world-class commercial product photographer and AI prompt engineer. "
        "Generate exactly TWO separate image generation prompts separated by the delimiter '---'. "
        "Prompt 1: dramatic dark-background studio hero shot of the product, "
        "dramatic uplighting, neon accent reflections, razor-sharp focus, commercial ad quality. "
        "Prompt 2: lifestyle shot of the product being used in a real environment relevant to the brand, "
        "natural light, cinematic shallow depth of field, editorial photography feel. "
        "CRITICAL RULES:\n"
        "- Describe the product EXACTLY as given — never invent new colors or shapes.\n"
        "- Use the real brand colors as accents in both shots.\n"
        "- End every prompt with: photorealistic, 8K resolution, professional product photography.\n"
        "- Output ONLY the two prompts separated by '---'. No labels, no extra text."
    )
    user = f"{brand_ctx}\nProduct: {product_description}"
    raw = _chat(system, user, temperature=0.72, max_tokens=500)
    parts = [p.strip() for p in raw.split("---") if p.strip()]
    if len(parts) >= 2:
        return parts[0], parts[1]
    fallback = (
        f"Dramatic studio hero shot of {product_description}, "
        f"black background, dramatic lighting, sharp focus, photorealistic, 8K"
    )
    return fallback, fallback


def generate_ugc_script(description: str, brand: dict) -> str:
    """
    Generate a natural, energetic UGC talking-head script for an Instagram Reel.
    3-5 sentences, at least 32 words — enough to fill a compelling 10-15 second clip.
    """
    brand_ctx = (
        f"Brand: {brand.get('brand_name') or ''}\n"
        f"Brand voice: {brand.get('brand_voice') or 'friendly and authentic'}\n"
        f"Goal: {brand.get('social_goal') or 'increase awareness'}\n"
    )
    system = (
        "You write UGC (user-generated content) talking-head scripts for Instagram Reels. "
        "The script is spoken directly to camera by a real person who genuinely loves the product/service.\n"
        "RULES:\n"
        "1. Write 3 to 5 natural spoken sentences.\n"
        "2. The script MUST be at least 32 words — aim for 35-50 words total.\n"
        "3. Open with a punchy hook in the first sentence that grabs attention instantly.\n"
        "4. Describe a real problem the product solves or a transformation it delivers.\n"
        "5. Close with an enthusiastic call to action or personal recommendation.\n"
        "6. Sound natural and conversational — like a friend talking, NOT a scripted ad.\n"
        "7. No hashtags, no stage directions, no speaker labels.\n"
        "8. Output ONLY the spoken words."
    )
    user = f"{brand_ctx}\nProduct/service to promote: {description}"

    raw = _chat(system, user, temperature=0.78, max_tokens=200).strip()
    raw = raw.strip('"').strip("'").strip()

    # Retry once if under 32 words
    if len(raw.split()) < 32:
        raw = _chat(system, user, temperature=0.85, max_tokens=200).strip()
        raw = raw.strip('"').strip("'").strip()

    return raw


def voice_reply_text(bot_message: str) -> str:
    """
    Shorten / rewrite a bot message so it sounds natural when spoken aloud.
    Removes markdown (*bold*, _italic_), button prompts, and trims to ~120 words.
    """
    system = (
        "You convert WhatsApp chatbot messages into short, natural spoken replies. "
        "Remove all markdown formatting (* _ ~ `), remove numbered lists and bullet points. "
        "Keep the core information but make it sound conversational, like talking to a friend. "
        "Maximum 2 short sentences. Never mention 'tap a button' or 'reply with a number'."
    )
    try:
        return _chat(system, bot_message, temperature=0.4, max_tokens=100)
    except Exception:
        # Fallback: strip basic markdown manually
        import re as _re
        clean = _re.sub(r"[*_~`]", "", bot_message)
        return clean[:300]


def generate_reel_text_overlays(description: str, brand: dict) -> list[str]:
    """
    Generate 3 short punchy text overlay lines for a service-based cinematic reel.
    Each line is displayed at a different point in the video (start, middle, end).
    Returns a list of exactly 3 strings, each max 6 words.
    """
    brand_name = brand.get("brand_name") or ""
    system = (
        "You write short punchy text overlays for Instagram Reels promoting a service. "
        "Rules:\n"
        "1. Return EXACTLY 3 lines, one per line, nothing else.\n"
        "2. Each line is maximum 6 words — short and impactful.\n"
        "3. Line 1: a bold hook or problem statement.\n"
        "4. Line 2: the key benefit or transformation.\n"
        "5. Line 3: a short call to action.\n"
        "6. No hashtags, no punctuation beyond exclamation marks, no quotes.\n"
        "7. Output only the 3 lines."
    )
    user = f"Service: {description}\nBrand: {brand_name}"
    raw = _chat(system, user, temperature=0.75, max_tokens=80).strip()

    lines = [l.strip().strip('"').strip("'") for l in raw.splitlines() if l.strip()]
    # Ensure exactly 3 lines
    if len(lines) >= 3:
        return lines[:3]
    # Pad if model returned fewer
    fallbacks = ["Experience the difference", "Your solution is here", "Get started today"]
    while len(lines) < 3:
        lines.append(fallbacks[len(lines)])
    return lines
