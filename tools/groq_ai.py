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
    style_ctx     = brand.get("_style_context", "")  # injected from user's style skill
    location_hint = ""  # will be extracted from description if present

    brand_context = ""
    if brand_name:
        brand_context += f"Brand: {brand_name}. "
    if brand_colors:
        brand_context += f"Brand colors: {brand_colors}. "
    if brand_voice:
        brand_context += f"Visual tone: {brand_voice}. "
    if style_ctx:
        brand_context += f"\n\n{style_ctx}\n"

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

    style_ctx = brand.get("_style_context", "")
    brand_ctx = (
        f"Brand: {brand.get('brand_name') or 'the brand'}\n"
        f"Tone/voice: {brand.get('brand_voice') or 'professional'}\n"
        f"Brand colors: {brand.get('brand_colors') or 'derive elegant colors from context'}\n"
        f"Brand description: {brand.get('brand_description') or ''}\n"
    )
    if style_ctx:
        brand_ctx += f"\n{style_ctx}\n"

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


def art_director_analyze(image_url: str, description: str, brand: dict) -> dict:
    """
    Two-step art director pipeline (Joseph Kosinski / commercial advertising style).

    Step 1 — Vision analysis: deeply understand the product/service image.
    Step 2 — Art direction: decide whether to reimagine the environment OR enhance in place,
              then write the SeedDream img2img prompt accordingly.

    Returns:
        {
          "analysis":    str   — what the vision model sees,
          "strategy":    "reimagine" | "enhance",
          "reasoning":   str   — why this strategy,
          "prompt":      str   — final SeedDream img2img prompt,
        }
    """
    import json as _json
    import re as _re

    brand_name   = brand.get("brand_name") or "the brand"
    brand_colors = brand.get("brand_colors") or "rich neutrals with depth"
    brand_voice  = brand.get("brand_voice") or "premium and cinematic"
    social_goal  = brand.get("social_goal") or "create desire"

    # ── Step 1: Vision analysis ──────────────────────────────────────────────
    try:
        vision_resp = _client().chat.completions.create(
            model=config.GROQ_VISION_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": (
                        "You are a senior art director analyzing a product/service image for a commercial shoot. "
                        "Analyze every detail:\n"
                        "1. What is the product/service? Describe it precisely (shape, color, material, texture, size).\n"
                        "2. What is the current environment/setting? (studio, lifestyle, outdoor, indoor, plain white, etc.)\n"
                        "3. What is the lighting quality? (hard/soft, direction, color temperature, shadows)\n"
                        "4. What is the overall aesthetic quality? (professional, amateur, flat, dynamic)\n"
                        "5. Is the product placement contextually meaningful (e.g. food in a kitchen, gym gear at a gym)? "
                        "   Or is it context-neutral (e.g. a bottle on white)?\n"
                        "6. What is missing that would make this image more commercially powerful?\n"
                        "Be specific and technical. 6-8 sentences."
                    )},
                ],
            }],
            temperature=0.6,
            max_completion_tokens=512,
            top_p=0.95,
            reasoning_effort="default",
            stop=None,
        )
        analysis = (vision_resp.choices[0].message.content or "").strip()
    except Exception:
        analysis = f"A product image for {brand_name}. The product is centered with standard lighting."

    # ── Step 2: Art direction decision + prompt generation ──────────────────
    system = (
        "You are the most sought-after advertising art director in the world — "
        "with the cinematic eye of Joseph Kosinski (Top Gun: Maverick), the commercial "
        "craft of Annie Leibovitz, and the product storytelling of the best Super Bowl ad directors.\n\n"

        "Your job: turn an ordinary product photo into a jaw-dropping premium advertisement image.\n\n"

        "═══ DECISION 1 — STRATEGY ═══\n"
        "Choose ONE:\n"
        "  'reimagine' — product is on plain/neutral background or the current setting adds NO emotional value. "
        "  You will build a rich, aspirational world AROUND the product.\n"
        "  'enhance' — product is already in a meaningful context that would be wrong to remove "
        "  (e.g. burger in a restaurant, car on a mountain road). Upgrade everything else: "
        "  lighting, atmosphere, color grade, cinematic depth — but keep the location.\n\n"

        "═══ DECISION 2 — CAMERA & COMPOSITION ═══\n"
        "Pick the camera angle and lens that makes this product look its absolute best:\n"
        "  - Low angle (worm's eye): makes products feel powerful, grand, premium (great for bottles, cans, shoes)\n"
        "  - 45° hero angle: classic advertising sweet spot — shows top + front simultaneously\n"
        "  - Macro / extreme close-up: reveals texture, quality, craftsmanship (great for food, skincare, jewelry)\n"
        "  - Overhead / flat lay: works for food, cosmetics, lifestyle products\n"
        "  - Dutch tilt (slight rotation): adds energy and dynamism\n"
        "  - Eye level straight: honest, confident, premium for beverages and tech\n"
        "Choose what best amplifies the product's premium appeal.\n\n"

        "═══ DECISION 3 — PRODUCT CATEGORY VISUAL LANGUAGE ═══\n"
        "Every product category has a visual language that signals PREMIUM. Use it:\n\n"
        "  BEVERAGES (drinks, juice, soda, alcohol, coffee):\n"
        "  → Scatter the hero ingredients around the product (lemons bouncing/floating for lemonade, "
        "  coffee beans mid-air for espresso, hops for beer, herbs for gin).\n"
        "  → Add condensation droplets on glass, splash of liquid frozen mid-motion, "
        "  crushed ice scattering, citrus cross-sections, steam wisps.\n"
        "  → Backlit rim light to make liquid glow. Dark moody bar atmosphere OR "
        "  sun-drenched poolside. Never plain white.\n\n"
        "  FOOD:\n"
        "  → Steam rising, sauce dripping mid-shot, fresh ingredients artfully scattered.\n"
        "  → Dark dramatic background (chiaroscuro) OR bright airy editorial.\n"
        "  → Hero ingredient exploding outward from the dish.\n\n"
        "  SKINCARE / BEAUTY / COSMETICS:\n"
        "  → Marble or brushed metal surface, soft botanical elements (petals, herbs).\n"
        "  → Soft wrap-around diffused light, silk fabric draped nearby, dew droplets.\n"
        "  → Pearlescent reflections, clean negative space.\n\n"
        "  FASHION / ACCESSORIES / JEWELRY:\n"
        "  → Editorial: dramatic shadow patterns, fabric texture close-up.\n"
        "  → High contrast black + gold or white + silver color palette.\n"
        "  → Shot on velvet, stone, or glass surface with long shadows.\n\n"
        "  TECH / ELECTRONICS:\n"
        "  → Dark background with light trails, halo glow, lens flare.\n"
        "  → Floating in mid-air, surrounded by particles of light.\n"
        "  → Reflective glass surface below product.\n\n"
        "  REAL ESTATE / SERVICES:\n"
        "  → Place the concept in an aspirational lifestyle scene — golden hour, luxury interior.\n\n"
        "  HEALTH / FITNESS / SUPPLEMENTS:\n"
        "  → Dynamic energy: water splashes, motion blur, gym environment, sweat-on-skin close-up.\n\n"
        "  For ANY other product: think 'what environment makes someone CRAVE this?'\n\n"

        "═══ DECISION 4 — LIGHTING ═══\n"
        "Be specific about the lighting setup:\n"
        "  Rembrandt lighting | cinematic key + fill | golden hour rim light | "
        "  neon accent lights | dramatic top-down spotlight | soft beauty dish | "
        "  backlit translucent glow | multiple colored gel lights | moonlight + practical lamps\n\n"

        "═══ WRITE THE PROMPT ═══\n"
        "CRITICAL RULES:\n"
        "  1. NEVER describe or modify the product itself — it is LOCKED in the reference image.\n"
        "  2. Describe EVERYTHING around it: environment, camera angle & lens, lighting, "
        "     scattered elements, motion, texture, color grade, atmosphere.\n"
        "  3. Be cinematically specific — not 'nice background' but "
        "'sun-drenched Amalfi terrace with terracotta tiles, golden 5pm light casting long shadows'.\n"
        "  4. Include motion/energy elements where appropriate (floating ingredients, splashes, bokeh particles).\n"
        "  5. Always end with: photorealistic, ultra-sharp product focus, "
        "commercial advertising photography, 8K, shot on Hasselblad H6D, Phase One medium format, "
        "Cannes Lions Grand Prix quality.\n\n"

        "Return ONLY valid JSON: {strategy, reasoning (1 sentence), camera_choice (1 phrase), prompt}"
    )
    user = (
        f"Brand: {brand_name} | Visual tone: {brand_voice} | Brand colors: {brand_colors} | Goal: {social_goal}\n"
        f"Content idea: {description}\n\n"
        f"Product analysis:\n{analysis}\n\n"
        "Decide strategy and write the img2img prompt. Return JSON only."
    )
    raw = _chat(system, user, temperature=1.0, max_tokens=1024)
    try:
        clean = _re.sub(r"```[a-z]*\n?", "", raw).strip().strip("`")
        data = _json.loads(clean)
        return {
            "analysis":      analysis,
            "strategy":      data.get("strategy", "reimagine"),
            "reasoning":     data.get("reasoning", ""),
            "camera_choice": data.get("camera_choice", ""),
            "prompt":        data.get("prompt", ""),
        }
    except Exception:
        return {
            "analysis":      analysis,
            "strategy":      "reimagine",
            "reasoning":     "parsed from raw output",
            "camera_choice": "",
            "prompt":        raw[:800],
        }


