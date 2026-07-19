"""Groq Cloud — image prompt generation and social media caption writing."""

from __future__ import annotations

import logging

from groq import Groq

import config
from tools.tracing import traceable

_logger = logging.getLogger(__name__)


def _client() -> Groq:
    return Groq(api_key=config.GROQ_API_KEY)


def _chat_langchain(system: str, user: str, temperature: float, effective_max: int) -> str:
    """Run the text LLM call through LangChain's ChatGroq (native LangSmith LLM span)."""
    from langchain_groq import ChatGroq
    from langchain_core.messages import SystemMessage, HumanMessage
    llm = ChatGroq(
        model=config.GROQ_MODEL,
        api_key=config.GROQ_API_KEY,
        temperature=temperature,
        model_kwargs={
            "max_completion_tokens": effective_max,
            "top_p": 1,
            "reasoning_effort": "medium",
        },
    )
    resp = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
    return (resp.content or "").strip()


@traceable(run_type="llm", name="groq_chat")
def _chat(system: str, user: str, temperature: float = 1.0, max_tokens: int = 8192) -> str:
    # reasoning_effort="medium" spends part of max_completion_tokens on hidden reasoning.
    # If a caller passes a small budget (e.g. 80-400), the reasoning can consume it all and
    # the visible answer comes back EMPTY. Always leave generous headroom for the output.
    effective_max = max(max_tokens, 1500)

    # Optional LangChain path (opt-in) — falls back to the raw Groq SDK on any error so
    # behaviour never changes if LangChain misbehaves.
    if config.USE_LANGCHAIN:
        try:
            return _chat_langchain(system, user, temperature, effective_max)
        except Exception as exc:
            _logger.warning("_chat: LangChain path failed, falling back to Groq SDK: %s", exc)

    resp = _client().chat.completions.create(
        model=config.GROQ_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        temperature=temperature,
        max_completion_tokens=effective_max,
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


@traceable(run_type="chain", name="art_director_analyze")
def art_director_analyze(image_url: str, description: str, brand: dict,
                         preserve_subject: bool = False) -> dict:
    """
    Two-step art director pipeline (Joseph Kosinski / commercial advertising style).

    Step 1 — Vision analysis: deeply understand the product/service image.
    Step 2 — Art direction: decide whether to reimagine the environment OR enhance in place,
              then write the SeedDream img2img prompt accordingly.

    preserve_subject=True  → STRICT mode for real photos of the actual subject
        (real estate, a specific property/product/service the user is selling).
        Forces 'enhance' — the subject and scene structure are LOCKED. Only camera
        angle, lens, lighting, time of day, color grade and minor staging may change.
        The output must be recognisably the SAME building/object, never a new one.

    Returns:
        { "analysis", "strategy", "reasoning", "camera_choice", "prompt" }
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

    # ── Step 2 (STRICT): preserve the actual subject, only restage ──────────
    if preserve_subject:
        strict_system = (
            "You are an elite real-estate & product photographer re-shooting an EXISTING, "
            "REAL subject. The reference image is an actual photograph of the specific "
            "property/product/service the client is selling. Your ONLY job is to make it look "
            "like a more professional photo of the SAME EXACT thing.\n\n"

            "═══ ABSOLUTE GUARDRAILS — NON-NEGOTIABLE ═══\n"
            "The output MUST be recognisably the SAME subject. You must PRESERVE, unchanged:\n"
            "  • For property/real estate: the exact architecture, building shape, roofline, "
            "    number and placement of windows/doors, exterior materials & colour, the room "
            "    layout, walls, flooring, built-in fixtures, cabinetry and overall structure.\n"
            "  • For a product: exact shape, colour, label, logo, materials, proportions.\n"
            "  • For a service/scene: the real people, objects and setting that define it.\n"
            "  NEVER invent a different house, room, product or scene. NEVER add or remove "
            "  rooms, floors, windows or structural features. NEVER restyle the architecture.\n\n"

            "═══ WHAT YOU MAY CHANGE (and should) ═══\n"
            "  • Camera angle, lens and framing (a better hero angle of the same subject)\n"
            "  • Lighting quality, direction and time of day (e.g. warm golden-hour light)\n"
            "  • Colour grade, contrast, clarity and overall photographic polish\n"
            "  • Sky/weather for exteriors; tasteful, realistic staging of EXISTING spaces\n"
            "  • Remove clutter, correct exposure, sharpen — like a pro real-estate edit\n\n"

            "Choose the camera angle/lens and lighting that best flatters the SAME subject.\n\n"

            "═══ WRITE THE PROMPT ═══\n"
            "Write a SeedDream img2img prompt that explicitly instructs the model to keep the "
            "subject from the reference image IDENTICAL in structure and identity, and to only "
            "adjust camera angle, lighting, time of day and photographic quality. "
            "Begin the prompt with: 'Same exact property/product as the reference, unchanged "
            "structure and identity — re-photographed with '. "
            "End with: photorealistic, true-to-source, professional real-estate/product "
            "photography, natural perspective, ultra-sharp, 8K.\n\n"
            "strategy MUST be 'enhance'. Return ONLY valid JSON: "
            "{strategy, reasoning (1 sentence), camera_choice (1 phrase), prompt}"
        )
        strict_user = (
            f"Brand: {brand_name} | Visual tone: {brand_voice} | Goal: {social_goal}\n"
            f"Content idea: {description}\n\n"
            f"Subject analysis (this is the REAL subject to preserve):\n{analysis}\n\n"
            "Write the img2img prompt that keeps this exact subject and only restages the shot. "
            "Return JSON only."
        )
        raw = _chat(strict_system, strict_user, temperature=0.5, max_tokens=900)
        try:
            clean = _re.sub(r"```[a-z]*\n?", "", raw).strip().strip("`")
            data = _json.loads(clean)
            return {
                "analysis":      analysis,
                "strategy":      "enhance",  # forced
                "reasoning":     data.get("reasoning", "preserve real subject"),
                "camera_choice": data.get("camera_choice", ""),
                "prompt":        data.get("prompt", ""),
            }
        except Exception:
            return {
                "analysis":      analysis,
                "strategy":      "enhance",
                "reasoning":     "preserve real subject (raw)",
                "camera_choice": "",
                "prompt":        ("Same exact property/product as the reference, unchanged "
                                  "structure and identity — re-photographed with professional "
                                  "lighting and a flattering camera angle, golden-hour warmth, "
                                  "photorealistic, true-to-source, ultra-sharp, 8K."),
            }

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
    Generate a punchy, hook-driven carousel script. Each slide carries ONE short
    scroll-stopping line; the lines flow as a single narrative across the whole
    carousel. Minimal text per slide — designed to sit cleanly over a photo.

    Returns:
        {
          "hook":   "Cover line — max 7 words",
          "slides": [
            { "headline": "Short punchy line (max 7 words)",
              "subtext":  "one tiny supporting line (max 9 words) or empty" },
            ...
          ],
          "cta": "Final CTA line"
        }
    """
    import json as _json
    import re as _re

    system = (
        "You are a viral Instagram carousel writer. You write CLEAN, MINIMAL, hook-driven "
        "carousels where each slide shows ONE short punchy line that makes people swipe.\n\n"
        "HARD RULES:\n"
        "- Every line is SHORT: the headline is max 7 words. No paragraphs, no filler.\n"
        "- The slides tell ONE flowing story: slide 1 sets up curiosity, each next slide "
        "continues the thought, the last slide pays it off with a call to action.\n"
        "- Think hooks, not essays: 'You walked right past it.' → 'Most buyers do.' → "
        "'But this one's different.' Each line builds on the last.\n"
        "- subtext is OPTIONAL and tiny (max 9 words). Use it only when it adds punch; "
        "otherwise leave it an empty string.\n"
        "- If real facts about the subject are provided, weave the most attractive ones in "
        "(price, location, a standout feature) — but keep every line short and seductive.\n"
        "- No hashtags, no emojis inside slide text, no quotation marks.\n"
        "- Output ONLY valid JSON. No markdown, no extra keys."
    )

    brand_ctx = (
        f"Brand: {brand.get('brand_name') or 'the brand'}\n"
        f"Industry/niche: {brand.get('brand_description') or ''}\n"
        f"Tone: {brand.get('brand_voice') or 'bold and inviting'}\n"
    )

    user = (
        f"{brand_ctx}\n"
        f"Carousel topic / facts:\n{topic}\n\n"
        f"Number of slides (not counting the cover hook): {slide_count}\n\n"
        "Write a flowing, hook-driven carousel. Return JSON exactly:\n"
        '{"hook": "short cover line", '
        '"slides": [{"headline": "short punchy line", "subtext": ""}, ...], '
        '"cta": "final call to action line"}'
    )

    raw = _chat(system, user, temperature=0.7, max_tokens=900)
    try:
        clean = _re.sub(r"```[a-z]*", "", raw).strip().strip("`")
        data = _json.loads(clean)
        if "hook" not in data or "slides" not in data:
            raise ValueError("Missing keys")
        cta = data.get("cta", "")
        # Normalise slides → keep only short headline + subtext; attach swipe/cta hints
        norm_slides = []
        slides = data.get("slides", [])[:slide_count]
        for i, s in enumerate(slides):
            is_last = (i == len(slides) - 1)
            norm_slides.append({
                "headline": str(s.get("headline") or "").strip(),
                "body":     str(s.get("subtext") or s.get("body") or "").strip(),
                "swipe":    None if is_last else "SWIPE →",
                "cta":      (cta or "Tap the link in bio") if is_last else None,
            })
        return {"hook": str(data.get("hook") or "").strip(), "slides": norm_slides}
    except Exception:
        return {
            "hook": f"{topic[:48]}",
            "slides": [
                {
                    "headline": f"Reason {i+1} to look closer.",
                    "body": "",
                    "swipe": "SWIPE →" if i < slide_count - 1 else None,
                    "cta": "Tap the link in bio" if i == slide_count - 1 else None,
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


@traceable(run_type="chain", name="extract_full_intent")
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

        "reel_type: (check in THIS order — FIRST match wins)\n"
        "  0. 'video_editor' — the user wants to EDIT / transform a video THEY WILL SEND / already sent "
        "(their own footage). Signals: 'edit my video', 'edit this video', 'turn my video into a reel', "
        "'make a trending cut of my video', 'add b-roll to my video', 'a video is attached to edit'. "
        "If the message references editing the user's OWN existing video, choose 'video_editor'.\n"
        "  1. 'ugc_presentation' — the user appears/talks on camera AND it is ABOUT a specific "
        "product / property / service / listing (or a link is or will be involved). This is the "
        "DEFAULT for 'me talking about my <product/property>'. Signals: 'reel of me talking about my "
        "property', 'video of me presenting my listing', 'with my property in the background', "
        "'product behind me', 'presentation video', 'presenter video', 'me showing the property'.\n"
        "  2. 'ugc' — ONLY when the user explicitly wants just a talking head with NOTHING shown "
        "(no product/property/photos): e.g. 'just me talking to camera', 'plain talking head', "
        "'a vlog about my day'. If a product/property/service is the subject, prefer ugc_presentation.\n"
        "  3. 'cinematic' — 'cinematic' / 'product video' / 'product reel' / 'showcase' / 'b-roll'.\n"
        "  4. 'ad' — 'ad' / 'advertisement' / 'commercial' / 'promo'.\n"
        "  - Only null if genuinely unclear.\n\n"

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
        # Normalise content_type — LLM sometimes returns "post", "image", "video" etc.
        _ct_raw = str(data.get("content_type") or "unknown").lower().strip()
        _ct_map = {
            "image_post": "image_post", "image post": "image_post",
            "image": "image_post", "post": "image_post", "photo": "image_post",
            "single": "image_post", "single post": "image_post",
            "carousel": "carousel", "slides": "carousel", "swipe": "carousel",
            "reel": "reel", "video": "reel", "reels": "reel",
        }
        _ct = _ct_map.get(_ct_raw, "unknown")
        # Ensure required keys with safe defaults
        return {
            "content_type":        _ct,
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


@traceable(run_type="chain", name="classify_message_routing")
def classify_message_routing(
    user_message: str,
    step_purpose: str,
    has_media: bool = False,
) -> str:
    """
    State-aware top-level router. Given what the bot is CURRENTLY waiting for
    (step_purpose) and the user's new message, decide how to handle it:

      "continue" — the message answers / responds to the current question or step
      "new"      — the user is starting a DIFFERENT content request, ignoring the
                   current step (e.g. mid-reel they say "actually make a post about X")
      "command"  — a global command: reset/cancel/start over/menu/help

    Fast-pathed for obvious cases; LLM for ambiguous ones.
    """
    import re as _re
    msg = (user_message or "").strip()
    low = msg.lower()

    # Fast paths
    _COMMANDS = {"reset", "restart", "start over", "start again", "cancel", "menu",
                 "stop", "exit", "quit", "nevermind", "never mind", "nvm"}
    if low in _COMMANDS:
        return "command"
    if not msg and has_media:
        return "continue"          # a bare photo/voice answers the current step
    if not msg:
        return "continue"

    system = (
        "You route messages for BeeQ, a WhatsApp social-media agent. "
        "The bot is currently at a specific step and waiting for something. "
        "Decide how to handle the user's new message.\n\n"
        "Return ONLY one word:\n"
        "  continue — the message is responding to / answering the current step\n"
        "  new      — the user is clearly starting a DIFFERENT content request, "
        "abandoning the current step (e.g. 'actually, make a carousel about X', "
        "'forget that, post about my sale')\n"
        "  command  — a global command: reset, cancel, start over, menu, help\n\n"
        "Default to 'continue' unless the user clearly wants something different. "
        "A short answer, a topic, a number, a yes/no, an edit, or 'skip' is 'continue'."
    )
    user = (
        f"The bot is currently: {step_purpose}\n"
        f"User's new message: \"{msg}\"\n"
        f"Media attached: {has_media}\n\n"
        "Answer with one word: continue, new, or command."
    )
    try:
        raw = _chat(system, user, temperature=0.0, max_tokens=8).strip().lower()
        raw = _re.sub(r"[^a-z]", "", raw)
        if raw in {"continue", "new", "command"}:
            return raw
    except Exception:
        pass
    return "continue"


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


@traceable(run_type="chain", name="analyze_post_style")
def analyze_post_style(image_url: str) -> dict:
    """
    Two-step style fingerprinting pipeline.

    Step 1 — Vision (VLM): Qwen analyzes the image with extreme precision, measuring
    spatial positions as percentages of the canvas, estimating pixel sizes, and
    cataloguing every design element present or absent.

    Step 2 — LLM: Distills the raw observation into two outputs:
      a) A human-readable style skill (for prompt injection)
      b) A compositor block with concrete pixel/percentage values the Pillow
         carousel composer can use directly to replicate the layout.

    Returns:
      {
        "skill":      dict  — full structured style (layout, typography, badge, colors, composition, caption_style)
        "compositor": dict  — pixel-ready values for carousel_composer.py
        "summary":    str   — 1-2 sentence human description
      }
    """
    import json as _json
    import re as _re

    # ── Step 1: Deep VLM analysis ────────────────────────────────────────────
    try:
        vision_resp = _client().chat.completions.create(
            model=config.GROQ_VISION_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": (
                        "You are a senior graphic designer reverse-engineering a social media post to "
                        "recreate it pixel-perfectly. Analyze this image with extreme precision.\n\n"

                        "TREAT THE IMAGE AS A GRID: 0% = top/left edge, 100% = bottom/right edge.\n\n"

                        "Report EXACTLY:\n\n"

                        "1. CANVAS SIZE & RATIO — is this square (1:1), portrait (4:5 or 9:16), landscape? "
                        "Estimate pixel dimensions (e.g. 1080×1080, 1080×1350).\n\n"

                        "2. BACKGROUND — type: solid color / gradient / photograph / texture / abstract. "
                        "If solid/gradient: exact hex colors. If photo: subject (person, product, scene, abstract). "
                        "Any overlay (dark gradient from bottom, frosted glass panel, vignette)? "
                        "Overlay opacity estimate (light ~30%, medium ~60%, heavy ~80%).\n\n"

                        "3. PROFILE / BRAND BADGE — is a profile badge, avatar circle, logo watermark, or "
                        "username handle present?\n"
                        "   - If YES: position as % from top-left (e.g. 'top:5% left:6%'), "
                        "     size as % of canvas width (e.g. 'avatar diameter ~7%'), "
                        "     shape (perfect circle / rounded rect / rectangular), "
                        "     background (white pill / dark pill / transparent / branded color), "
                        "     border/ring (color, thickness estimate), "
                        "     text alongside avatar (brand name + handle / brand name only / none), "
                        "     font size of name vs handle as % of canvas height.\n"
                        "   - If NO badge: state explicitly 'NO BADGE PRESENT'.\n\n"

                        "4. SLIDE COUNTER / PAGE INDICATOR — is there a '1/7' style counter or dot indicators? "
                        "Position, size, pill/dot style, colors.\n\n"

                        "5. HEADLINE / MAIN TEXT — position as % from top (e.g. 'starts at 55% from top'), "
                        "alignment (left / center / right), estimated font size as % of canvas height "
                        "(e.g. '~7% of height ≈ 75px on 1080px canvas'), "
                        "font weight (thin/light/regular/semibold/bold/black/extra-black), "
                        "font category (sans-serif geometric / humanist sans / serif / display / script), "
                        "text color (hex or precise description), "
                        "letter spacing (tight=-2px / normal=0 / wide=+2px / very-wide=+5px+), "
                        "uppercase? any text effects (shadow / outline / gradient fill / glow).\n\n"

                        "6. BODY / SUBHEADLINE TEXT — same details as above. Position, size, weight, color.\n\n"

                        "7. DECORATIVE ELEMENTS — any lines, geometric shapes, pills/tags, icons, "
                        "accent bars, dividers? Position, color, size.\n\n"

                        "8. SUBJECT / PRODUCT PLACEMENT — where is the main visual subject? "
                        "Center / left-third / right-third / top-half / bottom-half / full-bleed / "
                        "centered with negative space around. How much of the canvas does it occupy (%)?\n\n"

                        "9. COLOR PALETTE — list all distinct colors used with hex estimates: "
                        "background, primary text, secondary text, accent/highlight, badge background, badge border.\n\n"

                        "10. OVERALL AESTHETIC — describe the vibe in 3-5 words "
                        "(e.g. 'luxury dark minimalist', 'bright playful bold', 'clean editorial white').\n\n"

                        "Be surgical. Use percentages, pixel estimates, hex colors. "
                        "This exact description will be fed to code that DRAWS a new image replicating this style."
                    )},
                ],
            }],
            temperature=0.3,
            max_completion_tokens=1400,
            top_p=0.95,
            reasoning_effort="default",
            stop=None,
        )
        vision_analysis = (vision_resp.choices[0].message.content or "").strip()
    except Exception:
        vision_analysis = (
            "Square 1080x1080 canvas. Dark solid background #111111. "
            "Profile badge: top-left at 5% top 6% left, white pill, circular avatar ~7% diameter, "
            "brand name + @handle text. Headline at 55% from top, large bold sans-serif white text. "
            "No body text. Dark editorial aesthetic."
        )

    # ── Step 2: Distill into structured skill + compositor params ────────────
    system = (
        "You are a design systems engineer. Convert the visual analysis of a social media post "
        "into two things:\n"
        "1. A structured style skill JSON (for AI prompt injection)\n"
        "2. A compositor block with CONCRETE PIXEL VALUES for a 1080×1080 Pillow canvas\n\n"
        "The compositor block values are used directly in Python code to draw slides. "
        "Be precise — wrong values produce broken layouts.\n\n"
        "Output ONLY valid JSON with this exact schema (no markdown, no extra keys):\n"
        "{\n"
        '  "layout": {\n'
        '    "text_placement": "bottom-left | bottom-center | bottom-right | center | top-left | overlay-center",\n'
        '    "headline_zone": "string — e.g. lower-third left-aligned",\n'
        '    "body_zone": "string or null",\n'
        '    "subject_position": "center | left-third | right-third | top-half | full-bleed",\n'
        '    "negative_space": "minimal | moderate | generous",\n'
        '    "overlay": "none | dark-gradient | frosted-glass | color-wash | vignette"\n'
        "  },\n"
        '  "typography": {\n'
        '    "font_style": "sans-serif | serif | script | bold-display | mixed",\n'
        '    "headline_weight": "light | regular | semibold | bold | black",\n'
        '    "headline_size": "small | medium | large | very-large",\n'
        '    "letter_spacing": "tight | normal | wide | very-wide",\n'
        '    "text_color_primary": "#hex or description",\n'
        '    "text_color_secondary": "#hex or null",\n'
        '    "text_effects": "none | drop-shadow | outline | glow | uppercase-all"\n'
        "  },\n"
        '  "badge": {\n'
        '    "present": true,\n'
        '    "type": "profile-avatar | logo-watermark | username-handle | brand-stamp",\n'
        '    "position": "top-left | top-right | bottom-left | bottom-right | top-center",\n'
        '    "size": "small | medium | large",\n'
        '    "shape": "circular | rounded-rect | rectangular",\n'
        '    "background": "white-pill | dark-pill | transparent | branded",\n'
        '    "border_color": "#hex or null",\n'
        '    "shows_handle": true\n'
        "  },\n"
        '  "colors": {\n'
        '    "palette": ["#hex1", "#hex2"],\n'
        '    "background": "#hex or description",\n'
        '    "background_type": "solid | gradient | photographic | textured | abstract",\n'
        '    "accent": "#hex",\n'
        '    "mood": "warm | cool | neutral | high-contrast | pastel | dark"\n'
        "  },\n"
        '  "composition": {\n'
        '    "style": "luxury-minimalist | bold-editorial | warm-lifestyle | dark-moody | clean-minimal | vibrant-bold",\n'
        '    "framing": "centered | rule-of-thirds-left | rule-of-thirds-right | full-bleed | flat-lay",\n'
        '    "depth": "flat-2d | shallow-dof | deep-focus"\n'
        "  },\n"
        '  "caption_style": {\n'
        '    "tone": "professional | casual | excited | inspirational | minimal | humorous",\n'
        '    "emoji_density": "none | light | moderate | heavy",\n'
        '    "hashtag_placement": "inline | end-grouped | none",\n'
        '    "hashtag_count": "none | few | moderate | many",\n'
        '    "cta_style": "direct-command | soft-invite | question | none"\n'
        "  },\n"
        '  "compositor": {\n'
        '    "canvas_w": 1080,\n'
        '    "canvas_h": 1080,\n'
        '    "pad_x": 72,\n'
        '    "overlay_top_alpha": 20,\n'
        '    "overlay_bot_alpha": 200,\n'
        '    "badge_present": true,\n'
        '    "badge_position": "top-left | top-right | bottom-left | bottom-right",\n'
        '    "badge_avatar_px": 72,\n'
        '    "badge_offset_x": 72,\n'
        '    "badge_offset_y": 56,\n'
        '    "badge_shape": "pill | circle | square",\n'
        '    "badge_bg_rgba": [255, 255, 255, 235],\n'
        '    "badge_border_color": null,\n'
        '    "badge_border_px": 0,\n'
        '    "headline_size_px": 78,\n'
        '    "headline_weight": "black | bold | regular",\n'
        '    "headline_y_pct": 0.56,\n'
        '    "headline_color": "#ffffff",\n'
        '    "body_size_px": 26,\n'
        '    "body_color": "#cccccc",\n'
        '    "content_top_y": 220,\n'
        '    "slide_counter_present": true,\n'
        '    "slide_counter_position": "top-right | top-left | bottom-right | none",\n'
        '    "accent_color": "#c0392b",\n'
        '    "bg_primary": "#111111",\n'
        '    "bg_secondary": "#ede8df",\n'
        '    "text_uppercase": false,\n'
        '    "letter_spacing_px": 2\n'
        "  },\n"
        '  "summary": "string — 1-2 sentence human description of the style"\n'
        "}\n\n"
        "COMPOSITOR RULES:\n"
        "- canvas_w/h always 1080 for Instagram square\n"
        "- badge_avatar_px: small=48, medium=72, large=96\n"
        "- badge_offset_x/y: pixels from canvas edge to badge (based on observed position %)\n"
        "- headline_y_pct: 0.0=top, 1.0=bottom — where the headline starts on cover slide\n"
        "- headline_size_px: small=48, medium=62, large=78, very-large=92\n"
        "- overlay_bot_alpha: none=0, light=100, medium=180, heavy=230\n"
        "- If no badge: set badge_present=false and all badge_* to null/0\n"
        "Output ONLY the JSON."
    )

    raw = _chat(system, f"Visual analysis:\n{vision_analysis}", temperature=0.1, max_tokens=1200)
    try:
        cleaned = _re.sub(r"```[a-z]*\n?", "", raw).strip().strip("`").strip()
        data = _json.loads(cleaned)
        summary    = data.pop("summary", "A clean, well-composed social media post style.")
        compositor = data.pop("compositor", {})
        return {"skill": data, "compositor": compositor, "summary": summary}
    except Exception:
        # Safe defaults
        default_compositor = {
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
            "accent_color": "#f59e0b", "bg_primary": "#111111", "bg_secondary": "#ede8df",
            "text_uppercase": False, "letter_spacing_px": 2,
        }
        return {
            "skill": {
                "layout": {"text_placement": "bottom-center", "headline_zone": "lower-third left-aligned",
                           "body_zone": None, "subject_position": "center", "negative_space": "moderate",
                           "overlay": "dark-gradient"},
                "typography": {"font_style": "sans-serif", "headline_weight": "bold",
                               "headline_size": "large", "letter_spacing": "normal",
                               "text_color_primary": "#ffffff", "text_color_secondary": None,
                               "text_effects": "none"},
                "badge": {"present": True, "type": "profile-avatar", "position": "top-left",
                          "size": "medium", "shape": "circular", "background": "white-pill",
                          "border_color": None, "shows_handle": True},
                "colors": {"palette": ["#1a1a1a", "#ffffff"], "background": "dark solid",
                           "background_type": "solid", "accent": "#f59e0b", "mood": "dark"},
                "composition": {"style": "bold-editorial", "framing": "centered", "depth": "flat-2d"},
                "caption_style": {"tone": "professional", "emoji_density": "light",
                                  "hashtag_placement": "end-grouped", "hashtag_count": "few",
                                  "cta_style": "soft-invite"},
            },
            "compositor": default_compositor,
            "summary": "Bold editorial style with dark background and centered layout.",
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


@traceable(run_type="llm", name="detect_gender_from_image")
def detect_gender_from_image(image_url: str) -> str:
    """
    Use Groq vision to detect the apparent gender of the main person in an image,
    to pick a matching TTS voice. Returns "male" or "female".
    """
    import logging as _log
    _l = _log.getLogger(__name__)
    try:
        resp = _client().chat.completions.create(
            model=config.GROQ_VISION_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": (
                        "Look closely at the main person in this photo. Decide whether they "
                        "appear to be a man or a woman, so we can pick a matching voice-over voice. "
                        "Consider face, hair, build and clothing. "
                        "Answer with EXACTLY one word, lowercase: 'man' or 'woman'."
                    )},
                ],
            }],
            temperature=0.0,
            max_completion_tokens=200,   # reasoning headroom — small budgets return empty
            top_p=0.95,
            reasoning_effort="default",
            stop=None,
        )
        ans = (resp.choices[0].message.content or "").strip().lower()
        _l.info("detect_gender_from_image: raw='%s' for %s", ans[:40], image_url[:60])
        if "man" in ans and "woman" not in ans:
            return "male"
        if "woman" in ans or "female" in ans:
            return "female"
        if ans.startswith("m"):
            return "male"
        if ans.startswith("f") or ans.startswith("w"):
            return "female"
        _l.warning("detect_gender_from_image: unclear answer '%s' — defaulting male", ans[:40])
        return "male"
    except Exception as exc:
        _l.warning("detect_gender_from_image failed (%s) — defaulting male", exc)
        return "male"


