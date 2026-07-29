"""
Zernio social media scheduling integration.  https://zernio.com

Auth   : Authorization: Bearer ZERINI_API_KEY
Base   : https://zernio.com/api  (paths start with /v1/...)
Scope  : all calls include profileId = ZERINI_PROFILE_ID

Key endpoints used:
  GET  /v1/accounts          → list connected social accounts
  POST /v1/posts             → publish now or schedule a post
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import requests

import config


_BASE = "https://zernio.com/api"


def _headers() -> dict:
    if not config.ZERINI_API_KEY:
        raise RuntimeError(
            "ZERINI_API_KEY is not set in .env — get your key at https://zernio.com/dashboard/api-keys"
        )
    return {
        "Authorization": f"Bearer {config.ZERINI_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _url(path: str) -> str:
    return f"{_BASE}/{path.lstrip('/')}"


# ---------------------------------------------------------------------------
# Account lookup
# ---------------------------------------------------------------------------

def find_instagram_account_by_username(username: str) -> dict:
    """
    Search all connected accounts in Zernio for an Instagram account whose
    username matches `username` (case-insensitive, with or without leading @).

    Returns:
        {"ok": True,  "found": True,  "account_id": "...", "profile_id": "...", "username": "..."}
        {"ok": True,  "found": False, "error": "Not found ..."}
        {"ok": False, "error": "API error ..."}
    """
    if not config.ZERINI_API_KEY:
        return {"ok": False, "found": False, "error": "ZERINI_API_KEY not configured."}

    handle = username.lstrip("@").strip().lower()
    if not handle:
        return {"ok": False, "found": False, "error": "Instagram username is empty."}

    try:
        # No profileId filter — searches across ALL profiles in the workspace
        resp = requests.get(
            _url("/v1/accounts"),
            headers=_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.HTTPError as exc:
        return {"ok": False, "found": False,
                "error": f"Account lookup HTTP {exc.response.status_code}: {exc.response.text[:200]}"}
    except Exception as exc:
        return {"ok": False, "found": False, "error": str(exc)}

    raw: list[dict] = data.get("accounts") or (data if isinstance(data, list) else [])

    for account in raw:
        if str(account.get("platform") or "").lower() != "instagram":
            continue
        acct_username = str(account.get("username") or "").lstrip("@").lower()
        acct_display = str(account.get("displayName") or "").lstrip("@").lower()
        if handle in (acct_username, acct_display):
            account_id = str(account.get("_id") or account.get("id") or "")
            # Use the profileId attached to the account itself — no .env needed
            profile_id = str(account.get("profileId") or config.ZERINI_PROFILE_ID or "")
            display = account.get("username") or account.get("displayName") or handle
            return {
                "ok": True,
                "found": True,
                "account_id": account_id,
                "profile_id": profile_id,
                "username": display.lstrip("@"),
                "is_active": account.get("isActive", True),
            }

    return {
        "ok": True,
        "found": False,
        "error": (
            f"No Instagram account '@{handle}' found in your connected accounts. "
            f"Make sure it's connected to your Foundabee workspace and the username matches exactly."
        ),
    }


def get_instagram_accounts() -> dict:
    """
    Fetch all connected accounts from Zernio and return those on Instagram.

    Returns:
        {"ok": True,  "accounts": [...]}   — accounts is a list of Zernio account dicts
        {"ok": False, "error": "...", "accounts": []}
    """
    if not config.ZERINI_API_KEY:
        return {"ok": False, "error": "ZERINI_API_KEY not configured.", "accounts": []}

    params: dict[str, str] = {}
    if config.ZERINI_PROFILE_ID:
        params["profileId"] = config.ZERINI_PROFILE_ID

    try:
        resp = requests.get(
            _url("/v1/accounts"),
            headers=_headers(),
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.HTTPError as exc:
        return {"ok": False, "error": f"Zernio accounts HTTP {exc.response.status_code}: {exc.response.text[:200]}", "accounts": []}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "accounts": []}

    # Response: {"accounts": [...]}
    raw: list[dict] = data.get("accounts") or (data if isinstance(data, list) else [])
    instagram = [
        a for a in raw
        if str(a.get("platform") or "").lower() == "instagram"
        and a.get("isActive", True)
    ]
    return {"ok": True, "accounts": instagram}


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

def get_accounts(profile_id: str | None = None) -> dict:
    """All ACTIVE connected accounts (every platform) for a profile / workspace."""
    if not config.ZERINI_API_KEY:
        return {"ok": False, "error": "ZERINI_API_KEY not configured.", "accounts": []}
    params: dict[str, str] = {}
    pid = profile_id or config.ZERINI_PROFILE_ID
    if pid:
        params["profileId"] = pid
    try:
        resp = requests.get(_url("/v1/accounts"), headers=_headers(), params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.HTTPError as exc:
        return {"ok": False, "error": f"accounts HTTP {exc.response.status_code}: {exc.response.text[:200]}", "accounts": []}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "accounts": []}
    raw: list[dict] = data.get("accounts") or (data if isinstance(data, list) else [])
    active = [a for a in raw if a.get("isActive", True)]
    return {"ok": True, "accounts": active}


_PLATFORM_LABEL = {"instagram": "Instagram", "facebook": "Facebook", "tiktok": "TikTok",
                   "youtube": "YouTube", "linkedin": "LinkedIn", "twitter": "X", "x": "X",
                   "threads": "Threads", "pinterest": "Pinterest"}


def fmt_platforms(names: list[str]) -> str:
    """'instagram','facebook','tiktok' → 'Instagram, Facebook & TikTok'."""
    labels = [_PLATFORM_LABEL.get((n or "").lower(), (n or "").title()) for n in (names or []) if n]
    labels = list(dict.fromkeys(labels))
    if not labels:
        return "your socials"
    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:-1]) + " & " + labels[-1]


def _platform_content_type(platform: str, content_type: str) -> str:
    """Best-fit per-platform contentType so each network gets the right post kind."""
    p = (platform or "").lower()
    if content_type == "reel":  # a video
        return {"instagram": "reel", "facebook": "reel", "youtube": "short",
                "tiktok": "video", "threads": "feed", "pinterest": "pin",
                "linkedin": "feed", "twitter": "feed", "x": "feed"}.get(p, "feed")
    if content_type == "carousel":
        return "carousel" if p in ("instagram", "facebook", "tiktok") else "feed"
    return "feed"


def _platforms_from_accounts(accounts: list[dict], content_type: str) -> list[dict]:
    plats: list[dict] = []
    for a in accounts:
        p = str(a.get("platform") or "").lower()
        aid = str(a.get("_id") or a.get("id") or a.get("accountId") or "")
        if not p or not aid:
            continue
        plats.append({
            "platform": p,
            "accountId": aid,
            "platformSpecificData": {"contentType": _platform_content_type(p, content_type)},
        })
    return plats


def _build_post_body(
    account_id: str,
    image_urls: list[str],
    caption: str,
    content_type: str,
    publish_now: bool,
    scheduled_at: datetime | None,
    profile_id: str | None = None,
    music: dict | None = None,          # {"name": "...", "artist": "..."}
    platforms: list[dict] | None = None,
) -> dict:
    # Reels need media type "video"; everything else is "image"
    media_item_type = "video" if content_type == "reel" else "image"
    media_items = [{"type": media_item_type, "url": u} for u in image_urls]

    # If an explicit multi-platform list wasn't provided, fall back to Instagram only.
    if not platforms:
        platforms = [{
            "platform": "instagram",
            "accountId": account_id,
            "platformSpecificData": {"contentType": _platform_content_type("instagram", content_type)},
        }]

    # NOTE: Instagram's music library cannot be attached via the API (including Zernio).

    body: dict[str, Any] = {
        "content": caption,
        "publishNow": publish_now,
        "isDraft": False,
        "platforms": platforms,
        "mediaItems": media_items,
    }

    resolved_profile = profile_id or config.ZERINI_PROFILE_ID
    if resolved_profile:
        body["profileId"] = resolved_profile

    if not publish_now and scheduled_at:
        body["scheduledFor"] = scheduled_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        body["timezone"] = "UTC"

    return body


_POST_TIMEOUT  = 90   # seconds — Zernio queues with Instagram which can be slow
_POST_RETRIES  = 3
_POST_BACKOFF  = [10, 25, 50]


def _is_retryable_post_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(t in msg for t in (
        "timed out", "timeout", "read timeout", "connect timeout",
        "502", "503", "504", "bad gateway", "service unavailable",
        "connection", "remotedisconnected",
    ))


def _post_with_retry(body: dict) -> dict:
    """POST to /v1/posts with retry on transient errors. Returns raw response data or raises."""
    last_exc: Exception | None = None
    for attempt in range(_POST_RETRIES):
        try:
            resp = requests.post(_url("/v1/posts"), headers=_headers(), json=body, timeout=_POST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as exc:
            # Non-retryable HTTP error (4xx etc.)
            raise
        except Exception as exc:
            last_exc = exc
            if not _is_retryable_post_error(exc) or attempt == _POST_RETRIES - 1:
                raise
            import time as _time
            _time.sleep(_POST_BACKOFF[attempt])
    raise last_exc  # unreachable but satisfies type checker


def _resolve_platforms(account_id: str, profile_id: str | None, content_type: str,
                       all_platforms: bool) -> tuple[list[dict], list[str]]:
    """Build the target platform list. When all_platforms, post to EVERY connected
    account for the profile; otherwise just the given Instagram account. Returns
    (platforms, platform_names)."""
    if all_platforms:
        acc = get_accounts(profile_id)
        if acc.get("ok") and acc.get("accounts"):
            plats = _platforms_from_accounts(acc["accounts"], content_type)
            if plats:
                return plats, [p["platform"] for p in plats]
    # fallback: single instagram account
    plats = [{
        "platform": "instagram", "accountId": account_id,
        "platformSpecificData": {"contentType": _platform_content_type("instagram", content_type)},
    }]
    return plats, ["instagram"]


def publish_now(
    account_id: str,
    image_urls: list[str],
    caption: str,
    content_type: str = "image_post",
    profile_id: str | None = None,
    music: dict | None = None,
    all_platforms: bool = True,
) -> dict:
    """
    Publish a post immediately via Zernio to ALL connected social platforms for the
    profile (Instagram, Facebook, TikTok, YouTube, LinkedIn, X, etc.) by default.
    Returns {"ok": True, "post_id": "...", "platforms": [...]} or {"ok": False, "error": "..."}.
    """
    try:
        platforms, names = _resolve_platforms(account_id, profile_id, content_type, all_platforms)
        body = _build_post_body(
            account_id, image_urls, caption, content_type,
            publish_now=True, scheduled_at=None, profile_id=profile_id, music=music,
            platforms=platforms,
        )
        data = _post_with_retry(body)
        post_id = str(data.get("_id") or data.get("id") or data.get("postId") or "")
        return {"ok": True, "post_id": post_id, "data": data, "platforms": names}
    except requests.exceptions.HTTPError as exc:
        return {"ok": False, "error": f"Publishing HTTP {exc.response.status_code}: {exc.response.text[:300]}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def schedule_post(
    account_id: str,
    image_urls: list[str],
    caption: str,
    scheduled_at: datetime,
    content_type: str = "image_post",
    profile_id: str | None = None,
    music: dict | None = None,
    all_platforms: bool = True,
) -> dict:
    """
    Schedule a post for a future date/time via Zernio, to ALL connected platforms
    for the profile by default.
    Returns {"ok": True, "post_id": "...", "platforms": [...]} or {"ok": False, "error": "..."}.
    """
    try:
        platforms, names = _resolve_platforms(account_id, profile_id, content_type, all_platforms)
        body = _build_post_body(
            account_id, image_urls, caption, content_type,
            publish_now=False, scheduled_at=scheduled_at, profile_id=profile_id, music=music,
            platforms=platforms,
        )
        data = _post_with_retry(body)
        post_id = str(data.get("_id") or data.get("id") or data.get("postId") or "")
        return {"ok": True, "post_id": post_id, "data": data, "platforms": names}
    except requests.exceptions.HTTPError as exc:
        return {"ok": False, "error": f"Scheduling HTTP {exc.response.status_code}: {exc.response.text[:300]}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
