"""Stream timestamps for clippers: streamers post their own, clippers claim and cut them, management sees it all."""

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import Access, current_access, require_role
from app.core.database import aget_db
from app.models.clips import Clip, ClipperProfile
from app.models.talent import MarkedMoment, MomentClaim, Stream, Streamer
from app.services import MomentService as M
from app.services.AuditService import audit

router = APIRouter(tags=["moments"])
MANAGEMENT = ("command", "production", "social", "talent")


class MomentIn(BaseModel):
    stream_id: str
    at: str = Field(min_length=1, max_length=12)  # 1:23:45, 23:45, 83m or seconds
    label: str = Field(min_length=2, max_length=160)
    kind: str = "highlight"
    note: Optional[str] = Field(default=None, max_length=300)


async def _my_streamer(db: AsyncSession, access: Access) -> Streamer:
    s = await db.scalar(select(Streamer).where(Streamer.user_id == access.user.id))
    if not s:
        raise HTTPException(403, "Your streamer profile isn't set up yet. Ask the Talent team.")
    return s


# The streamer's own timestamps ------------------------------------------------------------------------------

@router.get("/talent/moments")
async def my_moments(access: Access = Depends(require_role("streamer")), db: AsyncSession = Depends(aget_db)):
    star = await _my_streamer(db, access)
    streams = await M.recent_streams(db, star.id)
    return {"streams": await M.board(db, streams, access.user), "kinds": M.KINDS}


@router.post("/talent/moments")
async def post_moment(body: MomentIn, request: Request, access: Access = Depends(require_role("streamer")),
                      db: AsyncSession = Depends(aget_db)):
    star = await _my_streamer(db, access)
    try:
        stream = await db.get(Stream, uuid.UUID(body.stream_id))
    except ValueError:
        stream = None
    if not stream or stream.streamer_id != star.id:
        raise HTTPException(404, "Pick one of your streams.")
    if stream.status not in ("live", "done"):
        raise HTTPException(409, "You can post moments once the stream has started.")
    m = await M.add(db, stream, access.user, "streamer", body.at, body.label, body.kind, body.note)
    await audit(db, access.user, "moment.posted", "stream", stream.id, request=request, summary=f"{M.fmt(m.at_seconds)} · {m.label}")
    await db.commit()
    return {"id": str(m.id), "at": M.fmt(m.at_seconds)}


@router.delete("/talent/moments/{moment_id}")
async def remove_moment(moment_id: uuid.UUID, access: Access = Depends(current_access), db: AsyncSession = Depends(aget_db)):
    m = await db.get(MarkedMoment, moment_id)
    if not m or m.marked_by != access.user.id:
        raise HTTPException(404, "Moment not found.")
    if await db.scalar(select(func.count()).select_from(Clip).where(Clip.moment_id == m.id)):
        raise HTTPException(409, "A clipper has already posted a clip from this moment, so it stays.")
    await db.delete(m)
    await db.commit()
    return {"ok": True}


# Clippers claim what they're cutting -------------------------------------------------------------------------

@router.post("/clipper/moments/{moment_id}/claim")
async def claim(moment_id: uuid.UUID, access: Access = Depends(require_role("clipper")), db: AsyncSession = Depends(aget_db)):
    profile = await db.scalar(select(ClipperProfile).where(ClipperProfile.user_id == access.user.id))
    if not profile or profile.status != "active":
        raise HTTPException(403, "Your desk is paused. Talk to the Social Media Manager.")
    if not await db.get(MarkedMoment, moment_id):
        raise HTTPException(404, "Moment not found.")
    mine = await db.scalar(select(MomentClaim).where(MomentClaim.moment_id == moment_id, MomentClaim.clipper_user_id == access.user.id))
    if mine:
        await db.execute(delete(MomentClaim).where(MomentClaim.id == mine.id))
    else:
        db.add(MomentClaim(moment_id=moment_id, clipper_user_id=access.user.id))
    await db.commit()
    count = await db.scalar(select(func.count()).select_from(MomentClaim).where(MomentClaim.moment_id == moment_id)) or 0
    return {"claimed": not mine, "claims": count}


# Management ---------------------------------------------------------------------------------------------------

@router.get("/moments")
async def all_moments(access: Access = Depends(current_access), db: AsyncSession = Depends(aget_db)):
    if access.user.role == "streamer" or not any(access.can(a) for a in MANAGEMENT):
        raise HTTPException(403, "You don't have access to this part of Hub OS.")
    streams = await M.recent_streams(db)
    rows = await M.board(db, streams, access.user, detail=True, only_with_moments=True)
    flat = [m for s in rows for m in s["moments"]]
    return {"streams": rows, "kinds": M.KINDS,
            "totals": {"moments": len(flat), "by_streamer": sum(m["source"] == "streamer" for m in flat),
                       "claimed": sum(m["claims"] > 0 for m in flat), "clipped": sum(m["clips"] > 0 for m in flat),
                       "untouched": sum(m["claims"] == 0 and m["clips"] == 0 for m in flat)}}
