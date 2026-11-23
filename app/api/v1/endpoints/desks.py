"""Department desks: Production, Partnerships, Social, Creative, Tech and the streamer's Talent desk.
Each summary returns exactly what its screen shows; each desk is refused to anyone without that area."""

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import Access, require_area
from app.core.database import aget_db
from app.core.security import now
from app.models.clips import Clip
from app.models.money import Decision, PayrollLine, PayrollRun
from app.models.partnerships import BRAND_STAGES, Brand, Deal, Deliverable, Invoice, RateCardItem
from app.models.people import Goal, KpiSnapshot, Task, User
from app.models.store import Design
from app.models.talent import (CreativeRequest, EditJob, Honour, MarkedMoment, PlatformIncome, RunOfShowItem, SalaryTierAssignment,
                               SocialPost, Stream, StreamHealth, Streamer)
from app.models.tech import AccessAccount, Equipment, EquipmentIssue, Incident
from app.services import ClipService, CompanyService, PayrollService
from app.services.AuditService import audit
from app.services.CompanyService import ACCRA, day_start, latest_kpi, today
from app.services.money import ghs, month_key, month_label, prev_month
from app.services.SettingsService import get_setting
from app.api.v1.endpoints.designs import design_out
from app.api.v1.endpoints.network import PLATFORM_LABEL, _fetched

router = APIRouter(tags=["desks"])


def _local(dt: Optional[datetime]) -> Optional[datetime]:
    return dt.astimezone(ACCRA) if dt else None


def _week() -> tuple[date, date]:
    start = today() - timedelta(days=today().weekday())
    return start, start + timedelta(days=7)


STREAM_PLATFORMS = {"youtube": "YouTube", "twitch": "Twitch", "kick": "Kick", "tiktok": "TikTok"}


def notice_label(s: Stream) -> Optional[str]:
    """The streamer's own notice, as the Production desk and their desk show it."""
    if s.notice_kind == "late":
        return f"Running {s.notice_minutes} min late"
    if s.notice_kind == "cant":
        return "Can't stream"
    return None


