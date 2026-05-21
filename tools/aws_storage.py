"""Upload files and remote URLs to AWS S3, returning presigned URLs for public access."""

from __future__ import annotations

import mimetypes
import os
import uuid
from io import BytesIO
from urllib.parse import urlparse

import boto3
import requests

import config


# Presigned URLs are valid for 7 days — long enough for WhatsApp/Zernio to fetch the media.
_PRESIGNED_EXPIRY = 7 * 24 * 3600  # 604800 seconds


def _s3_client():
    return boto3.client(
        "s3",
        region_name=config.AWS_REGION,
        aws_access_key_id=config.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
    )


def _ext_from_url(url: str, content_type: str | None = None) -> str:
    path = urlparse(url).path
    _, ext = os.path.splitext(path)
    if ext:
        return ext.lower()
    if content_type:
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ".bin"
        return ext
    return ".jpg"


def _presign(s3_key: str) -> str:
    """Generate a presigned GET URL valid for 7 days."""
    s3 = _s3_client()
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": config.AWS_BUCKET_NAME, "Key": s3_key},
        ExpiresIn=_PRESIGNED_EXPIRY,
    )


def _download(url: str, content_type: str | None = None) -> tuple[bytes, str]:
    """
    Download a URL, automatically adding Twilio Basic Auth for Twilio media URLs.
    Returns (raw_bytes, detected_content_type).
    """
    auth = None
    if "twilio.com" in url:
        auth = (config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)

    resp = requests.get(url, timeout=60, stream=True, auth=auth)
    resp.raise_for_status()
    detected_ct = resp.headers.get("Content-Type", content_type or "image/jpeg")
    return resp.content, detected_ct


def upload_from_url(
    source_url: str,
    user_id: str,
    media_kind: str = "post",
    content_type: str | None = None,
    public: bool = False,  # kept for API compat; ACL is never set (bucket disallows it)
) -> dict:
    """
    Download a remote URL (including authenticated Twilio media URLs) and re-upload to S3.
    Returns a 7-day presigned URL in s3_url (usable by AI models and Twilio).
    Returns:
      {"ok": True, "s3_url": "<7-day presigned>", "s3_key": "...", "permanent_url": "<presigned>"}
      {"ok": False, "error": "..."}
    """
    try:
        data, detected_ct = _download(source_url, content_type)
        ext = _ext_from_url(source_url, detected_ct)
    except Exception as exc:
        return {"ok": False, "error": f"Download failed: {exc}"}

    filename = f"{uuid.uuid4().hex}{ext}"
    s3_key = f"{config.AWS_BASE_DIR}/{user_id}/{media_kind}/{filename}"
    try:
        s3 = _s3_client()
        s3.upload_fileobj(
            BytesIO(data),
            config.AWS_BUCKET_NAME,
            s3_key,
            ExtraArgs={"ContentType": detected_ct.split(";")[0].strip()},
        )
        presigned = _presign(s3_key)
        return {"ok": True, "s3_url": presigned, "s3_key": s3_key, "permanent_url": presigned}
    except Exception as exc:
        return {"ok": False, "error": f"S3 upload failed: {exc}"}


def upload_bytes(
    data: bytes,
    content_type: str = "audio/mpeg",
    extension: str = "mp3",
    folder: str = "voice",
    public: bool = False,  # kept for API compat; ACL is never set (bucket disallows it)
) -> dict:
    """
    Upload raw bytes to S3 under a generated key.
    Returns a 7-day presigned URL (usable by AI models and Twilio).
    Returns {"ok": True, "s3_url": "<7-day presigned>", "s3_key": "...", "permanent_url": "<presigned>"}
    """
    filename = f"{uuid.uuid4().hex}.{extension}"
    s3_key = f"{config.AWS_BASE_DIR}/{folder}/{filename}"
    try:
        s3 = _s3_client()
        s3.upload_fileobj(
            BytesIO(data),
            config.AWS_BUCKET_NAME,
            s3_key,
            ExtraArgs={"ContentType": content_type},
        )
        presigned = _presign(s3_key)
        return {"ok": True, "s3_url": presigned, "s3_key": s3_key, "permanent_url": presigned}
    except Exception as exc:
        return {"ok": False, "error": f"S3 upload failed: {exc}"}


def upload_urls(urls: list[str], user_id: str, media_kind: str = "post") -> dict:
    """
    Upload multiple remote URLs to S3.
    Returns {"ok": True, "s3_urls": [<presigned>, ...], "s3_keys": [...]}
    """
    s3_urls: list[str] = []
    s3_keys: list[str] = []
    for url in urls:
        result = upload_from_url(url, user_id, media_kind)
        if not result.get("ok"):
            return result
        s3_urls.append(result["s3_url"])
        s3_keys.append(result["s3_key"])
    return {"ok": True, "s3_urls": s3_urls, "s3_keys": s3_keys}
