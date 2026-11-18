"""What the public website shows. No contract terms, equity, projections or earnings, ever."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import aget_db
from app.models.partnerships import Brand
from app.models.people import User
from app.models.store import COLLECTIONS, Design, Product
from app.models.talent import Honour, RosterSeat, Streamer
from app.services import ClipService, S3Service
from app.services.CompanyService import today
from app.services.SettingsService import get_setting
from app.services.StoreService import product_by_line

router = APIRouter(prefix="/public", tags=["public"])


async def product_card(db: AsyncSession, p: Product, base: str = "") -> dict:
    designer = None
    if p.design_id:
        design = await db.get(Design, p.design_id)
        designer = await db.get(User, design.designer_user_id) if design and design.designer_user_id else None
    streamer = await db.get(Streamer, p.streamer_id) if p.streamer_id else None
    by = product_by_line(p, designer)
    if p.category == "roster" and streamer:
        by = f"{COLLECTIONS['roster']} · {streamer.stage_name}"
    return {"id": str(p.id), "slug": p.slug, "name": p.name, "price_minor": p.price_minor, "category": p.category,
            "collection": COLLECTIONS.get(p.category), "by": by, "tone": p.tone, "shape": f"g-{_shape(p.product_type)}",
            "tag": p.tag, "tag_tone": {"Bestseller": "y", "New": "y", "Designer": "info"}.get(p.tag or "", "k"),
            "sizes": p.sizes or [], "in_stock": p.status == "live" and p.stock > 0, "stock_low": 0 < p.stock <= 5,
            "images": [S3Service.signed_url(k, 3600, base) for k in (p.images or [])],
            "description": p.description, "designer_handle": designer.handle if designer else None}


def _shape(product_type: str) -> str:
    return {"tee": "tee", "hoodie": "hood", "cap": "cap", "jersey": "jer", "mat": "mat", "poster": "post"}.get(product_type, "tee")


@router.get("/home")
async def home(request: Request, db: AsyncSession = Depends(aget_db)):
    base = str(request.base_url).rstrip("/")
    streamers = (await db.execute(select(Streamer).where(Streamer.is_public.is_(True), Streamer.status == "signed")
                                  .order_by(Streamer.signing_number))).scalars().all()
    roster = []
    for s in streamers:
        honours = (await db.execute(select(Honour).where(Honour.streamer_id == s.id, Honour.is_public.is_(True))
                                    .order_by(Honour.position))).scalars().all()
        roster.append({"id": str(s.id), "number": f"{s.signing_number:02d}", "stage_name": s.stage_name,
                       "known_as": s.known_as, "content": s.content, "bio": s.bio, "category": s.category,
                       "image_url": s.image_url,
                       "honours": [{"n": f"{h.position:02d}", "t": h.title, "d": h.detail} for h in honours]})
    seats = (await db.execute(select(RosterSeat).where(RosterSeat.streamer_id.is_(None)).order_by(RosterSeat.number))).scalars().all()
    brands = (await db.execute(select(Brand).where(Brand.is_public.is_(True)).order_by(Brand.public_order, Brand.name))).scalars().all()
    products = (await db.execute(select(Product).where(Product.status.in_(("live", "sold_out")))
                                 .order_by(Product.position).limit(4))).scalars().all()
    on = today()
    board = await ClipService.leaderboard(db, on, limit=6)
    rules = await get_setting(db, "clipper_pay")
    commission = await get_setting(db, "commission")
    return {
        "roster": roster,
        "seats": [{"n": f"{s.number:02d}", "t": s.title, "d": s.description} for s in seats],
        "seat_count": len(streamers) + len(seats),
        "clip_table": {"week": on.isocalendar()[1], "rows": board},
        "products": [await product_card(db, p, base) for p in products],
        "partners": [{"n": b.name, "c": b.wordmark} for b in brands],
        "join": {"clipper_rate_minor": rules["rate_per_1000_minor"] if rules.get("rate_public") else None,
                 "commission_bps": commission["default_rate_bps"] if commission.get("rate_public") else None},
    }
