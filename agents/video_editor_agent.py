"""
AI Video Editor sub-agent.

User sends their talking video → we turn it into a trending short-form cut:
  matting → green screen → transcript → LLM edit plan → (later stages) B-roll +
  chromakey composite + Remotion captions/overlays → final render.

Pipeline (built stage by stage):
  STAGE 1 (this file, implemented):
    - user sends a video → upload to S3
    - robust_video_matting → green-screen version (chroma-key ready)
    - Whisper transcript of what they say
    - LLM edit plan (segments: B-roll prompts, zoom, captions, overlays)
    - send the plan back for approval
  STAGE 2+ (next): p-video B-roll per segment → chromakey user over B-roll →
    Remotion captions/zoom/infographics → final render → S3 → approve → publish.

Internal sub_steps (intent["_sub_step"]):
  awaiting_video      — waiting for the user to send their video
  planning            — matting + transcript + edit plan running
  awaiting_plan_ok     — plan shown, awaiting approval
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from session_store import UserSession, save_session, STEP_AGENT_REEL, STEP_CHOOSE_CONTENT_TYPE
from tools import aws_storage, video_gen, voice, groq_ai

logger = logging.getLogger(__name__)


def _send(phone: str, payload: dict, tts: bool = False) -> None:
    from workflow import _send_async
    _send_async(phone, payload, tts=tts)


def _gather_assets(phone: str, user_id: str, intent: dict, clean: str,
                   media_urls: list[str], media_types: list[str]) -> int:
    """
    Collect background-source images from THIS message: attached product photos
    and/or images scraped from a Zillow/listing link in the caption. Accumulates
    into intent["_bg_images"] (deduped, capped). Returns the total count so far.
    """
    imgs = list(intent.get("_bg_images") or [])

    for url, mt in zip(media_urls or [], media_types or []):
        if mt.startswith("image/"):
            up = aws_storage.upload_from_url(url, user_id=user_id, media_kind="editor_photo")
            if up.get("ok"):
                imgs.append(up["s3_url"])

    try:
        from tools.url_context import find_all_urls, scrape_url as _scrape_url
        urls = find_all_urls(clean or "")
        for u in urls[:1]:
            _send(phone, {"kind": "text", "text": "🔗 Pulling photos from your link…"})
            ctx = _scrape_url(u, phone)
            if ctx.get("ok") and ctx.get("image_urls"):
                imgs.extend(ctx["image_urls"])
                logger.info("video_editor: scraped %d images from %s", len(ctx["image_urls"]), u)
            elif ctx.get("error") == "blocked":
                _send(phone, {"kind": "text",
                              "text": "⚠️ That site blocked me from reading its photos — I'll use AI backgrounds instead."})
            else:
                _send(phone, {"kind": "text",
                              "text": "⚠️ Couldn't pull photos from that link — I'll use AI backgrounds instead."})
    except Exception as exc:
        logger.warning("video_editor: link scrape failed: %s", exc)

    imgs = list(dict.fromkeys(imgs))[:8]   # dedupe, keep order, cap
    intent["_bg_images"] = imgs
    return len(imgs)


def start(phone: str, session: UserSession, intent: dict) -> dict:
    intent["reel_type"] = "video_editor"
    intent["_sub_step"] = "awaiting_video"
    session.agent_intent = intent
    session.step = STEP_AGENT_REEL
    save_session(session)
    return {
        "kind": "text",
        "text": (
            "🎬 *AI Video Editor*\n\n"
            "Send me your talking video and I'll turn it into a trending cut — "
            "removed background, dynamic B-roll behind you, punchy captions and zoom effects.\n\n"
            "📹 *Send your video now.*\n"
            "🏠 Optional: attach product photos or paste a *Zillow / listing link* in the same "
            "message — I'll animate those photos as the background."
        ),
    }


def handle_step(
    phone: str,
    session: UserSession,
    clean: str,
    button_payload: Optional[str],
    media_urls: list[str],
    media_types: list[str],
    voice_confirmed: bool,
) -> dict:
    intent   = session.agent_intent or {}
    sub_step = intent.get("_sub_step", "")
    low      = (button_payload or clean or "").strip().lower()

    if sub_step == "awaiting_video":
        has_video = bool(media_urls and any(t.startswith("video/") for t in media_types))
        if not has_video:
            return {"kind": "text", "text": "📹 Please send your talking video (a normal WhatsApp video)."}
        # Upload the video to S3
        user_id = session.verified_user_id or phone
        video_url = None
        for url, mt in zip(media_urls, media_types):
            if mt.startswith("video/"):
                up = aws_storage.upload_from_url(url, user_id=user_id, media_kind="editor_src")
                if up.get("ok"):
                    video_url = up["s3_url"]
                break
        if not video_url:
            return {"kind": "text", "text": "😕 Couldn't read that video — please send it again."}
        intent["_src_video_url"] = video_url

        # Grab any photos / link that came WITH the video
        n = _gather_assets(phone, user_id, intent, clean, media_urls, media_types)
        if n > 0:
            return _start_planning(phone, session, intent)

        # Nothing yet — offer to add photos/link (or skip to AI backgrounds)
        intent["_sub_step"] = "awaiting_assets"
        session.agent_intent = intent
        save_session(session)
        return {"kind": "text",
                "text": ("✅ Got your video!\n\nWant your own visuals in the background?\n"
                         "🏠 Paste a *Zillow / listing link*, or 📷 send *product photos* now — "
                         "I'll animate them behind you.\n\nOr reply *skip* to use AI-generated backgrounds.")}

    if sub_step == "awaiting_assets":
        user_id = session.verified_user_id or phone
        if low in ("skip", "no", "none", "go", "proceed", "continue", "ai", "generate"):
            return _start_planning(phone, session, intent)
        has_image = any(t.startswith("image/") for t in media_types)
        has_link = False
        try:
            from tools.url_context import find_all_urls
            has_link = bool(find_all_urls(clean or ""))
        except Exception:
            pass
        if has_image or has_link:
            n = _gather_assets(phone, user_id, intent, clean, media_urls, media_types)
            if n > 0:
                return _start_planning(phone, session, intent)
            # scraping/photos yielded nothing → proceed with AI backgrounds
            return _start_planning(phone, session, intent)
        return {"kind": "text",
                "text": "Send a *Zillow/listing link* or *product photos* for the background, or reply *skip*."}

    if sub_step == "planning":
        return {"kind": "text", "text": "⏳ Still analyzing your video — hang tight!"}

    if sub_step == "building":
        return {"kind": "text", "text": "⏳ Still building your edit — B-roll takes a few minutes. I'll send it when ready!"}

    if sub_step == "awaiting_plan_ok":
        if low in ("approve", "yes", "ok", "okay", "go", "looks good", "perfect", "make it", "continue"):
            intent["_sub_step"] = "building"
            session.agent_intent = intent
            save_session(session)
            _send(phone, {"kind": "text",
                          "text": "🎬 Building your edit! Generating B-roll for each scene and compositing "
                                  "you over it with zoom cuts.\n⏱ This takes a few minutes — I'll send it when it's ready ☕"})
            threading.Thread(target=_build_bg, args=(phone, session, intent), daemon=True).start()
            return {"kind": "none"}
        if "regenerate" in low or "again" in low or "redo" in low:
            intent["_sub_step"] = "planning"
            session.agent_intent = intent
            save_session(session)
            threading.Thread(target=_plan_bg, args=(phone, session, intent), daemon=True).start()
            return {"kind": "none"}
        return {"kind": "text", "text": "Reply *approve* to build the video, or *regenerate* to re-plan."}

    if sub_step == "awaiting_publish":
        if "regenerate" in low or "again" in low or "redo" in low or "rebuild" in low:
            intent["_sub_step"] = "building"
            session.agent_intent = intent
            save_session(session)
            _send(phone, {"kind": "text", "text": "🔁 Rebuilding your reel with fresh B-roll…"})
            threading.Thread(target=_build_bg, args=(phone, session, intent), daemon=True).start()
            return {"kind": "none"}
        if low in ("skip", "later", "draft", "no"):
            session.step = STEP_CHOOSE_CONTENT_TYPE
            session.agent_intent = None
            save_session(session)
            return {"kind": "text", "text": "👍 Saved as a draft. Type *create* whenever you want to make more."}
        if low in ("approve", "yes", "ok", "okay", "perfect", "love it", "done", "great",
                   "post", "post now", "publish", "go"):
            intent["_sub_step"] = "publishing"
            session.agent_intent = intent
            save_session(session)
            threading.Thread(target=_publish_bg, args=(phone, session, intent), daemon=True).start()
            return {"kind": "text", "text": "📤 Publishing your reel to Instagram…"}
        return {"kind": "text",
                "text": "Reply *post now* to publish to Instagram, *regenerate* to rebuild the reel, or *skip* to save as draft."}

    if sub_step == "publishing":
        return {"kind": "text", "text": "⏳ Publishing in progress — one moment!"}

    return start(phone, session, intent)


def _reap_tmp(max_age_h: float = 2.0) -> None:
    """Delete stale /tmp working dirs from crashed/old renders so the disk can't
    fill up over time (Remotion + ffmpeg leave temp dirs on failure)."""
    try:
        import glob, time, os as _os, shutil
        cutoff = time.time() - max_age_h * 3600
        for p in glob.glob("/tmp/tmp*"):
            try:
                if _os.path.getmtime(p) < cutoff:
                    shutil.rmtree(p, ignore_errors=True) if _os.path.isdir(p) else _os.remove(p)
            except Exception:
                pass
    except Exception:
        pass


def _start_planning(phone: str, session: UserSession, intent: dict) -> dict:
    """Kick off the matte→transcript→plan background worker."""
    intent["_sub_step"] = "planning"
    session.agent_intent = intent
    save_session(session)
    n = len(intent.get("_bg_images") or [])
    note = f"\n• Animating your {n} photo(s) as backgrounds" if n else ""
    _send(phone, {"kind": "text",
                  "text": "🎬 Building your reel:\n• Removing your background (green screen)\n"
                          "• Transcribing what you say\n• Planning the trending cut" + note + "\n⏱ ~2 minutes ☕"})
    threading.Thread(target=_plan_bg, args=(phone, session, intent), daemon=True).start()
    return {"kind": "none"}


def _plan_bg(phone: str, session: UserSession, intent: dict) -> None:
    try:
        src = intent.get("_src_video_url", "")
        brand = session.brand_profile() if session.onboarding_complete else {}

        # 1) Matting → green screen
        _send(phone, {"kind": "text", "text": "🟢 Removing your background..."})
        matte = video_gen.matte_video_greenscreen(src)
        if not matte.get("ok") or not matte.get("url"):
            raise RuntimeError(f"matting failed: {matte.get('error')}")
        # persist to S3
        import requests as _req
        gbytes = _req.get(matte["url"], timeout=120).content
        gup = aws_storage.upload_bytes(gbytes, content_type="video/mp4", extension="mp4",
                                       folder=f"{phone}/video_editor")
        intent["_greenscreen_video_url"] = gup.get("s3_url") or matte["url"]

        # 2) Transcript + word-level timing (for kinetic captions)
        _send(phone, {"kind": "text", "text": "📝 Transcribing what you say..."})
        transcript, words = voice.transcribe_video_words(src)
        if not transcript:
            transcript = voice.transcribe_video_url(src) or ""
        intent["_transcript"] = transcript
        intent["_words"] = words

        # 3) Duration (for planning)
        duration = _video_duration_seconds(src)
        intent["_duration"] = duration

        # 4) Edit plan
        _send(phone, {"kind": "text", "text": "🎬 Planning your trending cut..."})
        plan = groq_ai.generate_video_edit_plan(transcript, duration, brand)
        intent["_edit_plan"] = plan
        intent["_sub_step"] = "awaiting_plan_ok"
        session.agent_intent = intent
        save_session(session)

        # 5) Show the plan
        segs = plan.get("segments", [])
        lines = [f"🎬 *{plan.get('title','Your edit')}*", f"_{plan.get('story','')}_", ""]
        for i, s in enumerate(segs, 1):
            cap = s.get("caption") or ""
            zoom = s.get("zoom", "none")
            lines.append(f"*{i}.* {s.get('start',0):.0f}-{s.get('end',0):.0f}s · zoom:{zoom} · "
                         f"B-roll: _{(s.get('broll_prompt') or '')[:60]}_" + (f"\n   💬 {cap}" if cap else ""))
        lines.append(f"\n📣 {plan.get('cta','')}")
        lines.append("\n✅ Reply *approve* to build it, or *regenerate* to re-plan.")
        _send(phone, {"kind": "text", "text": "\n".join(lines)})
    except Exception as exc:
        logger.exception("video_editor _plan_bg failed: %s", exc)
        intent["_sub_step"] = "awaiting_video"
        session.agent_intent = intent
        save_session(session)
        _send(phone, {"kind": "text",
                      "text": f"😕 Couldn't analyze that video: {exc}\nSend a different video or type *reset*."})


_SCENE_CYCLE = ["grid", "solid", "cardboard", "split", "solid"]
_SOLID_COLORS = ["#E7B10A", "#7A1F2B", "#2E6E8E", "#C24914"]   # yellow, maroon, blue, orange
_ZOOM_CYCLE = ["in", "out", "in", "none"]


def _generate_stopmotion_broll(phone: str, segments: list[dict]) -> list[str]:
    """
    Generate a stop-motion style B-roll clip per segment via SeedDream → prunaai
    p-video, upload each to S3, and return the URLs (parallel, order-preserving).
    Missing/failed clips come back as "" so the scene falls back to a designed bg.
    """
    from tools import image_gen
    from concurrent.futures import ThreadPoolExecutor

    _STYLE = (" — stop motion animation style, claymation, handmade miniature set, "
              "tactile textures, stepped choppy stop-motion movement")

    def _gen(seg: dict) -> str:
        prompt = (seg.get("broll_prompt") or "cinematic establishing shot").strip()
        styled = prompt + _STYLE
        try:
            img = image_gen.generate_image(styled, aspect_ratio="2:3")
            seed = (img.get("s3_url") or img.get("url")) if img.get("ok") else None
            if not seed:
                logger.warning("video_editor: broll seed image failed: %s", img.get("error"))
                return ""
            seg_dur = max(2, int(round(seg["end"] - seg["start"])))
            # p-video only renders at fps 24/48 — the stop-motion cadence is baked in post.
            vid = video_gen.generate_video_from_image(
                seed, styled, duration=min(seg_dur, 10), aspect_ratio="9:16", fps=24)
            if not (vid.get("ok") and vid.get("bytes")):
                logger.warning("video_editor: p-video failed: %s", vid.get("error"))
                return ""
            clip = video_gen.apply_stopmotion(vid["bytes"], step_fps=12) or vid["bytes"]
            up = aws_storage.upload_bytes(clip, content_type="video/mp4",
                                          extension="mp4", folder=f"{phone}/video_editor/broll")
            return up.get("s3_url") or vid.get("url") or ""
        except Exception as exc:
            logger.warning("video_editor: stopmotion broll failed: %s", exc, exc_info=True)
        return ""

    with ThreadPoolExecutor(max_workers=3) as ex:
        return list(ex.map(_gen, segments))


_DOODLES = ("none", "arrow", "arrows", "circle", "underline", "highlighter",
            "box", "brackets", "stars", "action_lines", "check", "cross")
_ZOOMS = ("in", "out", "punch", "none")
_TRANSITIONS = ("none", "flash", "whip", "glitch", "shake")
_INFO_TYPES = ("none", "counter", "progress", "ring", "stat", "callout")
# Varied fallbacks — mostly clean beats (less-is-more) so it never feels busy
_DOODLE_FALLBACK = ("none", "circle", "none", "action_lines", "none", "underline", "none", "stars")
_TRANS_FALLBACK = ("flash", "whip", "flash", "glitch", "flash", "whip")


def _fallback_words(segments: list[dict]) -> list[dict]:
    """Build evenly-spaced word timings from segment captions when Whisper gave none."""
    out: list[dict] = []
    for seg in segments:
        cap = (seg.get("caption") or "").strip()
        toks = cap.split()
        if not toks:
            continue
        s, e = float(seg.get("start", 0)), float(seg.get("end", 0))
        if e <= s:
            e = s + 1.0
        step = (e - s) / len(toks)
        for j, tok in enumerate(toks):
            out.append({"start": s + j * step, "end": s + (j + 1) * step, "text": tok})
    return out


def _animate_images_to_broll(phone: str, segments: list[dict], images: list[str]) -> list[str]:
    """
    Animate the user's own photos (attached product shots or scraped Zillow
    listing images) into cinematic B-roll clips via Replicate p-video, and assign
    one to each segment (round-robin if there are fewer photos than segments).
    Real photos get smooth cinematic motion (not stop-motion).
    """
    from concurrent.futures import ThreadPoolExecutor
    _MOTION = ("smooth cinematic slow push-in with subtle parallax, gentle camera drift, "
               "premium real-estate / product showcase, photorealistic, no text")
    uniq = images[: min(len(images), max(1, len(segments)), 6)]

    def _gen(img_url: str) -> str:
        try:
            vid = video_gen.generate_video_from_image(
                img_url, _MOTION, duration=6, aspect_ratio="9:16", fps=24)
            if vid.get("ok") and vid.get("bytes"):
                up = aws_storage.upload_bytes(vid["bytes"], content_type="video/mp4",
                                              extension="mp4", folder=f"{phone}/video_editor/broll")
                return up.get("s3_url") or vid.get("url") or ""
        except Exception as exc:
            logger.warning("video_editor: image animation failed: %s", exc)
        return ""

    with ThreadPoolExecutor(max_workers=3) as ex:
        clips = list(ex.map(_gen, uniq))
    valid = [c for c in clips if c]
    if not valid:
        return []
    return [valid[i % len(valid)] for i in range(len(segments))]


def _plan_to_scenes(segments: list[dict], duration: float, broll_urls: list[str]) -> list[dict]:
    """
    Turn the edit-plan segments into scenes. The LLM chooses the per-scene overlays
    (doodle / big-text / zoom / lens / peak) so every reel is edited differently;
    we only fall back to a rotating default when the LLM left a field unset/invalid.
    Background = stop-motion B-roll video when available, else a designed background.
    """
    scenes: list[dict] = []
    solid_i = 0
    for i, seg in enumerate(segments):
        cap = (seg.get("caption") or "").strip()
        broll = broll_urls[i] if i < len(broll_urls) else ""
        peak = bool(seg.get("peak") or seg.get("emphasis"))

        # ── LLM-chosen styling, validated; varied fallback if missing ──
        doodle = str(seg.get("doodle", "")).lower().strip()
        if doodle not in _DOODLES:
            doodle = _DOODLE_FALLBACK[i % len(_DOODLE_FALLBACK)]
        # Arrows never look good here — remove them from every video.
        if doodle in ("arrow", "arrows"):
            doodle = "none"

        zoom = str(seg.get("zoom", "")).lower().strip()
        if zoom not in _ZOOMS:
            zoom = _ZOOM_CYCLE[i % len(_ZOOM_CYCLE)]
        if peak and zoom in ("none", ""):
            zoom = "punch"

        transition = str(seg.get("transition", "")).lower().strip()
        if transition not in _TRANSITIONS:
            transition = _TRANS_FALLBACK[i % len(_TRANS_FALLBACK)]

        # Icons/emoji and infographics are intentionally disabled — they made the
        # video feel cluttered. Captions + doodles + big-text only.
        emoji = ""
        info_out = {"type": "none"}

        big_llm = str(seg.get("big_text", "")).strip().upper()
        big_fallback = " ".join(cap.split()[:2]).upper() if cap else ""
        lens = bool(seg.get("lens", False))

        # ── ONE hero element per beat (elite discipline): infographic > big_text
        #    > doodle. Suppress the losers so overlays never stack. ──
        has_info = info_out.get("type") != "none"
        if has_info:
            doodle, emoji, big_llm, lens = "none", "", "", False
        elif big_llm:
            doodle, emoji = "none", ""

        scene = {
            "start": seg["start"], "end": seg["end"],
            "presenter": "full",
            "doodle": doodle,
            "emoji": emoji,
            "info": info_out,
            "transition": transition,
            "lens": lens,
            "zoom": zoom,
            "emphasis": peak,
            "bigText": big_llm,   # over video: only what the LLM explicitly asked for
            "color": "", "color2": "",
        }
        if broll:
            scene["bg"] = "broll"
            scene["brollSrc"] = broll
        else:
            bg = _SCENE_CYCLE[i % len(_SCENE_CYCLE)]
            scene["bg"] = bg
            if bg == "solid":
                scene["color"] = _SOLID_COLORS[solid_i % len(_SOLID_COLORS)]
                solid_i += 1
            elif bg == "split":
                scene["color"] = "#EDE6D6"
                scene["color2"] = "#E7B10A"
            # designed bgs look great with a big keyword; use LLM's or a fallback
            if bg in ("solid", "split"):
                scene["bigText"] = big_llm or big_fallback
        scenes.append(scene)
    return scenes


def _build_bg(phone: str, session: UserSession, intent: dict) -> None:
    """
    Build the Hormozi-style talking-head reel (all in Remotion):
      1. turn the user's green-screen video into a TRANSPARENT WebM (green → alpha)
      2. map the edit-plan segments to designed SCENES (grid / cardboard / solid /
         split backgrounds, big-text-behind, doodles, lens, zoom punch-ins)
      3. Remotion renders: designed backgrounds + presenter (full / sticker) +
         kinetic word captions + doodles + clean cut transitions + audio.
    Produces a single production-grade reel and sends it for post/regenerate/skip.
    """
    try:
        from tools import remotion_render
        _reap_tmp()   # self-heal: clear stale temp dirs before a heavy render
        plan     = intent.get("_edit_plan", {}) or {}
        segments = plan.get("segments", []) or []
        words    = intent.get("_words", []) or []
        gs_url   = intent.get("_greenscreen_video_url", "")
        src_url  = intent.get("_src_video_url", "")
        duration = float(intent.get("_duration") or 15.0)

        if not gs_url or not segments:
            raise RuntimeError("missing green-screen video or edit plan")

        # Clamp segment times to the real video length
        for s in segments:
            s["start"] = max(0.0, min(float(s.get("start", 0)), duration))
            s["end"]   = max(s["start"] + 0.5, min(float(s.get("end", duration)), duration))
        segments.sort(key=lambda s: s["start"])

        # Captions must ALWAYS appear. If Whisper word-timestamps came back empty,
        # synthesize word timings from each segment's caption spread over its window.
        if not words:
            words = _fallback_words(segments)

        # 1) Backgrounds: animate the user's photos / Zillow images if provided,
        #    otherwise generate stop-motion B-roll from the transcript.
        bg_images = intent.get("_bg_images") or []
        if bg_images:
            _send(phone, {"kind": "text", "text": f"🏠 Animating your {len(bg_images)} photo(s) into cinematic backgrounds… ⏱ a few minutes"})
            broll_urls = _animate_images_to_broll(phone, segments, bg_images)
            if not any(broll_urls):   # animation failed → fall back to generated B-roll
                broll_urls = _generate_stopmotion_broll(phone, segments)
        else:
            _send(phone, {"kind": "text", "text": f"🎥 Creating {len(segments)} B-roll background scenes… ⏱ a few minutes"})
            broll_urls = _generate_stopmotion_broll(phone, segments)
        scenes = _plan_to_scenes(segments, duration, broll_urls)

        # 2) Remotion back plate: background videos + big-text (opaque h264)
        _send(phone, {"kind": "text", "text": "🎨 Designing your backgrounds and big text…"})
        back = remotion_render.render_layer(scenes, words, duration, layer="back", caption_pos="bottom")
        if not back.get("ok") or not back.get("bytes"):
            raise RuntimeError(f"back-layer render failed: {back.get('error')}")

        # 3) ffmpeg: chroma-key the presenter over the back plate → opaque mid.mp4
        _send(phone, {"kind": "text", "text": "🟢 Cutting you out over the background…"})
        mid = video_gen.key_presenter_over(back["bytes"], gs_url, src_url)
        if not mid.get("ok") or not mid.get("bytes"):
            raise RuntimeError(f"presenter composite failed: {mid.get('error')}")
        mu = aws_storage.upload_bytes(mid["bytes"], content_type="video/mp4",
                                      extension="mp4", folder=f"{phone}/video_editor")
        mid_url = mu.get("s3_url") or mu.get("permanent_url")
        if not mid_url:
            raise RuntimeError("mid upload failed")

        # 4) Remotion front pass: mid video + doodles + lens + captions (opaque h264)
        _send(phone, {"kind": "text", "text": "✏️ Adding captions, doodles and effects…"})
        out = remotion_render.render_layer(scenes, words, duration, layer="front",
                                           bg_video=mid_url, caption_pos="bottom")
        if not out.get("ok") or not out.get("bytes"):
            raise RuntimeError(f"front-layer render failed: {out.get('error')}")

        # 5) Guarantee sound: mux the original voice (+ SFX) onto the final video.
        final_bytes = video_gen.finalize_audio(out["bytes"], src_url) or out["bytes"]
        logger.info("video_editor: final reel %.1f MB", len(final_bytes) / 1e6)
        # WhatsApp media cap is 16 MB — compress if we're over so it actually sends.
        if len(final_bytes) > 15 * 1024 * 1024:
            _send(phone, {"kind": "text", "text": "📦 Compressing for WhatsApp…"})
            final_bytes = video_gen.compress_for_whatsapp(final_bytes) or final_bytes
            logger.info("video_editor: compressed reel %.1f MB", len(final_bytes) / 1e6)

        up = aws_storage.upload_bytes(final_bytes, content_type="video/mp4",
                                      extension="mp4", folder=f"{phone}/video_editor")
        final_url = up.get("s3_url") or up.get("permanent_url")
        if not final_url:
            raise RuntimeError(f"S3 upload failed: {up.get('error')}")
        intent["_final_video_url"] = final_url
        intent["_sub_step"] = "awaiting_publish"
        session.agent_intent = intent
        save_session(session)

        # Deliver the video; always follow with the link as text so nothing is lost
        # even if Twilio rejects the media (size/type).
        _send(phone, {"kind": "media", "media_url": final_url,
                      "text": "🎬 Your studio reel is ready — you cut out over dynamic B-roll with cuts, zooms and captions!"})
        _send(phone, {"kind": "text",
                      "text": f"🔗 Direct link (if the video didn't load): {final_url}\n\n"
                              "Reply:\n✅ *post now* — publish to Instagram\n🔄 *regenerate* — rebuild the reel\n"
                              "⏭ *skip* — save as draft"})
    except Exception as exc:
        logger.exception("video_editor _build_bg failed: %s", exc)
        intent["_sub_step"] = "awaiting_plan_ok"
        session.agent_intent = intent
        save_session(session)
        _send(phone, {"kind": "text",
                      "text": f"😕 Couldn't build the reel: {exc}\nReply *approve* to try again or *regenerate* to re-plan."})


def _publish_bg(phone: str, session: UserSession, intent: dict) -> None:
    """STAGE 4: publish the final trending cut to Instagram via Zerini."""
    import db
    from tools import zerini
    try:
        plan      = intent.get("_edit_plan", {}) or {}
        final_url = intent.get("_final_video_url", "")
        if not final_url:
            raise RuntimeError("no final video to publish")

        # Caption: the plan's story + CTA, or fall back to the transcript.
        caption = "\n\n".join(p for p in [
            (plan.get("story") or "").strip(),
            (plan.get("cta") or "").strip(),
        ] if p) or (intent.get("_transcript") or "").strip()[:500]

        result = zerini.publish_now(
            account_id=session.zerini_account_id or "",
            image_urls=[final_url],
            caption=caption,
            content_type="reel",
            profile_id=session.zerini_profile_id or "",
        )
        db.log_post(phone_number=phone, content_type="reel", image_urls=[final_url],
                    caption=caption, prompts=[], status="published" if result.get("ok") else "failed")

        session.step = STEP_CHOOSE_CONTENT_TYPE
        session.agent_intent = None
        save_session(session)
        if result.get("ok"):
            _send(phone, {"kind": "text", "text": "✅ *Posted to Instagram!* 🎉 What would you like to create next?"}, tts=True)
        else:
            _send(phone, {"kind": "text", "text": f"😕 Publish failed: {result.get('error')}\nYour video is saved — try again anytime."})
    except Exception as exc:
        logger.exception("video_editor _publish_bg failed: %s", exc)
        intent["_sub_step"] = "awaiting_publish"
        session.agent_intent = intent
        save_session(session)
        _send(phone, {"kind": "text", "text": f"😕 Publish failed: {exc}\nReply *post now* to try again or *skip* to save as draft."})


def _video_duration_seconds(url: str) -> float:
    """Best-effort duration via moviepy; falls back to 15s."""
    try:
        import tempfile, os as _os, requests as _req
        from moviepy.editor import VideoFileClip
        tmp = tempfile.mktemp(suffix=".mp4")
        with open(tmp, "wb") as f:
            f.write(_req.get(url, timeout=120).content)
        clip = VideoFileClip(tmp)
        d = float(clip.duration or 15.0)
        clip.close(); _os.remove(tmp)
        return d
    except Exception:
        return 15.0
