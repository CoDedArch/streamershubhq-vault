"""The public store: products, bag pricing, checkout, payment confirmation, order tracking and refund requests.
Guests can buy; prices always come from the database."""

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from app.core.types import Email
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import aget_db
from app.core.security import get_optional_user, now
from app.models.people import User
from app.models.store import COLLECTIONS, Order, OrderItem, Product
from app.services import EmailService, StoreService
from app.services.PaymentProvider import TEST_OUTCOMES, PaymentError, provider
from app.services.SettingsService import get_setting
from app.api.v1.endpoints.public import product_card

router = APIRouter(tags=["store"])


@router.get("/store/config")
async def store_config(db: AsyncSession = Depends(aget_db)):
    """What the website needs to know: whether the store takes payment or interest right now."""
    from app.models.store import STORE_MODES

    mode = await StoreService.store_mode(db)
    return {"mode": mode, "label": STORE_MODES[mode], "delivery_options": await StoreService.delivery_options(db),
            "currency": await StoreService.currencies(db)}


@router.get("/products")
async def products(request: Request, collection: Optional[str] = None, db: AsyncSession = Depends(aget_db)):
    q = select(Product).where(Product.status.in_(("live", "sold_out")))
    if collection:
        if collection not in COLLECTIONS:
            raise HTTPException(400, "Unknown collection.")
        q = q.where(Product.category == collection)
    rows = (await db.execute(q.order_by(Product.position, Product.created_at))).scalars().all()
    base = str(request.base_url).rstrip("/")
    return {"products": [await product_card(db, p, base) for p in rows],
            "collections": [{"key": k, "label": v} for k, v in COLLECTIONS.items()]}


@router.get("/products/{slug}")
async def product(slug: str, request: Request, db: AsyncSession = Depends(aget_db)):
    p = (await db.execute(select(Product).where(Product.slug == slug))).scalar_one_or_none()
    if not p or p.status not in ("live", "sold_out"):
        raise HTTPException(404, "This product isn't in the store.")
    base = str(request.base_url).rstrip("/")
    more = (await db.execute(select(Product).where(Product.status == "live", Product.category == p.category,
                                                   Product.id != p.id).order_by(Product.position).limit(4))).scalars().all()
    return {**await product_card(db, p, base), "stock_left": p.stock if p.stock <= 5 else None,
            "more": [await product_card(db, m, base) for m in more]}


class BagItem(BaseModel):
    product_id: Optional[str] = None
    slug: Optional[str] = None
    qty: int = Field(ge=1, le=20)
    size: Optional[str] = Field(default=None, max_length=10)


class QuoteIn(BaseModel):
    items: list[BagItem] = Field(max_length=30)
    zone: Optional[str] = None


def _quote_out(q: dict) -> dict:
    return {"lines": [{k: v for k, v in line.items() if k != "product"} for line in q["lines"]],
            "subtotal_minor": q["subtotal_minor"], "delivery_minor": q["delivery_minor"], "total_minor": q["total_minor"],
            "problems": q["problems"], "delivery_options": q["delivery_options"]}


@router.post("/cart/quote")
async def cart_quote(body: QuoteIn, db: AsyncSession = Depends(aget_db)):
    if not body.items:
        return {"lines": [], "subtotal_minor": 0, "delivery_minor": None, "total_minor": 0, "problems": [],
                "delivery_options": await StoreService.delivery_options(db)}
    return _quote_out(await StoreService.quote(db, [i.model_dump() for i in body.items], body.zone))


class InterestIn(BaseModel):
    items: list[BagItem] = Field(min_length=1, max_length=30)
    name: str = Field(min_length=2, max_length=120)
    phone: str = Field(min_length=9, max_length=20)
    email: Optional[Email] = None
    city: Optional[str] = Field(default=None, max_length=80)
    message: Optional[str] = Field(default=None, max_length=1000)


