"""Private file storage. The shared S3 bucket under the SH_S3_PREFIX key prefix, or a local folder
when no bucket is configured (development and tests). Files are always served by short-lived signed
URLs; nothing is public by default."""

import hashlib
import hmac
import mimetypes
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from app.core.config import settings

LOCAL_ROOT = Path(os.getenv("SH_LOCAL_STORAGE", "storage"))

ALLOWED = {
    "image": {"image/png": b"\x89PNG", "image/jpeg": b"\xff\xd8\xff", "image/webp": b"RIFF"},
    "document": {"application/pdf": b"%PDF"},
}
MAX_BYTES = {"image": 15 * 1024 * 1024, "document": 20 * 1024 * 1024}


class UploadRejected(ValueError):
    pass


def use_s3() -> bool:
    return bool(settings.AWS_S3_BUCKET and settings.AWS_ACCESS_KEY_ID) and os.getenv("SH_STORAGE", "") != "local"


def _client():
    import boto3

    return boto3.client("s3", region_name=settings.AWS_REGION, aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY)


def check_file(data: bytes, content_type: str, kinds: tuple[str, ...]) -> str:
    """Type by content (not just the name) and size. Returns the kind. A virus-scan hook would go here."""
    for kind in kinds:
        magic = ALLOWED[kind].get(content_type)
        if magic is None:
            continue
        if not data.startswith(magic):
            raise UploadRejected("The file doesn't match its type.")
        if len(data) > MAX_BYTES[kind]:
            raise UploadRejected(f"Files can be up to {MAX_BYTES[kind] // (1024 * 1024)} MB.")
        return kind
    allowed = ", ".join(sorted({t.split('/')[1].upper() for k in kinds for t in ALLOWED[k]}))
    raise UploadRejected(f"Upload a {allowed} file.")


def put(folder: str, filename: str, data: bytes, content_type: str) -> str:
    ext = mimetypes.guess_extension(content_type) or Path(filename).suffix or ""
    key = f"{settings.SH_S3_PREFIX}/{folder}/{uuid.uuid4().hex}{ext}"
    if use_s3():
        _client().put_object(Bucket=settings.AWS_S3_BUCKET, Key=key, Body=data, ContentType=content_type,
                             ServerSideEncryption="AES256")
    else:
        path = LOCAL_ROOT / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return key


def _sign(key: str, exp: int) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), f"{key}:{exp}".encode(), hashlib.sha256).hexdigest()


def signed_url(key: Optional[str], seconds: int = 300, api_base: str = "") -> Optional[str]:
    if not key:
        return None
    if key.startswith("/") or key.startswith("http"):
        return key  # a bundled asset path, not a stored file
    if use_s3():
        return _client().generate_presigned_url("get_object", Params={"Bucket": settings.AWS_S3_BUCKET, "Key": key},
                                                ExpiresIn=seconds)
    exp = int(time.time()) + seconds
    return f"{api_base}/api/v1/files/{key}?exp={exp}&sig={_sign(key, exp)}"


def local_read(key: str, exp: int, sig: str) -> Optional[bytes]:
    if exp < time.time() or not hmac.compare_digest(_sign(key, exp), sig):
        return None
    path = (LOCAL_ROOT / key).resolve()
    if LOCAL_ROOT.resolve() not in path.parents or not path.exists():
        return None
    return path.read_bytes()


def read_bytes(key: str) -> Optional[bytes]:
    """The stored file itself, for the few public images we serve through the API (post previews)."""
    if not key or key.startswith("/") or key.startswith("http"):
        return None
    if use_s3():
        try:
            return _client().get_object(Bucket=settings.AWS_S3_BUCKET, Key=key)["Body"].read()
        except Exception:
            return None
    path = (LOCAL_ROOT / key).resolve()
    if LOCAL_ROOT.resolve() not in path.parents or not path.exists():
        return None
    return path.read_bytes()