def _stream_row(s: Stream) -> dict:
    local = _local(s.scheduled_start)
    end = _local(s.scheduled_end)
    late = None
    if s.actual_start:
        late = int((s.actual_start - s.scheduled_start).total_seconds() // 60)
    status = {"live": "Live", "scheduled": "Scheduled", "cancelled": "Cancelled"}.get(s.status, "Done")
    tone = {"live": "y", "scheduled": "", "cancelled": "no"}.get(s.status, "ok")
    if s.status == "done" and late and late > 5:
        status, tone = f"Done · {late} min late", "warn"
    return {"id": str(s.id), "day": local.strftime("%a"), "date": local.date().isoformat(), "time": local.strftime("%H:%M"),
            "title": s.title, "category": s.category, "brand": s.brand_moment or "—", "status": status, "tone": tone,
            "avg_viewers": s.avg_viewers, "peak_viewers": s.peak_viewers, "state": s.status, "late_minutes": late,
            "actual_start": s.actual_start.isoformat() if s.actual_start else None,
            "uptime": float(s.uptime_percent) if s.uptime_percent is not None else None,
            "streamer_id": str(s.streamer_id), "starts_at": s.scheduled_start.isoformat(),
            "end": end.strftime("%H:%M") if end else None, "end_day": end.strftime("%a") if end and end.date() != local.date() else None,
            "platforms": [STREAM_PLATFORMS.get(p, p) for p in (s.platforms or [])],
            "notice": notice_label(s), "notice_note": s.notice_note}


# Production -----------------------------------------------------------------------------------------

production_desk = require_area("production")


async def _live_or_next(db: AsyncSession) -> Optional[Stream]:
    live = (await db.execute(select(Stream).where(Stream.status == "live").order_by(Stream.scheduled_start.desc()).limit(1))).scalar_one_or_none()
    if live:
        return live
    return (await db.execute(select(Stream).where(Stream.status == "scheduled", Stream.scheduled_start >= now() - timedelta(hours=3))
                             .order_by(Stream.scheduled_start).limit(1))).scalar_one_or_none()


@router.get("/production/summary")
async def production(access: Access = Depends(production_desk), db: AsyncSession = Depends(aget_db)):
    current = await _live_or_next(db)
    run, health, moments = [], None, []
    if current:
        run = [{"id": str(r.id), "time": r.at_time, "title": r.title, "note": r.note, "status": r.status}
               for r in (await db.execute(select(RunOfShowItem).where(RunOfShowItem.stream_id == current.id)
                                          .order_by(RunOfShowItem.position, RunOfShowItem.at_time))).scalars()]
        h = (await db.execute(select(StreamHealth).where(StreamHealth.stream_id == current.id)
                              .order_by(StreamHealth.created_at.desc()).limit(1))).scalar_one_or_none()
        if h:
            audio = float(h.audio_db) if h.audio_db is not None else None
            health = [
                {"t": "Bitrate", "v": f"{h.bitrate_kbps:,} kbps" if h.bitrate_kbps else "—", "s": "Good" if (h.bitrate_kbps or 0) >= 4500 else "Low", "c": "ok" if (h.bitrate_kbps or 0) >= 4500 else "warn"},
                {"t": "Dropped frames", "v": f"{float(h.dropped_frames_pct):.1f}%" if h.dropped_frames_pct is not None else "—", "s": "Good" if float(h.dropped_frames_pct or 0) < 1 else "High", "c": "ok" if float(h.dropped_frames_pct or 0) < 1 else "no"},
                {"t": "Internet upload", "v": f"{float(h.upload_mbps):g} Mbps" if h.upload_mbps is not None else "—", "s": "Good" if float(h.upload_mbps or 0) >= 10 else "Weak", "c": "ok" if float(h.upload_mbps or 0) >= 10 else "warn"},
                {"t": "Power", "v": (h.power or "—").title(), "s": "Backup ready" if h.backup_ready else "No backup", "c": "info" if h.backup_ready else "warn"},
                {"t": "Audio level", "v": f"{audio:g} dB" if audio is not None else "—", "s": "Slightly hot" if audio is not None and audio > -10 else "Good", "c": "warn" if audio is not None and audio > -10 else "ok"},
            ]
        moments = [{"id": str(m.id), "at": m.at_seconds, "label": m.label} for m in (await db.execute(
            select(MarkedMoment).where(MarkedMoment.stream_id == current.id).order_by(MarkedMoment.at_seconds))).scalars()]
    last7 = (await db.execute(select(Stream).where(Stream.status.in_(("done", "live"))).order_by(Stream.scheduled_start.desc()).limit(7))).scalars().all()
    last7 = list(reversed(last7))
    top = max((s.avg_viewers or s.watching_now or 0 for s in last7), default=0) or 1
    ws, we = today() - timedelta(days=6), today() + timedelta(days=7)  # the last week, tonight, and what's booked next
    week = (await db.execute(select(Stream).where(Stream.scheduled_start >= day_start(ws), Stream.scheduled_start < day_start(we))
                             .order_by(Stream.scheduled_start))).scalars().all()
    jobs = (await db.execute(select(EditJob).where(EditJob.status != "done").order_by(EditJob.due_at))).scalars().all()
    streamers = (await db.execute(select(Streamer).where(Streamer.status == "signed"))).scalars().all()
    return {
        "current": None if not current else {
            **_stream_row(current), "live": current.status == "live",
            "panel": [{"l": "Watching now", "v": current.watching_now}, {"l": "Peak", "v": current.peak_viewers},
                      {"l": "Chat / min", "v": current.chat_per_min},
                      {"l": "New followers", "v": f"+{current.new_followers}" if current.new_followers is not None else None}]},
        "run_of_show": run, "health": health, "moments": moments,
        "last7": [{"day": _local(s.scheduled_start).strftime("%a"), "value": s.avg_viewers or s.watching_now or 0,
                   "height": max(5, round((s.avg_viewers or s.watching_now or 0) * 90 / top)), "latest": i == len(last7) - 1}
                  for i, s in enumerate(last7)],
        "week": [_stream_row(s) for s in week],
        "edit_jobs": [{"id": str(j.id), "title": j.title, "format": j.format, "status": j.status,
                       "due_at": j.due_at.isoformat() if j.due_at else None} for j in jobs],
        "streamers": [{"id": str(s.id), "stage_name": s.stage_name} for s in streamers],
    }


class StreamIn(BaseModel):
    """One stream, or a regular schedule: the same start (and end) time on the chosen weekdays (0 = Monday) for a
    number of weeks, from the week of starts_at. An end time at or before the start time ends the next morning."""

    streamer_id: uuid.UUID
    title: str = Field(min_length=2, max_length=160)
    category: str = Field(min_length=2, max_length=60)
    starts_at: datetime
    end_time: Optional[str] = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    platforms: list[str] = Field(default=["youtube"], min_length=1)
    brand_moment: Optional[str] = Field(default=None, max_length=160)
    weekdays: list[int] = Field(default=[], max_length=7)
    weeks: int = Field(default=1, ge=1, le=12)


@router.post("/streams")
async def add_stream(body: StreamIn, request: Request, access: Access = Depends(production_desk), db: AsyncSession = Depends(aget_db)):
    if not await db.get(Streamer, body.streamer_id):
        raise HTTPException(400, "Pick a streamer.")
    if any(p not in STREAM_PLATFORMS for p in body.platforms):
        raise HTTPException(400, {"message": "Pick where it streams: YouTube, Twitch, Kick or TikTok.", "missing": ["platforms"]})
    if any(d < 0 or d > 6 for d in body.weekdays):
        raise HTTPException(400, {"message": "Pick the days of the week.", "missing": ["weekdays"]})
    first = (body.starts_at if body.starts_at.tzinfo else body.starts_at.replace(tzinfo=ACCRA)).astimezone(ACCRA)
    if body.weekdays:
        monday = first.date() - timedelta(days=first.weekday())
        days = sorted(monday + timedelta(days=7 * w + d) for w in range(body.weeks) for d in set(body.weekdays))
        days = [d for d in days if d >= first.date()]
    else:
        days = [first.date()]
    end_t = datetime.strptime(body.end_time, "%H:%M").time() if body.end_time else None
    made = []
    for d in days:
        start = datetime.combine(d, first.timetz().replace(tzinfo=None), ACCRA)
        end = None
        if end_t:
            end = datetime.combine(d, end_t, ACCRA)
            if end <= start:
                end += timedelta(days=1)
        start_utc = start.astimezone(timezone.utc)
        if await db.scalar(select(func.count()).select_from(Stream).where(Stream.streamer_id == body.streamer_id,
                                                                           Stream.scheduled_start == start_utc,
                                                                           Stream.status != "cancelled")):
            continue  # already booked; planning the same week twice adds nothing
        s = Stream(streamer_id=body.streamer_id, title=body.title, category=body.category, scheduled_start=start_utc,
                   scheduled_end=end.astimezone(timezone.utc) if end else None, platforms=list(dict.fromkeys(body.platforms)),
                   brand_moment=body.brand_moment, status="scheduled")
        db.add(s)
        made.append(s)
    if not made:
        raise HTTPException(409, "Those streams are already on the schedule.")
    await db.flush()
    await audit(db, access.user, "stream.scheduled", "stream", made[0].id, request=request,
                summary=body.title if len(made) == 1 else f"{body.title} · {len(made)} streams")
    await db.commit()
    return {"created": len(made), "streams": [_stream_row(x) for x in made]}


class StreamUpdate(BaseModel):
    status: Optional[str] = Field(default=None, pattern="^(scheduled|live|done|cancelled)$")
    watching_now: Optional[int] = Field(default=None, ge=0)
    peak_viewers: Optional[int] = Field(default=None, ge=0)
    avg_viewers: Optional[int] = Field(default=None, ge=0)
    chat_per_min: Optional[int] = Field(default=None, ge=0)
    new_followers: Optional[int] = Field(default=None, ge=0)
    uptime_percent: Optional[float] = Field(default=None, ge=0, le=100)
    recording_url: Optional[str] = Field(default=None, max_length=500)


@router.patch("/streams/{stream_id}")
async def update_stream(stream_id: uuid.UUID, body: StreamUpdate, access: Access = Depends(production_desk), db: AsyncSession = Depends(aget_db)):
    s = await db.get(Stream, stream_id)
    if not s:
        raise HTTPException(404, "Stream not found.")
    if body.status == "live" and not s.actual_start:
        s.actual_start = now()
    if body.status == "done" and not s.ended_at:
        s.ended_at = now()
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(s, field, value)
    await db.commit()
    return _stream_row(s)


class RunItemIn(BaseModel):
    time: str = Field(pattern=r"^\d{2}:\d{2}$")
    title: str = Field(min_length=1, max_length=160)
    note: Optional[str] = Field(default=None, max_length=200)
    status: str = Field(default="queued", pattern="^(queued|next|done|skipped)$")


@router.put("/streams/{stream_id}/run-of-show")
async def set_run_of_show(stream_id: uuid.UUID, items: list[RunItemIn], access: Access = Depends(production_desk),
                          db: AsyncSession = Depends(aget_db)):
    if not await db.get(Stream, stream_id):
        raise HTTPException(404, "Stream not found.")
    for row in (await db.execute(select(RunOfShowItem).where(RunOfShowItem.stream_id == stream_id))).scalars():
        await db.delete(row)
    for i, item in enumerate(sorted(items, key=lambda x: x.time)):
        db.add(RunOfShowItem(stream_id=stream_id, at_time=item.time, title=item.title, note=item.note, status=item.status, position=i))
    await db.commit()
    return {"ok": True}


class RunStatusIn(BaseModel):
    status: str = Field(pattern="^(queued|next|done|skipped)$")


@router.patch("/run-of-show/{item_id}")
async def set_item_status(item_id: uuid.UUID, body: RunStatusIn, access: Access = Depends(production_desk), db: AsyncSession = Depends(aget_db)):
    item = await db.get(RunOfShowItem, item_id)
    if not item:
        raise HTTPException(404, "Item not found.")
    item.status = body.status
    if body.status == "done":  # the next queued item becomes "next"
        nxt = (await db.execute(select(RunOfShowItem).where(RunOfShowItem.stream_id == item.stream_id, RunOfShowItem.status == "queued",
                                                            RunOfShowItem.position > item.position).order_by(RunOfShowItem.position).limit(1))).scalar_one_or_none()
        if nxt:
            nxt.status = "next"
    await db.commit()
    return {"ok": True}


class MomentIn(BaseModel):
    at_seconds: int = Field(ge=0, le=24 * 3600)
    label: str = Field(min_length=2, max_length=160)
    kind: str = Field(default="highlight", max_length=16)
    note: Optional[str] = Field(default=None, max_length=300)


@router.post("/streams/{stream_id}/moments")
async def mark_moment(stream_id: uuid.UUID, body: MomentIn, access: Access = Depends(production_desk), db: AsyncSession = Depends(aget_db)):
    s = await db.get(Stream, stream_id)
    if not s:
        raise HTTPException(404, "Stream not found.")
    from app.services.MomentService import KINDS

    m = MarkedMoment(stream_id=stream_id, at_seconds=body.at_seconds, label=body.label, marked_by=access.user.id, source="producer",
                     kind=body.kind if body.kind in KINDS else "highlight", note=(body.note or "").strip() or None)
    db.add(m)
    db.add(EditJob(source_stream_id=stream_id, title=f"Clip: {body.label}", format="short", status="todo",
                   due_at=now() + timedelta(days=1)))
    await db.commit()
    return {"id": str(m.id)}


class HealthIn(BaseModel):
    bitrate_kbps: Optional[int] = Field(default=None, ge=0)
    dropped_frames_pct: Optional[float] = Field(default=None, ge=0, le=100)
    upload_mbps: Optional[float] = Field(default=None, ge=0)
    power: Optional[str] = Field(default=None, pattern="^(mains|backup)$")
    backup_ready: Optional[bool] = None
    audio_db: Optional[float] = Field(default=None, ge=-90, le=10)


@router.post("/streams/{stream_id}/health")
async def stream_health(stream_id: uuid.UUID, body: HealthIn, access: Access = Depends(require_area("production", "tech")),
                        db: AsyncSession = Depends(aget_db)):
    if not await db.get(Stream, stream_id):
        raise HTTPException(404, "Stream not found.")
    db.add(StreamHealth(stream_id=stream_id, **body.model_dump()))
    await db.commit()
    return {"ok": True}


class EditJobUpdate(BaseModel):
    status: str = Field(pattern="^(todo|editing|review|done)$")


@router.patch("/edit-jobs/{job_id}")
async def update_edit_job(job_id: uuid.UUID, body: EditJobUpdate, access: Access = Depends(require_area("production", "creative")),
                          db: AsyncSession = Depends(aget_db)):
    j = await db.get(EditJob, job_id)
    if not j:
        raise HTTPException(404, "Job not found.")
    j.status = body.status
    await db.commit()
    return {"ok": True}


# Partnerships ---------------------------------------------------------------------------------------

partnerships_desk = require_area("partnerships")
STAGE_LABEL = {"contacted": "Contacted", "replied": "Replied", "in_talks": "In talks", "at_contract": "At contract", "signed": "Signed"}


@router.get("/partnerships/summary")
async def partnerships(access: Access = Depends(partnerships_desk), db: AsyncSession = Depends(aget_db)):
    brands = (await db.execute(select(Brand).where(Brand.status.not_in(("lost", "listed"))).order_by(Brand.updated_at.desc()))).scalars().all()
    deals = {}
    for d in (await db.execute(select(Deal).where(Deal.status != "cancelled").order_by(Deal.created_at.desc()))).scalars():
        deals.setdefault(d.brand_id, d)
    stages = []
    for key in ("contacted", "replied", "in_talks", "at_contract", "signed"):
        rows = [b for b in brands if b.status == key]
        value = sum(deals[b.id].value_minor for b in rows if b.id in deals)
        cards = []
        for b in rows:
            deal = deals.get(b.id)
            when = b.next_step or (f"{(today() - b.last_contact_at.astimezone(ACCRA).date()).days} days" if b.last_contact_at else "")
            cards.append({"id": str(b.id), "name": b.name, "detail": f"{b.sector} · {b.note}" if b.note else b.sector,
                          "value_minor": deal.value_minor if deal and deal.value_minor else None, "next": when,
                          "tone": "warn" if key == "at_contract" or (b.next_step or "").lower().startswith("reply") else ("info" if key in ("replied", "in_talks") else ""),
                          "deal_id": str(deal.id) if deal else None, "stage": key, "public": b.is_public, "wordmark": b.wordmark})
        summary = f"{len(rows)} brand{'s' if len(rows) != 1 else ''}" if key in ("contacted", "replied") else f"GHS {value // 100:,}"
        if key in ("in_talks", "at_contract") and value >= 100000:
            summary = f"GHS {value // 100000}k"
        stages.append({"key": key, "label": STAGE_LABEL[key], "summary": summary, "cards": cards})
    deliverables = (await db.execute(select(Deliverable, Brand).join(Brand, Brand.id == Deliverable.brand_id, isouter=True)
                                     .where(Deliverable.status != "done").order_by(Deliverable.due_at))).all()
    sectors = {}
    for b in brands:
        sectors[b.sector] = sectors.get(b.sector, 0) + 1
    top = max(sectors.values(), default=1)
    pipe = await CompanyService.pipeline(db)
    signed = [d for d in deals.values() if d.status in ("signed", "done")]
    rate = (await db.execute(select(RateCardItem).order_by(RateCardItem.position))).scalars().all()
    return {
        "kpis": {"brands": len(brands), "in_talks": sum(1 for b in brands if b.status in ("in_talks", "at_contract")),
                 "pipeline_minor": pipe["value_minor"], "signed_minor": sum(d.value_minor for d in signed),
                 "reply_rate": round(sum(1 for b in brands if b.status != "contacted") * 100 / len(brands)) if brands else 0},
        "stages": stages,
        "deliverables": [{"id": str(d.id), "title": f"{b.name} · {d.title}" if b else d.title, "detail": d.detail,
                          "due_at": d.due_at.isoformat() if d.due_at else None, "status": d.status,
                          "label": _due_label(d), "tone": {"sent": "ok", "done": "ok"}.get(d.status, "warn" if d.due_at and _local(d.due_at).date() <= today() else "info")}
                         for d, b in deliverables],
        "rate_card": [{"title": r.title, "detail": r.detail, "price_minor": r.price_minor, "unit": r.unit} for r in rate],
        "sectors": [{"label": s, "count": n, "percent": round(n * 100 / top)} for s, n in sorted(sectors.items(), key=lambda kv: -kv[1])],
        "can_sign": access.is_founder,
        # The Brand Partners grid on the website
        "website": [{"id": str(b.id), "name": b.name, "wordmark": b.wordmark, "stage": b.status, "sample": b.is_sample} for b in (await db.execute(
            select(Brand).where(Brand.is_public.is_(True)).order_by(Brand.public_order, Brand.name))).scalars()],
    }


def _due_label(d: Deliverable) -> str:
    if d.status in ("sent", "done"):
        return "Sent" if d.status == "sent" else "Done"
    if not d.due_at:
        return "No date"
    day = _local(d.due_at).date()
    if day < today():
        return "Overdue"
    if day == today():
        return "Due today"
    return day.strftime("%a")


class BrandIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    sector: str = Field(min_length=2, max_length=60)
    note: Optional[str] = Field(default=None, max_length=200)
    contact_name: Optional[str] = Field(default=None, max_length=120)
    contact_email: Optional[str] = Field(default=None, max_length=255)


@router.post("/brands")
async def add_brand(body: BrandIn, request: Request, access: Access = Depends(partnerships_desk), db: AsyncSession = Depends(aget_db)):
    if await db.scalar(select(func.count()).select_from(Brand).where(func.lower(Brand.name) == body.name.strip().lower())):
        raise HTTPException(409, "This brand is already on the board.")
    b = Brand(name=body.name.strip(), sector=body.sector, note=body.note, contact_name=body.contact_name,
              contact_email=body.contact_email, owner_user_id=access.user.id, status="contacted", last_contact_at=now())
    db.add(b)
    await audit(db, access.user, "brand.added", "brand", b.id, request=request, summary=b.name)
    await db.commit()
    return {"id": str(b.id)}


class WebsiteIn(BaseModel):
    show: bool
    wordmark: Optional[str] = Field(default=None, pattern="^[abcef]$")


@router.post("/brands/{brand_id}/website")
async def brand_on_website(brand_id: uuid.UUID, body: WebsiteIn, request: Request, access: Access = Depends(partnerships_desk),
                           db: AsyncSession = Depends(aget_db)):
    """Show a brand in the Brand Partners grid on the website, or take it off. Only for brands that have agreed to be named."""
    b = await db.get(Brand, brand_id, with_for_update=True)
    if not b:
        raise HTTPException(404, "Brand not found.")
    if body.show and not b.is_public:
        b.public_order = ((await db.scalar(select(func.max(Brand.public_order)).where(Brand.is_public.is_(True)))) or 0) + 1
    b.is_public = body.show
    if body.wordmark:
        b.wordmark = body.wordmark
    await audit(db, access.user, "brand.website", "brand", b.id, request=request,
                summary=f"{b.name} {'shown on' if body.show else 'taken off'} the website")
    await db.commit()
    return {"id": str(b.id), "public": b.is_public, "wordmark": b.wordmark}


@router.post("/brands/remove-samples")
async def remove_sample_partners(request: Request, access: Access = Depends(partnerships_desk), db: AsyncSession = Depends(aget_db)):
    """Take every sample partner off the website, ready for the real ones. A sample brand with no deals, work or
    invoices is deleted; one the team has started working with is only taken off the website."""
    removed = hidden = 0
    for b in (await db.execute(select(Brand).where(Brand.is_sample.is_(True)))).scalars().all():
        used = sum([await db.scalar(select(func.count()).select_from(m).where(m.brand_id == b.id)) or 0 for m in (Deal, Deliverable, Invoice)])
        if used:
            b.is_public, b.is_sample = False, False
            hidden += 1
        else:
            await db.delete(b)
            removed += 1
    await audit(db, access.user, "brand.samples_removed", "brand", None, request=request,
                summary=f"Sample partners removed from the website ({removed} deleted, {hidden} kept on the board)")
    await db.commit()
    return {"removed": removed, "kept": hidden}


class StageIn(BaseModel):
    stage: str
    note: Optional[str] = Field(default=None, max_length=200)
    next_step: Optional[str] = Field(default=None, max_length=80)
    deal_value: Optional[float] = Field(default=None, ge=0, le=10_000_000)
    deal_title: Optional[str] = Field(default=None, max_length=160)


@router.post("/brands/{brand_id}/stage")
async def move_brand(brand_id: uuid.UUID, body: StageIn, request: Request, access: Access = Depends(partnerships_desk),
                     db: AsyncSession = Depends(aget_db)):
    if body.stage not in BRAND_STAGES:
        raise HTTPException(400, "Unknown stage.")
    b = await db.get(Brand, brand_id, with_for_update=True)
    if not b:
        raise HTTPException(404, "Brand not found.")
    if body.stage == "signed" and not access.is_founder:
        raise HTTPException(403, "Deals are signed by the Founder. Move it to At contract.")
    before = b.status
    b.status, b.last_contact_at = body.stage, now()
    if body.note is not None:
        b.note = body.note
    b.next_step = body.next_step
    deal = (await db.execute(select(Deal).where(Deal.brand_id == b.id, Deal.status.in_(("draft", "at_contract")))
                             .order_by(Deal.created_at.desc()).limit(1))).scalar_one_or_none()
    if body.deal_value is not None or body.stage in ("in_talks", "at_contract"):
        if not deal:
            deal = Deal(brand_id=b.id, title=body.deal_title or f"{b.name} partnership", value_minor=0, status="draft")
            db.add(deal)
            await db.flush()
        if body.deal_value is not None:
            deal.value_minor = round(body.deal_value * 100)
        if body.deal_title:
            deal.title = body.deal_title
    if body.stage == "at_contract" and deal:
        deal.status = "at_contract"
        exists = await db.scalar(select(func.count()).select_from(Decision).where(Decision.kind == "deal", Decision.entity_id == str(deal.id),
                                                                                   Decision.status == "open"))
        if not exists:
            db.add(Decision(title=f"Sign {deal.title} · {ghs(deal.value_minor)}", source="Partnerships · contract ready for signature",
                            kind="deal", entity_id=str(deal.id), amount_minor=deal.value_minor, requested_by=access.user.id))
    if body.stage == "signed" and deal:
        deal.status, deal.signed_by, deal.signed_at = "signed", access.user.id, now()
    await audit(db, access.user, "brand.stage", "brand", b.id, request=request, before={"status": before}, after={"status": b.status},
                summary=f"{b.name}: {before} → {b.status}")
    await db.commit()
    return {"ok": True}


class DeliverableIn(BaseModel):
    brand_id: uuid.UUID
    title: str = Field(min_length=2, max_length=160)
    detail: Optional[str] = Field(default=None, max_length=200)
    due_on: Optional[date] = None


@router.post("/deliverables")
async def add_deliverable(body: DeliverableIn, access: Access = Depends(partnerships_desk), db: AsyncSession = Depends(aget_db)):
    deal = (await db.execute(select(Deal).where(Deal.brand_id == body.brand_id).order_by(Deal.created_at.desc()).limit(1))).scalar_one_or_none()
    streamer = (await db.execute(select(Streamer).where(Streamer.status == "signed").order_by(Streamer.signing_number).limit(1))).scalar_one_or_none()
    due = datetime.combine(body.due_on, datetime.min.time(), ACCRA).replace(hour=18) if body.due_on else None
    d = Deliverable(brand_id=body.brand_id, deal_id=deal.id if deal else None, streamer_id=streamer.id if streamer else None,
                    title=body.title, detail=body.detail, due_at=due, status="planned")
    db.add(d)
    await db.commit()
    return {"id": str(d.id)}


class DeliverableUpdate(BaseModel):
    status: str = Field(pattern="^(planned|due|done|sent)$")
    proof_url: Optional[str] = Field(default=None, max_length=500)


@router.patch("/deliverables/{deliverable_id}")
async def update_deliverable(deliverable_id: uuid.UUID, body: DeliverableUpdate, access: Access = Depends(require_area("partnerships", "talent")),
                             db: AsyncSession = Depends(aget_db)):
    d = await db.get(Deliverable, deliverable_id)
    if not d:
        raise HTTPException(404, "Deliverable not found.")
    if access.user.role == "streamer":
        s = await db.get(Streamer, d.streamer_id) if d.streamer_id else None
        if not s or s.user_id != access.user.id or body.status not in ("done",):
            raise HTTPException(403, "You can mark your own brand work done.")
    d.status = body.status
    if body.proof_url:
        d.proof_url = body.proof_url
    await db.commit()
    return {"ok": True}


# Social ----------------------------------------------------------------------------------------------

social_desk = require_area("social")
PLATFORM_KPIS = [("tiktok_followers", "TikTok followers"), ("yt_subscribers", "YouTube subscribers"),
                 ("instagram_followers", "Instagram followers"), ("twitch_kick_followers", "Twitch + Kick followers")]


@router.get("/social/summary")
async def social(access: Access = Depends(social_desk), db: AsyncSession = Depends(aget_db)):
    week_ago = now() - timedelta(days=7)
    plats = []
    for key, label in PLATFORM_KPIS:
        value = await latest_kpi(db, key)
        change = await CompanyService.kpi_change(db, key, week_ago)
        stamp = (await db.execute(select(KpiSnapshot.captured_at, KpiSnapshot.source).where(KpiSnapshot.metric_key == key)
                                  .order_by(KpiSnapshot.captured_at.desc()).limit(1))).first()
        plats.append({"key": key, "label": label, "value": value, "change": change,
                      "updated_at": stamp[0].isoformat() if stamp else None, "source": stamp[1] if stamp else None})
    period = month_key(today())
    real = Clip.clipper_user_id.not_in(ClipService.sample_clippers())
    month_views = await db.scalar(select(func.coalesce(func.sum(Clip.views_verified), 0)).where(Clip.period == period, Clip.status == "verified", real)) or 0
    week_views = await db.scalar(select(func.coalesce(func.sum(Clip.views_verified), 0)).where(Clip.posted_at >= week_ago, Clip.status == "verified", real)) or 0
    ws, we = _week()
    posts = (await db.execute(select(SocialPost).where(SocialPost.on_date >= ws, SocialPost.on_date < we).order_by(SocialPost.on_date, SocialPost.created_at))).scalars().all()
    days = []
    for i in range(7):
        d = ws + timedelta(days=i)
        days.append({"date": d.isoformat(), "day": d.strftime("%a"), "n": d.day, "today": d == today(),
                     "posts": [{"id": str(p.id), "title": p.title, "kind": p.kind, "status": p.status,
                                "tone": {"brand": "y", "live_promo": "info"}.get(p.kind, "")} for p in posts if p.on_date == d]})
    to_check = (await db.execute(select(Clip).where(Clip.status.in_(("submitted", "checking", "flagged", "verified")), real)
                                 .order_by(Clip.status.in_(("submitted", "checking")).desc(), Clip.posted_at.desc()).limit(8))).scalars().all()
    clips = []
    locked = set((await db.execute(select(PayrollRun.period).where(PayrollRun.status.in_(ClipService.LOCKED_RUN)))).scalars())
    for c in to_check:
        days_ago = (today() - _local(c.posted_at).date()).days
        clips.append({"id": str(c.id), "title": f"“{c.title}” · {await ClipService.handle_of(db, c.clipper_user_id)}",
                      "detail": f"{PLATFORM_LABEL.get(c.platform, c.platform)} · " + ("posted today" if days_ago == 0 else "posted yesterday" if days_ago == 1 else f"posted {days_ago} days ago") + (f" · {c.flag_reason}" if c.flag_reason else ""),
                      "views": c.views_verified if c.status == "verified" else c.views_reported, "status": c.status,
                      "label": {"submitted": "Verify", "checking": "Verify", "verified": "Verified", "flagged": "Flagged"}[c.status],
                      "tone": {"submitted": "warn", "checking": "warn", "verified": "ok", "flagged": "no"}[c.status], "url": c.url,
                      "views_reported": c.views_reported, "locked": c.period in locked, **_fetched(c)})
    streak = await latest_kpi(db, "posting_streak_days")
    return {"platforms": plats, "clipper_views_month": int(month_views), "clipper_views_week": int(week_views),
            "streak": streak, "days": days, "clips": clips,
            "leaderboard": await ClipService.leaderboard(db, today(), limit=5, with_earnings=True),
            "sample_clippers": await db.scalar(select(func.count()).select_from(User).where(User.is_sample.is_(True), User.role == "clipper")) or 0}


@router.post("/clippers/remove-samples")
async def remove_sample_clippers(request: Request, access: Access = Depends(social_desk), db: AsyncSession = Depends(aget_db)):
    """Delete the sample clipper accounts, with their clips and places on the Clip Table, ready for real clippers.
    (Sample clippers are never paid, so none has payout lines; one that somehow does is suspended instead.)"""
    from app.models.money import PayrollLine

    removed = suspended = 0
    for u in (await db.execute(select(User).where(User.is_sample.is_(True), User.role == "clipper"))).scalars().all():
        if await db.scalar(select(func.count()).select_from(PayrollLine).where(PayrollLine.payee_user_id == u.id)):
            u.status = "suspended"
            suspended += 1
        else:
            await db.delete(u)  # clips, profile, earnings, snapshots and claims go with the account
            removed += 1
    await audit(db, access.user, "clipper.samples_removed", "user", None, request=request,
                summary=f"Sample clippers removed ({removed} deleted{f', {suspended} suspended' if suspended else ''})")
    await db.commit()
    return {"removed": removed, "suspended": suspended}


class PostIn(BaseModel):
    on_date: date
    title: str = Field(min_length=2, max_length=160)
    kind: str = Field(default="post", pattern="^(post|brand|live_promo)$")
    platform: Optional[str] = Field(default=None, max_length=30)


@router.post("/social/posts")
async def add_post(body: PostIn, access: Access = Depends(social_desk), db: AsyncSession = Depends(aget_db)):
    p = SocialPost(on_date=body.on_date, title=body.title, kind=body.kind, platform=body.platform, owner_id=access.user.id)
    db.add(p)
    await db.commit()
    return {"id": str(p.id)}


class PostUpdate(BaseModel):
    status: str = Field(pattern="^(planned|posted)$")


@router.patch("/social/posts/{post_id}")
async def update_post(post_id: uuid.UUID, body: PostUpdate, access: Access = Depends(social_desk), db: AsyncSession = Depends(aget_db)):
    p = await db.get(SocialPost, post_id)
    if not p:
        raise HTTPException(404, "Post not found.")
    p.status = body.status
    await db.commit()
    return {"ok": True}


class SnapshotIn(BaseModel):
    metric_key: str = Field(pattern="^(yt_subscribers|avg_live_viewers|tiktok_followers|instagram_followers|twitch_kick_followers|posting_streak_days|paid_brand_deals)$")
    value: float = Field(ge=0, le=1_000_000_000)


@router.post("/kpis/snapshots")
async def add_snapshot(body: SnapshotIn, request: Request, access: Access = Depends(require_area("social", "production", "command")),
                       db: AsyncSession = Depends(aget_db)):
    """Manual entry for platforms without a usable API (Kick, TikTok), stamped with who and when."""
    db.add(KpiSnapshot(metric_key=body.metric_key, value=body.value, captured_at=now(), source="manual", entered_by=access.user.id))
    await audit(db, access.user, "kpi.entered", "kpi", body.metric_key, request=request, summary=f"{body.metric_key} = {body.value:g}")
    await db.commit()
    return {"ok": True}


# Creative --------------------------------------------------------------------------------------------

creative_desk = require_area("creative")
BOARD = [("requested", "Requested"), ("in_progress", "In progress"), ("in_review", "In review"), ("delivered", "Delivered")]


@router.get("/creative/summary")
async def creative(request: Request, access: Access = Depends(creative_desk), db: AsyncSession = Depends(aget_db)):
    since = now() - timedelta(days=14)
    reqs = (await db.execute(select(CreativeRequest).where((CreativeRequest.status != "delivered") | (CreativeRequest.delivered_at >= since))
                             .order_by(CreativeRequest.position, CreativeRequest.due_on))).scalars().all()
    people = {u.id: u for u in (await db.execute(select(User).where(User.id.in_([r.assignee_id for r in reqs if r.assignee_id])))).scalars()} if reqs else {}
    from app.api.v1.endpoints.people import _initials
    from app.core.access import load_access

    titles = {}
    for uid, u in people.items():
        titles[uid] = (await load_access(db, u)).title
    cols = []
    for key, label in BOARD:
        cards = [{"id": str(r.id), "from": r.from_area, "tone": {"Store": "y", "Production": "info", "Partnerships": "warn"}.get(r.from_area, ""),
                  "due": _due_day(r.due_on), "title": r.title, "detail": r.detail,
                  "who": _initials(people[r.assignee_id].name) if r.assignee_id in people else "—",
                  "who_title": titles.get(r.assignee_id) or "Unassigned", "status": r.status} for r in reqs if r.status == key]
        cols.append({"key": key, "label": label, "count": len(cards), "cards": cards})
    month_start = day_start(today().replace(day=1))
    delivered = (await db.execute(select(CreativeRequest).where(CreativeRequest.status == "delivered", CreativeRequest.delivered_at >= month_start))).scalars().all()
    on_time = sum(1 for r in delivered if r.on_time)
    checks = (await db.execute(select(Design).where(Design.status == "creative_review").order_by(Design.created_at))).scalars().all()
    base = str(request.base_url).rstrip("/")
    team = {}
    for r in delivered:
        team.setdefault(r.assignee_id, [0, 0])
        team[r.assignee_id][0] += 1
        team[r.assignee_id][1] += 1 if r.on_time else 0
    designers_live = await db.scalar(select(func.count(func.distinct(Design.designer_user_id))).where(Design.status == "live", Design.designer_user_id.is_not(None))) or 0
    designers_pending = await db.scalar(select(func.count()).select_from(Design).where(Design.status.in_(("creative_review", "founder_approval", "streamer_signoff")), Design.designer_user_id.is_not(None))) or 0
    team_rows = []
    for uid, (n, ok) in team.items():
        if uid in people:
            team_rows.append({"label": titles.get(uid) or people[uid].name, "value": f"{n} assets · {round(ok * 100 / n)}% on time", "percent": round(ok * 100 / n)})
    team_rows.append({"label": "Community designers", "value": f"{designers_live} approved · {designers_pending} pending",
                      "percent": round(designers_live * 100 / (designers_live + designers_pending)) if designers_live + designers_pending else 0})
    return {"kpis": {"delivered": len(delivered), "on_time_percent": round(on_time * 100 / len(delivered)) if delivered else None,
                     "open": sum(1 for r in reqs if r.status != "delivered"), "in_review": sum(1 for r in reqs if r.status == "in_review"),
                     "design_checks": len(checks)},
            "board": cols, "checks": [await design_out(db, d, base) for d in checks], "team": team_rows}


def _due_day(d: Optional[date]) -> str:
    if not d:
        return ""
    if d == today():
        return "Today"
    return d.strftime("%a")


class CreativeIn(BaseModel):
    title: str = Field(min_length=2, max_length=160)
    detail: Optional[str] = Field(default=None, max_length=200)
    from_area: str = Field(pattern="^(Social|Store|Production|Partnerships|Talent|Founder)$")
    due_on: Optional[date] = None
    assignee_id: Optional[uuid.UUID] = None


@router.post("/creative/requests")
async def add_request(body: CreativeIn, access: Access = Depends(require_area("creative", "social", "store", "production", "partnerships", "command")),
                      db: AsyncSession = Depends(aget_db)):
    r = CreativeRequest(title=body.title, detail=body.detail, from_area=body.from_area, due_on=body.due_on,
                        assignee_id=body.assignee_id, requested_by=access.user.id, status="requested")
    db.add(r)
    await db.commit()
    return {"id": str(r.id)}


class CreativeMove(BaseModel):
    status: str = Field(pattern="^(requested|in_progress|in_review|delivered)$")
    assignee_id: Optional[uuid.UUID] = None


@router.patch("/creative/requests/{request_id}")
async def move_request(request_id: uuid.UUID, body: CreativeMove, access: Access = Depends(creative_desk), db: AsyncSession = Depends(aget_db)):
    r = await db.get(CreativeRequest, request_id)
    if not r:
        raise HTTPException(404, "Request not found.")
    r.status = body.status
    if body.assignee_id:
        r.assignee_id = body.assignee_id
    if body.status == "delivered" and not r.delivered_at:
        r.delivered_at = now()
        r.on_time = r.due_on is None or today() <= r.due_on
    await db.commit()
    return {"ok": True}


# Tech ------------------------------------------------------------------------------------------------

tech_desk = require_area("tech")
CONDITION = {"good": ("Good", "ok"), "fault": ("Fault", "warn"), "on_order": ("On order", "info"), "retired": ("Retired", "")}


@router.get("/tech/summary")
async def tech(access: Access = Depends(tech_desk), db: AsyncSession = Depends(aget_db)):
    since = now() - timedelta(days=30)
    streams = (await db.execute(select(Stream).where(Stream.status.in_(("done", "live")), Stream.scheduled_start >= since).order_by(Stream.scheduled_start))).scalars().all()
    uptimes = [float(s.uptime_percent) for s in streams if s.uptime_percent is not None]
    incidents = (await db.execute(select(Incident).order_by(Incident.started_at.desc()).limit(20))).scalars().all()
    drops = [i for i in incidents if i.downtime_minutes > 0]
    last_drop = drops[0].started_at if drops else None
    eq = (await db.execute(select(Equipment).order_by(Equipment.tag))).scalars().all()
    streamers = {x.id: x for x in (await db.execute(select(Streamer).order_by(Streamer.signing_number))).scalars()}
    issues = (await db.execute(select(EquipmentIssue, Equipment).join(Equipment, Equipment.id == EquipmentIssue.equipment_id)
                               .where(EquipmentIssue.status == "open").order_by(EquipmentIssue.created_at.desc()))).all()
    reporters = {u.id: u for u in (await db.execute(select(User).where(User.id.in_([i.reported_by for i, _ in issues if i.reported_by])))).scalars()} if issues else {}
    upload = await db.scalar(select(StreamHealth.upload_mbps).order_by(StreamHealth.created_at.desc()).limit(1))
    accounts = (await db.execute(select(AccessAccount).order_by(AccessAccount.position))).scalars().all()
    return {
        "kpis": {"uptime": round(sum(uptimes) / len(uptimes), 1) if uptimes else None, "upload_mbps": float(upload) if upload is not None else None,
                 "days_without_drop": (today() - _local(last_drop).date()).days if last_drop else None,
                 "open_incidents": sum(1 for i in incidents if not i.resolved_at), "streams": len(streams)},
        "strip": [{"date": _local(s.scheduled_start).date().isoformat(), "title": s.title,
                   "uptime": float(s.uptime_percent) if s.uptime_percent is not None else None,
                   "dropped": s.uptime_percent is not None and float(s.uptime_percent) < 99.5} for s in streams[-26:]],
        "equipment": [{"id": str(e.id), "name": e.name, "tag": e.tag,
                       "assigned_to": (streamers[e.streamer_id].stage_name if e.streamer_id in streamers else None) or e.assigned_to or e.location or "—",
                       "assigned_text": e.assigned_to, "streamer_id": str(e.streamer_id) if e.streamer_id else None,
                       "serial_number": e.serial_number, "purchased_on": e.purchased_on.isoformat() if e.purchased_on else None,
                       "cost_minor": e.purchase_cost_minor, "from_purchase": e.expense_id is not None,
                       "condition": e.condition, "label": e.condition_note or CONDITION[e.condition][0], "tone": CONDITION[e.condition][1]} for e in eq],
        "issues": [{"id": str(i.id), "equipment": f"{g.name} · {g.tag}", "detail": i.detail,
                    "by": reporters[i.reported_by].name if i.reported_by in reporters else "Tech",
                    "at": i.created_at.isoformat()} for i, g in issues],
        "streamers": [{"id": str(x.id), "name": x.stage_name} for x in streamers.values()],
        "incidents": [{"id": str(i.id), "title": i.title, "open": i.resolved_at is None,
                       "detail": f"{'Opened' if not i.resolved_at else ''} {_local(i.started_at).strftime('%-d %b')}" + (f" · {i.fix}" if i.fix else (f" · {i.cause}" if i.cause else "")),
                       "downtime_minutes": i.downtime_minutes} for i in incidents],
        "accounts": [{"name": a.name, "managers": a.managers} for a in accounts],
    }


class EquipmentIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    assigned_to: Optional[str] = Field(default=None, max_length=120)
    streamer_id: Optional[uuid.UUID] = None
    serial_number: Optional[str] = Field(default=None, max_length=80)
    condition: str = Field(default="good", pattern="^(good|fault|on_order|retired)$")
    condition_note: Optional[str] = Field(default=None, max_length=120)
    cost: Optional[float] = Field(default=None, ge=0)


@router.post("/equipment")
async def add_equipment(body: EquipmentIn, request: Request, access: Access = Depends(tech_desk), db: AsyncSession = Depends(aget_db)):
    """Add gear to the register. With a cost, it is also recorded as an equipment purchase, so it shows in
    the finances (Finance can add the receipt and supplier later)."""
    from app.services import ExpenseService

    streamer = await db.get(Streamer, body.streamer_id) if body.streamer_id else None
    if body.streamer_id and not streamer:
        raise HTTPException(400, "Pick a streamer from the list.")
    if body.cost:
        _, gear = await ExpenseService.record(db, access.user, on_date=today(), category="equipment", description=body.name,
                                              amount_minor=round(body.cost * 100), streamer_id=body.streamer_id,
                                              items=[{"name": body.name, "serial_number": body.serial_number}])
        e = gear[0]
        e.condition, e.condition_note = body.condition, body.condition_note
        if body.assigned_to:
            e.assigned_to = body.assigned_to
    else:
        e = Equipment(tag=await ExpenseService.next_tag(db), name=body.name, assigned_to=body.assigned_to or (streamer.stage_name if streamer else None),
                      condition=body.condition, condition_note=body.condition_note, streamer_id=body.streamer_id,
                      serial_number=body.serial_number)
        db.add(e)
    await audit(db, access.user, "equipment.added", "equipment", e.tag, request=request,
                summary=f"{body.name} ({e.tag})" + (f" for {streamer.stage_name}" if streamer else "") + (f" · {ghs(round(body.cost * 100))}" if body.cost else ""))
    await db.commit()
    return {"id": str(e.id), "tag": e.tag}


class EquipmentUpdate(BaseModel):
    condition: Optional[str] = Field(default=None, pattern="^(good|fault|on_order|retired)$")
    condition_note: Optional[str] = Field(default=None, max_length=120)
    assigned_to: Optional[str] = Field(default=None, max_length=120)
    streamer_id: Optional[uuid.UUID] = None  # send null to take it back from a streamer
    serial_number: Optional[str] = Field(default=None, max_length=80)


@router.patch("/equipment/{equipment_id}")
async def update_equipment(equipment_id: uuid.UUID, body: EquipmentUpdate, request: Request, access: Access = Depends(tech_desk),
                           db: AsyncSession = Depends(aget_db)):
    e = await db.get(Equipment, equipment_id)
    if not e:
        raise HTTPException(404, "Item not found.")
    sent = body.model_dump(exclude_unset=True)
    for field, value in body.model_dump(exclude_none=True).items():
        if field != "streamer_id":
            setattr(e, field, value)
    if "streamer_id" in sent:
        streamer = await db.get(Streamer, body.streamer_id) if body.streamer_id else None
        if body.streamer_id and not streamer:
            raise HTTPException(400, "Pick a streamer from the list.")
        e.streamer_id = streamer.id if streamer else None
    if body.condition == "good" and body.condition_note is None:
        e.condition_note = None
        for issue in (await db.execute(select(EquipmentIssue).where(EquipmentIssue.equipment_id == e.id,
                                                                    EquipmentIssue.status == "open"))).scalars():
            issue.status, issue.resolved_at, issue.resolved_by = "resolved", now(), access.user.id
            issue.resolution = issue.resolution or "Marked good on the register"
    await audit(db, access.user, "equipment.updated", "equipment", e.tag, request=request, summary=f"{e.name} ({e.tag}) updated")
    await db.commit()
    return {"ok": True}


class IssueResolveIn(BaseModel):
    resolution: str = Field(min_length=2, max_length=500)


@router.post("/equipment-issues/{issue_id}/resolve")
async def resolve_issue(issue_id: uuid.UUID, body: IssueResolveIn, request: Request, access: Access = Depends(tech_desk),
                        db: AsyncSession = Depends(aget_db)):
    i = await db.get(EquipmentIssue, issue_id)
    if not i or i.status != "open":
        raise HTTPException(409, "This issue is already closed.")
    i.status, i.resolution, i.resolved_at, i.resolved_by = "resolved", body.resolution.strip(), now(), access.user.id
    e = await db.get(Equipment, i.equipment_id)
    still_open = await db.scalar(select(func.count()).select_from(EquipmentIssue).where(
        EquipmentIssue.equipment_id == e.id, EquipmentIssue.status == "open", EquipmentIssue.id != i.id)) or 0
    if e and e.condition == "fault" and not still_open:
        e.condition, e.condition_note = "good", None
    await audit(db, access.user, "equipment.issue_resolved", "equipment", e.tag if e else None, request=request,
                summary=f"{e.name if e else 'Equipment'}: {body.resolution.strip()}")
    await db.commit()
    return {"ok": True}


class IncidentIn(BaseModel):
    title: str = Field(min_length=2, max_length=160)
    cause: Optional[str] = Field(default=None, max_length=500)
    downtime_minutes: int = Field(default=0, ge=0, le=1440)
    stream_id: Optional[uuid.UUID] = None


@router.post("/incidents")
async def add_incident(body: IncidentIn, request: Request, access: Access = Depends(require_area("tech", "production")),
                       db: AsyncSession = Depends(aget_db)):
    i = Incident(title=body.title, cause=body.cause, downtime_minutes=body.downtime_minutes, stream_id=body.stream_id,
                 started_at=now(), reported_by=access.user.id)
    db.add(i)
    await audit(db, access.user, "incident.opened", "incident", i.id, request=request, summary=body.title)
    await db.commit()
    return {"id": str(i.id)}


class ResolveIn(BaseModel):
    fix: str = Field(min_length=2, max_length=500)


@router.post("/incidents/{incident_id}/resolve")
async def resolve_incident(incident_id: uuid.UUID, body: ResolveIn, access: Access = Depends(tech_desk), db: AsyncSession = Depends(aget_db)):
    i = await db.get(Incident, incident_id)
    if not i or i.resolved_at:
        raise HTTPException(409, "This incident is already closed.")
    i.fix, i.resolved_at = body.fix, now()
    await db.commit()
    return {"ok": True}


# Talent (the streamer's own desk) ----------------------------------------------------------------------

async def _streamer_for(db: AsyncSession, access: Access, streamer_id: Optional[uuid.UUID]) -> Streamer:
    if access.user.role == "streamer":
        s = (await db.execute(select(Streamer).where(Streamer.user_id == access.user.id))).scalar_one_or_none()
        if not s:
            raise HTTPException(404, "Your streamer profile isn't set up yet.")
        return s
    q = select(Streamer).where(Streamer.id == streamer_id) if streamer_id else select(Streamer).order_by(Streamer.signing_number).limit(1)
    s = (await db.execute(q)).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "No streamer found.")
    return s


