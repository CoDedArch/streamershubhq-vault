"""Store operations in Hub OS: orders and their next action, best sellers, sales by collection, stock alerts,
commission owed and where each cedi goes."""

import uuid
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import Access, require_area
from app.core.database import aget_db
from app.core.security import now
from app.models.people import User
from app.models.store import COLLECTIONS, CommissionEntry, Design, Order, OrderItem, Product
from app.services import EmailService, S3Service, StoreService
from app.services.AuditService import audit
from app.services.CompanyService import PAID_STATUSES, day_start, month_bounds, today
from app.services.money import month_key
from app.services.StoreService import NEXT_ACTION, STATUS_TEXT

router = APIRouter(prefix="/admin/store", tags=["store-admin"])
store_desk = require_area("store")
TONE = {"paid": "info", "packing": "warn", "to_ship": "no", "shipped": "ok", "delivered": "ok", "refund_requested": "no",
        "refunded": "", "pending": "", "cancelled": ""}
LABEL = {**STATUS_TEXT, "to_ship": "Ship today", "packing": "Packing", "paid": "Paid", "shipped": "Shipped"}


async def _order_row(db: AsyncSession, o: Order) -> dict:
    items = (await db.execute(select(OrderItem).where(OrderItem.order_id == o.id))).scalars().all()
    text = ", ".join(f"{i.product_name}{' · ' + i.size if i.size else ''}{' ×' + str(i.qty) if i.qty > 1 else ''}" for i in items)
    action = NEXT_ACTION.get(o.status)
    if o.status == "refund_requested":
        action = ("Review refund", None)
    return {"id": str(o.id), "number": o.number, "customer": _short_name(o.customer_name), "city": o.city, "items": text,
            "payment": "Mobile Money" if o.payment_method == "momo" else "Card", "total_minor": o.total_minor,
            "status": o.status, "status_label": LABEL.get(o.status, o.status), "tone": TONE.get(o.status, ""),
            "action": action[0] if action else ("View" if o.status == "delivered" else None),
            "refund_reason": o.refund_reason, "courier_ref": o.courier_ref,
            "created_at": o.created_at.isoformat(), "paid_at": o.paid_at.isoformat() if o.paid_at else None}


def _short_name(name: str) -> str:
    parts = name.split()
    return f"{parts[0]} {parts[-1][0]}." if len(parts) > 1 else name


