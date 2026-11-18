"""Live, events and links: posts for the website (a streamer's next stream, an event with tickets, news),
their numbers, ticket bookings, and the link pages of the label and each streamer.

Hub OS side: the "posts" area (Marketing and Production, and anyone stepping in). Public side: under
/public, no sign-in; every count is anonymous."""

import uuid
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import Access, require_area
from app.core.crypto import decrypt
from app.core.database import aget_db
from app.core.security import now
from app.core.types import Email
from app.core.access import DEPARTMENTS
from app.models.contact import JOB_STAGES, ContactMessage
from app.models.posts import (EMPLOYMENT, LINK_KINDS, PLATFORMS, POST_KINDS, POST_THEMES, POST_TOPICS, WORKPLACE, LinkItem, LinkPage,
                              LinkPageShare, Post, TicketBooking)
from app.models.talent import Streamer
from app.services import EmailService, PostService, S3Service, StoreService
from app.services.AuditService import audit
from app.services.money import ghs

router = APIRouter(tags=["posts"])
posts_desk = require_area("posts")
ORG_SLUG = "streamershub"


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


# Hub OS: posts ------------------------------------------------------------------------------------------

async def _admin_row(db: AsyncSession, p: Post, base: str) -> dict:
    out = await PostService.public_post(db, p, base)
    out.update({"status": p.status, "views": p.views, "clicks": p.clicks, "shares": p.shares,
                "click_rate": round(p.clicks * 100 / p.views, 1) if p.views else None,
                "streamer_id": str(p.streamer_id) if p.streamer_id else None,
                "ticket_price": (p.ticket_price_minor or 0) / 100 if p.ticket_price_minor else None,
                "ticket_capacity": p.ticket_capacity, "ticket_url": p.ticket_url, "tickets_open": p.tickets_open})
    if p.kind == "event":
        out["bookings"] = int(await db.scalar(select(func.coalesce(func.sum(TicketBooking.qty), 0)).where(
            TicketBooking.post_id == p.id, TicketBooking.status.in_(("paid", "free", "interest")))) or 0)
    if p.kind == "job":
        out["job_raw"] = p.job or {}
        stages = dict((await db.execute(select(ContactMessage.stage, func.count()).where(
            ContactMessage.post_id == p.id, ContactMessage.reason == "job", ContactMessage.status != "spam")
            .group_by(ContactMessage.stage))).all())
        out["applicants"] = {"total": sum(stages.values()), "new": int(await db.scalar(select(func.count()).select_from(ContactMessage).where(
            ContactMessage.post_id == p.id, ContactMessage.reason == "job", ContactMessage.status.in_(("new", "awaiting")))) or 0),
            "stages": [{"key": k, "label": v, "n": stages.get(k, 0)} for k, v in JOB_STAGES.items() if stages.get(k)]}
    return out


@router.get("/posts")
async def list_posts(request: Request, access: Access = Depends(posts_desk), db: AsyncSession = Depends(aget_db)):
    rows = (await db.execute(select(Post).where(Post.status != "archived")
                             .order_by(Post.pinned.desc(), Post.starts_at.desc().nulls_last(), Post.created_at.desc()))).scalars().all()
    streamers = (await db.execute(select(Streamer).where(Streamer.status == "signed").order_by(Streamer.signing_number))).scalars().all()
    base = _base(request)
    published = [p for p in rows if p.status == "published" and p.kind != "job"]
    roles = [p for p in rows if p.status == "published" and p.kind == "job"]
    return {
        "posts": [await _admin_row(db, p, base) for p in rows],
        "totals": {"published": len(published), "views": sum(p.views for p in published), "clicks": sum(p.clicks for p in published),
                   "likes": sum(p.likes for p in published), "shares": sum(p.shares for p in published),
                   "upcoming": sum(1 for p in published if PostService.state(p) in ("upcoming", "live"))},
        "job_totals": {"open": sum(1 for p in roles if PostService.job_state(p) == "open"), "views": sum(p.views for p in roles),
                       "shares": sum(p.shares for p in roles), "clicks": sum(p.clicks for p in roles),
                       "applicants": int(await db.scalar(select(func.count()).select_from(ContactMessage).where(
                           ContactMessage.reason == "job", ContactMessage.post_id.in_([p.id for p in roles]),
                           ContactMessage.status != "spam")) or 0)},
        "options": {"kinds": [{"key": k, "label": v} for k, v in POST_KINDS.items()], "topics": POST_TOPICS,
                    "themes": [{"key": k, "label": v} for k, v in POST_THEMES.items()],
                    "platforms": [{"key": k, "label": v} for k, v in PLATFORMS.items()],
                    "streamers": [{"id": str(s.id), "name": s.stage_name} for s in streamers],
                    "departments": [{"key": k, "label": v} for k, v in DEPARTMENTS],
                    "employment": [{"key": k, "label": v} for k, v in EMPLOYMENT.items()],
                    "workplace": [{"key": k, "label": v} for k, v in WORKPLACE.items()]},
        "store_mode": await StoreService.store_mode(db),
    }


