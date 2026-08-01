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


def reap_local_temp(max_age_h: float = 1.0) -> int:
    """
    Delete stale local temp working files/dirs (Remotion + ffmpeg scratch under
    /tmp/tmp*) once media has already been uploaded to S3 and delivered. Only
    touches items older than `max_age_h` so it never disturbs an in-flight render.
    Returns the number of items removed. Safe to call anytime; never raises.
    """
    import glob, time, shutil, tempfile
    removed = 0
    cutoff = time.time() - max_age_h * 3600
    scratch = tempfile.gettempdir()   # honours config's redirected TMPDIR
    for p in glob.glob(os.path.join(scratch, "tmp*")):
        try:
            if os.path.getmtime(p) >= cutoff:
                continue
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                os.remove(p)
            removed += 1
        except Exception:
            pass
    return removed


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


def _presign(s3_key: str, bucket: str | None = None) -> str:
    """Generate a presigned GET URL valid for 7 days."""
    s3 = _s3_client()
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket or config.AWS_BUCKET_NAME, "Key": s3_key},
        ExpiresIn=_PRESIGNED_EXPIRY,
    )


def bucket_and_key_from_url(url: str) -> tuple[str, str] | None:
    """Extract (bucket, key) from one of our own S3 URLs (virtual- or path-style)."""
    from urllib.parse import urlparse, unquote
    try:
        p = urlparse(url)
        host, path = p.netloc, unquote(p.path.lstrip("/"))
        if "amazonaws.com" not in host and "s3" not in host:
            return None
        if ".s3" in host:                      # virtual-hosted: {bucket}.s3[.region].amazonaws.com/{key}
            return host.split(".s3", 1)[0], path
        # path-style: s3[.region].amazonaws.com/{bucket}/{key}
        if "/" in path:
            b, k = path.split("/", 1)
            return b, k
        return None
    except Exception:
        return None


def key_from_url(url: str) -> str | None:
    bk = bucket_and_key_from_url(url)
    return bk[1] if bk else None


def refresh_presigned(url: str) -> str | None:
    """Re-sign one of our S3 URLs from its key → a fresh 7-day URL (fixes expiry).
    Returns None if the URL isn't one of ours or the object no longer exists."""
    bk = bucket_and_key_from_url(url)
    if not bk:
        return None
    bucket, key = bk
    try:
        _s3_client().head_object(Bucket=bucket, Key=key)  # 404/403 if gone
        return _presign(key, bucket=bucket)
    except Exception:
        return None


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
    bucket: str | None = None,
) -> dict:
    """
    Download a remote URL (including authenticated Twilio media URLs) and re-upload to S3.
    Returns a 7-day presigned URL in s3_url (usable by AI models and Twilio).
    Pass bucket=config.AWS_ASSETS_BUCKET for assets that must never auto-expire.
    """
    try:
        data, detected_ct = _download(source_url, content_type)
        ext = _ext_from_url(source_url, detected_ct)
    except Exception as exc:
        return {"ok": False, "error": f"Download failed: {exc}"}

    target = bucket or config.AWS_BUCKET_NAME
    filename = f"{uuid.uuid4().hex}{ext}"
    s3_key = f"{config.AWS_BASE_DIR}/{user_id}/{media_kind}/{filename}"
    try:
        s3 = _s3_client()
        s3.upload_fileobj(
            BytesIO(data), target, s3_key,
            ExtraArgs={"ContentType": detected_ct.split(";")[0].strip()},
        )
        presigned = _presign(s3_key, bucket=target)
        return {"ok": True, "s3_url": presigned, "s3_key": s3_key, "permanent_url": presigned}
    except Exception as exc:
        return {"ok": False, "error": f"S3 upload failed: {exc}"}


def upload_bytes(
    data: bytes,
    content_type: str = "audio/mpeg",
    extension: str = "mp3",
    folder: str = "voice",
    public: bool = False,  # kept for API compat; ACL is never set (bucket disallows it)
    bucket: str | None = None,
) -> dict:
    """
    Upload raw bytes to S3 under a generated key.
    Returns a 7-day presigned URL (usable by AI models and Twilio).
    Pass bucket=config.AWS_ASSETS_BUCKET for assets that must never auto-expire.
    """
    target = bucket or config.AWS_BUCKET_NAME
    filename = f"{uuid.uuid4().hex}.{extension}"
    s3_key = f"{config.AWS_BASE_DIR}/{folder}/{filename}"
    try:
        s3 = _s3_client()
        s3.upload_fileobj(BytesIO(data), target, s3_key, ExtraArgs={"ContentType": content_type})
        presigned = _presign(s3_key, bucket=target)
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