@router.get("/summary")
async def summary(access: Access = Depends(store_desk), db: AsyncSession = Depends(aget_db)):
    key = month_key(today())
    start, end = month_bounds(key)
    s0, s1 = day_start(start), day_start(end)
    paid = (await db.execute(select(Order).where(Order.status.in_(PAID_STATUSES), Order.paid_at >= s0, Order.paid_at < s1))).scalars().all()
    started = await db.scalar(select(func.count()).select_from(Order).where(Order.created_at >= s0, Order.created_at < s1)) or 0
    open_orders = (await db.execute(select(Order).where(Order.status.in_(("paid", "packing", "to_ship", "refund_requested")))
                                    .order_by(Order.created_at.desc()))).scalars().all()
    recent = (await db.execute(select(Order).where(Order.status != "pending").order_by(Order.created_at.desc()).limit(20))).scalars().all()
    owed = await db.scalar(select(func.coalesce(func.sum(CommissionEntry.amount_minor), 0)).where(
        CommissionEntry.status.in_(("pending", "payable", "on_hold")))) or 0
    payable = await db.scalar(select(func.coalesce(func.sum(CommissionEntry.amount_minor), 0)).where(
        CommissionEntry.status == "payable")) or 0
    paid_ids = [o.id for o in paid]
    items = (await db.execute(select(OrderItem).where(OrderItem.order_id.in_(paid_ids)))).scalars().all() if paid_ids else []
    units, by_coll = defaultdict(int), defaultdict(int)
    products = {p.id: p for p in (await db.execute(select(Product))).scalars()}
    for i in items:
        units[i.product_id] += i.qty
        p = products.get(i.product_id)
        if p:
            by_coll[p.category] += i.unit_price_minor * i.qty
    total_sales = sum(by_coll.values()) or 1
    best = []
    for pid, u in sorted(units.items(), key=lambda kv: -kv[1])[:5]:
        p = products[pid]
        design = await db.get(Design, p.design_id) if p.design_id else None
        designer = await db.get(User, design.designer_user_id) if design and design.designer_user_id else None
        best.append({"name": p.name, "by": designer.handle if designer else COLLECTIONS[p.category], "units": u})
    alerts = [{"id": str(p.id), "name": p.name, "stock": p.stock, "reorder_at": p.reorder_at,
               "status": p.status} for p in products.values() if p.status in ("live", "sold_out") and p.stock <= p.reorder_at]
    alerts.sort(key=lambda a: a["stock"])
    cogs = sum(i.production_cost_minor for i in items)
    commission = sum(i.commission_minor for i in items)
    fees = sum(o.fees_minor for o in paid)
    subtotal = sum(o.subtotal_minor for o in paid)
    to_ship = sum(1 for o in open_orders if o.status == "to_ship")
    from app.models.store import STORE_MODES, StoreInterest

    mode = await StoreService.store_mode(db)
    new_interest = await db.scalar(select(func.count()).select_from(StoreInterest).where(StoreInterest.status == "new")) or 0
    return {
        "mode": {"key": mode, "label": STORE_MODES[mode], "can_switch": access.is_founder, "new_interest": new_interest},
        "kpis": {"sales_minor": subtotal, "orders": len(paid), "open_orders": len(open_orders), "to_ship_today": to_ship,
                 "commission_owed_minor": owed, "commission_payable_minor": payable,
                 "conversion_percent": round(len(paid) * 100 / started) if started else None, "checkouts_started": started},
        "orders": [await _order_row(db, o) for o in recent],
        "best_sellers": best,
        "by_collection": [{"key": k, "label": COLLECTIONS[k], "minor": by_coll.get(k, 0),
                           "percent": round(by_coll.get(k, 0) * 100 / total_sales)} for k in COLLECTIONS],
        "stock_alerts": alerts,
        "unit_economics": StoreService.unit_economics(subtotal, cogs, commission, fees),
    }


@router.get("/orders")
async def orders(status: Optional[str] = None, q: Optional[str] = None, access: Access = Depends(store_desk),
                 db: AsyncSession = Depends(aget_db)):
    query = select(Order).where(Order.status != "pending")
    if status:
        query = query.where(Order.status.in_(status.split(",")))
    if q:
        like = f"%{q.strip()}%"
        query = query.where(Order.number.ilike(like) | Order.customer_name.ilike(like) | Order.city.ilike(like))
    rows = (await db.execute(query.order_by(Order.created_at.desc()).limit(200))).scalars().all()
    return [await _order_row(db, o) for o in rows]


@router.get("/orders/{order_id}")
async def order_detail(order_id: uuid.UUID, access: Access = Depends(store_desk), db: AsyncSession = Depends(aget_db)):
    o = await db.get(Order, order_id)
    if not o:
        raise HTTPException(404, "Order not found.")
    items = (await db.execute(select(OrderItem).where(OrderItem.order_id == o.id))).scalars().all()
    row = await _order_row(db, o)
    return {**row, "customer": o.customer_name, "email": o.customer_email, "address": o.address,
            "phone_hint": f"··· {o.customer_phone_last3}" if o.customer_phone_last3 else None,
            "subtotal_minor": o.subtotal_minor, "delivery_minor": o.delivery_minor, "fees_minor": o.fees_minor,
            "history": o.history or [],
            "lines": [{"name": i.product_name, "size": i.size, "qty": i.qty, "unit_minor": i.unit_price_minor,
                       "commission_minor": i.commission_minor, "production_minor": i.production_cost_minor} for i in items],
            "split": StoreService.unit_economics(o.subtotal_minor, sum(i.production_cost_minor for i in items),
                                                 sum(i.commission_minor for i in items), o.fees_minor)}


class OrderActionIn(BaseModel):
    action: str = Field(pattern="^(advance|refund|decline_refund|cancel)$")
    courier_ref: Optional[str] = Field(default=None, max_length=80)
    note: Optional[str] = Field(default=None, max_length=500)