@router.get("/talent/summary")
async def talent(request: Request, streamer_id: Optional[uuid.UUID] = None, access: Access = Depends(require_area("talent")),
                 db: AsyncSession = Depends(aget_db)):
    from app.api.v1.endpoints.streamplan import desk_plan

    s = await _streamer_for(db, access, streamer_id)
    tonight = (await db.execute(select(Stream).where(Stream.streamer_id == s.id, Stream.status.in_(("scheduled", "live")),
                                                     Stream.scheduled_start >= now() - timedelta(hours=4))
                                .order_by(Stream.scheduled_start).limit(1))).scalar_one_or_none()
    run = []
    if tonight:
        run = [{"time": r.at_time, "title": r.title, "status": r.status} for r in (await db.execute(
            select(RunOfShowItem).where(RunOfShowItem.stream_id == tonight.id).order_by(RunOfShowItem.position))).scalars()]
    subs, viewers = await latest_kpi(db, "yt_subscribers"), await latest_kpi(db, "avg_live_viewers")
    best = await db.scalar(select(func.max(Stream.peak_viewers)).where(Stream.streamer_id == s.id))
    ws, we = _week()
    week_streams = (await db.execute(select(Stream).where(Stream.streamer_id == s.id, Stream.scheduled_start >= day_start(ws),
                                                          Stream.scheduled_start < day_start(we)))).scalars().all()
    done_week = sum(1 for x in week_streams if x.status in ("done", "live"))
    goals = {g.metric_key: g for g in (await db.execute(select(Goal).where(Goal.is_pilot_target.is_(True)))).scalars()}
    yt_target = float(goals["yt_subscribers"].target) if "yt_subscribers" in goals else 500
    av_target = float(goals["avg_live_viewers"].target) if "avg_live_viewers" in goals else 30
    targets = [
        {"label": "YouTube subscribers", "value": subs, "of": f"/ {yt_target:,.0f}", "percent": min(100, round((subs or 0) * 100 / yt_target)),
         "note": f"6-month target · {max(0, yt_target - (subs or 0)):,.0f} to go"},
        {"label": "Average live viewers", "value": viewers, "of": f"/ {av_target:,.0f}+", "percent": min(100, round((viewers or 0) * 100 / av_target)),
         "note": f"Your best night: {best}" if best else "6-month target"},
        {"label": "Streams this week", "value": done_week, "of": f"/ {max(len(week_streams), 1)}",
         "percent": round(done_week * 100 / len(week_streams)) if week_streams else 0,
         "note": "Tonight completes the week" if tonight and done_week == len(week_streams) - 1 else f"{len(week_streams) - done_week} to go"},
    ]
    tasks = (await db.execute(select(Task).where(Task.owner_user_id == s.user_id, Task.is_weekly_milestone.is_(True))
                              .order_by(Task.created_at))).scalars().all() if s.user_id else []
    period = month_key(today())
    tier = (await db.execute(select(SalaryTierAssignment).where(SalaryTierAssignment.streamer_id == s.id,
                                                                SalaryTierAssignment.month == period))).scalar_one_or_none()
    income_now = await PayrollService.platform_income(db, s.id, period)
    tiers = await PayrollService.tiers(db)
    next_tier = next((t for t in tiers if t.min_platform_income_minor > income_now), None)
    share_bps = int((await get_setting(db, "payroll")).get("streamer_share_bps") or 0)
    pay_lines = (await db.execute(select(PayrollLine, PayrollRun).join(PayrollRun, PayrollRun.id == PayrollLine.run_id)
                                  .where(PayrollLine.payee_user_id == s.user_id).order_by(PayrollRun.period.desc()).limit(6))).all() if s.user_id else []
    brand = (await db.execute(select(Deliverable, Brand).join(Brand, Brand.id == Deliverable.brand_id, isouter=True)
                              .where(Deliverable.streamer_id == s.id).order_by(Deliverable.due_at.desc()).limit(6))).all()
    designs = (await db.execute(select(Design).where(Design.uses_streamer_id == s.id).order_by(Design.created_at.desc()))).scalars().all()
    base = str(request.base_url).rstrip("/")
    honours = (await db.execute(select(Honour).where(Honour.streamer_id == s.id).order_by(Honour.position))).scalars().all()
    return {
        "streamer": {"id": str(s.id), "stage_name": s.stage_name, "number": f"{s.signing_number:02d}", "content": s.content},
        "tonight": None if not tonight else {**_stream_row(tonight), "run": run},
        **await desk_plan(db, s, tonight, base),
        "can_notice": access.user.role == "streamer",
        "targets": targets,
        "week": [{"id": str(t.id), "title": t.title, "status": t.status, "progress": t.progress_label,
                  "label": {"done": "Done", "doing": "Doing", "open": "To do"}[t.status],
                  "tone": {"done": "ok", "doing": "warn", "open": ""}[t.status]} for t in tasks],
        "pay": {"month": month_label(period), "tier": tier.tier_label if tier else None, "has_tiers": bool(tiers),
                "share_percent": share_bps / 100 if share_bps else None,
                "salary_minor": tier.salary_minor if tier else None,
                "based_on": f"{month_label(prev_month(period))} platform income of {ghs(tier.previous_income_minor)}" if tier else None,
                "income_this_month_minor": income_now,
                "next_tier": {"label": next_tier.label, "at_minor": next_tier.min_platform_income_minor,
                              "salary_minor": next_tier.salary_minor} if next_tier else None,
                "lines": [{"id": str(l.id), "period": month_label(r.period), "type": PayrollService.KIND_LABEL.get(l.kind, l.kind),
                           "basis": (l.basis or {}).get("text"), "amount_minor": l.amount_minor,
                           "status": l.status if r.status in ("approved", "paid") else "awaiting approval"} for l, r in pay_lines]},
        "brand_work": [{"id": str(d.id), "title": f"{b.name} · {d.title}" if b else d.title, "detail": d.detail, "status": d.status,
                        "label": _due_label(d), "tone": "ok" if d.status in ("done", "sent") else ("warn" if d.due_at and _local(d.due_at).date() <= today() else "info")}
                       for d, b in brand],
        "merch": [await design_out(db, d, base) for d in designs],
        "honours": [{"title": h.title, "detail": h.detail, "public": h.is_public} for h in honours],
        "can_sign_off": access.user.role == "streamer",
        "equipment": await _my_equipment(db, s),
        "can_report": access.user.role == "streamer",
    }


