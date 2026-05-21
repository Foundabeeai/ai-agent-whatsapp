#!/usr/bin/env python3
"""
One-time script to upload Maya and George avatar images to S3
and print the .env lines to paste into your .env file.

Usage:
  1. Save the two avatar images as:
       george.jpg   (man in Apex Fitness shirt)
       maya.jpg     (woman in white turtleneck)
     — put them in the same folder as this script, OR pass full paths as args.

  2. Run:
       python scripts/upload_avatars.py
     or:
       python scripts/upload_avatars.py /path/to/george.jpg /path/to/maya.jpg

  3. Copy the two AVATAR_*_URL lines printed at the end into your .env file.
"""

import os
import sys
import uuid
from io import BytesIO
from pathlib import Path

# ── locate project root (one level up from scripts/) ──────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402 — needs sys.path set first
import boto3   # noqa: E402


# ── resolve image paths ────────────────────────────────────────────────────
SCRIPTS_DIR = Path(__file__).resolve().parent

if len(sys.argv) == 3:
    george_path = Path(sys.argv[1])
    maya_path   = Path(sys.argv[2])
else:
    george_path = SCRIPTS_DIR / "george.jpg"
    maya_path   = SCRIPTS_DIR / "maya.jpg"

for p in (george_path, maya_path):
    if not p.exists():
        print(f"❌  File not found: {p}")
        print()
        print("Place the images as:")
        print(f"   {SCRIPTS_DIR / 'george.jpg'}  (man in Apex Fitness shirt)")
        print(f"   {SCRIPTS_DIR / 'maya.jpg'}    (woman in white turtleneck)")
        print()
        print("Or pass paths directly:")
        print("   python scripts/upload_avatars.py /path/to/george.jpg /path/to/maya.jpg")
        sys.exit(1)


# ── upload helper ──────────────────────────────────────────────────────────
_EXPIRY = 365 * 24 * 3600  # 1-year presigned URL

def upload(local_path: Path, name: str) -> str:
    s3 = boto3.client(
        "s3",
        region_name=config.AWS_REGION,
        aws_access_key_id=config.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
    )
    ext = local_path.suffix.lower() or ".jpg"
    s3_key = f"{config.AWS_BASE_DIR}/avatars/{name}{ext}"
    data = local_path.read_bytes()

    content_type = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"
    s3.upload_fileobj(
        BytesIO(data),
        config.AWS_BUCKET_NAME,
        s3_key,
        ExtraArgs={"ContentType": content_type},
    )
    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": config.AWS_BUCKET_NAME, "Key": s3_key},
        ExpiresIn=_EXPIRY,
    )
    return url


# ── run ────────────────────────────────────────────────────────────────────
print("Uploading George…", end=" ", flush=True)
george_url = upload(george_path, "george")
print("✅")

print("Uploading Maya…  ", end=" ", flush=True)
maya_url = upload(maya_path, "maya")
print("✅")

print()
print("=" * 60)
print("Add these two lines to your .env file:")
print("=" * 60)
print(f'AVATAR_GEORGE_URL="{george_url}"')
print(f'AVATAR_MAYA_URL="{maya_url}"')
print("=" * 60)