@router.patch("/orders/{order_id}")
async def order_action(order_id: uuid.UUID, body: OrderActionIn, request: Request, access: Access = Depends(store_desk),
                       db: AsyncSession = Depends(aget_db)):
    o = await db.get(Order, order_id, with_for_update=True)
    if not o:
        raise HTTPException(404, "Order not found.")
    before = o.status
    email_text = None
    if body.action == "advance":
        nxt = NEXT_ACTION.get(o.status)
        if not nxt:
            raise HTTPException(409, "There's no next step for this order.")
        o.status = nxt[1]
        if o.status == "shipped":
            o.shipped_at, o.courier_ref = now(), (body.courier_ref or o.courier_ref)
            email_text = "On its way" + (f" · courier reference {o.courier_ref}" if o.courier_ref else "")
        if o.status == "delivered":
            o.delivered_at = now()
            email_text = "Delivered. Enjoy it!"
    elif body.action == "refund":
        if o.status not in ("refund_requested", "paid", "packing", "to_ship", "shipped", "delivered"):
            raise HTTPException(409, "This order can't be refunded.")
        await StoreService.refund(db, o)
        email_text = "Refunded. The money goes back the way you paid."
    elif body.action == "decline_refund":
        if o.status != "refund_requested":
            raise HTTPException(409, "There's no refund request on this order.")
        if not (body.note or "").strip():
            raise HTTPException(400, "Write why, for the customer.")
        o.status = "delivered" if o.delivered_at else ("shipped" if o.shipped_at else "paid")
        await StoreService.set_commission_status(db, o, ("on_hold",), "pending")
        email_text = f"Refund not approved: {body.note.strip()}"
    elif body.action == "cancel":
        if o.status != "pending":
            raise HTTPException(409, "Only unpaid orders can be cancelled. Refund a paid order instead.")
        o.status = "cancelled"
    StoreService._log(o, o.status, access.user.name)
    await audit(db, access.user, f"order.{body.action}", "order", o.id, request=request, before={"status": before},
                after={"status": o.status}, summary=f"{o.number}: {before} → {o.status}")
    await db.commit()
    if email_text:
        await EmailService.order_status(o.customer_email, o.customer_name, o.number, email_text,
                                        f"/orders/{o.number}?code={o.tracking_code}")
    return await _order_row(db, o)


@router.get("/products")
async def products(request: Request, access: Access = Depends(store_desk), db: AsyncSession = Depends(aget_db)):
    rows = (await db.execute(select(Product).order_by(Product.position, Product.created_at))).scalars().all()
    ordered = dict((await db.execute(select(OrderItem.product_id, func.count()).group_by(OrderItem.product_id))).all())
    base = str(request.base_url).rstrip("/")
    return [_product_row(p, ordered.get(p.id, 0), base) for p in rows]


def _product_row(p: Product, orders: int, base: str) -> dict:
    return {"id": str(p.id), "slug": p.slug, "name": p.name, "collection": COLLECTIONS.get(p.category), "status": p.status,
            "status_label": PRODUCT_STATUS.get(p.status, p.status), "product_type": p.product_type,
            "price_minor": p.price_minor, "stock": p.stock, "reorder_at": p.reorder_at, "tag": p.tag, "orders": orders,
            "images": [S3Service.signed_url(k, 3600, base) for k in (p.images or [])],
            "commission_rate_bps": p.commission_rate_bps, "design_id": str(p.design_id) if p.design_id else None, "sample": p.is_sample}


# What each status means to the people running the store
PRODUCT_STATUS = {"live": "In the store", "sold_out": "Sold out · still listed", "draft": "Unpublished", "archived": "Deleted"}


class ProductIn(BaseModel):
    # live: publish · draft: unpublish (or restore a deleted one) · archived: delete, kept for the record
    status: Optional[str] = Field(default=None, pattern="^(draft|live|sold_out|archived)$")
    reorder_at: Optional[int] = Field(default=None, ge=0, le=1000)
    tag: Optional[str] = Field(default=None, max_length=30)
    position: Optional[int] = None
    restock: Optional[int] = Field(default=None, ge=1, le=5000)


