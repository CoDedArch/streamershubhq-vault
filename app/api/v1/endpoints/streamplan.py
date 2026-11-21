"""Planning streams with a streamer, the things the streamer playbook points to in Hub OS:

- each streamer's setup, kept by Production: their producer, the repeat segments that make clips, their moderators
  and chat rules;
- the run of show for any planned stream (the producer writes it; the streamer sees it on their desk);
- the streamer's own notice before a stream ("running late", "can't stream"), which emails their producer.
"""

import uuid
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import OS_ROLES, Access, require_area
from app.core.crypto import decrypt
from app.core.database import aget_db
from app.core.security import now
from app.models.people import Department, User
from app.models.posts import Post
from app.models.talent import RunOfShowItem, Stream, Streamer
from app.services import EmailService, PostService
from app.services.AuditService import audit
from app.api.v1.endpoints.desks import _local, _stream_row, notice_label, production_desk

router = APIRouter(tags=["stream planning"])

MAX_SEGMENTS = 8
LATE_MINUTES = (15, 30, 45, 60, 90, 120)


def setup_out(s: Streamer) -> dict:
    return {"segments": list(s.segments or []), "moderators": s.moderators, "chat_rules": s.chat_rules}


async def producer_out(db: AsyncSession, s: Streamer) -> Optional[dict]:
    u = await db.get(User, s.producer_user_id) if s.producer_user_id else None
    if not u:
        return None
    return {"id": str(u.id), "name": u.name, "email": u.email, "phone": decrypt(u.phone_enc)}


async def producer_emails(db: AsyncSession, s: Streamer) -> list[str]:
    """Who hears about the streamer's notice: their producer, or else the Production lead, or else the Founder."""
    u = await db.get(User, s.producer_user_id) if s.producer_user_id else None
    if u and u.status == "active":
        return [u.email]
    lead_id = await db.scalar(select(Department.lead_user_id).where(Department.slug == "production"))
    lead = await db.get(User, lead_id) if lead_id else None
    if lead and lead.status == "active":
        return [lead.email]
    founder = await db.scalar(select(User.email).where(User.role == "founder", User.status == "active").order_by(User.created_at).limit(1))
    return [founder] if founder else []


async def stream_post(db: AsyncSession, stream: Stream, api_base: str) -> Optional[dict]:
    """The website post for this stream (same streamer, starting within two hours of it): its title and thumbnail
    are what the streamer sets on the platform."""
    p = (await db.execute(select(Post).where(Post.kind == "stream", Post.streamer_id == stream.streamer_id, Post.status == "published",
                                             Post.starts_at >= stream.scheduled_start - timedelta(hours=2),
                                             Post.starts_at <= stream.scheduled_start + timedelta(hours=2))
                          .order_by(Post.starts_at).limit(1))).scalar_one_or_none()
    if not p:
        return None
    return {"title": p.title, "summary": p.summary, "image_url": PostService.image_url(p, api_base),
            "link_url": p.link_url, "slug": p.slug}


async def desk_plan(db: AsyncSession, s: Streamer, tonight: Optional[Stream], api_base: str) -> dict:
    """What the streamer's desk adds from planning: their producer, setup, coming streams and tonight's post."""
    upcoming = (await db.execute(select(Stream).where(Stream.streamer_id == s.id, Stream.status.in_(("scheduled", "live")),
                                                      Stream.scheduled_start >= now() - timedelta(hours=4))
                                 .order_by(Stream.scheduled_start).limit(8))).scalars().all()
    return {"producer": await producer_out(db, s), "setup": setup_out(s), "upcoming": [_stream_row(x) for x in upcoming],
            "post": await stream_post(db, tonight, api_base) if tonight else None, "late_minutes": list(LATE_MINUTES)}


# Production: each streamer's setup ----------------------------------------------------------------------------

@router.get("/production/streamers")
async def streamers(access: Access = Depends(production_desk), db: AsyncSession = Depends(aget_db)):
    rows = (await db.execute(select(Streamer).where(Streamer.status == "signed").order_by(Streamer.signing_number))).scalars().all()
    staff = (await db.execute(select(User).where(User.role.in_(OS_ROLES - {"streamer"}), User.status == "active")
                              .order_by(User.name))).scalars().all()
    out = []
    for s in rows:
        p = await producer_out(db, s)
        out.append({"id": str(s.id), "stage_name": s.stage_name, "producer": {"id": p["id"], "name": p["name"]} if p else None,
                    **setup_out(s)})
    return {"streamers": out, "producers": [{"id": str(u.id), "name": u.name} for u in staff], "max_segments": MAX_SEGMENTS}


class SetupIn(BaseModel):
    producer_user_id: Optional[uuid.UUID] = None
    segments: list[str] = Field(default=[], max_length=MAX_SEGMENTS)
    moderators: Optional[str] = Field(default=None, max_length=300)
    chat_rules: Optional[str] = Field(default=None, max_length=2000)


