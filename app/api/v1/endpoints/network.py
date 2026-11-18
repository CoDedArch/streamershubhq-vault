"""Designer Studio, Clipper Desk, and clip verification by the Social Media Manager.
A designer or clipper only ever loads their own records."""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import Access, require_area, require_role
from app.core.database import aget_db
from app.core.security import now
from app.models.clips import Clip, ClipperEarning, ClipperProfile
from app.models.money import PayrollLine, PayrollRun
from app.models.store import CommissionEntry, Design, DesignBrief, Order, OrderItem
from app.models.talent import MarkedMoment, Stream
from app.services import ClipService
from app.services.AuditService import audit
from app.services.CompanyService import day_start, month_bounds, today
from app.services.money import month_key, month_label, prev_month
from app.services.SettingsService import get_setting
from app.api.v1.endpoints.designs import design_out

router = APIRouter(tags=["network"])
PLATFORM_HOSTS = {"tiktok": ("tiktok.com",), "instagram": ("instagram.com",), "youtube_shorts": ("youtube.com", "youtu.be"),
                  "x": ("x.com", "twitter.com"), "facebook": ("facebook.com", "fb.watch")}
PLATFORM_LABEL = {"tiktok": "TikTok", "instagram": "Instagram", "youtube_shorts": "YouTube Shorts", "x": "X", "facebook": "Facebook"}
CLIP_TONE = {"submitted": "warn", "checking": "warn", "verified": "ok", "flagged": "no", "rejected": "no"}
CLIP_LABEL = {"submitted": "Checking", "checking": "Checking", "verified": "Verified", "flagged": "Flagged", "rejected": "Rejected"}


# Designer Studio -------------------------------------------------------------------------------------

@router.get("/designer/summary")
async def designer_summary(request: Request, access: Access = Depends(require_role("designer")), db: AsyncSession = Depends(aget_db)):
    uid = access.user.id
    key = month_key(today())
    start, end = month_bounds(key)
    s0 = day_start(start)
    entries = (await db.execute(
        select(CommissionEntry, OrderItem, Order).join(OrderItem, OrderItem.id == CommissionEntry.order_item_id)
        .join(Order, Order.id == OrderItem.order_id).where(CommissionEntry.designer_user_id == uid)
        .order_by(Order.paid_at.desc()))).all()
    live = [e for e in entries if e[0].status != "reversed"]
    month = [e for e in live if e[2].paid_at and e[2].paid_at >= s0]
    weeks = []
    wk_start = s0
    for i in range(5):
        wk_end = wk_start + timedelta(days=7)
        if wk_start >= day_start(end):
            break
        units = sum(e[1].qty for e in month if wk_start <= e[2].paid_at < wk_end)
        weeks.append({"label": f"Wk {i + 1}", "units": units})
        wk_start = wk_end
    top = max((w["units"] for w in weeks), default=0) or 1
    for w in weeks:
        w["height"] = max(5, round(w["units"] * 100 / top))
    next_payout = sum(e[0].amount_minor for e in live if e[0].status in ("payable",))
    pending = sum(e[0].amount_minor for e in live if e[0].status in ("pending", "on_hold"))
    pay_rules = await get_setting(db, "payroll")
    designs = (await db.execute(select(Design).where(Design.designer_user_id == uid).order_by(Design.created_at.desc()))).scalars().all()
    base = str(request.base_url).rstrip("/")
    sold = {}
    for e in month:
        sold.setdefault(e[1].product_id, [0, 0])
        sold[e[1].product_id][0] += e[1].qty
        sold[e[1].product_id][1] += e[0].amount_minor
    from app.models.store import Product

    tiles = []
    for d in designs:
        out = await design_out(db, d, base)
        product = (await db.execute(select(Product).where(Product.design_id == d.id))).scalar_one_or_none()
        if product:
            units, earned = sold.get(product.id, [0, 0])
            out["line"] = f"GHS {product.price_minor // 100:,} · {units} sold this month"
            out["earned_minor"] = earned
        elif d.status == "rejected":
            out["line"] = d.decline_reason
        else:
            out["line"] = f"Uploaded {d.created_at.strftime('%-d %b')}" + (f" · asks GHS {d.asked_price_minor // 100:,}" if d.asked_price_minor else "")
        out["shape"] = {"tee": "g-tee", "hoodie": "g-hood", "cap": "g-cap", "jersey": "g-jer", "mat": "g-mat", "poster": "g-post"}[d.product_type]
        tiles.append(out)
    sales = [{"title": f"{e[1].product_name}{' × ' + str(e[1].qty) if e[1].qty > 1 else ''}",
              "detail": f"Order {e[2].number} · {e[2].city} · {e[2].status.replace('_', ' ')}",
              "amount_minor": e[0].amount_minor if e[0].status != "reversed" else None,
              "status": e[0].status} for e in entries[:8]]
    payouts = (await db.execute(select(PayrollLine, PayrollRun).join(PayrollRun, PayrollRun.id == PayrollLine.run_id)
                                .where(PayrollLine.payee_user_id == uid).order_by(PayrollRun.period.desc()))).all()
    notes = []
    for d in designs:
        for n in d.review_notes or []:
            if n.get("stage") not in ("resubmitted",):
                notes.append({"design": d.title, **n})
    notes.sort(key=lambda n: n["at"], reverse=True)
    briefs = (await db.execute(select(DesignBrief).where(DesignBrief.is_open.is_(True)).order_by(DesignBrief.created_at.desc()))).scalars().all()
    return {
        "handle": access.user.handle, "earned_month_minor": sum(e[0].amount_minor for e in month),
        "earned_all_minor": sum(e[0].amount_minor for e in live), "next_payout_minor": next_payout,
        "pending_minor": pending, "payout_day": pay_rules["draft_day"], "month": month_label(key), "weeks": weeks,
        "designs": tiles, "sales": sales,
        "payouts": [{"period": month_label(r.period), "amount_minor": l.amount_minor, "status": l.status,
                     "paid_ref": l.paid_ref} for l, r in payouts],
        "notes": notes[:6],
        "briefs": [{"id": str(b.id), "title": b.title, "detail": b.detail,
                    "due_on": b.due_on.isoformat() if b.due_on else None} for b in briefs],
        "commission_bps": (await get_setting(db, "commission"))["default_rate_bps"],
    }


