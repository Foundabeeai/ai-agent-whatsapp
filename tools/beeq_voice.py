"""
BeeQ Personality Engine.

BeeQ is a sharp, warm, no-fluff AI social media manager.
She speaks like a real person — brief, direct, occasionally witty,
always helpful. Never sounds like a chatbot or a system message.

Usage:
    from tools.beeq_voice import msg, dynamic

    # Static message (randomly picks from variants)
    return {"kind": "text", "text": msg("welcome")}

    # Dynamic message (Groq generates a contextual one-liner)
    return {"kind": "text", "text": dynamic("post_done", brand_name="HT Goodies", count=2)}
"""

from __future__ import annotations
import random
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Personality: who BeeQ is
# ---------------------------------------------------------------------------
BEEQ_SYSTEM = (
    "You are BeeQ, an AI social media manager built by Foundabee. "
    "Your personality: sharp, warm, confident, occasionally witty — like a brilliant friend "
    "who happens to be a world-class social media strategist. "
    "You speak in short punchy sentences. No corporate jargon. No filler words. "
    "No 'Great!' or 'Sure!' or 'Of course!' at the start of messages. "
    "No emojis overload — use 1 max per message, only when it adds meaning. "
    "You always sound like a real person texting, not a bot replying. "
    "Keep every message under 3 sentences unless listing options."
)