def generate_product_poster_prompt(description: str, brand: dict) -> str:
    """Legacy wrapper — kept for compatibility. Prefer art_director_analyze()."""
    brand_name   = brand.get("brand_name") or "the brand"
    brand_colors = brand.get("brand_colors") or "neutral professional tones"
    brand_voice  = brand.get("brand_voice") or "premium and clean"
    system = (
        "You are an expert commercial photography art director. "
        "Describe ONLY the environment and lighting around the product (product is fixed). "
        "One paragraph, no headers. End with: photorealistic, 8K, commercial photography quality."
    )
    user = (
        f"Brand: {brand_name} | Colors: {brand_colors} | Voice: {brand_voice}\n"
        f"Content idea: {description}\n"
        "Write the environment/lighting prompt only."
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


def extract_full_intent(
    text_body: str,
    audio_transcript: str | None,
    has_image: bool,
    has_video: bool,
    session_context: dict,
) -> dict:
    """
    One-shot intent extraction from any combination of text, audio transcript,
    and media signals. Returns a structured intent dict that the harness uses
    to route to a sub-agent with minimal follow-up questions.

    session_context keys: brand_name, brand_description, brand_voice,
                          has_style_skill, recent_content_types, social_goal

    Returns:
    {
      "content_type":        "image_post" | "carousel" | "reel" | "unknown",
      "confidence":          0.0-1.0,
      "description":         str — clean subject/topic (no request framing),
      "count":               int — number of images/slides (default 1),
      "reel_type":           "cinematic" | "ugc" | "ad" | null,
      "use_reference_image": bool — user wants attached image as product base,
      "use_style_skill":     bool — user wants to replicate their stored style,
      "publish_action":      "now" | "schedule" | null,
      "scheduled_at":        str | null,
      "style_notes":         str — any aesthetic requests ("dark", "minimal", "vibrant"),
      "missing_fields":      list[str] — critical fields still needed,
      "smart_question":      str — ONE conversational question covering all missing fields,
      "ready_to_generate":   bool — true if enough info to start generation now,
    }
    """
    import json as _json
    import re as _re

    combined_text = ""
    if text_body and text_body.strip():
        combined_text += f"Text message: {text_body.strip()}\n"
    if audio_transcript and audio_transcript.strip():
        combined_text += f"Voice message (transcribed): {audio_transcript.strip()}\n"
    if not combined_text.strip():
        combined_text = "(no text — user sent media only)"

    media_signals = []
    if has_image:
        media_signals.append("image attached")
    if has_video:
        media_signals.append("video attached")

    brand_ctx = (
        f"Brand: {session_context.get('brand_name') or 'unknown'}\n"
        f"Business: {session_context.get('brand_description') or ''}\n"
        f"Brand voice: {session_context.get('brand_voice') or ''}\n"
        f"Goal: {session_context.get('social_goal') or ''}\n"
        f"User has stored style skill: {session_context.get('has_style_skill', False)}\n"
        f"Recent content types created: {session_context.get('recent_content_types') or 'none yet'}\n"
    )

    system = (
        "You are the intelligent routing brain of BeeQ, a WhatsApp social media agent. "
        "A verified business user has sent a message. Extract their FULL intent in one shot.\n\n"

        "═══ INFERENCE RULES ═══\n"
        "content_type:\n"
        "  - Image attached + no explicit type → 'image_post'\n"
        "  - 'carousel' / 'slides' / 'tips' / 'steps' / 'list' → 'carousel'\n"
        "  - 'reel' / 'video' / 'clip' / 'short' → 'reel'\n"
        "  - 'post' / 'image' / 'photo' alone → 'image_post'\n"
        "  - Unclear → 'unknown'\n\n"

        "reel_type:\n"
        "  - 'cinematic' / 'product video' / 'product reel' → 'cinematic'\n"
        "  - 'ugc' / 'talking head' / 'my face' / 'selfie video' → 'ugc'\n"
        "  - 'ad' / 'advertisement' → 'ad'\n"
        "  - Not specified for reel → null (will ask)\n\n"

        "use_reference_image:\n"
        "  - Image is attached AND user wants a post made FROM it → true\n"
        "  - Image is for style reference only → false (set use_style_skill=true)\n"
        "  - No image → false\n\n"

        "use_style_skill:\n"
        "  - 'like my previous posts' / 'same style' / 'based on my usual' / "
        "    'how I usually post' / 'match my style' → true\n"
        "  - User has stored style skill AND no explicit different style requested → true by default\n\n"

        "description: REMOVE all request framing. Keep only the subject.\n"
        "  - 'Can you make a cool post about my new coffee blend?' → 'new coffee blend launch'\n"
        "  - 'Create a carousel on 5 skincare tips' → '5 skincare tips'\n\n"

        "missing_fields: ONLY list fields that are TRULY needed and NOT inferable:\n"
        "  - For image_post: need 'description' if not clear. 'publish_action' is optional.\n"
        "  - For carousel: need 'description'. 'count' defaults to 3 if not given.\n"
        "  - For reel: need 'reel_type' if not clear. 'description' required.\n"
        "  - NEVER ask for things already known from brand context or session.\n\n"

        "ready_to_generate:\n"
        "  - true if content_type is known AND description is clear\n"
        "  - For reel: also need reel_type\n"
        "  - publish_action being missing does NOT block generation\n\n"

        "smart_question: ONE short, warm, conversational question covering ALL missing fields.\n"
        "  - Do NOT list multiple questions. Merge them into one natural sentence.\n"
        "  - Sound like a creative collaborator, not a form.\n\n"

        "Output ONLY valid JSON. No markdown. No extra keys."
    )

    user = (
        f"{brand_ctx}\n"
        f"Media signals: {', '.join(media_signals) if media_signals else 'none'}\n\n"
        f"User message:\n{combined_text}\n\n"
        "Extract full intent and return JSON."
    )

    schema = (
        '{"content_type":"image_post","confidence":0.9,"description":"...","count":1,'
        '"reel_type":null,"use_reference_image":true,"use_style_skill":true,'
        '"publish_action":null,"scheduled_at":null,"style_notes":"...","missing_fields":[],'
        '"smart_question":"...","ready_to_generate":true}'
    )

    raw = _chat(system + f"\n\nSchema: {schema}", user, temperature=0.15, max_tokens=512)
    try:
        clean = _re.sub(r"```[a-z]*\n?", "", raw).strip().strip("`")
        data = _json.loads(clean)
        # Ensure required keys with safe defaults
        return {
            "content_type":        str(data.get("content_type") or "unknown"),
            "confidence":          float(data.get("confidence") or 0.5),
            "description":         str(data.get("description") or "").strip(),
            "count":               int(data.get("count") or 1),
            "reel_type":           data.get("reel_type"),
            "use_reference_image": bool(data.get("use_reference_image", has_image)),
            "use_style_skill":     bool(data.get("use_style_skill", True)),
            "publish_action":      data.get("publish_action"),
            "scheduled_at":        data.get("scheduled_at"),
            "style_notes":         str(data.get("style_notes") or ""),
            "missing_fields":      list(data.get("missing_fields") or []),
            "smart_question":      str(data.get("smart_question") or "What would you like to create?"),
            "ready_to_generate":   bool(data.get("ready_to_generate", False)),
        }
    except Exception:
        # Safe fallback
        ct = "image_post" if has_image else "unknown"
        desc = (audio_transcript or text_body or "").strip()
        missing = [] if (ct != "unknown" and desc) else ["description"] if ct != "unknown" else ["content_type"]
        return {
            "content_type": ct,
            "confidence": 0.4,
            "description": desc,
            "count": 1,
            "reel_type": None,
            "use_reference_image": has_image,
            "use_style_skill": session_context.get("has_style_skill", False),
            "publish_action": None,
            "scheduled_at": None,
            "style_notes": "",
            "missing_fields": missing,
            "smart_question": "What would you like to create today? 🐝",
            "ready_to_generate": ct != "unknown" and bool(desc),
        }


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


def analyze_post_style(image_url: str) -> dict:
    """
    Two-step style fingerprinting pipeline.

    Step 1 — Vision: Qwen deeply analyzes a reference social media post/graphic for
    every visual and copy design decision it can observe.

    Step 2 — LLM: GPT-OSS distills the observation into a structured style skill JSON
    that can be injected verbatim into future image + caption prompts to replicate the look.

    Returns a dict with two top-level keys:
      "skill"  — the structured style JSON (stored in MongoDB, injected into prompts)
      "summary" — 1-2 sentence human-readable description of the style
    """
    import json as _json
    import re as _re

    # ── Step 1: Vision analysis — observe every design detail ───────────────
    try:
        vision_resp = _client().chat.completions.create(
            model=config.GROQ_VISION_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": (
                        "You are a senior graphic designer and social media art director. "
                        "Analyze every visual and copy design decision in this post/image with extreme precision.\n\n"
                        "Examine and describe:\n"
                        "1. TEXT PLACEMENT — where is text positioned? (top-center, bottom-left, overlay-center, etc.) "
                        "Is there a headline? Where exactly? Body text? Caption area?\n"
                        "2. TYPOGRAPHY — font weight (bold/regular/light), apparent font style "
                        "(serif/sans-serif/script), text size hierarchy (large headline vs small body), "
                        "letter spacing (tight/normal/wide), text color and any outline or shadow effects.\n"
                        "3. PROFILE BADGE / BRANDING ELEMENT — is there a profile avatar, logo watermark, "
                        "username handle, or brand badge visible? If yes: exact position, size (small/medium/large), "
                        "shape (circular/rectangular), border/ring color if any.\n"
                        "4. COLOR PALETTE — list the 2-4 dominant colors (describe them precisely: "
                        "'deep navy #1a2b4c', 'warm cream', 'neon yellow'). What is the background color/treatment? "
                        "What are the text colors? Any gradient?\n"
                        "5. COMPOSITION & LAYOUT — where is the main subject/product placed? "
                        "(center, left-third, full-bleed, top-half) How much negative/white space? "
                        "Is there an overlay (dark gradient, frosted glass, color wash, none)?\n"
                        "6. BACKGROUND STYLE — solid color, gradient, textured, photographic, environmental, "
                        "abstract, pattern? Describe it.\n"
                        "7. VISUAL MOOD & AESTHETIC — what overall aesthetic does this communicate? "
                        "(luxury minimalist, bold vibrant, editorial clean, warm lifestyle, dark & moody, etc.)\n"
                        "8. CAPTION/COPY STYLE — if visible: tone (professional/casual/excited), "
                        "emoji usage (none/light/heavy), hashtag placement (inline/end-grouped/none), "
                        "CTA style (direct/soft/question).\n\n"
                        "Be extremely precise. Use exact position descriptions. "
                        "This analysis will be used to replicate this exact style for future posts."
                    )},
                ],
            }],
            temperature=0.4,
            max_completion_tokens=900,
            top_p=0.95,
            reasoning_effort="default",
            stop=None,
        )
        vision_analysis = (vision_resp.choices[0].message.content or "").strip()
    except Exception as e:
        vision_analysis = "A social media post with standard layout, sans-serif text, centered subject, and clean background."

    # ── Step 2: Distill into a reusable style skill JSON ────────────────────
    system = (
        "You are a design systems expert. Your job is to convert a visual analysis "
        "of a social media post into a precise, structured style skill JSON "
        "that a graphic designer could follow to exactly replicate the style.\n\n"
        "Output ONLY valid JSON matching this schema exactly (no extra keys, no markdown):\n"
        "{\n"
        '  "layout": {\n'
        '    "text_placement": "string — e.g. bottom-center, top-left, overlay-center",\n'
        '    "headline_zone": "string — where the main headline sits, e.g. upper-third centered",\n'
        '    "body_zone": "string — where supporting text sits, or null if none",\n'
        '    "subject_position": "string — where the main visual subject sits",\n'
        '    "negative_space": "minimal | moderate | generous",\n'
        '    "overlay": "none | dark-gradient | frosted-glass | color-wash | vignette"\n'
        "  },\n"
        '  "typography": {\n'
        '    "font_style": "sans-serif | serif | script | bold-display | mixed",\n'
        '    "headline_weight": "light | regular | semibold | bold | black/extra-bold",\n'
        '    "headline_size": "small | medium | large | very-large",\n'
        '    "letter_spacing": "tight | normal | wide | very-wide",\n'
        '    "text_color_primary": "string — hex or description",\n'
        '    "text_color_secondary": "string — hex or description, or null",\n'
        '    "text_effects": "none | drop-shadow | outline | glow | uppercase-all | mixed-case"\n'
        "  },\n"
        '  "badge": {\n'
        '    "present": true | false,\n'
        '    "type": "profile-avatar | logo-watermark | username-handle | brand-stamp | null",\n'
        '    "position": "string — e.g. bottom-right, top-left, or null",\n'
        '    "size": "small | medium | large | null",\n'
        '    "shape": "circular | rounded-rect | rectangular | null",\n'
        '    "border_color": "string — color or null"\n'
        "  },\n"
        '  "colors": {\n'
        '    "palette": ["string", "string"],\n'
        '    "background": "string — color or treatment description",\n'
        '    "background_type": "solid | gradient | photographic | textured | abstract",\n'
        '    "accent": "string — color used for highlights, buttons, or emphasis",\n'
        '    "mood": "string — warm / cool / neutral / high-contrast / pastel / dark"\n'
        "  },\n"
        '  "composition": {\n'
        '    "style": "string — e.g. luxury-minimalist, bold-editorial, warm-lifestyle, dark-moody",\n'
        '    "framing": "centered | rule-of-thirds-left | rule-of-thirds-right | full-bleed | flat-lay",\n'
        '    "depth": "flat-2d | shallow-dof | deep-focus"\n'
        "  },\n"
        '  "caption_style": {\n'
        '    "tone": "professional | casual | excited | inspirational | minimal | humorous",\n'
        '    "emoji_density": "none | light (1-2) | moderate (3-5) | heavy (6+)",\n'
        '    "hashtag_placement": "inline | end-grouped | none",\n'
        '    "hashtag_count": "none | few (1-3) | moderate (4-7) | many (8+)",\n'
        '    "cta_style": "direct-command | soft-invite | question | none"\n'
        "  },\n"
        '  "summary": "string — 1-2 sentence description of the overall style for a human designer"\n'
        "}"
    )

    raw = _chat(system, f"Visual analysis:\n{vision_analysis}", temperature=0.2, max_tokens=900)
    try:
        clean = _re.sub(r"```[a-z]*\n?", "", raw).strip().strip("`").strip()
        data = _json.loads(clean)
        summary = data.pop("summary", "A clean, well-composed social media post style.")
        return {"skill": data, "summary": summary}
    except Exception:
        # Return a minimal safe default
        return {
            "skill": {
                "layout": {"text_placement": "bottom-center", "headline_zone": "upper-third centered",
                           "body_zone": None, "subject_position": "center", "negative_space": "moderate",
                           "overlay": "none"},
                "typography": {"font_style": "sans-serif", "headline_weight": "bold",
                               "headline_size": "large", "letter_spacing": "normal",
                               "text_color_primary": "#ffffff", "text_color_secondary": None,
                               "text_effects": "none"},
                "badge": {"present": False, "type": None, "position": None, "size": None,
                          "shape": None, "border_color": None},
                "colors": {"palette": ["#1a1a1a", "#ffffff"], "background": "dark solid",
                           "background_type": "solid", "accent": "#f59e0b", "mood": "neutral"},
                "composition": {"style": "clean editorial", "framing": "centered", "depth": "flat-2d"},
                "caption_style": {"tone": "professional", "emoji_density": "light (1-2)",
                                  "hashtag_placement": "end-grouped", "hashtag_count": "few (1-3)",
                                  "cta_style": "soft-invite"},
            },
            "summary": "Clean, professional social media post with centered layout.",
        }