# Clipper Desk ------------------------------------------------------------------------------------------

async def _profile(db: AsyncSession, user_id) -> ClipperProfile:
    p = (await db.execute(select(ClipperProfile).where(ClipperProfile.user_id == user_id))).scalar_one_or_none()
    if not p:
        raise HTTPException(403, "Your clipper desk isn't set up. Contact the Social Media Manager.")
    return p


def _clip_row(c: Clip, stream: Optional[Stream], rate: int) -> dict:
    earned = ClipService.fixed_pay(c.views_verified or 0, rate, None) if c.status == "verified" else None
    return {"id": str(c.id), "title": c.title, "url": c.url, "platform": PLATFORM_LABEL.get(c.platform, c.platform),
            "source": f"{stream.title} · {stream.scheduled_start.strftime('%a')}" if stream else "Own pick",
            "posted_at": c.posted_at.isoformat(), "views": c.views_verified if c.status == "verified" else c.views_reported,
            "views_reported": c.views_reported, "views_verified": c.views_verified, "status": c.status,
            "status_label": CLIP_LABEL[c.status], "tone": CLIP_TONE[c.status], "earned_minor": earned,
            "flag_reason": c.flag_reason, "count_note": c.count_note,
            "count_updated_at": c.count_updated_at.isoformat() if c.count_updated_at else None, **_fetched(c)}


GAP_WARN = 0.2  # a reported count more than 20% above the platform's is worth a closer look