class JobIn(BaseModel):
    department: str = Field(max_length=20)
    employment: str = Field(max_length=12)
    workplace: str = Field(max_length=10)
    location: str = Field(min_length=2, max_length=120)
    closes_on: Optional[date] = None
    pay: Optional[str] = Field(default=None, max_length=120)
    show_pay: bool = False
    openings: int = Field(default=1, ge=1, le=50)
    responsibilities: list[str] = Field(default_factory=list, max_length=20)
    requirements: list[str] = Field(default_factory=list, max_length=20)
    nice_to_have: list[str] = Field(default_factory=list, max_length=20)
    apply_via: str = Field(default="form", pattern="^(form|link)$")
    apply_url: Optional[str] = Field(default=None, max_length=500)
    closed: bool = False


def _lines(items: list[str]) -> list[str]:
    return [x.strip().lstrip("•-*· ").strip()[:300] for x in items if x and x.strip().lstrip("•-*· ").strip()]


class PostIn(BaseModel):
    kind: str = Field(pattern="^(stream|event|news|job)$")
    title: str = Field(min_length=3, max_length=160)
    summary: Optional[str] = Field(default=None, max_length=2000)
    topic: Optional[str] = Field(default=None, max_length=40)
    theme: str = Field(default="y", pattern="^(y|k|t|light)$")
    streamer_id: Optional[uuid.UUID] = None
    link_url: Optional[str] = Field(default=None, max_length=500)
    platform: Optional[str] = Field(default=None, max_length=12)
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    venue: Optional[str] = Field(default=None, max_length=200)
    ticket_price: Optional[float] = Field(default=None, ge=0, le=100_000)
    ticket_capacity: Optional[int] = Field(default=None, ge=1, le=100_000)
    ticket_url: Optional[str] = Field(default=None, max_length=500)
    tickets_open: bool = False
    pinned: bool = False
    job: Optional[JobIn] = None


async def _apply(db: AsyncSession, p: Post, body: PostIn) -> None:
    if body.streamer_id and not await db.get(Streamer, body.streamer_id):
        raise HTTPException(400, "Pick a streamer from the list.")
    if body.platform and body.platform not in PLATFORMS:
        raise HTTPException(400, "Pick a platform from the list.")
    if body.kind in ("stream", "event") and not body.starts_at:
        raise HTTPException(400, {"message": "When does it start?", "missing": ["starts_at"]})
    if body.starts_at and body.ends_at and body.ends_at <= body.starts_at:
        raise HTTPException(400, {"message": "It has to end after it starts.", "missing": ["ends_at"]})
    if body.kind == "stream" and not body.link_url:
        raise HTTPException(400, {"message": "Add the link to the stream, so people can tap straight through.", "missing": ["link_url"]})
    if body.kind == "job":
        j = body.job
        if not j:
            raise HTTPException(400, {"message": "Add the role's details.", "missing": ["department", "employment", "workplace", "location"]})
        bad = [k for k, ok in (("department", j.department in dict(DEPARTMENTS)), ("employment", j.employment in EMPLOYMENT),
                               ("workplace", j.workplace in WORKPLACE)) if not ok]
        if bad:
            raise HTTPException(400, {"message": "Pick from the lists.", "missing": bad})
        if j.apply_via == "link" and not j.apply_url:
            raise HTTPException(400, {"message": "Add the link where people apply, or let them apply here.", "missing": ["apply_url"]})
        if not _lines(j.responsibilities) and not (body.summary or "").strip():
            raise HTTPException(400, {"message": "Say what the job is: a summary or what they'll do.", "missing": ["responsibilities"]})
        p.job = {"department": j.department, "employment": j.employment, "workplace": j.workplace, "location": j.location.strip(),
                 "closes_on": j.closes_on.isoformat() if j.closes_on else None, "pay": (j.pay or "").strip() or None, "show_pay": j.show_pay,
                 "openings": j.openings, "responsibilities": _lines(j.responsibilities), "requirements": _lines(j.requirements),
                 "nice_to_have": _lines(j.nice_to_have), "apply_via": j.apply_via,
                 "apply_url": PostService.ok_url(j.apply_url, allow_contact=True) if j.apply_via == "link" else None, "closed": j.closed}
        body.starts_at = body.ends_at = None
        body.link_url = None
        body.platform = None
    else:
        p.job = None
    p.kind, p.title, p.summary = body.kind, body.title.strip(), (body.summary or "").strip() or None
    p.topic = (body.topic or "").strip() or None
    p.theme, p.streamer_id, p.pinned = body.theme, body.streamer_id, body.pinned
    p.link_url = PostService.ok_url(body.link_url)
    p.platform = body.platform or None
    p.starts_at, p.ends_at = body.starts_at, body.ends_at
    if body.kind == "event":
        p.venue = (body.venue or "").strip() or None
        p.ticket_price_minor = round(body.ticket_price * 100) if body.ticket_price else None
        p.ticket_capacity = body.ticket_capacity
        p.ticket_url = PostService.ok_url(body.ticket_url)
        p.tickets_open = body.tickets_open
    else:
        p.venue = p.ticket_price_minor = p.ticket_capacity = p.ticket_url = None
        p.tickets_open = False