def style_skill_to_prompt_context(skill: dict) -> str:
    """
    Convert a stored style skill dict into a concise natural-language block
    that can be prepended to any image prompt or caption system prompt.
    """
    if not skill:
        return ""

    layout = skill.get("layout", {})
    typo = skill.get("typography", {})
    badge = skill.get("badge", {})
    colors = skill.get("colors", {})
    comp = skill.get("composition", {})
    caption = skill.get("caption_style", {})

    lines = ["═══ USER STYLE SKILL (replicate this exactly) ═══"]

    # Layout
    tp = layout.get("text_placement", "")
    if tp:
        lines.append(f"• Text placement: {tp}")
    hl = layout.get("headline_zone", "")
    if hl:
        lines.append(f"• Headline zone: {hl}")
    sp = layout.get("subject_position", "")
    if sp:
        lines.append(f"• Subject position: {sp}")
    ov = layout.get("overlay", "none")
    if ov and ov != "none":
        lines.append(f"• Overlay: {ov}")
    ns = layout.get("negative_space", "")
    if ns:
        lines.append(f"• Negative space: {ns}")

    # Typography
    fw = typo.get("headline_weight", "")
    fs = typo.get("font_style", "")
    hs = typo.get("headline_size", "")
    te = typo.get("text_effects", "none")
    ls = typo.get("letter_spacing", "")
    tc = typo.get("text_color_primary", "")
    typo_parts = [x for x in [f"{fw} {fs}".strip(), f"{hs} size" if hs else "", f"{ls} letter-spacing" if ls else ""] if x.strip()]
    if typo_parts:
        lines.append(f"• Typography: {', '.join(typo_parts)}")
    if tc:
        lines.append(f"• Text color: {tc}")
    if te and te != "none":
        lines.append(f"• Text effects: {te}")

    # Badge
    if badge.get("present"):
        btype = badge.get("type", "badge")
        bpos = badge.get("position", "")
        bsize = badge.get("size", "")
        bshape = badge.get("shape", "")
        bborder = badge.get("border_color", "")
        badge_desc = f"• Profile badge: {btype}"
        details = [x for x in [bpos, bsize, bshape, f"border {bborder}" if bborder else ""] if x]
        if details:
            badge_desc += f" — {', '.join(details)}"
        lines.append(badge_desc)
    else:
        lines.append("• No profile badge/watermark")

    # Colors
    palette = colors.get("palette", [])
    bg = colors.get("background", "")
    bgt = colors.get("background_type", "")
    accent = colors.get("accent", "")
    mood = colors.get("mood", "")
    if palette:
        lines.append(f"• Color palette: {', '.join(palette)}")
    if bg:
        lines.append(f"• Background: {bg} ({bgt})" if bgt else f"• Background: {bg}")
    if accent:
        lines.append(f"• Accent color: {accent}")
    if mood:
        lines.append(f"• Color mood: {mood}")

    # Composition
    cstyle = comp.get("style", "")
    framing = comp.get("framing", "")
    depth = comp.get("depth", "")
    comp_parts = [x for x in [cstyle, framing, depth] if x]
    if comp_parts:
        lines.append(f"• Composition: {', '.join(comp_parts)}")

    # Caption
    tone = caption.get("tone", "")
    emojis = caption.get("emoji_density", "")
    htag_place = caption.get("hashtag_placement", "")
    htag_count = caption.get("hashtag_count", "")
    cta = caption.get("cta_style", "")
    if tone:
        lines.append(f"• Caption tone: {tone}")
    if emojis:
        lines.append(f"• Emoji usage: {emojis}")
    if htag_place and htag_count:
        lines.append(f"• Hashtags: {htag_count}, {htag_place}")
    if cta:
        lines.append(f"• CTA style: {cta}")

    lines.append("═══════════════════════════════════════════════")
    return "\n".join(lines)