def _fetched(c: Clip) -> dict:
    """What the platform itself says, where we can ask it (YouTube), next to what the clipper reported."""
    gap = None
    if c.views_fetched is not None and c.views_reported:
        gap = round((c.views_reported - c.views_fetched) / max(c.views_fetched, 1) * 100)
    return {"views_fetched": c.views_fetched, "views_fetched_at": c.views_fetched_at.isoformat() if c.views_fetched_at else None,
            "platform_channel": c.platform_channel, "fetch_note": c.fetch_note, "auto_counted": c.platform == "youtube_shorts",
            "reported_gap_percent": gap, "gap_warning": gap is not None and gap > GAP_WARN * 100}


@router.get("/clipper/summary")
async def clipper_summary(access: Access = Depends(require_role("clipper")), db: AsyncSession = Depends(aget_db)):
    uid = access.user.id
    profile = await _profile(db, uid)
    key = month_key(today())
    month = await ClipService.clipper_month(db, uid, key)
    rules = await get_setting(db, "clipper_pay")
    last = (await db.execute(select(ClipperEarning).where(ClipperEarning.clipper_user_id == uid,
                                                          ClipperEarning.period == prev_month(key)))).scalar_one_or_none()
    board = await ClipService.leaderboard(db, today(), limit=10)
    mine = next((r for r in board if r["user_id"] == str(uid)), None)
    for r in board:
        r["you"] = r["user_id"] == str(uid)
    clips = (await db.execute(select(Clip).where(Clip.clipper_user_id == uid).order_by(Clip.posted_at.desc()).limit(50))).scalars().all()
    streams = {s.id: s for s in (await db.execute(select(Stream).where(Stream.id.in_([c.source_stream_id for c in clips if c.source_stream_id])))).scalars()} if clips else {}
    rate = int(rules["rate_per_1000_minor"])
    return {
        "handle": profile.handle, "status": profile.status, "strikes": profile.strikes,
        "strikes_to_pause": rules["strikes_to_pause"], "month": month_label(key),
        "verified_views": month["verified_views"], "earned_minor": month["earned_minor"],
        "rank": mine["rank"] if mine else None, "rank_move": mine["move"] if mine else None,
        "pay": {"mode": rules["mode"], "rate_per_1000_minor": rate, "checking_views": month["checking_views"],
                "potential_minor": month["potential_minor"], "paid_last_month_minor": last.amount_minor if last and last.status == "paid" else None,
                "last_month": month_label(prev_month(key)), "monthly_cap_minor": rules.get("monthly_cap_minor")},
        "clips": [_clip_row(c, streams.get(c.source_stream_id), rate) for c in clips],
        "leaderboard": board,
    }


@router.get("/clipper/footage")
async def clipper_footage(access: Access = Depends(require_role("clipper")), db: AsyncSession = Depends(aget_db)):
    from app.services import MomentService

    await _profile(db, access.user.id)
    streams = await MomentService.recent_streams(db)
    return await MomentService.board(db, streams, access.user, only_with_moments=True)


class ClipIn(BaseModel):
    url: str = Field(min_length=10, max_length=500)
    platform: str = Field(pattern="^(tiktok|instagram|youtube_shorts|x|facebook)$")
    title: str = Field(min_length=2, max_length=200)
    source_stream_id: Optional[str] = None
    moment_id: Optional[str] = None  # the marked moment this clip cuts
    posted_on: Optional[str] = None  # YYYY-MM-DD, defaults to today
    views_reported: int = Field(default=0, ge=0, le=1_000_000_000)


async def _open_period(db: AsyncSession, period: str) -> str:
    """A month whose payout run has been submitted is closed; a late clip counts in the next open month."""
    from app.services.money import next_month

    for _ in range(24):
        run = (await db.execute(select(PayrollRun).where(PayrollRun.period == period))).scalar_one_or_none()
        if not run or run.status not in ClipService.LOCKED_RUN:
            return period
        period = next_month(period)
    return period