@traceable(run_type="chain", name="generate_presentation_script")
def generate_presentation_script(context: str, brand: dict, target_seconds: int = 20) -> str:
    """
    Write a spoken presentation script for a UGC presentation reel, based on scraped
    product/property facts. ~2.5 words/sec → target word count for the desired length.
    Returns plain spoken words (no stage directions, no hashtags).
    """
    target_words = max(28, int(target_seconds * 2.5))
    brand_ctx = (
        f"Brand: {brand.get('brand_name') or ''}\n"
        f"Voice/tone: {brand.get('brand_voice') or 'warm, confident, engaging'}\n"
    )
    system = (
        "You write spoken scripts for a person presenting a product/property/service to "
        "camera in an Instagram reel. The viewer also sees photos of it behind the presenter.\n"
        "RULES:\n"
        f"1. About {target_words} words (~{target_seconds}s spoken). Stay close to this length.\n"
        "2. Open with a strong hook in the first sentence.\n"
        "3. Use the REAL facts provided (price, location, features) — be specific, never generic.\n"
        "4. Natural, conversational, enthusiastic — like a real creator, not an ad read.\n"
        "5. End with a clear call to action.\n"
        "6. Output ONLY the spoken words. No emojis, no hashtags, no stage directions, no labels."
    )
    user = f"{brand_ctx}\nFacts / context to present:\n{context}\n\nWrite the spoken script."
    # NOTE: reasoning_effort="medium" consumes part of max_completion_tokens, so keep
    # the budget large enough that the actual script isn't truncated to empty.
    raw = _chat(system, user, temperature=0.75, max_tokens=2000).strip()
    return raw.strip('"').strip("'").strip()


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