@router.put("/production/streamers/{streamer_id}/setup")
async def save_setup(streamer_id: uuid.UUID, body: SetupIn, request: Request, access: Access = Depends(production_desk),
                     db: AsyncSession = Depends(aget_db)):
    s = await db.get(Streamer, streamer_id)
    if not s:
        raise HTTPException(404, "Streamer not found.")
    if body.producer_user_id:
        u = await db.get(User, body.producer_user_id)
        if not u or u.role not in OS_ROLES - {"streamer"} or u.status != "active":
            raise HTTPException(400, {"message": "Pick a producer from the team.", "missing": ["producer_user_id"]})
    segments = []
    for raw in body.segments:
        name = " ".join(raw.split())[:60]
        if name and name.lower() not in (x.lower() for x in segments):
            segments.append(name)
    before = {"producer_user_id": str(s.producer_user_id) if s.producer_user_id else None, **setup_out(s)}
    s.producer_user_id = body.producer_user_id
    s.segments = segments
    s.moderators = (body.moderators or "").strip() or None
    s.chat_rules = (body.chat_rules or "").strip() or None
    await audit(db, access.user, "streamer.setup_changed", "streamer", s.id, request=request, before=before,
                after={"producer_user_id": str(s.producer_user_id) if s.producer_user_id else None, **setup_out(s)},
                summary=f"{s.stage_name}'s stream setup")
    await db.commit()
    p = await producer_out(db, s)
    return {"id": str(s.id), "stage_name": s.stage_name, "producer": {"id": p["id"], "name": p["name"]} if p else None, **setup_out(s)}


# Production: the run of show for any planned stream -----------------------------------------------------------

@router.get("/streams/{stream_id}/plan")
async def stream_plan(stream_id: uuid.UUID, access: Access = Depends(production_desk), db: AsyncSession = Depends(aget_db)):
    s = await db.get(Stream, stream_id)
    if not s:
        raise HTTPException(404, "Stream not found.")
    star = await db.get(Streamer, s.streamer_id)
    items = (await db.execute(select(RunOfShowItem).where(RunOfShowItem.stream_id == s.id)
                              .order_by(RunOfShowItem.position, RunOfShowItem.at_time))).scalars().all()
    return {"stream": {**_stream_row(s), "streamer": star.stage_name if star else ""},
            "run": [{"id": str(r.id), "time": r.at_time, "title": r.title, "note": r.note, "status": r.status} for r in items],
            "segments": list(star.segments or []) if star else []}


# The streamer: running late, or can't stream ---------------------------------------------------------------------

class NoticeIn(BaseModel):
    kind: str = Field(pattern="^(late|cant)$")
    minutes: Optional[int] = None
    note: Optional[str] = Field(default=None, max_length=300)


@router.post("/talent/streams/{stream_id}/notice")
async def stream_notice(stream_id: uuid.UUID, body: NoticeIn, request: Request, access: Access = Depends(require_area("talent")),
                        db: AsyncSession = Depends(aget_db)):
    if access.user.role != "streamer":
        raise HTTPException(403, "Only the streamer sends this, from their desk.")
    star = (await db.execute(select(Streamer).where(Streamer.user_id == access.user.id))).scalar_one_or_none()
    s = await db.get(Stream, stream_id)
    if not star or not s or s.streamer_id != star.id:
        raise HTTPException(404, "Pick one of your streams.")
    if s.status != "scheduled":
        raise HTTPException(409, "This stream has already started or ended.")
    note = (body.note or "").strip() or None
    if body.kind == "late" and body.minutes not in LATE_MINUTES:
        raise HTTPException(400, {"message": "Say roughly how late you'll be.", "missing": ["minutes"]})
    if body.kind == "cant" and not note:
        raise HTTPException(400, {"message": "Tell your producer why, in a line.", "missing": ["note"]})
    s.notice_kind, s.notice_minutes, s.notice_note, s.notice_at = body.kind, body.minutes if body.kind == "late" else None, note, now()
    await audit(db, access.user, "stream.notice", "stream", s.id, request=request, summary=f"{notice_label(s)} · {s.title}")
    await db.commit()
    when = _local(s.scheduled_start).strftime("%a %d %b, %H:%M")
    lines = [f"{star.stage_name}: {notice_label(s)} for “{s.title}” ({when}).", *([f"“{note}”"] if note else []),
             "Update the socials and the website post if the time changes." if body.kind == "late"
             else "Cancel or move the stream on the Production desk, and let the Social team know."]
    await EmailService.notify(await producer_emails(db, star), f"{star.stage_name}: {notice_label(s)}", notice_label(s), lines,
                              ("Open the Production desk", "/os/production"))
    return {"stream": _stream_row(s)}