@router.post("/clips")
async def submit_clip(body: ClipIn, request: Request, access: Access = Depends(require_role("clipper")),
                      db: AsyncSession = Depends(aget_db)):
    profile = await _profile(db, access.user.id)
    if profile.status != "active":
        raise HTTPException(403, "Your desk is paused after three strikes. Talk to the Social Media Manager.")
    url = body.url.strip()
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not any(host == h or host.endswith("." + h) for h in PLATFORM_HOSTS[body.platform]):
        raise HTTPException(400, {"message": f"That link isn't a {PLATFORM_LABEL[body.platform]} link.", "missing": ["url"]})
    url = parsed.geturl()
    if await db.scalar(select(func.count()).select_from(Clip).where(Clip.url == url)):
        raise HTTPException(409, "This clip has already been submitted.")
    stream_id = moment_id = None
    if body.moment_id:
        try:
            moment = await db.get(MarkedMoment, uuid.UUID(body.moment_id))
        except ValueError:
            moment = None
        if not moment:
            raise HTTPException(400, "Pick a moment from the footage list.")
        moment_id, stream_id = moment.id, moment.stream_id
    if body.source_stream_id and not stream_id:
        try:
            stream = await db.get(Stream, uuid.UUID(body.source_stream_id))
        except ValueError:
            stream = None
        if not stream:
            raise HTTPException(400, "Pick a stream from the list.")
        stream_id = stream.id
    posted = now()
    if body.posted_on:
        try:
            d = datetime.fromisoformat(body.posted_on).date()
        except ValueError:
            raise HTTPException(400, {"message": "Use the date picker.", "missing": ["posted_on"]})
        if d > today() or d < today() - timedelta(days=60):
            raise HTTPException(400, {"message": "Clips count for 60 days after posting.", "missing": ["posted_on"]})
        posted = datetime(d.year, d.month, d.day, 12, tzinfo=timezone.utc)
    clip = Clip(clipper_user_id=access.user.id, source_stream_id=stream_id, moment_id=moment_id, url=url, platform=body.platform,
                title=body.title.strip(), posted_at=posted, views_reported=body.views_reported, status="submitted",
                period=await _open_period(db, month_key(posted.date())))
    db.add(clip)
    await db.flush()
    await ClipService.refresh_youtube(db, [clip])  # YouTube's own count, when we can read it; never blocks the submission
    await db.commit()
    rate = int((await get_setting(db, "clipper_pay"))["rate_per_1000_minor"])
    return _clip_row(clip, None, rate)


# Verification (Social Media Manager) -------------------------------------------------------------------

social_desk = require_area("social")


@router.get("/clips")
async def clips_to_check(status: str = "submitted,checking", access: Access = Depends(social_desk), db: AsyncSession = Depends(aget_db)):
    rows = (await db.execute(select(Clip).where(Clip.status.in_(status.split(","))).order_by(Clip.posted_at.desc()).limit(100))).scalars().all()
    rate = int((await get_setting(db, "clipper_pay"))["rate_per_1000_minor"])
    out = []
    for c in rows:
        row = _clip_row(c, await db.get(Stream, c.source_stream_id) if c.source_stream_id else None, rate)
        row["handle"] = await ClipService.handle_of(db, c.clipper_user_id)
        out.append(row)
    return out


async def _clip_for_check(db: AsyncSession, clip_id: uuid.UUID) -> Clip:
    c = await db.get(Clip, clip_id, with_for_update=True)
    if not c:
        raise HTTPException(404, "Clip not found.")
    run = (await db.execute(select(PayrollRun).where(PayrollRun.period == c.period))).scalar_one_or_none()
    if run and run.status in ("approved", "paid"):
        raise HTTPException(409, f"{month_label(c.period)} is closed: its payout run was approved.")
    if run and run.status == "submitted":
        raise HTTPException(409, f"{month_label(c.period)} is closed: its payout run is waiting for the Founder. "
                                 "If a count must change, ask the Founder to send the run back first.")
    return c


async def _recalculate(db: AsyncSession, period: str, actor) -> bool:
    """A draft payout run is rebuilt as soon as a count in its month changes, so Finance always sees the real total."""
    run = (await db.execute(select(PayrollRun).where(PayrollRun.period == period))).scalar_one_or_none()
    if not run or run.status != "draft":
        return False
    from app.services import PayrollService

    await PayrollService.draft_run(db, period, actor)
    return True