@router.post("/posts")
async def create_post(body: PostIn, request: Request, access: Access = Depends(posts_desk), db: AsyncSession = Depends(aget_db)):
    p = Post(slug=await PostService.unique_slug(db, body.title), status="draft", created_by=access.user.id)
    await _apply(db, p, body)
    db.add(p)
    await db.flush()
    await audit(db, access.user, "post.created", "post", p.slug, request=request, summary=f"Draft: {p.title}")
    await db.commit()
    return await _admin_row(db, p, _base(request))


async def _post(db: AsyncSession, post_id: uuid.UUID) -> Post:
    p = await db.get(Post, post_id)
    if not p:
        raise HTTPException(404, "Post not found.")
    return p


@router.put("/posts/{post_id}")
async def edit_post(post_id: uuid.UUID, body: PostIn, request: Request, access: Access = Depends(posts_desk), db: AsyncSession = Depends(aget_db)):
    p = await _post(db, post_id)
    await _apply(db, p, body)
    p.updated_at = now()
    await audit(db, access.user, "post.edited", "post", p.slug, request=request, summary=f"Edited {p.title}")
    await db.commit()
    return await _admin_row(db, p, _base(request))


class StatusIn(BaseModel):
    status: str = Field(pattern="^(draft|published|archived)$")


@router.post("/posts/{post_id}/status")
async def set_status(post_id: uuid.UUID, body: StatusIn, request: Request, access: Access = Depends(posts_desk), db: AsyncSession = Depends(aget_db)):
    p = await _post(db, post_id)
    if body.status == "published":
        if p.kind in ("stream", "event") and not p.starts_at:
            raise HTTPException(400, "Add when it starts before publishing.")
        if p.kind == "job" and (not p.image_key or p.image_layout != "flyer"):
            raise HTTPException(400, {"message": "An open role goes up with its flyer. Upload the flyer, then publish.", "missing": ["image"]})
        p.published_at = p.published_at or now()
    p.status = body.status
    word = {"published": "Published", "draft": "Unpublished", "archived": "Archived"}[body.status]
    await audit(db, access.user, f"post.{body.status}", "post", p.slug, request=request, summary=f"{word} {p.title}")
    await db.commit()
    return await _admin_row(db, p, _base(request))


@router.post("/posts/{post_id}/image")
async def upload_image(post_id: uuid.UUID, request: Request, file: UploadFile = File(...),
                       layout: str = Form("thumbnail"), access: Access = Depends(posts_desk), db: AsyncSession = Depends(aget_db)):
    """A thumbnail (wide, like a stream thumbnail) or a full flyer (tall). A small preview for WhatsApp and
    other apps is made from it."""
    p = await _post(db, post_id)
    if layout not in ("thumbnail", "flyer"):
        raise HTTPException(400, "Choose a thumbnail or a flyer.")
    if p.kind == "job":
        layout = "flyer"  # an open role is always shown with its whole flyer
    data = await file.read()
    try:
        S3Service.check_file(data, file.content_type or "", ("image",))
        shown, kind = PostService.display_image(data, file.content_type or "image/jpeg")
        share = PostService.share_image(data, layout)
    except S3Service.UploadRejected as exc:
        raise HTTPException(400, str(exc))
    except Exception:
        raise HTTPException(400, "We couldn't read that image. Try a JPG or PNG.")
    p.image_key = S3Service.put("posts", file.filename or "image", shown, kind)
    p.share_image_key = S3Service.put("posts", "share.jpg", share, "image/jpeg")
    p.image_layout = layout
    p.updated_at = now()
    await audit(db, access.user, "post.image", "post", p.slug, request=request, summary=f"New {layout} for {p.title}")
    await db.commit()
    return await _admin_row(db, p, _base(request))