@router.patch("/products/{product_id}")
async def update_product(product_id: uuid.UUID, body: ProductIn, request: Request, access: Access = Depends(store_desk),
                         db: AsyncSession = Depends(aget_db)):
    p = await db.get(Product, product_id, with_for_update=True)
    if not p:
        raise HTTPException(404, "Product not found.")
    before = {"status": p.status, "stock": p.stock}
    if body.status == "live":
        design = await db.get(Design, p.design_id) if p.design_id else None
        if not design or design.status != "live":
            raise HTTPException(409, "Only a design approved by the Founder can go live.")
        if p.stock <= 0 and not body.restock:
            raise HTTPException(409, "Add stock before putting it live.")
    if body.restock:
        p.stock += body.restock
        if p.status == "sold_out":
            p.status = "live"
    for field in ("status", "reorder_at", "tag", "position"):
        value = getattr(body, field)
        if value is not None:
            setattr(p, field, value)
    what = {"live": "published", "draft": "unpublished" if before["status"] != "archived" else "restored",
            "archived": "deleted"}.get(body.status or "", "updated")
    await audit(db, access.user, f"product.{what}", "product", p.id, request=request, before=before,
                after={"status": p.status, "stock": p.stock}, summary=p.name)
    await db.commit()
    return {"id": str(p.id), "status": p.status, "stock": p.stock, "status_label": PRODUCT_STATUS.get(p.status, p.status)}


@router.post("/products/remove-samples")
async def remove_sample_products(request: Request, access: Access = Depends(store_desk), db: AsyncSession = Depends(aget_db)):
    """Remove every sample product from the store, ready for the real line. Like deleting one: gone for good if
    nobody ordered it, hidden (and kept for the record) if someone did."""
    removed = hidden = 0
    for p in (await db.execute(select(Product).where(Product.is_sample.is_(True)))).scalars().all():
        if await db.scalar(select(func.count()).select_from(OrderItem).where(OrderItem.product_id == p.id)):
            p.status, p.is_sample = "archived", False
            hidden += 1
        else:
            await db.delete(p)
            removed += 1
    await audit(db, access.user, "product.samples_removed", "product", None, request=request,
                summary=f"Sample products removed from the store ({removed} deleted, {hidden} hidden with orders on record)")
    await db.commit()
    return {"removed": removed, "kept": hidden}


@router.delete("/products/{product_id}")
async def delete_product(product_id: uuid.UUID, request: Request, access: Access = Depends(store_desk),
                         db: AsyncSession = Depends(aget_db)):
    """Gone for good if nobody ever ordered it. With orders it is hidden instead, so receipts, refunds and
    commission still add up, and it can be restored."""
    p = await db.get(Product, product_id, with_for_update=True)
    if not p:
        raise HTTPException(404, "Product not found.")
    orders = await db.scalar(select(func.count()).select_from(OrderItem).where(OrderItem.product_id == p.id)) or 0
    if orders:
        before = p.status
        p.status = "archived"
        await audit(db, access.user, "product.deleted", "product", p.id, request=request, before={"status": before},
                    after={"status": "archived"}, summary=f"{p.name} (hidden: {orders} order line{'s' if orders != 1 else ''} on record)")
        await db.commit()
        return {"deleted": False, "archived": True, "orders": orders}
    await audit(db, access.user, "product.deleted", "product", p.id, request=request, before={"name": p.name, "slug": p.slug},
                summary=f"{p.name} (removed for good)")
    await db.delete(p)
    await db.commit()
    return {"deleted": True, "archived": False, "orders": 0}


@router.post("/products/{product_id}/images")
async def add_image(product_id: uuid.UUID, request: Request, file: UploadFile = File(...), access: Access = Depends(store_desk),
                    db: AsyncSession = Depends(aget_db)):
    p = await db.get(Product, product_id, with_for_update=True)
    if not p:
        raise HTTPException(404, "Product not found.")
    if len(p.images or []) >= 8:
        raise HTTPException(400, "A product can have up to 8 photos.")
    data = await file.read()
    try:
        S3Service.check_file(data, file.content_type or "", ("image",))
    except S3Service.UploadRejected as exc:
        raise HTTPException(400, str(exc))
    key = S3Service.put("products", file.filename or "photo", data, file.content_type)
    p.images = [*(p.images or []), key]
    await audit(db, access.user, "product.photo_added", "product", p.id, request=request, summary=p.name)
    await db.commit()
    return _product_row(p, 0, str(request.base_url).rstrip("/"))