@router.post("/clips/{clip_id}/refresh")
async def refresh_clip(clip_id: uuid.UUID, access: Access = Depends(social_desk), db: AsyncSession = Depends(aget_db)):
    """Ask YouTube again for this clip's views. Only changes the guide number; the verified count stays yours."""
    from app.services import YouTubeService

    c = await _clip_for_check(db, clip_id)
    if c.platform != "youtube_shorts":
        raise HTTPException(400, "Only YouTube Shorts views can be read automatically. Check this one on the platform.")
    if not YouTubeService.enabled():
        raise HTTPException(409, "YouTube counts are off: add SH_YOUTUBE_API_KEY to the API's settings.")
    await ClipService.refresh_youtube(db, [c])
    await db.commit()
    rate = int((await get_setting(db, "clipper_pay"))["rate_per_1000_minor"])
    row = _clip_row(c, await db.get(Stream, c.source_stream_id) if c.source_stream_id else None, rate)
    row["handle"] = await ClipService.handle_of(db, c.clipper_user_id)
    return row


class VerifyIn(BaseModel):
    views_verified: int = Field(ge=0, le=1_000_000_000)


@router.post("/clips/{clip_id}/verify")
async def verify_clip(clip_id: uuid.UUID, body: VerifyIn, request: Request, access: Access = Depends(social_desk),
                      db: AsyncSession = Depends(aget_db)):
    c = await _clip_for_check(db, clip_id)
    if c.status in ("flagged", "rejected"):
        raise HTTPException(409, "A flagged or rejected clip earns nothing.")
    before = {"status": c.status, "views_verified": c.views_verified}
    if c.status == "verified":
        raise HTTPException(409, "Already verified. Use Correct the count to change it, with a reason.")
    c.status, c.views_verified, c.verified_by, c.verified_at = "verified", body.views_verified, access.user.id, now()
    c.count_updated_at, c.count_note = c.verified_at, None
    await audit(db, access.user, "clip.verified", "clip", c.id, request=request, before=before,
                after={"views_verified": body.views_verified}, summary=c.title)
    await _recalculate(db, c.period, access.user)
    await db.commit()
    return {"id": str(c.id), "status": c.status, "views_verified": c.views_verified}


class CorrectIn(BaseModel):
    views_verified: int = Field(ge=0, le=1_000_000_000)
    reason: str = Field(min_length=3, max_length=200)


async def _set_count(db: AsyncSession, c: Clip, views: int, reason: str, actor, request: Optional[Request]) -> None:
    before = c.views_verified
    c.views_verified, c.count_updated_at, c.count_note = views, now(), reason.strip()
    await audit(db, actor, "clip.count_corrected", "clip", c.id, request=request, before={"views_verified": before},
                after={"views_verified": views}, summary=f"{c.title}: {before:,} → {views:,} · {reason.strip()}")


@router.post("/clips/{clip_id}/correct")
async def correct_clip(clip_id: uuid.UUID, body: CorrectIn, request: Request, access: Access = Depends(social_desk),
                       db: AsyncSession = Depends(aget_db)):
    """Change a verified count (the month-end count, or a mistake), until the month's payout run is submitted."""
    c = await _clip_for_check(db, clip_id)
    if c.status != "verified":
        raise HTTPException(409, "Only a verified clip's count can be corrected. Verify it first.")
    await _set_count(db, c, body.views_verified, body.reason, access.user, request)
    recalculated = await _recalculate(db, c.period, access.user)
    await db.commit()
    rate = int((await get_setting(db, "clipper_pay"))["rate_per_1000_minor"])
    row = _clip_row(c, await db.get(Stream, c.source_stream_id) if c.source_stream_id else None, rate)
    row["handle"], row["run_recalculated"] = await ClipService.handle_of(db, c.clipper_user_id), recalculated
    return row