@router.post("/store/interest")
async def register_interest(body: InterestIn, db: AsyncSession = Depends(aget_db)):
    if sum(1 for c in body.phone if c.isdigit()) < 9:
        raise HTTPException(400, {"message": "Enter a phone number we can call.", "missing": ["phone"]})
    row = await StoreService.create_interest(db, items=[i.model_dump() for i in body.items], name=body.name, email=body.email,
                                             phone=body.phone, city=body.city, message=body.message)
    await db.commit()
    if row.email:
        await EmailService.interest_received(row.email, row.name, row.number,
                                             [f"{i['name']}{' · ' + i['size'] if i['size'] else ''} ×{i['qty']}" for i in row.items],
                                             row.total_minor)
    return {"number": row.number, "total_minor": row.total_minor}


class CheckoutIn(BaseModel):
    items: list[BagItem] = Field(min_length=1, max_length=30)
    zone: str
    method: str = Field(pattern="^(momo|card)$")
    name: str = Field(min_length=2, max_length=120)
    email: Email
    phone: str = Field(min_length=9, max_length=20)
    address: str = Field(min_length=3, max_length=300)
    city: str = Field(min_length=2, max_length=80)


@router.post("/orders")
async def checkout(body: CheckoutIn, db: AsyncSession = Depends(aget_db), user: Optional[User] = Depends(get_optional_user)):
    if sum(1 for c in body.phone if c.isdigit()) < 9:
        raise HTTPException(400, {"message": "Enter a phone number we can call about delivery.", "missing": ["phone"]})
    order, url = await StoreService.create_order(
        db, items=[i.model_dump() for i in body.items], zone=body.zone, method=body.method, name=body.name,
        email=body.email, phone=body.phone, address=body.address, city=body.city, customer=user)
    await db.commit()
    return {"number": order.number, "code": order.tracking_code, "total_minor": order.total_minor, "pay_url": url,
            "test_mode": provider().name == "test"}


async def _order_for(db: AsyncSession, number: str, code: Optional[str] = None, email: Optional[str] = None) -> Order:
    order = (await db.execute(select(Order).where(Order.number == number.strip().upper()))).scalar_one_or_none()
    if not order or not ((code and code == order.tracking_code) or (email and email.strip().lower() == order.customer_email)):
        raise HTTPException(404, "We couldn't find that order. Check the number and email.")
    return order


async def order_view(db: AsyncSession, order: Order) -> dict:
    items = (await db.execute(select(OrderItem).where(OrderItem.order_id == order.id))).scalars().all()
    steps = ["paid", "packing", "to_ship", "shipped", "delivered"]
    reached = steps.index(order.status) if order.status in steps else -1
    window = (await get_setting(db, "store"))["refund_window_days"]
    can_refund = order.status in ("paid", "packing", "to_ship", "shipped") or (
        order.status == "delivered" and order.delivered_at and (now() - order.delivered_at).days <= window)
    return {"number": order.number, "code": order.tracking_code, "status": order.status,
            "status_text": StoreService.STATUS_TEXT[order.status], "name": order.customer_name, "city": order.city,
            "address": order.address, "payment_method": order.payment_method, "phone_hint": order.customer_phone_last3,
            "subtotal_minor": order.subtotal_minor, "delivery_minor": order.delivery_minor, "total_minor": order.total_minor,
            "created_at": order.created_at.isoformat(), "paid_at": order.paid_at.isoformat() if order.paid_at else None,
            "courier_ref": order.courier_ref, "can_request_refund": bool(can_refund), "refund_window_days": window,
            "timeline": [{"key": s, "label": {"paid": "Paid", "packing": "Packing", "to_ship": "Packed",
                                               "shipped": "Shipped", "delivered": "Delivered"}[s], "done": i <= reached}
                         for i, s in enumerate(steps)],
            "items": [{"name": i.product_name, "size": i.size, "qty": i.qty, "unit_minor": i.unit_price_minor,
                       "line_minor": i.unit_price_minor * i.qty} for i in items]}