# ---------------------------------------------------------------------------
# Static message bank — multiple natural variants per situation
# ---------------------------------------------------------------------------
_MESSAGES: dict[str, list[str]] = {

    # Auth
    "welcome": [
        "Hey! I'm BeeQ — your AI social media manager from Foundabee.\n\nDrop your account email to get started.",
        "Hi there! BeeQ here, from Foundabee.\n\nWhat's your account email? I'll pull up your profile.",
        "Hey, BeeQ here 👋 — let's get your Foundabee account connected.\n\nWhat's your email?",
    ],
    "verifying": [
        "On it — checking your account now...",
        "Give me a sec, looking you up...",
        "Pulling up your account, one moment...",
    ],
    "still_verifying": [
        "Still on it — verification takes a few seconds, hang tight.",
        "Almost there, just a moment more...",
        "Still checking — shouldn't be long.",
    ],
    "email_not_found": [
        "Hmm, that email doesn't match any Foundabee account. Double-check and try again?",
        "Can't find that one — are you sure that's the email on your Foundabee account?",
        "No account found with that email. Try a different one or sign up at foundabee.com.",
    ],
    "invalid_email": [
        "That doesn't look like a valid email — can you check and resend?",
        "Doesn't look right. Send me your email address (e.g. you@example.com).",
    ],

    # Instagram
    "ask_instagram": [
        "What's your Instagram handle? I'll connect it to your account.",
        "What Instagram account should I manage? (Just the username, no @ needed)",
        "Which Instagram profile are we working with?",
    ],
    "instagram_not_found": [
        "Couldn't find that handle on your Foundabee account. Try a different one?",
        "That Instagram isn't linked to your account. Check the username and try again.",
    ],
    "instagram_connected": [
        "Got it — connected to @{handle}. Let's build something.",
        "Linked to @{handle}. Ready to go.",
        "@{handle} connected ✓",
    ],

    # Onboarding
    "onboarding_start": [
        "Let's get your profile set up — takes about 5 minutes and makes everything I create way more on-brand.\n\nFirst up: what's your brand called, and what do you sell or offer?",
        "Quick setup first — I need to know your brand so I don't create generic content. What's your business and what do you offer?",
        "Before we dive in — tell me about your brand. Name and what you sell/do?",
    ],
    "onboarding_goal": [
        "Got it. What's the main thing you want from social media right now?\n\n1. 📈 Sales & Leads\n2. 👥 Grow Audience\n3. 📣 Brand Awareness\n4. 🤝 All of the above",
        "Nice. What's your social media goal?\n\n1. 📈 Sales & Leads\n2. 👥 Grow Audience\n3. 📣 Brand Awareness\n4. 🤝 All of the above",
        "Love it. What are you optimising for?\n\n1. 📈 Sales & Leads\n2. 👥 Grow Audience\n3. 📣 Brand Awareness\n4. 🤝 All of the above",
    ],
    "onboarding_website": [
        "Do you have a website? Drop the URL — I'll use it to make captions and bios more accurate.\n(Type *skip* if not)",
        "Website URL? Helps me write better captions.\n(Or *skip*)",
        "Got a website I can reference? Drop it here, or *skip*.",
    ],
    "onboarding_voice": [
        "How does your brand talk?\n\n1. 🤗 Warm & empathetic\n2. 💪 Bold & direct\n3. 😄 Light & witty\n4. 👔 Formal & professional\n5. ✏️ Something else — describe it",
        "What's your brand's vibe?\n\n1. 🤗 Warm & empathetic\n2. 💪 Bold & direct\n3. 😄 Light & witty\n4. 👔 Formal & professional\n5. ✏️ Describe it yourself",
        "How should your brand sound on social?\n\n1. 🤗 Warm & empathetic\n2. 💪 Bold & direct\n3. 😄 Light & witty\n4. 👔 Formal & professional\n5. ✏️ Something else",
    ],
    "onboarding_voice_custom": [
        "Describe your brand's communication style in your own words:",
        "Go for it — how would you describe how your brand talks?",
    ],
    "onboarding_colors": [
        "What are your brand colors? (e.g. *navy and gold*, *#FF5733 and white*, or *not sure* to skip)",
        "Brand colors? Even rough ones help — like *dark green and cream*. Or *skip*.",
        "Drop your brand colors — hex codes or just names. Or *skip* if unsure.",
    ],
    "onboarding_reference": [
        "Show me content you actually like — your own posts, a competitor, anyone. Paste a link.\n(Or *skip*)",
        "Any Instagram/TikTok content that feels like what you want? Link me — or *skip*.",
        "Is there content out there that vibes with your brand? Drop a link or *skip*.",
    ],
    "onboarding_competitors": [
        "Who are your main competitors on social? Drop their handles.\n(Or *skip*)",
        "Any competitor accounts I should know about? Their handles help me position you better.\n(Or *skip*)",
        "Competitor Instagram handles? I'll factor them in.\n(Or *skip*)",
    ],
    "onboarding_assets": [
        "Send me 2–3 brand photos — product shots, your logo, lifestyle images. I'll use these to keep everything on-brand.\n\nSend them one by one. Type *done* when finished, or *skip* to continue without.",
        "Drop 2–3 photos — logo, product, anything that represents your brand visually. One at a time.\n\nType *done* when you're done, or *skip*.",
        "Brand photos time — product shots, logo, team photos. Send them one by one.\n\nType *done* when finished, or *skip* to move on.",
    ],
    "onboarding_schedule": [
        "How often do you want to post?\n\n1. Daily\n2. 3–4x per week\n3. Weekly\n4. Let me decide based on your goals",
        "Posting frequency?\n\n1. Daily\n2. 3–4x per week\n3. Weekly\n4. You figure it out",
        "How often should I be posting for you?\n\n1. Daily\n2. 3–4x per week\n3. Weekly\n4. Optimise automatically",
    ],
    "onboarding_report": [
        "How often do you want performance reports?\n\n1. Weekly\n2. Monthly\n3. Only when I ask",
        "Performance updates — how often?\n\n1. Weekly\n2. Monthly\n3. On demand only",
    ],
    "onboarding_timezone": [
        "Last one — what city are you in? I'll use it to schedule posts at the right times.",
        "Nearly done. What city or timezone are you in? I'll post at optimal times.",
        "What city are you based in? Helps me post when your audience is active.",
    ],
    "onboarding_done": [
        "You're all set. Let's make some content — what do you want to create?\n\nType *post*, *carousel*, or *reel*.",
        "Setup complete. Ready to create — say *post*, *carousel*, or *reel* and let's go.",
        "Done. You can now say *post*, *carousel*, or *reel* to start creating. What'll it be?",
    ],

    # Content creation
    "ask_content_type": [
        "What are we making?\n\n📸 *post* — single image\n🎠 *carousel* — swipeable slides\n🎬 *reel* — video",
        "What do you want to create?\n\n📸 *post* — single image\n🎠 *carousel* — multi-slide\n🎬 *reel* — video",
        "Let's create something. What type?\n\n📸 *post*\n🎠 *carousel*\n🎬 *reel*",
    ],
    "ask_description": [
        "What's this post about? Give me a topic, idea, or brief.",
        "Tell me what you want this to be about.",
        "What's the post about? A few words or a full brief — whatever you've got.",
    ],
    "ask_product_image": [
        "Got a product photo? Send it and I'll build around it.\n\nOr type *skip* to generate from scratch.",
        "Send your product image if you have one — or *skip* to create from description only.",
        "Product photo? (Send it, or *skip* if you don't have one)",
    ],
    "generating": [
        "Creating your content now — I'll send it over when it's ready.",
        "On it. Generating now, won't be long.",
        "Working on it — sit tight.",
    ],
    "post_ready": [
        "Here's your post — what do you think?",
        "Done. Take a look:",
        "Here it is. Happy with this?",
    ],

    # Caption
    "ask_caption": [
        "How do you want to caption this?\n\n✨ *ai* — I'll write one\n✏️ *mine* — you write it",
        "Caption time:\n\n✨ *ai* — let me write it\n✏️ *mine* — you write your own",
        "Want me to write the caption, or are you doing it yourself?\n\n✨ *ai* / ✏️ *mine*",
    ],
    "ask_custom_caption": [
        "Write your caption below:",
        "Go ahead — what's your caption?",
        "Drop your caption here:",
    ],

    # Publish
    "ask_publish": [
        "Ready to go live?\n\n📤 *now* — post immediately\n📅 *schedule* — pick a time",
        "How do you want to publish?\n\n📤 *now*\n📅 *schedule*",
        "Publish now or schedule it?\n\n📤 *now* / 📅 *schedule*",
    ],
    "ask_schedule_time": [
        "When should this go out? (e.g. *tomorrow 9am*, *Friday 6pm*, or a date like *May 25 at 10am*)",
        "What time should I post this? (e.g. *tomorrow at 8am* or *Friday 7pm*)",
        "Tell me the date and time — e.g. *tomorrow 9am* or *25 May 6pm*.",
    ],
    "publishing": [
        "Posting it now...",
        "Going live...",
        "Publishing...",
    ],
    "published": [
        "Done — it's live on Instagram.",
        "Posted. It's live.",
        "Up on Instagram ✓",
    ],
    "scheduled": [
        "Scheduled. I'll post it at {time}.",
        "Locked in — posting at {time}.",
        "Done, scheduled for {time}.",
    ],

    # Approval
    "approve_or_regen": [
        "Happy with it?\n\n✅ *approve* — move to caption\n🔄 *regenerate* — try again",
        "What do you think?\n\n✅ *approve*\n🔄 *regenerate*",
        "Keep this one or try again?\n\n✅ *approve* / 🔄 *regenerate*",
    ],

    # Errors
    "upload_error": [
        "Couldn't upload that image — try again, or type *skip* to continue without it.",
        "Upload failed. Send it again or type *skip*.",
    ],
    "generation_error": [
        "Something went wrong generating that. Try again?",
        "Hit a snag — want to try again?",
    ],
    "publish_error": [
        "Publishing failed — want me to try again, or would you rather schedule it instead?",
        "Couldn't post that. Try again or say *schedule* to queue it.",
    ],
    "not_understood": [
        "Not sure what you mean — try *post*, *carousel*, or *reel* to create content, or *help* for options.",
        "Didn't catch that. Say *post*, *reel*, or *help* to see what I can do.",
        "Hmm, didn't get that. Type *help* to see your options.",
    ],
}