@router.delete("/posts/{post_id}/image")
async def remove_image(post_id: uuid.UUID, request: Request, access: Access = Depends(posts_desk), db: AsyncSession = Depends(aget_db)):
    p = await _post(db, post_id)
    if p.kind == "job" and p.status == "published":
        raise HTTPException(409, "A published role needs its flyer. Upload a new one to replace it, or unpublish the role first.")
    p.image_key = p.share_image_key = None
    p.updated_at = now()
    await audit(db, access.user, "post.image_removed", "post", p.slug, request=request, summary=f"Removed the image from {p.title}")
    await db.commit()
    return await _admin_row(db, p, _base(request))


@router.get("/posts/{post_id}/stats")
async def post_stats(post_id: uuid.UUID, request: Request, access: Access = Depends(posts_desk), db: AsyncSession = Depends(aget_db)):
    p = await _post(db, post_id)
    return {"post": await _admin_row(db, p, _base(request)), "stats": await PostService.stats(db, p)}


class SharedIn(BaseModel):
    via: Optional[str] = Field(default="team", max_length=20)


@router.post("/posts/{post_id}/shared")
async def team_shared(post_id: uuid.UUID, body: SharedIn, access: Access = Depends(posts_desk), db: AsyncSession = Depends(aget_db)):
    """The team copied the share link from Hub OS to post it: counted as a share, from "Team"."""
    p = await _post(db, post_id)
    if p.status != "published":
        raise HTTPException(409, "Publish it first, then share it.")
    counted = await PostService.track(db, p, "share", PostService.visitor(f"team:{access.user.id}"), "team", None)
    await db.commit()
    return {"counted": counted, "shares": p.shares}


TICKET_STATUS = {"interest": ("Reserved · to follow up", "y"), "pending": ("Paying", ""), "paid": ("Paid", "ok"),
                 "free": ("Free entry", "ok"), "cancelled": ("Cancelled", "no")}


@router.get("/posts/{post_id}/tickets")
async def post_tickets(post_id: uuid.UUID, access: Access = Depends(posts_desk), db: AsyncSession = Depends(aget_db)):
    p = await _post(db, post_id)
    rows = (await db.execute(select(TicketBooking).where(TicketBooking.post_id == p.id).order_by(TicketBooking.created_at.desc()))).scalars().all()
    out = []
    for b in rows:
        try:
            phone = decrypt(b.phone_enc) if b.phone_enc else None
        except Exception:
            phone = None
        out.append({"id": str(b.id), "code": b.code, "name": b.name, "email": b.email, "phone": phone, "qty": b.qty,
                    "amount_minor": b.amount_minor, "status": b.status, "status_label": TICKET_STATUS[b.status][0],
                    "tone": TICKET_STATUS[b.status][1], "note": b.note, "created_at": b.created_at.isoformat()})
    return {"rows": out, "left": await PostService.tickets_left(db, p), "capacity": p.ticket_capacity,
            "paid_minor": sum(b.amount_minor for b in rows if b.status == "paid")}


class BookingUpdate(BaseModel):
    status: str = Field(pattern="^(paid|cancelled|interest)$")
    note: Optional[str] = Field(default=None, max_length=500)


@router.patch("/tickets/{booking_id}")
async def update_booking(booking_id: uuid.UUID, body: BookingUpdate, request: Request, access: Access = Depends(posts_desk),
                         db: AsyncSession = Depends(aget_db)):
    """Reservations taken while payments are off: mark paid once the money is in (it goes to the ledger),
    or cancel to free the seats."""
    b = await db.get(TicketBooking, booking_id, with_for_update=True)
    if not b:
        raise HTTPException(404, "Not found.")
    p = await db.get(Post, b.post_id)
    if body.status == "paid":
        if b.status not in ("interest", "pending"):
            raise HTTPException(409, "Only a reservation can be marked paid.")
        await PostService.mark_paid(db, b, p, f"collected by {access.user.name}")
    elif body.status == "cancelled":
        if b.status == "paid":
            raise HTTPException(409, "This one is paid. Refund it first, then cancel.")
        b.status = "cancelled"
    else:
        b.status = "interest"
    if body.note:
        b.note = body.note.strip()
    b.handled_by = access.user.id
    await audit(db, access.user, "tickets.updated", "ticket_booking", b.code, request=request,
                summary=f"{p.title} · {b.code} · {TICKET_STATUS[b.status][0]}" + (f" · {ghs(b.amount_minor)}" if b.status == "paid" else ""))
    await db.commit()
    if b.status == "paid":
        await EmailService.ticket_confirmed(b.email, b.name, p.title, PostService.when_text(p), b.code, b.qty, "paid", f"/tickets/{b.code}")
    return {"ok": True}


