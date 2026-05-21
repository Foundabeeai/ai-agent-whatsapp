"""Verify a user's email against the Foundabee registration API."""

from __future__ import annotations

import time
from urllib.parse import quote

import requests

import config


def check_user_registration(email: str) -> dict:
    """
    Returns dict with keys: ok, registered, enterprise, user_id, email, message/error.
    """
    email = (email or "").strip()
    if not email:
        return {"ok": False, "email": email, "registered": False, "enterprise": False,
                "error": "No email provided."}

    url = f"{config.CHECK_USER_BASE_URL.rstrip('/')}/{quote(email, safe='')}"
    headers = {
        "X-Integration-Key": config.INTEGRATION_LOOKUP_API_KEY,
        "Accept": "application/json",
        "User-Agent": "foundabee-whatsapp-automation/1.0",
    }
    timeout = (config.CHECK_USER_CONNECT_TIMEOUT, config.CHECK_USER_TIMEOUT)

    response = None
    for attempt in range(max(1, config.CHECK_USER_MAX_ATTEMPTS)):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            break
        except requests.exceptions.Timeout:
            if attempt + 1 < config.CHECK_USER_MAX_ATTEMPTS:
                time.sleep(0.75)
                continue
            return {"ok": False, "email": email, "registered": False, "enterprise": False,
                    "error": "Registration check timed out."}
        except requests.exceptions.RequestException as exc:
            return {"ok": False, "email": email, "registered": False, "enterprise": False,
                    "error": f"Could not reach registration service: {exc}"}

    if response is None:
        return {"ok": False, "email": email, "registered": False, "enterprise": False,
                "error": "No response from registration service."}

    if response.status_code == 404:
        return {"ok": True, "email": email, "registered": False, "enterprise": False,
                "user_id": None, "message": "No active Foundabee user found for that email."}

    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        return {"ok": False, "email": email, "registered": False, "enterprise": False,
                "error": f"HTTP {response.status_code}: {response.text[:200]}"}

    try:
        data = response.json()
    except ValueError:
        return {"ok": False, "email": email, "registered": False, "enterprise": False,
                "error": "Non-JSON response from registration service."}

    # Foundabee integration lookup shape: { "found": bool, "plan": str, "user_id": str }
    registered = bool(data.get("found", False))
    plan = str(data.get("plan") or "").strip().lower()
    enterprise = registered and ("enterprise" in plan)
    user_id = str(data.get("user_id") or data.get("id") or "").strip() or None

    return {
        "ok": True,
        "email": email,
        "registered": registered,
        "enterprise": enterprise,
        "user_id": user_id,
        "message": data.get("message") or ("Verified." if registered else "Not registered."),
        "raw": data,
    }