def msg(key: str, **kwargs) -> str:
    """
    Return a random natural variant for the given message key.
    kwargs are formatted into the chosen string (e.g. msg("scheduled", time="Friday 9am")).
    Falls back to the key itself if not found.
    """
    variants = _MESSAGES.get(key)
    if not variants:
        logger.warning("beeq_voice: unknown message key %r", key)
        return key
    text = random.choice(variants)
    if kwargs:
        try:
            text = text.format(**kwargs)
        except KeyError:
            pass
    return text


def dynamic(situation: str, **context) -> str:
    """
    Generate a truly contextual one-liner from Groq using BeeQ's personality.
    Use for moments that need brand-specific or data-specific natural language.
    Falls back to a static msg() if Groq fails.

    situation examples:
        "post_done"       — post generated, brand_name="X", content_type="carousel"
        "onboarding_ack"  — acknowledged something, brand_name="X", field="brand_voice"
        "reel_done"       — reel finished, reel_type="ugc"
        "caption_written" — caption ready, caption="..."
    """
    try:
        from tools.groq_ai import _chat  # local import to avoid circular
        context_str = ", ".join(f"{k}={v}" for k, v in context.items())
        user_prompt = (
            f"Situation: {situation}\n"
            f"Context: {context_str}\n\n"
            "Write ONE natural, conversational message BeeQ would send in this situation. "
            "1–2 sentences max. No filler opener. Sound like a real person texting."
        )
        return _chat(BEEQ_SYSTEM, user_prompt, temperature=0.85, max_tokens=80)
    except Exception as exc:
        logger.warning("beeq_voice.dynamic failed: %s", exc)
        # Map situation to nearest static key
        fallback_map = {
            "post_done": "post_ready",
            "reel_done": "approve_or_regen",
            "caption_written": "ask_publish",
            "onboarding_ack": "onboarding_done",
        }
        return msg(fallback_map.get(situation, "not_understood"))
