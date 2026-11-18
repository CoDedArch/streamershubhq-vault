"""Posts: slugs, share images, the anonymous numbers (views, clicks, likes, shares) and event tickets."""

import hashlib
import io
import re
import secrets
import uuid
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crypto import digits_tail, encrypt
from app.core.security import now
from app.models.money import LedgerEntry
from app.models.posts import EMPLOYMENT, PLATFORMS, POST_KINDS, WORKPLACE, Post, PostEvent, PostLike, TicketBooking
from app.models.store import PaymentEvent
from app.models.talent import Streamer
from app.services import EmailService
from app.services.CompanyService import ACCRA
from app.services.money import ghs

LIVE_WINDOW = timedelta(hours=3)  # a stream with no end time counts as live this long after it starts
PENDING_HOLD = timedelta(minutes=30)  # an unpaid checkout holds its tickets this long
SHARE_SIZE = (1200, 630)
VIA = {"whatsapp", "x", "facebook", "telegram", "instagram", "tiktok", "linkedin", "copy", "site", "direct", "qr", "email", "sms",
       "native", "links", "team"}


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:80] or "post"


async def unique_slug(db: AsyncSession, title: str, model=Post, column: str = "slug", exclude=None) -> str:
    base = slugify(title)
    slug, n = base, 2
    col = getattr(model, column)
    while True:
        q = select(func.count()).select_from(model).where(col == slug)
        if exclude is not None:
            q = q.where(model.id != exclude)
        if not await db.scalar(q):
            return slug
        slug, n = f"{base}-{n}", n + 1


def visitor(raw: Optional[str]) -> str:
    """A one-way, salted hash: the same browser counts once, and nothing about the person is stored."""
    return hashlib.sha256(f"{settings.SECRET_KEY}:visitor:{raw or 'anon'}".encode()).hexdigest()[:32]


def ok_url(url: Optional[str], *, allow_contact: bool = False) -> Optional[str]:
    if not url:
        return None
    url = url.strip()
    if allow_contact and re.match(r"^(mailto:|tel:)", url, re.I):
        return url
    if not re.match(r"^https?://", url, re.I):
        if re.match(r"^[\w.-]+\.[a-z]{2,}(/.*)?$", url, re.I):
            url = "https://" + url
        else:
            raise HTTPException(400, "Links must start with https://")
    return url[:500]


def job_state(p: Post, today: Optional[date] = None) -> str:
    """An open role is open until its closing date (the whole of that day, Accra time) or until the team
    stops applications."""
    j = p.job or {}
    if j.get("closed"):
        return "closed"
    closes = j.get("closes_on")
    today = today or now().astimezone(ACCRA).date()
    return "closed" if closes and date.fromisoformat(closes) < today else "open"


def state(p: Post, at: Optional[datetime] = None) -> str:
    """live, upcoming, ended, or posted (news and anything without a time). Open roles: open or closed."""
    if p.kind == "job":
        return job_state(p)
    at = at or now()
    if not p.starts_at:
        return "posted"
    end = p.ends_at or (p.starts_at + LIVE_WINDOW)
    if p.starts_at <= at <= end:
        return "live"
    return "upcoming" if at < p.starts_at else "ended"


