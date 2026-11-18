"""Serves locally stored files by signed URL (development; production uses S3 presigned URLs)."""

import mimetypes

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.services import S3Service

router = APIRouter(tags=["files"])


@router.get("/files/{key:path}")
async def read_file(key: str, exp: int, sig: str):
    data = S3Service.local_read(key, exp, sig)
    if data is None:
        raise HTTPException(404, "This link has expired.")
    return Response(data, media_type=mimetypes.guess_type(key)[0] or "application/octet-stream",
                    headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"})