def generate_caption_with_style(
    description: str,
    content_type: str,
    website_url: str = "",
    style_skill: dict | None = None,
) -> str:
    """
    Generate a caption that mirrors the user's stored style skill if available,
    otherwise falls back to the standard caption generator.
    """
    if not style_skill:
        return generate_caption(description, content_type, website_url)

    style_context = style_skill_to_prompt_context(style_skill)
    cap_style = style_skill.get("caption_style", {})
    tone = cap_style.get("tone", "professional")
    emojis = cap_style.get("emoji_density", "light (1-2)")
    htag_place = cap_style.get("hashtag_placement", "end-grouped")
    htag_count = cap_style.get("hashtag_count", "few (1-3)")
    cta = cap_style.get("cta_style", "soft-invite")

    link_instruction = (
        f"\nInclude this link naturally near the end: {website_url}"
        if website_url else ""
    )

    system = (
        f"{style_context}\n\n"
        "You are a social media copywriter. Write a caption that EXACTLY matches the style skill above.\n"
        f"Tone: {tone}. Emoji usage: {emojis}. Hashtags: {htag_count}, placed {htag_place}. CTA style: {cta}."
        + link_instruction +
        "\nOutput ONLY the caption text, nothing else."
    )
    user = (
        f"Content type: {content_type}\n"
        f"Content description: {description}\n"
        "Write the caption matching the style skill exactly."
    )
    return _chat(system, user, temperature=0.75)


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