# Public: posts ------------------------------------------------------------------------------------------

@router.get("/public/posts")
async def public_posts(request: Request, limit: int = 30, db: AsyncSession = Depends(aget_db)):
    rows = (await db.execute(select(Post).where(Post.status == "published", Post.kind != "job")
                             .order_by(Post.starts_at.desc().nulls_last(), Post.published_at.desc()).limit(min(limit, 60)))).scalars().all()
    base = _base(request)
    items = [await PostService.public_post(db, p, base) for p in rows]
    soon = sorted([i for i in items if i["state"] in ("live", "upcoming")],
                  key=lambda i: (i["state"] != "live", not i["pinned"], i["starts_at"] or ""))
    rest = [i for i in items if i["state"] in ("ended", "posted")]
    rest.sort(key=lambda i: (not i["pinned"], -(datetime.fromisoformat(i["starts_at"] or i["published_at"]).timestamp())))
    return {"upcoming": soon, "past": rest}


async def _published(db: AsyncSession, slug: str) -> Post:
    p = (await db.execute(select(Post).where(Post.slug == slug))).scalar_one_or_none()
    if not p or p.status != "published":
        raise HTTPException(404, "This post isn't up.")
    return p


@router.get("/public/posts/{slug}")
async def public_post(slug: str, request: Request, vid: Optional[str] = None, db: AsyncSession = Depends(aget_db)):
    p = await _published(db, slug)
    out = await PostService.public_post(db, p, _base(request), full=True)
    out["liked"] = await PostService.liked(db, p, PostService.visitor(vid)) if vid else False
    if p.kind == "job":
        more = (await db.execute(select(Post).where(Post.status == "published", Post.kind == "job", Post.id != p.id)
                                 .order_by(Post.published_at.desc()).limit(6))).scalars().all()
        out["more"] = [await PostService.public_post(db, m, _base(request)) for m in more if PostService.job_state(m) == "open"][:3]
    else:
        more = (await db.execute(select(Post).where(Post.status == "published", Post.id != p.id, Post.kind != "job")
                                 .order_by(Post.starts_at.desc().nulls_last()).limit(6))).scalars().all()
        out["more"] = [await PostService.public_post(db, m, _base(request)) for m in more
                       if PostService.state(m) in ("live", "upcoming")][:3]
    out["store_mode"] = await StoreService.store_mode(db)
    return out


@router.get("/public/jobs")
async def public_jobs(request: Request, db: AsyncSession = Depends(aget_db)):
    """Open roles for the Careers page, and the ones that closed lately."""
    rows = (await db.execute(select(Post).where(Post.status == "published", Post.kind == "job")
                             .order_by(Post.pinned.desc(), Post.published_at.desc()).limit(40))).scalars().all()
    base = _base(request)
    items = [await PostService.public_post(db, p, base) for p in rows]
    cutoff = (now() - timedelta(days=60)).isoformat()
    return {"open": [i for i in items if i["state"] == "open"],
            "closed": [i for i in items if i["state"] == "closed" and (i["published_at"] or "") >= cutoff][:6]}