class ImageAction(BaseModel):
    index: int = Field(ge=0, le=7)
    action: str = Field(pattern="^(remove|cover)$")


@router.post("/products/{product_id}/images/arrange")
async def arrange_images(product_id: uuid.UUID, body: ImageAction, request: Request, access: Access = Depends(store_desk),
                         db: AsyncSession = Depends(aget_db)):
    p = await db.get(Product, product_id, with_for_update=True)
    if not p:
        raise HTTPException(404, "Product not found.")
    images = list(p.images or [])
    if body.index >= len(images):
        raise HTTPException(400, "That photo isn't there any more.")
    chosen = images.pop(body.index)
    if body.action == "cover":
        images.insert(0, chosen)
    p.images = images
    await audit(db, access.user, f"product.photo_{'removed' if body.action == 'remove' else 'cover'}", "product", p.id, request=request,
                summary=p.name)
    await db.commit()
    return _product_row(p, 0, str(request.base_url).rstrip("/"))


# Interest (while the store takes interest instead of payment) -----------------------------------------

INTEREST_STATUS = {"new": ("New", "y"), "contacted": ("Contacted", "info"), "converted": ("Paid and sorted", "ok"),
                   "closed": ("Closed", "")}


def _interest_row(r, reveal_phone: bool) -> dict:
    from app.core.crypto import decrypt

    phone = None
    if reveal_phone and r.phone_enc:
        try:
            phone = decrypt(r.phone_enc)
        except Exception:
            phone = None
    return {"id": str(r.id), "number": r.number, "name": r.name, "email": r.email, "city": r.city,
            "phone": phone, "phone_hint": r.phone_last3, "items": r.items, "total_minor": r.total_minor,
            "message": r.message, "status": r.status, "status_label": INTEREST_STATUS[r.status][0],
            "tone": INTEREST_STATUS[r.status][1], "notes": r.notes or [], "created_at": r.created_at.isoformat()}


@router.get("/interests")
async def interests(status: Optional[str] = None, access: Access = Depends(store_desk), db: AsyncSession = Depends(aget_db)):
    from app.models.store import StoreInterest

    q = select(StoreInterest)
    if status:
        q = q.where(StoreInterest.status.in_(status.split(",")))
    rows = (await db.execute(q.order_by(StoreInterest.created_at.desc()).limit(300))).scalars().all()
    counts = dict((await db.execute(select(StoreInterest.status, func.count()).group_by(StoreInterest.status))).all())
    return {"rows": [_interest_row(r, True) for r in rows], "counts": {k: counts.get(k, 0) for k in INTEREST_STATUS},
            "mode": await StoreService.store_mode(db), "can_switch": access.is_founder}


class InterestUpdate(BaseModel):
    status: Optional[str] = Field(default=None, pattern="^(new|contacted|converted|closed)$")
    note: Optional[str] = Field(default=None, min_length=2, max_length=500)


@router.patch("/interests/{interest_id}")
async def update_interest(interest_id: uuid.UUID, body: InterestUpdate, request: Request, access: Access = Depends(store_desk),
                          db: AsyncSession = Depends(aget_db)):
    from app.models.store import StoreInterest

    r = await db.get(StoreInterest, interest_id, with_for_update=True)
    if not r:
        raise HTTPException(404, "Not found.")
    before = r.status
    if body.status:
        r.status = body.status
    if body.note:
        r.notes = [*(r.notes or []), {"at": now().isoformat(), "by": access.user.name, "note": body.note.strip()}]
    r.handled_by = access.user.id
    await audit(db, access.user, "store.interest_updated", "store_interest", r.number, request=request,
                summary=f"{r.number} · {INTEREST_STATUS[before][0]} → {INTEREST_STATUS[r.status][0]}" + (f" · {body.note.strip()}" if body.note else ""))
    await db.commit()
    return _interest_row(r, True)