async def _month_end(db: AsyncSession) -> dict:
    from app.services.money import month_key as mk

    period = await _open_period(db, mk(today()))
    draft_day = int((await get_setting(db, "payroll"))["draft_day"])
    run = (await db.execute(select(PayrollRun).where(PayrollRun.period == period))).scalar_one_or_none()
    t = today()
    due = t.replace(day=min(draft_day, 28))
    clips = (await db.execute(select(Clip).where(Clip.period == period, Clip.status.in_(("submitted", "checking", "verified")))
                              .order_by(Clip.posted_at))).scalars().all()
    stale_after = now() - timedelta(days=7)
    return {"period": period, "label": month_label(period), "draft_day": draft_day,
            "due_on": due.isoformat() if mk(t) == period else None,
            "days_left": (due - t).days if mk(t) == period and due >= t else None,
            "run_status": run.status if run else None, "clips": clips, "stale_after": stale_after}


@router.get("/clips/month-end")
async def month_end(access: Access = Depends(social_desk), db: AsyncSession = Depends(aget_db)):
    """The open month's clips: first checks still waiting, and verified counts to bring up to date before the run."""
    state = await _month_end(db)
    rate = int((await get_setting(db, "clipper_pay"))["rate_per_1000_minor"])
    rows = []
    for c in state.pop("clips"):
        row = _clip_row(c, None, rate)
        row["handle"] = await ClipService.handle_of(db, c.clipper_user_id)
        row["youtube_ahead"] = c.status == "verified" and c.views_fetched is not None and c.views_fetched > (c.views_verified or 0)
        row["stale"] = c.status == "verified" and c.platform != "youtube_shorts" and (c.count_updated_at or c.verified_at or c.posted_at) < state["stale_after"]
        row["waiting_days"] = (now() - c.created_at).days if c.status in ("submitted", "checking") else None
        rows.append(row)
    state.pop("stale_after")
    return {**state, "clips": rows,
            "totals": {"verified": sum(r["status"] == "verified" for r in rows),
                       "first_checks": sum(r["status"] in ("submitted", "checking") for r in rows),
                       "first_checks_late": sum((r["waiting_days"] or 0) >= 2 for r in rows),
                       "youtube_ahead": sum(r["youtube_ahead"] for r in rows), "stale": sum(r["stale"] for r in rows)}}


@router.post("/clips/month-end/youtube")
async def month_end_youtube(request: Request, access: Access = Depends(social_desk), db: AsyncSession = Depends(aget_db)):
    """Bring every verified YouTube clip of the open month up to YouTube's latest count, in one go."""
    state = await _month_end(db)
    if state["run_status"] in ClipService.LOCKED_RUN:
        raise HTTPException(409, f"{state['label']} is closed: its payout run has been submitted.")
    await ClipService.refresh_youtube(db, [c for c in state["clips"] if c.status == "verified"])
    changed = 0
    for c in state["clips"]:
        if c.status == "verified" and c.views_fetched is not None and c.views_fetched > (c.views_verified or 0):
            await _set_count(db, c, c.views_fetched, "Month-end count from YouTube", access.user, request)
            changed += 1
    recalculated = await _recalculate(db, state["period"], access.user) if changed else False
    await db.commit()
    return {"updated": changed, "run_recalculated": recalculated}


class FlagIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
    strike: bool = True
    reject: bool = False


@router.post("/clips/{clip_id}/flag")
async def flag_clip(clip_id: uuid.UUID, body: FlagIn, request: Request, access: Access = Depends(social_desk),
                    db: AsyncSession = Depends(aget_db)):
    c = await _clip_for_check(db, clip_id)
    c.status, c.flag_reason, c.verified_by, c.verified_at = ("rejected" if body.reject else "flagged"), body.reason.strip(), access.user.id, now()
    profile = await ClipService.add_strike(db, c.clipper_user_id) if body.strike else None
    await audit(db, access.user, "clip.flagged", "clip", c.id, request=request,
                summary=f"{c.title}: {body.reason.strip()}" + (f" · strike {profile.strikes}" if profile else ""))
    await db.commit()
    return {"id": str(c.id), "status": c.status, "strikes": profile.strikes if profile else None,
            "paused": bool(profile and profile.status == "paused")}