@router.get("/public/posts/{slug}/image")
async def post_image(slug: str, size: str = "full", db: AsyncSession = Depends(aget_db)):
    """Served by the API so preview crawlers (WhatsApp, X, Facebook) get a stable, public image URL."""
    p = await _published(db, slug)
    key = p.share_image_key if size == "share" else p.image_key
    data = S3Service.read_bytes(key) if key else None
    if not data:
        raise HTTPException(404, "No image.")
    kind = "image/jpeg" if size == "share" or key.endswith((".jpg", ".jpe", ".jpeg")) else ("image/png" if key.endswith(".png") else "image/webp")
    return Response(data, media_type=kind, headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"})


class TrackIn(BaseModel):
    kind: str = Field(pattern="^(view|click|share)$")
    vid: str = Field(min_length=6, max_length=100)
    via: Optional[str] = Field(default=None, max_length=20)
    target: Optional[str] = Field(default=None, pattern="^(watch|tickets|more|streamer|apply)$")


@router.post("/public/posts/{slug}/track")
async def track(slug: str, body: TrackIn, db: AsyncSession = Depends(aget_db)):
    p = await _published(db, slug)
    counted = await PostService.track(db, p, body.kind, PostService.visitor(body.vid), body.via, body.target)
    await db.commit()
    return {"counted": counted}


class LikeIn(BaseModel):
    vid: str = Field(min_length=6, max_length=100)
    on: bool = True


@router.post("/public/posts/{slug}/like")
async def like(slug: str, body: LikeIn, db: AsyncSession = Depends(aget_db)):
    p = await _published(db, slug)
    on, count = await PostService.like(db, p, PostService.visitor(body.vid), body.on)
    await db.commit()
    return {"liked": on, "likes": count}


class TicketIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: Email
    phone: str = Field(min_length=9, max_length=20)
    qty: int = Field(ge=1, le=10)
    method: str = Field(default="momo", pattern="^(momo|card)$")


@router.post("/public/posts/{slug}/tickets")
async def get_tickets(slug: str, body: TicketIn, db: AsyncSession = Depends(aget_db)):
    p = await _published(db, slug)
    if sum(1 for c in body.phone if c.isdigit()) < 9:
        raise HTTPException(400, {"message": "Enter a phone number we can call.", "missing": ["phone"]})
    mode = await StoreService.store_mode(db)
    b, url = await PostService.book(db, p, mode=mode, name=body.name, email=body.email, phone=body.phone, qty=body.qty, method=body.method)
    await db.commit()
    if b.status in ("interest", "free"):
        await EmailService.ticket_confirmed(b.email, b.name, p.title, PostService.when_text(p), b.code, b.qty, b.status, f"/tickets/{b.code}")
    return {"code": b.code, "status": b.status, "pay_url": url, "amount_minor": b.amount_minor}


async def _ticket_view(db: AsyncSession, b: TicketBooking, base: str) -> dict:
    p = await db.get(Post, b.post_id)
    return {"code": b.code, "status": b.status, "status_label": TICKET_STATUS[b.status][0], "name": b.name, "qty": b.qty,
            "amount_minor": b.amount_minor, "event": {"title": p.title, "slug": p.slug, "when": PostService.when_text(p), "venue": p.venue,
                                                      "image_url": PostService.image_url(p, base) if p.status == "published" else None}}


@router.get("/public/tickets/{code}")
async def ticket(code: str, request: Request, db: AsyncSession = Depends(aget_db)):
    b = (await db.execute(select(TicketBooking).where(TicketBooking.code == code.strip().upper()))).scalar_one_or_none()
    if not b:
        raise HTTPException(404, "We couldn't find that ticket.")
    return await _ticket_view(db, b, _base(request))


@router.post("/public/tickets/{code}/verify")
async def verify_ticket(code: str, request: Request, db: AsyncSession = Depends(aget_db)):
    """Back from the payment page: confirm with the gateway (safe to call more than once)."""
    b = (await db.execute(select(TicketBooking).where(TicketBooking.code == code.strip().upper()))).scalar_one_or_none()
    if not b:
        raise HTTPException(404, "We couldn't find that ticket.")
    if b.status == "pending" and b.payment_ref:
        from app.services.PaymentProvider import PaymentError

        try:
            b = await PostService.confirm_ticket_payment(db, b.payment_ref)
        except PaymentError as exc:
            raise HTTPException(503, str(exc))
    return await _ticket_view(db, b, _base(request))


# Link pages ----------------------------------------------------------------------------------------------

async def _org_page(db: AsyncSession) -> LinkPage:
    page = (await db.execute(select(LinkPage).where(LinkPage.streamer_id.is_(None), LinkPage.slug == ORG_SLUG))).scalar_one_or_none()
    if not page:
        page = LinkPage(slug=ORG_SLUG, title="Streamers Hub", bio="Africa's first content label for streamers. Accra, Ghana.", theme="k")
        db.add(page)
        await db.flush()
        for i, (kind, label, url) in enumerate([("website", "Our website", "https://streamershubmedia.com"),
                                                ("store", "The store", "https://streamershubmedia.com/store"),
                                                ("email", "Email us", "mailto:contact@streamershubmedia.com")]):
            db.add(LinkItem(page_id=page.id, kind=kind, label=label, url=url, position=i))
        await db.flush()
    return page


async def streamer_page(db: AsyncSession, s: Streamer) -> LinkPage:
    page = (await db.execute(select(LinkPage).where(LinkPage.streamer_id == s.id))).scalar_one_or_none()
    if not page:
        page = LinkPage(slug=await PostService.unique_slug(db, s.stage_name, LinkPage), streamer_id=s.id, title=s.stage_name,
                        bio=s.content, theme="y")
        db.add(page)
        await db.flush()
    return page


async def page_out(db: AsyncSession, page: LinkPage, base: str, *, public: bool) -> dict:
    items = (await db.execute(select(LinkItem).where(LinkItem.page_id == page.id).order_by(LinkItem.position))).scalars().all()
    s = await db.get(Streamer, page.streamer_id) if page.streamer_id else None
    avatar = S3Service.signed_url(page.avatar_key, 3600, base) if page.avatar_key else (s.image_url if s else "/brand/sh-mark-avatar.png")
    return {"id": str(page.id), "slug": page.slug, "title": page.title, "bio": page.bio, "theme": page.theme, "avatar_url": avatar,
            "kind": "streamer" if s else "label", "streamer_id": str(s.id) if s else None, "views": None if public else page.views,
            "shares": None if public else page.shares,
            "items": [{"id": str(i.id), "kind": i.kind, "kind_label": LINK_KINDS.get(i.kind, "Link"), "label": i.label,
                       **({"go": f"{base}/api/v1/public/links/go/{i.id}"} if public else {"url": i.url, "clicks": i.clicks, "is_active": i.is_active})}
                      for i in items if i.is_active or not public]}


@router.get("/public/links/{slug}")
async def public_links(slug: str, request: Request, db: AsyncSession = Depends(aget_db)):
    page = (await db.execute(select(LinkPage).where(LinkPage.slug == slug))).scalar_one_or_none()
    if not page and slug == ORG_SLUG:
        page = await _org_page(db)
        await db.commit()
    if not page:
        raise HTTPException(404, "No link page here.")
    out = await page_out(db, page, _base(request), public=True)
    if page.streamer_id:
        nxt = (await db.execute(select(Post).where(Post.status == "published", Post.streamer_id == page.streamer_id,
                                                   Post.starts_at >= now() - PostService.LIVE_WINDOW).order_by(Post.starts_at).limit(1))).scalar_one_or_none()
        out["next"] = await PostService.public_post(db, nxt, _base(request)) if nxt else None
    else:
        streamers = (await db.execute(select(Streamer).where(Streamer.is_public.is_(True), Streamer.status == "signed")
                                      .order_by(Streamer.signing_number))).scalars().all()
        out["roster"] = []
        for s in streamers:
            sp = await streamer_page(db, s)
            out["roster"].append({"name": s.stage_name, "slug": sp.slug, "image_url": s.image_url})
        await db.commit()
    return out


@router.post("/public/links/{slug}/view")
async def link_view(slug: str, db: AsyncSession = Depends(aget_db)):
    page = (await db.execute(select(LinkPage).where(LinkPage.slug == slug))).scalar_one_or_none()
    if page:
        page.views = (page.views or 0) + 1
        await db.commit()
    return {"ok": True}


class LinkShareIn(BaseModel):
    vid: str = Field(min_length=6, max_length=100)
    via: Optional[str] = Field(default=None, max_length=20)


async def count_link_share(db: AsyncSession, page: LinkPage, who: str, via: Optional[str]) -> bool:
    via = via if via in PostService.VIA else "other"
    recent = await db.scalar(select(func.count()).select_from(LinkPageShare).where(
        LinkPageShare.page_id == page.id, LinkPageShare.visitor == who, LinkPageShare.created_at >= now() - timedelta(minutes=1)))
    if recent:
        return False
    db.add(LinkPageShare(page_id=page.id, visitor=who, via=via))
    page.shares = (page.shares or 0) + 1
    await db.flush()
    return True


@router.post("/public/links/{slug}/share")
async def link_share(slug: str, body: LinkShareIn, db: AsyncSession = Depends(aget_db)):
    page = (await db.execute(select(LinkPage).where(LinkPage.slug == slug))).scalar_one_or_none()
    if not page:
        raise HTTPException(404, "No link page here.")
    counted = await count_link_share(db, page, PostService.visitor(body.vid), body.via)
    await db.commit()
    return {"counted": counted}


@router.get("/public/links/go/{item_id}")
async def link_go(item_id: uuid.UUID, db: AsyncSession = Depends(aget_db)):
    item = await db.get(LinkItem, item_id)
    if not item or not item.is_active:
        raise HTTPException(404, "This link has been removed.")
    item.clicks = (item.clicks or 0) + 1
    await db.commit()
    return RedirectResponse(item.url, status_code=302, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


class LinkIn(BaseModel):
    id: Optional[uuid.UUID] = None
    kind: str = Field(default="other", max_length=12)
    label: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=3, max_length=500)
    is_active: bool = True


class PageIn(BaseModel):
    title: str = Field(min_length=2, max_length=80)
    bio: Optional[str] = Field(default=None, max_length=300)
    theme: str = Field(default="k", pattern="^(y|k|t|light)$")
    items: list[LinkIn] = Field(default_factory=list, max_length=40)


async def save_page(db: AsyncSession, page: LinkPage, body: PageIn) -> None:
    page.title, page.bio, page.theme = body.title.strip(), (body.bio or "").strip() or None, body.theme
    existing = {i.id: i for i in (await db.execute(select(LinkItem).where(LinkItem.page_id == page.id))).scalars()}
    keep = set()
    for n, raw in enumerate(body.items):
        kind = raw.kind if raw.kind in LINK_KINDS else "other"
        url = PostService.ok_url(raw.url, allow_contact=True)
        item = existing.get(raw.id) if raw.id else None
        if not item:
            item = LinkItem(page_id=page.id)
            db.add(item)
        item.kind, item.label, item.url, item.position, item.is_active = kind, raw.label.strip(), url, n, raw.is_active
        await db.flush()
        keep.add(item.id)
    for item_id, item in existing.items():
        if item_id not in keep:
            await db.delete(item)
    await db.flush()


@router.get("/link-pages")
async def link_pages(request: Request, access: Access = Depends(posts_desk), db: AsyncSession = Depends(aget_db)):
    pages = [await _org_page(db)]
    for s in (await db.execute(select(Streamer).where(Streamer.status == "signed").order_by(Streamer.signing_number))).scalars():
        pages.append(await streamer_page(db, s))
    await db.commit()
    return {"pages": [await page_out(db, p, _base(request), public=False) for p in pages],
            "kinds": [{"key": k, "label": v} for k, v in LINK_KINDS.items()]}


@router.post("/link-pages/{page_id}/shared")
async def link_page_team_shared(page_id: uuid.UUID, access: Access = Depends(posts_desk), db: AsyncSession = Depends(aget_db)):
    page = await db.get(LinkPage, page_id)
    if not page:
        raise HTTPException(404, "Not found.")
    counted = await count_link_share(db, page, PostService.visitor(f"team:{access.user.id}"), "team")
    await db.commit()
    return {"counted": counted, "shares": page.shares}


@router.put("/link-pages/{page_id}")
async def edit_link_page(page_id: uuid.UUID, body: PageIn, request: Request, access: Access = Depends(posts_desk),
                         db: AsyncSession = Depends(aget_db)):
    page = await db.get(LinkPage, page_id)
    if not page:
        raise HTTPException(404, "Not found.")
    await save_page(db, page, body)
    await audit(db, access.user, "links.edited", "link_page", page.slug, request=request, summary=f"Link page {page.title}: {len(body.items)} links")
    await db.commit()
    return await page_out(db, page, _base(request), public=False)


@router.post("/link-pages/{page_id}/avatar")
async def link_avatar(page_id: uuid.UUID, request: Request, file: UploadFile = File(...), access: Access = Depends(posts_desk),
                      db: AsyncSession = Depends(aget_db)):
    page = await db.get(LinkPage, page_id)
    if not page:
        raise HTTPException(404, "Not found.")
    data = await file.read()
    try:
        S3Service.check_file(data, file.content_type or "", ("image",))
    except S3Service.UploadRejected as exc:
        raise HTTPException(400, str(exc))
    page.avatar_key = S3Service.put("links", file.filename or "avatar", data, file.content_type or "image/jpeg")
    await audit(db, access.user, "links.avatar", "link_page", page.slug, request=request, summary=f"New picture for {page.title}")
    await db.commit()
    return await page_out(db, page, _base(request), public=False)


# The streamer's own link page, from their desk ----------------------------------------------------------

async def _my_streamer(db: AsyncSession, access: Access) -> Streamer:
    if access.user.role != "streamer":
        raise HTTPException(403, "This is the streamer's own page. Edit it from Live, events and links.")
    s = (await db.execute(select(Streamer).where(Streamer.user_id == access.user.id))).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "Your streamer profile isn't set up yet.")
    return s


@router.get("/talent/links")
async def my_links(request: Request, access: Access = Depends(require_area("talent")), db: AsyncSession = Depends(aget_db)):
    s = await _my_streamer(db, access)
    page = await streamer_page(db, s)
    await db.commit()
    return {"page": await page_out(db, page, _base(request), public=False), "kinds": [{"key": k, "label": v} for k, v in LINK_KINDS.items()]}


@router.put("/talent/links")
async def save_my_links(body: PageIn, request: Request, access: Access = Depends(require_area("talent")), db: AsyncSession = Depends(aget_db)):
    s = await _my_streamer(db, access)
    page = await streamer_page(db, s)
    await save_page(db, page, body)
    await audit(db, access.user, "links.edited", "link_page", page.slug, request=request, summary=f"{s.stage_name} updated their links")
    await db.commit()
    return {"page": await page_out(db, page, _base(request), public=False)}