def share_image(data: bytes, layout: str) -> bytes:
    """A 1200×630 JPEG for link previews (WhatsApp wants it small). A wide thumbnail is cropped to fit; a
    tall flyer is shown whole on a blurred copy of itself, so nothing on the flyer is cut off."""
    from PIL import Image, ImageFilter, ImageOps

    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    w, h = SHARE_SIZE
    if layout == "flyer" or img.width / img.height < 1.2:
        back = ImageOps.fit(img, (w, h), Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(28))
        back = Image.blend(back, Image.new("RGB", (w, h), (20, 20, 20)), 0.45)
        fg = img.copy()
        fg.thumbnail((w - 80, h - 40), Image.Resampling.LANCZOS)
        back.paste(fg, ((w - fg.width) // 2, (h - fg.height) // 2))
        out_img = back
    else:
        out_img = ImageOps.fit(img, (w, h), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    out_img.save(out, "JPEG", quality=84, optimize=True, progressive=True)
    return out.getvalue()


def display_image(data: bytes, content_type: str) -> tuple[bytes, str]:
    """The image shown on the site: no bigger than 1600px on its long side."""
    from PIL import Image, ImageOps

    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    if max(img.size) <= 1600 and len(data) < 1_500_000:
        return data, content_type
    img.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    if img.mode in ("RGBA", "LA", "P") and content_type == "image/png":
        img.save(out, "PNG", optimize=True)
        return out.getvalue(), "image/png"
    img.convert("RGB").save(out, "JPEG", quality=86, optimize=True, progressive=True)
    return out.getvalue(), "image/jpeg"


# Numbers ----------------------------------------------------------------------------------------------

async def track(db: AsyncSession, p: Post, kind: str, who: str, via: Optional[str], target: Optional[str]) -> bool:
    """Count a view (once per visitor a day), a click (once per visitor and button every 10 minutes) or a
    share. Returns True if it counted."""
    via = via if via in VIA else ("direct" if via is None else "other")
    window = {"view": timedelta(hours=24), "click": timedelta(minutes=10), "share": timedelta(minutes=1)}[kind]
    q = select(func.count()).select_from(PostEvent).where(PostEvent.post_id == p.id, PostEvent.kind == kind,
                                                         PostEvent.visitor == who, PostEvent.created_at >= now() - window)
    if kind == "click":
        q = q.where(PostEvent.target == target)
    if await db.scalar(q):
        return False
    db.add(PostEvent(post_id=p.id, kind=kind, visitor=who, via=via, target=target if kind == "click" else None))
    if kind == "view":
        p.views = (p.views or 0) + 1
    elif kind == "click":
        p.clicks = (p.clicks or 0) + 1
    else:
        p.shares = (p.shares or 0) + 1
    await db.flush()
    return True


async def like(db: AsyncSession, p: Post, who: str, on: bool) -> tuple[bool, int]:
    row = (await db.execute(select(PostLike).where(PostLike.post_id == p.id, PostLike.visitor == who))).scalar_one_or_none()
    if on and not row:
        db.add(PostLike(post_id=p.id, visitor=who))
    elif not on and row:
        await db.delete(row)
    await db.flush()
    p.likes = await db.scalar(select(func.count()).select_from(PostLike).where(PostLike.post_id == p.id)) or 0
    return on, p.likes


async def liked(db: AsyncSession, p: Post, who: str) -> bool:
    return bool(await db.scalar(select(func.count()).select_from(PostLike).where(PostLike.post_id == p.id, PostLike.visitor == who)))


async def stats(db: AsyncSession, p: Post, days: int = 14) -> dict:
    since = now() - timedelta(days=days)
    rows = (await db.execute(select(PostEvent).where(PostEvent.post_id == p.id, PostEvent.created_at >= since))).scalars().all()
    daily: dict[str, dict] = {}
    for i in range(days - 1, -1, -1):
        d = (now() - timedelta(days=i)).astimezone(ACCRA).date().isoformat()
        daily[d] = {"date": d, "views": 0, "clicks": 0, "shares": 0}
    via: dict[str, dict] = {}
    targets: dict[str, int] = {}
    for e in rows:
        d = e.created_at.astimezone(ACCRA).date().isoformat()
        if d in daily:
            daily[d][e.kind + "s"] += 1
        v = via.setdefault(e.via or "direct", {"via": e.via or "direct", "views": 0, "clicks": 0, "shares": 0})
        v[e.kind + "s"] += 1
        if e.kind == "click":
            targets[e.target or "more"] = targets.get(e.target or "more", 0) + 1
    unique = await db.scalar(select(func.count(func.distinct(PostEvent.visitor))).where(PostEvent.post_id == p.id, PostEvent.via.is_distinct_from("team"))) or 0
    tickets = (await db.execute(select(TicketBooking.status, func.coalesce(func.sum(TicketBooking.qty), 0))
                                .where(TicketBooking.post_id == p.id).group_by(TicketBooking.status))).all()
    t = {s: int(q) for s, q in tickets}
    out = {"views": p.views, "clicks": p.clicks, "likes": p.likes, "shares": p.shares, "people": unique,
           "click_rate": round(p.clicks * 100 / p.views, 1) if p.views else None,
           "daily": list(daily.values()), "via": sorted(via.values(), key=lambda r: -(r["views"] + r["clicks"] + r["shares"])),
           "targets": targets, "tickets": t}
    if p.kind == "job":
        from app.models.contact import ContactMessage

        apps = (await db.execute(select(ContactMessage.via, func.count()).where(ContactMessage.post_id == p.id, ContactMessage.reason == "job",
                                                                                 ContactMessage.status != "spam").group_by(ContactMessage.via))).all()
        out["applications"] = sum(n for _, n in apps)
        out["applications_via"] = {(v or "direct"): n for v, n in apps}
        out["apply_rate"] = round(out["applications"] * 100 / p.views, 1) if p.views else None
    return out


# Tickets ----------------------------------------------------------------------------------------------

async def tickets_taken(db: AsyncSession, p: Post) -> int:
    q = select(func.coalesce(func.sum(TicketBooking.qty), 0)).where(
        TicketBooking.post_id == p.id,
        (TicketBooking.status.in_(("paid", "free", "interest")))
        | ((TicketBooking.status == "pending") & (TicketBooking.created_at >= now() - PENDING_HOLD)))
    return int(await db.scalar(q) or 0)


async def tickets_left(db: AsyncSession, p: Post) -> Optional[int]:
    if not p.ticket_capacity:
        return None
    return max(0, p.ticket_capacity - await tickets_taken(db, p))


def when_text(p: Post) -> str:
    if not p.starts_at:
        return ""
    local = p.starts_at.astimezone(ACCRA)
    text = local.strftime("%a %-d %b %Y · %H:%M")
    if p.ends_at:
        end = p.ends_at.astimezone(ACCRA)
        text += f" to {end.strftime('%H:%M') if end.date() == local.date() else end.strftime('%a %-d %b %H:%M')}"
    return text + " (Accra)"


async def book(db: AsyncSession, p: Post, *, mode: str, name: str, email: str, phone: str, qty: int, method: str):
    """Returns (booking, payment page URL or None)."""
    from app.services.PaymentProvider import PaymentError, provider

    if p.kind != "event" or p.status != "published":
        raise HTTPException(404, "This event isn't taking tickets.")
    if p.ticket_url:
        raise HTTPException(409, "Tickets for this event are sold on the organiser's page.")
    if not p.tickets_open or state(p) == "ended":
        raise HTTPException(409, "Tickets for this event are closed.")
    left = await tickets_left(db, p)
    if left is not None and qty > left:
        raise HTTPException(409, "Sold out." if left == 0 else f"Only {left} ticket{'s' if left > 1 else ''} left.")
    price = p.ticket_price_minor or 0
    status = "free" if price == 0 else ("interest" if mode != "checkout" else "pending")
    b = TicketBooking(post_id=p.id, code=_code(), name=name.strip(), email=email.strip().lower(), phone_enc=encrypt(phone),
                      phone_last3=digits_tail(phone, 3), qty=qty, amount_minor=price * qty, status=status,
                      payment_ref=f"SHT-{uuid.uuid4().hex[:18].upper()}" if status == "pending" else None)
    db.add(b)
    await db.flush()
    if status != "pending":
        return b, None
    callback = f"{settings.SH_FRONTEND_URL.rstrip('/')}/tickets/{b.code}?paid=1"
    try:
        url = await provider().start(reference=b.payment_ref, email=b.email, amount_minor=b.amount_minor, method=method,
                                     callback_url=callback)
    except PaymentError as exc:
        raise HTTPException(503, str(exc))
    return b, url


def _code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "SH-" + "".join(secrets.choice(alphabet) for _ in range(6))


async def mark_paid(db: AsyncSession, b: TicketBooking, p: Post, by: str, fees_minor: int = 0) -> None:
    b.status, b.paid_at = "paid", now()
    db.add(LedgerEntry(on_date=b.paid_at.astimezone(ACCRA).date(), type="revenue", category="events", amount_minor=b.amount_minor,
                       source_table="ticket_bookings", source_id=str(b.id), note=f"{p.title} · {b.qty} ticket{'s' if b.qty > 1 else ''} · {by}"[:300]))
    if fees_minor:
        db.add(LedgerEntry(on_date=b.paid_at.astimezone(ACCRA).date(), type="expense", category="payment_fees", amount_minor=fees_minor,
                           source_table="ticket_bookings", source_id=str(b.id), note=f"Tickets {b.code}"))
    await db.flush()


async def confirm_ticket_payment(db: AsyncSession, reference: str) -> Optional[TicketBooking]:
    """Verify with the gateway and mark paid once (the webhook and the return page both call this)."""
    from app.services.PaymentProvider import provider
    from app.services.SettingsService import get_setting
    from app.services.StoreService import commission_for

    b = (await db.execute(select(TicketBooking).where(TicketBooking.payment_ref == reference).with_for_update())).scalar_one_or_none()
    if not b or b.status != "pending":
        return b
    pay = provider()
    verified = await pay.verify(reference)
    if (await db.execute(select(PaymentEvent).where(PaymentEvent.event_key == verified.event_key))).scalar_one_or_none():
        return b
    db.add(PaymentEvent(provider=pay.name, event_key=verified.event_key, reference=reference,
                        payload={"status": verified.status, "amount": verified.amount_minor, "channel": verified.channel}))
    if verified.status != "success" or verified.amount_minor not in (-1, b.amount_minor):
        await db.commit()
        return b
    p = await db.get(Post, b.post_id)
    fee_bps = (await get_setting(db, "store"))["fee_bps"]
    fees = verified.fees_minor if verified.fees_minor is not None else commission_for(b.amount_minor, 1, fee_bps)
    await mark_paid(db, b, p, "paid online", fees)
    await db.commit()
    await EmailService.ticket_confirmed(b.email, b.name, p.title, when_text(p), b.code, b.qty, b.status, f"/tickets/{b.code}")
    return b


# What the website shows -------------------------------------------------------------------------------

def image_url(p: Post, api_base: str, size: str = "full") -> Optional[str]:
    key = p.share_image_key if size == "share" else p.image_key
    if not key:
        return None
    if key.startswith("/"):
        return key  # a bundled image on the website
    v = int((p.updated_at or now()).timestamp())
    return f"{api_base}/api/v1/public/posts/{p.slug}/image?size={size}&v={v}"


async def public_post(db: AsyncSession, p: Post, api_base: str, *, full: bool = False) -> dict:
    s = await db.get(Streamer, p.streamer_id) if p.streamer_id else None
    out = {"id": str(p.id), "slug": p.slug, "kind": p.kind, "kind_label": POST_KINDS.get(p.kind, p.kind), "title": p.title,
           "summary": p.summary, "topic": p.topic, "theme": p.theme, "layout": p.image_layout,
           "image_url": image_url(p, api_base), "share_image_url": image_url(p, api_base, "share"),
           "link_url": p.link_url, "platform": p.platform, "platform_label": PLATFORMS.get(p.platform or "", None),
           "starts_at": p.starts_at.isoformat() if p.starts_at else None, "ends_at": p.ends_at.isoformat() if p.ends_at else None,
           "when": when_text(p), "state": state(p), "venue": p.venue, "likes": p.likes, "pinned": p.pinned,
           "streamer": {"name": s.stage_name, "image_url": s.image_url} if s else None,
           "published_at": p.published_at.isoformat() if p.published_at else None}
    if p.kind == "event":
        left = await tickets_left(db, p)
        out["tickets"] = {"price_minor": p.ticket_price_minor or 0, "price": ghs(p.ticket_price_minor) if p.ticket_price_minor else "Free",
                          "open": bool(p.tickets_open and state(p) != "ended"), "external_url": p.ticket_url, "left": left,
                          "sold_out": left == 0}
    if p.kind == "job":
        out["job"] = job_public(p)
    if full and s:
        from app.models.posts import LinkPage

        page = (await db.execute(select(LinkPage).where(LinkPage.streamer_id == s.id))).scalar_one_or_none()
        out["streamer"]["links_slug"] = page.slug if page else None
    return out


def job_public(p: Post) -> dict:
    from app.core.access import DEPARTMENTS

    j = p.job or {}
    closes = j.get("closes_on")
    closes_text = None
    if closes:
        d = date.fromisoformat(closes)
        closes_text = d.strftime("%a %-d %b %Y")
    return {"department": j.get("department"), "department_label": dict(DEPARTMENTS).get(j.get("department") or "", None),
            "employment": j.get("employment"), "employment_label": EMPLOYMENT.get(j.get("employment") or "", None),
            "workplace": j.get("workplace"), "workplace_label": WORKPLACE.get(j.get("workplace") or "", None),
            "location": j.get("location"), "closes_on": closes, "closes_text": closes_text,
            "pay": j.get("pay") if j.get("show_pay") else None, "openings": j.get("openings") or 1,
            "responsibilities": j.get("responsibilities") or [], "requirements": j.get("requirements") or [],
            "nice_to_have": j.get("nice_to_have") or [], "apply_via": j.get("apply_via") or "form", "apply_url": j.get("apply_url"),
            "open": job_state(p) == "open" and p.status == "published"}