def generate_30_day_calendar(brand: dict, start_date_str: str) -> list[dict]:
    """
    Generate a 30-day content calendar for a brand.
    Returns a list of 30 dicts:
      { day: int, date: "YYYY-MM-DD", content_type: str, reel_type: str|None,
        topic: str, caption_idea: str, status: "pending" }
    """
    import json as _json
    import re as _re
    from datetime import datetime as _dt, timedelta as _td

    brand_name = brand.get("brand_name", "the brand")
    brand_desc = brand.get("brand_description", "")
    social_goal = brand.get("social_goal", "grow engagement")

    system = (
        "You are a social media strategist. Generate a 30-day content calendar. "
        "Rules:\n"
        "- Vary content types: image posts, carousels, reels (cinematic/ugc/ad)\n"
        "- Monthly targets: 10 image posts, 8 carousels, 12 reels\n"
        "- Reels: 70% cinematic, 20% ugc, 10% ad\n"
        "- Topics must be specific, actionable, relevant to the brand\n"
        "- caption_idea is a 1-sentence hook\n"
        "Return ONLY a JSON array of 30 objects with keys: "
        "day (1-30), content_type (image_post|carousel|reel), "
        "reel_type (cinematic|ugc|ad|null), topic (string), caption_idea (string).\n"
        "No markdown, no explanation, just the JSON array."
    )
    user = (
        f"Brand: {brand_name}\n"
        f"Description: {brand_desc}\n"
        f"Goal: {social_goal}\n"
        "Generate the 30-day calendar JSON array now."
    )

    raw = _chat(system, user, temperature=1.0, max_tokens=4096)

    # Extract JSON array
    try:
        match = _re.search(r'\[[\s\S]+\]', raw)
        data = _json.loads(match.group(0) if match else raw)
    except Exception:
        data = []

    # Build final list with dates + status
    start = _dt.strptime(start_date_str, "%Y-%m-%d")
    calendar = []
    for i in range(30):
        entry = data[i] if i < len(data) else {}
        calendar.append({
            "day": i + 1,
            "date": (start + _td(days=i)).strftime("%Y-%m-%d"),
            "content_type": entry.get("content_type", "image_post"),
            "reel_type": entry.get("reel_type"),
            "topic": entry.get("topic", f"Day {i+1} content"),
            "caption_idea": entry.get("caption_idea", ""),
            "status": "pending",
        })
    return calendar