@router.get("/orders/{number}")
async def track(number: str, code: Optional[str] = None, email: Optional[str] = None, db: AsyncSession = Depends(aget_db)):
    return await order_view(db, await _order_for(db, number, code, email))


@router.post("/orders/{number}/verify")
async def verify_payment(number: str, code: str, db: AsyncSession = Depends(aget_db)):
    """Called when the customer comes back from the payment page. Safe to call more than once."""
    order = await _order_for(db, number, code)
    if order.status == "pending":
        try:
            verified = await provider().verify(order.payment_ref)
        except PaymentError as exc:
            raise HTTPException(503, str(exc))
        if await StoreService.confirm_payment(db, order, verified):
            items = (await db.execute(select(OrderItem).where(OrderItem.order_id == order.id))).scalars().all()
            await db.commit()
            await StoreService.after_paid_email(order, list(items))
        else:
            await db.commit()
    return await order_view(db, order)


class TestPayIn(BaseModel):
    outcome: str = Field(pattern="^(success|failed)$")


@router.post("/payments/test/{reference}")
async def test_pay(reference: str, body: TestPayIn, db: AsyncSession = Depends(aget_db)):
    """Only in test mode (no Paystack key, not production): stands in for the payment page."""
    if provider().name != "test":
        raise HTTPException(404, "Not found.")
    order = (await db.execute(select(Order).where(Order.payment_ref == reference))).scalar_one_or_none()
    if not order:
        from app.models.posts import TicketBooking

        booking = (await db.execute(select(TicketBooking).where(TicketBooking.payment_ref == reference))).scalar_one_or_none()
        if not booking:
            raise HTTPException(404, "Not found.")
        TEST_OUTCOMES[reference] = body.outcome
        return {"ticket": booking.code}
    TEST_OUTCOMES[reference] = body.outcome
    return {"number": order.number, "code": order.tracking_code}


@router.post("/payments/webhook")
async def payment_webhook(request: Request, db: AsyncSession = Depends(aget_db)):
    body = await request.body()
    pay = provider()
    if not pay.webhook_ok(body, request.headers.get("x-paystack-signature")):
        raise HTTPException(401, "Bad signature.")
    event = json.loads(body or b"{}")
    data = event.get("data") or {}
    reference = data.get("reference")
    if event.get("event") != "charge.success" or not reference:
        return {"ok": True}
    order = (await db.execute(select(Order).where(Order.payment_ref == reference))).scalar_one_or_none()
    if not order:
        from app.services import PostService

        await PostService.confirm_ticket_payment(db, reference)
        return {"ok": True}
    verified = await pay.verify(reference)  # never trust the webhook body for the amount
    if await StoreService.confirm_payment(db, order, verified):
        items = (await db.execute(select(OrderItem).where(OrderItem.order_id == order.id))).scalars().all()
        await db.commit()
        await StoreService.after_paid_email(order, list(items))
    else:
        await db.commit()
    return {"ok": True}


class RefundIn(BaseModel):
    code: str
    reason: str = Field(min_length=5, max_length=1000)


@router.post("/orders/{number}/refund-request")
async def request_refund(number: str, body: RefundIn, db: AsyncSession = Depends(aget_db)):
    order = await _order_for(db, number, body.code)
    view = await order_view(db, order)
    if not view["can_request_refund"]:
        raise HTTPException(409, "This order can't be refunded online. Email us and we'll help.")
    order.status, order.refund_reason = "refund_requested", body.reason.strip()
    StoreService._log(order, "refund_requested", "customer")
    await StoreService.set_commission_status(db, order, ("pending", "payable"), "on_hold")
    await db.commit()
    await EmailService.order_status(order.customer_email, order.customer_name, order.number,
                                    "Refund requested. We'll reply within 2 working days.",
                                    f"/orders/{order.number}?code={order.tracking_code}")
    return await order_view(db, order)