async def _my_equipment(db: AsyncSession, s: Streamer) -> list[dict]:
    """The gear the label bought for or gave this streamer. Never what it cost."""
    gear = (await db.execute(select(Equipment).where(Equipment.streamer_id == s.id, Equipment.condition != "retired")
                             .order_by(Equipment.tag))).scalars().all()
    out = []
    for g in gear:
        issues = (await db.execute(select(EquipmentIssue).where(EquipmentIssue.equipment_id == g.id)
                                   .order_by(EquipmentIssue.created_at.desc()).limit(3))).scalars().all()
        out.append({"id": str(g.id), "name": g.name, "tag": g.tag, "condition": g.condition,
                    "label": g.condition_note or CONDITION[g.condition][0], "tone": CONDITION[g.condition][1],
                    "since": (g.purchased_on or g.created_at.date()).isoformat(), "serial_number": g.serial_number,
                    "issues": [{"detail": i.detail, "status": i.status, "resolution": i.resolution, "at": i.created_at.isoformat()}
                               for i in issues]})
    return out


class IssueIn(BaseModel):
    detail: str = Field(min_length=5, max_length=1000)


@router.post("/talent/equipment/{equipment_id}/issue")
async def report_issue(equipment_id: uuid.UUID, body: IssueIn, request: Request, access: Access = Depends(require_area("talent")),
                       db: AsyncSession = Depends(aget_db)):
    """A streamer reports a problem with their gear. Tech sees it on the register straight away."""
    if access.user.role != "streamer":
        raise HTTPException(403, "Only the streamer who uses it reports an issue here. Tech logs it on the register.")
    s = await _streamer_for(db, access, None)
    g = await db.get(Equipment, equipment_id)
    if not g or g.streamer_id != s.id:
        raise HTTPException(404, "Item not found.")
    db.add(EquipmentIssue(equipment_id=g.id, reported_by=access.user.id, detail=body.detail.strip()))
    g.condition, g.condition_note = "fault", body.detail.strip()[:120]
    await audit(db, access.user, "equipment.issue_reported", "equipment", g.tag, request=request, summary=f"{g.name}: {body.detail.strip()}")
    await db.commit()
    return {"equipment": await _my_equipment(db, s)}