@traceable(run_type="chain", name="generate_video_edit_plan")
def generate_video_edit_plan(transcript: str, duration_sec: float, brand: dict) -> dict:
    """
    Turn a user's talking-video transcript into a structured, trending edit plan that the
    video-editor pipeline (B-roll + chromakey + captions + effects) executes.

    Returns:
      {
        "title": str,
        "story": str,                 # 1-line story summary
        "segments": [
          { "start": float, "end": float,   # seconds within the source video
            "broll_prompt": str,            # what B-roll to generate for this beat (p-video)
            "zoom": "in" | "out" | "none",  # camera move on the presenter
            "caption": str,                 # short on-screen caption for this beat
            "emphasis": str }               # optional overlay/infographic idea
        ],
        "cta": str
      }
    """
    import json as _json, re as _re
    target_segments = max(3, min(8, int(duration_sec // 4) or 3))
    system = (
        "You are a viral short-form video editor (Instagram Reels / TikTok) in the high-retention "
        "'Hormozi' style. Given a transcript of someone talking to camera, produce an EDIT PLAN. "
        "The person is cut out over AI stop-motion B-roll, with kinetic captions and hand-drawn "
        "graphic overlays. YOU decide the overlays per beat so every video feels uniquely edited — "
        "do NOT apply the same treatment to every segment; vary them to match the meaning and energy.\n"
        "Rules:\n"
        f"- Break the video into about {target_segments} contiguous, non-overlapping segments "
        f"covering 0..{duration_sec:.1f}s.\n"
        "- Each segment needs:\n"
        "  • broll_prompt: cinematic stop-motion B-roll relevant to what's said (no text/logos)\n"
        "  • caption: SHORT (max 6 words), matching what they say in that beat\n"
        "  • zoom: 'in' (build tension), 'out' (reveal/breathe), 'punch' (hard emphasis), or 'none'\n"
        "  • doodle: a hand-drawn overlay chosen for MEANING. Options: 'arrow' (point at them / one big "
        "claim), 'arrows' (surround for hype), 'circle' (spotlight one thing), 'underline' or 'highlighter' "
        "(drive home a key caption word), 'box' or 'brackets' (frame/focus on them), 'stars' (a win / "
        "delight), 'action_lines' (high-energy hype), 'check' (yes/correct/do this), 'cross' (no/myth/don't), "
        "or 'none' (let a calm beat breathe). VARY these across the video — never repeat the same one back "
        "to back, and leave some beats with 'none'.\n"
        "  • emoji: a single relevant emoji to pop on a punchy beat (🔥💰✅❌👀🚀📈), else \"\".\n"
        "  • info: an infographic (renders in the top band, clear of the face). Use a DATA type when a "
        "number/percentage is spoken: {\"type\":\"counter\"|\"progress\"|\"ring\"|\"stat\", \"value\":<number>, "
        "\"label\":\"SHORT LABEL\", \"suffix\":\"\"} (counter=count-up, ring/progress=%, stat=callout card). "
        "For a QUALITATIVE point (a benefit/feature/step, no number), use {\"type\":\"callout\", "
        "\"icon\":\"<one emoji>\", \"label\":\"2-3 WORDS\"}. Aim to include a fitting infographic on ROUGHLY "
        "A THIRD of the segments (vary the type — don't repeat the same one); use {\"type\":\"none\"} on the "
        "rest. NEVER invent numbers not in the transcript — if unsure, use a callout, not a fake stat.\n"
        "  • big_text: 1-2 word ALL-CAPS phrase to slam BEHIND the subject for a punchy keyword, else \"\" "
        "(only the 1-3 biggest beats).\n"
        "  • transition: how this cut ENTERS — 'flash' (default), 'whip' (fast energetic swipe), 'glitch' "
        "(digital/tech beat), 'shake' (impact/hard hit), or 'none'. Match it to the energy of the beat.\n"
        "  • lens: true only for a rare intense 'through-the-scope' moment, else false\n"
        "  • peak: true if this is a climax/punchline beat, else false\n"
        "- Think like a real editor: hook beats get an arrow/action_lines/big_text, explanation beats stay "
        "clean, stat beats get an infographic, the payoff beat gets a punch zoom + shake. Keep it authentic "
        "— never invent numbers or facts not in the transcript.\n"
        "\nDIRECTOR DISCIPLINE (this is what makes it look elite, not amateur):\n"
        "- ONE hero element per beat MAX. Never combine a doodle AND an infographic AND big_text on the same "
        "segment — pick the single strongest one; leave the rest empty.\n"
        "- LESS IS MORE: at least half the segments should have doodle='none' and no emoji — clean beats let "
        "the captions and presenter breathe. Overusing overlays is the #1 amateur mistake.\n"
        "- Emoji: use RARELY (at most 1-2 in the whole video), only when it truly amplifies a punchline. "
        "It renders small in a corner — never rely on it as the main graphic.\n"
        "- 'circle', 'brackets' and 'lens' FRAME THE PERSON'S FACE — only use them to spotlight the speaker "
        "on an intense/personal line, not on B-roll-heavy beats.\n"
        "- big_text is a rare power move (1-3 times per video), for the single biggest keyword only.\n"
        "- Vary transitions with the energy; most cuts should be 'flash' or 'whip', save 'glitch'/'shake' "
        "for genuine impact moments.\n"
        "- The captions are always on — treat overlays as seasoning, not the meal.\n"
        "- Output ONLY valid JSON. No markdown."
    )
    user = (
        f"Brand: {brand.get('brand_name') or ''} | Voice: {brand.get('brand_voice') or 'energetic'}\n"
        f"Video duration: {duration_sec:.1f}s\n"
        f"Transcript:\n{transcript}\n\n"
        'Return JSON: {"title":"...","story":"...","segments":[{"start":0,"end":4,'
        '"broll_prompt":"...","caption":"...","zoom":"in","doodle":"arrow","emoji":"","big_text":"",'
        '"info":{"type":"none"},"transition":"flash","lens":false,"peak":false}],"cta":"..."}'
    )
    raw = _chat(system, user, temperature=0.7, max_tokens=2200)
    try:
        clean = _re.sub(r"```[a-z]*\n?", "", raw).strip().strip("`")
        data = _json.loads(clean)
        if "segments" not in data or not isinstance(data["segments"], list):
            raise ValueError("no segments")
        return data
    except Exception:
        # Safe fallback: single full-length segment
        return {
            "title": (brand.get("brand_name") or "My video"),
            "story": transcript[:120],
            "segments": [{
                "start": 0.0, "end": float(duration_sec or 15),
                "broll_prompt": f"cinematic b-roll for {brand.get('brand_description') or 'the topic'}",
                "caption": "", "zoom": "none", "doodle": "none", "emoji": "", "big_text": "",
                "info": {"type": "none"}, "transition": "flash", "lens": False, "peak": False,
            }],
            "cta": "Follow for more",
        }


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


# ---------------------------------------------------------------------------
# Sub-agent action classifier — replaces keyword matching in step handlers
# ---------------------------------------------------------------------------

@traceable(run_type="chain", name="classify_post_review")
def classify_post_review(user_message: str, caption: str = "", content_kind: str = "post") -> tuple[str, str]:
    """
    Understand what the user wants after seeing a generated post/carousel — the
    conversational-editing brain. Returns (action, value):
      action ∈ {approve, edit_image, edit_caption, regenerate, publish, unknown}
      value  = the edit instruction (for edit_image) or caption text/request (edit_caption)

    Defaults an ambiguous free-form message to edit_image, because in review the user is
    almost always asking to change how it looks.
    """
    import json as _json, re as _re
    msg = (user_message or "").strip()
    low = msg.lower()

    # Fast, unambiguous cases
    if low in {"approve", "approved", "yes", "yep", "looks good", "looks great", "perfect",
               "great", "love it", "nice", "good", "ok", "okay", "👍", "✅", "done"}:
        return "approve", ""
    if low in {"regenerate", "redo", "again", "new image", "start over", "fresh", "different image"}:
        return "regenerate", ""
    if low in {"publish", "post", "post now", "publish now", "go live", "post it"}:
        return "publish", ""

    system = (
        f"The user was just shown a generated {content_kind} (image + caption) and replied. "
        "Classify what they want and extract any instruction. Return ONLY JSON:\n"
        '{"action": "...", "value": "..."}\n\n'
        "action options:\n"
        "  approve      — they're happy with the image as-is\n"
        "  edit_image   — they want to CHANGE how the image LOOKS. Examples: 'make it brighter', "
        "'bigger logo', 'change the background to a beach', 'more vibrant', 'remove the text', "
        "'warmer tones', 'move the product to the left', 'add sunlight', 'less cluttered'. "
        "Put the exact requested change in value.\n"
        "  edit_caption — they want to change the CAPTION wording/text. Put the request in value.\n"
        "  regenerate   — they want a completely different, fresh image\n"
        "  publish      — they want to publish/post it now\n"
        "Rules: if the message describes a visual change, use edit_image. If it's about the "
        "words/caption, use edit_caption. When in doubt, prefer edit_image with the message as value. "
        "Output ONLY the JSON."
    )
    user = f'Caption shown: "{caption[:200]}"\nUser said: "{msg}"'
    try:
        raw = _chat(system, user, temperature=0.0, max_tokens=1500)
        raw = _re.sub(r"```[a-z]*\n?", "", raw).strip().strip("`")
        data = _json.loads(raw)
        action = str(data.get("action") or "").strip()
        value = str(data.get("value") or "").strip()
        if action not in {"approve", "edit_image", "edit_caption", "regenerate", "publish"}:
            action = "edit_image"
            value = value or msg
        if action in ("edit_image", "edit_caption") and not value:
            value = msg
        return action, value
    except Exception:
        # Safe default: treat as an image edit using the raw message
        return "edit_image", msg


@traceable(run_type="chain", name="classify_action")
def classify_action(
    user_message: str,
    step: str,
    options: list[str],
    extra_context: str = "",
) -> tuple[str, str]:
    """
    Lightweight LLM call that understands what the user wants in a specific
    sub-agent step, replacing brittle keyword matching.

    Args:
        user_message: what the user typed/said
        step: one of "caption_choice", "publish_action", "product_image", "schedule_time"
        options: list of valid action keys to return (e.g. ["approve","regenerate","custom"])
        extra_context: optional extra context (e.g. the caption text being reviewed)

    Returns:
        (action, value)
        - action: one of `options`, or "unknown"
        - value: extracted text (e.g. custom caption, schedule time) or ""

    Fast path: very short single-word messages are matched without an LLM call.
    LLM fallback: used for natural-language or ambiguous messages.
    """
    import re as _re

    msg = (user_message or "").strip()
    msg_lower = msg.lower()

    # ── Fast path — unambiguous single-word inputs ───────────────────────────
    _FAST = {
        "approve":    {"approve", "yes", "✅", "ok", "okay", "yep", "great", "perfect", "good",
                       "looks good", "looks great", "love it", "nice", "send it"},
        "regenerate": {"regenerate", "again", "redo", "retry", "new", "different", "change",
                       "try again", "nope", "no", "not good", "bad"},
        "custom":     {"custom", "write", "my own", "i'll write", "let me write", "manual"},
        "now":        {"now", "publish now", "post now", "post it", "send", "go", "yes", "yep",
                       "yeah", "do it", "do it now", "go ahead", "upload", "publish"},
        "schedule":   {"schedule", "later", "schedule it", "pick a time", "set time"},
        "skip":       {"skip", "no", "nope", "generate", "scratch", "without", "ai", "no image",
                       "no photo", "don't have", "dont have"},
    }
    for action, synonyms in _FAST.items():
        if action in options and (msg_lower in synonyms or any(s in msg_lower for s in synonyms)):
            return action, ""

    # ── LLM path ─────────────────────────────────────────────────────────────
    step_descriptions = {
        "caption_choice": (
            "The bot just showed the user an AI-generated caption for their post.\n"
            "Options:\n"
            "  approve      — user likes the caption as-is (e.g. 'looks great', 'that works', 'yes')\n"
            "  regenerate   — user wants a new/different caption (e.g. 'try again', 'not feeling it')\n"
            "  custom       — user says they want to write their own but hasn't written it yet\n"
            "  custom_text  — user has already written their own caption in this message\n"
        ),
        "publish_action": (
            "The bot is ready to publish a post and asking when to do it.\n"
            "Options:\n"
            "  now       — user wants to publish immediately (e.g. 'do it now', 'go ahead', 'yes', 'post it')\n"
            "  schedule  — user wants to pick a specific future time\n"
            "  cancel    — user wants to cancel or start over\n"
        ),
        "product_image": (
            "The bot asked if the user wants to attach a product image for the post.\n"
            "Options:\n"
            "  skip      — user says to generate without a product image (e.g. 'no', 'skip', 'just create it')\n"
            "  wait      — user is about to send an image soon\n"
        ),
        "schedule_time": (
            "The user is providing a time to schedule the post.\n"
            "Options:\n"
            "  time      — user gave a valid date/time (extract it in `value`)\n"
            "  unknown   — user didn't provide a clear time\n"
        ),
    }

    step_desc = step_descriptions.get(step, f"Step: {step}. Options: {', '.join(options)}")
    opts_str  = " | ".join(options)

    system = (
        "You are the decision brain of BeeQ, a WhatsApp social media agent. "
        "A user is mid-flow. Understand their intent and return JSON.\n\n"
        f"{step_desc}\n"
        f"Return JSON: {{\"action\": \"<one of: {opts_str}>\", \"value\": \"<extracted text or empty>\"}}\n"
        "Rules:\n"
        "- If the user wrote an actual caption/text (not a reaction word), action=custom_text and value=that text\n"
        "- If the message is a date/time string, action=time and value=that string\n"
        "- If unclear, action=unknown\n"
        "- Output ONLY the JSON, no explanation."
    )
    user_prompt = f"User said: \"{msg}\""
    if extra_context:
        user_prompt = f"Context: {extra_context}\n{user_prompt}"

    try:
        raw = _chat(system, user_prompt, temperature=0.0, max_tokens=80)
        raw = _re.sub(r"```[a-z]*\n?", "", raw).strip().strip("`")
        import json as _json
        data = _json.loads(raw)
        action = str(data.get("action") or "unknown").strip()
        value  = str(data.get("value") or "").strip()
        if action not in options and action not in {"custom_text", "time", "unknown"}:
            action = "unknown"
        return action, value
    except Exception:
        return "unknown", ""