class IncomeIn(BaseModel):
    streamer_id: uuid.UUID
    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    platform: str = Field(pattern="^(youtube|twitch|kick|tiktok)$")
    amount: float = Field(ge=0, le=10_000_000)


@router.put("/platform-income")
async def set_income(body: IncomeIn, request: Request, access: Access = Depends(require_area("finance")), db: AsyncSession = Depends(aget_db)):
    """Finance records platform income per month; it sets next month's salary tier and lands in the ledger."""
    from app.models.money import LedgerEntry

    row = (await db.execute(select(PlatformIncome).where(PlatformIncome.streamer_id == body.streamer_id, PlatformIncome.month == body.month,
                                                         PlatformIncome.platform == body.platform))).scalar_one_or_none()
    amount = round(body.amount * 100)
    if not row:
        row = PlatformIncome(streamer_id=body.streamer_id, month=body.month, platform=body.platform, amount_minor=amount)
        db.add(row)
        await db.flush()
    row.amount_minor = amount
    ledger = (await db.execute(select(LedgerEntry).where(LedgerEntry.source_table == "platform_income",
                                                         LedgerEntry.source_id == str(row.id)))).scalar_one_or_none()
    y, m = map(int, body.month.split("-"))
    if not ledger:
        db.add(LedgerEntry(on_date=date(y, m, 28), type="revenue", category="platform", amount_minor=amount,
                           source_table="platform_income", source_id=str(row.id), note=f"{body.platform.title()} {body.month}"))
    else:
        ledger.amount_minor = amount
    await audit(db, access.user, "platform_income.set", "platform_income", row.id, request=request, summary=f"{body.platform} {body.month}: {ghs(amount)}")
    await db.commit()
    return {"ok": True}
