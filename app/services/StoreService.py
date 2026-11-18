"""Store rules: prices come from the database (never the cart), stock, delivery, payment confirmation,
designer commission (6.3) and where each cedi goes (7.4)."""

import secrets
import uuid
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import digits_tail, encrypt
from app.core.security import now
from app.models.money import LedgerEntry
from app.models.store import COLLECTIONS, CommissionEntry, Design, Order, OrderItem, PaymentEvent, Product
from app.models.people import User
from app.services import EmailService
from app.services.PaymentProvider import PaymentError, Verified, provider
from app.services.SettingsService import get_setting

STATUS_TEXT = {"pending": "Waiting for payment", "paid": "Paid", "packing": "Being packed", "to_ship": "Ready to ship",
               "shipped": "On its way", "delivered": "Delivered", "refund_requested": "Refund requested",
               "refunded": "Refunded", "cancelled": "Cancelled"}
NEXT_ACTION = {"paid": ("Start packing", "packing"), "packing": ("Mark packed", "to_ship"),
               "to_ship": ("Mark shipped", "shipped"), "shipped": ("Mark delivered", "delivered")}


def commission_for(unit_minor: int, qty: int, bps: int) -> int:
    return int((Decimal(unit_minor) * qty * Decimal(bps) / Decimal(10000)).quantize(Decimal(1), ROUND_HALF_UP))


def product_by_line(p: Product, designer: Optional[User]) -> str:
    if p.category == "designer" and designer:
        return f"Designed by {designer.handle or designer.name}"
    return COLLECTIONS.get(p.category, p.category)


async def currencies(db: AsyncSession) -> dict:
    """The currencies the website can show prices in. Payment is always in cedis; dollars are shown at the
    Founder's rate once it's set."""
    s = await get_setting(db, "store")
    rate = s.get("ghs_per_usd")
    options = [{"code": "GHS", "label": "Ghana cedis", "short": "GHS", "ghs_per_unit": 1}]
    if rate:
        options.append({"code": "USD", "label": "US dollars", "short": "USD", "ghs_per_unit": float(rate)})
    default = s.get("default_currency") if rate and s.get("default_currency") in ("GHS", "USD") else "GHS"
    when = s.get("rate_updated_on")
    note = f"1 USD = GHS {float(rate):,.2f}" + (f", set {date.fromisoformat(when).strftime('%-d %b %Y')}" if when else "") if rate else None
    return {"charge": "GHS", "default": default, "options": options, "rate_note": note}


async def store_mode(db: AsyncSession) -> str:
    """"interest" or "checkout". Checkout falls back to interest if payments can't run (no key in production)."""
    mode = (await get_setting(db, "store")).get("mode", "interest")
    if mode == "checkout":
        try:
            provider()
        except PaymentError:
            return "interest"
    return mode


async def delivery_options(db: AsyncSession) -> list[dict]:
    return (await get_setting(db, "store"))["delivery"]


async def quote(db: AsyncSession, items: list[dict], zone: Optional[str]) -> dict:
    """Price a bag from the database. Each item: {product_id or slug, qty, size}."""
    if not items:
        raise HTTPException(400, "Your bag is empty.")
    lines, subtotal, problems = [], 0, []
    for raw in items:
        qty = int(raw.get("qty") or 0)
        if qty < 1 or qty > 20:
            raise HTTPException(400, "Quantities must be between 1 and 20.")
        p = None
        if raw.get("product_id"):
            try:
                p = await db.get(Product, uuid.UUID(str(raw["product_id"])))
            except ValueError:
                p = None
        elif raw.get("slug"):
            p = (await db.execute(select(Product).where(Product.slug == raw["slug"]))).scalar_one_or_none()
        if not p or p.status not in ("live", "sold_out"):
            problems.append({"product_id": raw.get("product_id"), "problem": "This item is no longer in the store."})
            continue
        size = raw.get("size") or None
        if p.sizes and size not in p.sizes:
            problems.append({"product_id": str(p.id), "problem": f"Pick a size for {p.name}."})
            continue
        if p.status == "sold_out" or p.stock <= 0:
            problems.append({"product_id": str(p.id), "problem": f"{p.name} is sold out."})
            continue
        if qty > p.stock:
            problems.append({"product_id": str(p.id), "problem": f"Only {p.stock} {p.name} left."})
            continue
        line = p.price_minor * qty
        subtotal += line
        lines.append({"product": p, "product_id": str(p.id), "slug": p.slug, "name": p.name, "size": size, "qty": qty,
                      "unit_minor": p.price_minor, "line_minor": line})
    options = await delivery_options(db)
    chosen = next((o for o in options if o["zone"] == zone), None)
    delivery = chosen["minor"] if chosen else None
    return {"lines": lines, "subtotal_minor": subtotal, "delivery_minor": delivery,
            "total_minor": subtotal + (delivery or 0), "problems": problems, "delivery_options": options}


async def next_order_number(db: AsyncSession) -> str:
    n = await db.scalar(text("SELECT nextval('order_number_seq')"))
    return f"SH-{n}"


async def create_order(db: AsyncSession, *, items, zone, method, name, email, phone, address, city,
                       customer: Optional[User]) -> tuple[Order, str]:
    if await store_mode(db) != "checkout":
        raise HTTPException(409, "We're taking orders by message for now. Send us your list and we'll call you to arrange payment.")
    q = await quote(db, items, zone)
    if q["problems"]:
        raise HTTPException(409, {"message": "Some items in your bag changed.", "problems": q["problems"]})
    if q["delivery_minor"] is None:
        raise HTTPException(400, "Choose a delivery option.")
    if method not in ("momo", "card"):
        raise HTTPException(400, "Pay by mobile money or card.")
    order = Order(
        number=await next_order_number(db), customer_user_id=customer.id if customer else None,
        customer_name=name.strip(), customer_email=email.strip().lower(), customer_phone_enc=encrypt(phone),
        customer_phone_last3=digits_tail(phone, 3), address=address.strip(), city=city.strip(), delivery_zone=zone,
        subtotal_minor=q["subtotal_minor"], delivery_minor=q["delivery_minor"], total_minor=q["total_minor"],
        payment_method=method, payment_ref=f"SHP-{uuid.uuid4().hex[:18].upper()}",
        tracking_code=secrets.token_urlsafe(9), status="pending",
        history=[{"at": now().isoformat(), "status": "pending", "by": "customer"}],
    )
    db.add(order)
    await db.flush()
    for line in q["lines"]:
        p: Product = line["product"]
        db.add(OrderItem(order_id=order.id, product_id=p.id, product_name=p.name, size=line["size"], qty=line["qty"],
                         unit_price_minor=p.price_minor,
                         commission_minor=commission_for(p.price_minor, line["qty"], p.commission_rate_bps),
                         production_cost_minor=p.production_cost_minor * line["qty"]))
    from app.core.config import settings

    callback = f"{settings.SH_FRONTEND_URL.rstrip('/')}/checkout/complete?order={order.number}&code={order.tracking_code}"
    try:
        url = await provider().start(reference=order.payment_ref, email=order.customer_email,
                                     amount_minor=order.total_minor, method=method, callback_url=callback)
    except PaymentError as exc:
        raise HTTPException(503, str(exc))
    return order, url


def _log(order: Order, status: str, by: str) -> None:
    order.history = [*(order.history or []), {"at": now().isoformat(), "status": status, "by": by}]


async def confirm_payment(db: AsyncSession, order: Order, verified: Verified) -> bool:
    """Apply a verified payment once. Returns True if this call marked the order paid."""
    seen = (await db.execute(select(PaymentEvent).where(PaymentEvent.event_key == verified.event_key))).scalar_one_or_none()
    if seen or order.status != "pending":
        return False
    db.add(PaymentEvent(provider=provider().name, event_key=verified.event_key, reference=order.payment_ref,
                        payload={"status": verified.status, "amount": verified.amount_minor, "channel": verified.channel}))
    if verified.status != "success":
        await db.flush()
        return False
    if verified.amount_minor not in (-1, order.total_minor):  # -1: the test provider
        _log(order, "pending", "system: amount did not match")
        await db.flush()
        return False
    fee_bps = (await get_setting(db, "store"))["fee_bps"]
    order.fees_minor = verified.fees_minor if verified.fees_minor is not None else commission_for(order.total_minor, 1, fee_bps)
    order.status, order.paid_at = "paid", now()
    _log(order, "paid", "payment provider")
    items = (await db.execute(select(OrderItem).where(OrderItem.order_id == order.id))).scalars().all()
    for item in items:
        p = await db.get(Product, item.product_id, with_for_update=True)
        p.stock = max(0, p.stock - item.qty)
        if p.stock == 0:
            p.status = "sold_out"
        design = await db.get(Design, p.design_id) if p.design_id else None
        if design and design.designer_user_id and item.commission_minor:
            db.add(CommissionEntry(designer_user_id=design.designer_user_id, order_item_id=item.id,
                                   amount_minor=item.commission_minor, status="pending"))
    day = order.paid_at.date()
    db.add(LedgerEntry(on_date=day, type="revenue", category="store", amount_minor=order.subtotal_minor,
                       source_table="orders", source_id=str(order.id), note=f"Order {order.number}"))
    cogs = sum(i.production_cost_minor for i in items)
    if cogs:
        db.add(LedgerEntry(on_date=day, type="expense", category="store_cogs", amount_minor=cogs,
                           source_table="orders", source_id=str(order.id), note=f"Order {order.number}"))
    if order.fees_minor:
        db.add(LedgerEntry(on_date=day, type="expense", category="payment_fees", amount_minor=order.fees_minor,
                           source_table="orders", source_id=str(order.id), note=f"Order {order.number}"))
    await db.flush()
    return True


async def after_paid_email(order: Order, items: list[OrderItem]) -> None:
    lines = [f"{i.product_name}{' · ' + i.size if i.size else ''} ×{i.qty}" for i in items]
    await EmailService.order_confirmed(order.customer_email, order.customer_name, order.number, order.total_minor, lines,
                                       f"/orders/{order.number}?code={order.tracking_code}")


async def set_commission_status(db: AsyncSession, order: Order, frm: tuple[str, ...], to: str) -> None:
    items = (await db.execute(select(OrderItem.id).where(OrderItem.order_id == order.id))).scalars().all()
    if not items:
        return
    rows = (await db.execute(select(CommissionEntry).where(CommissionEntry.order_item_id.in_(items),
                                                           CommissionEntry.status.in_(frm)))).scalars().all()
    for row in rows:
        row.status = to


async def make_commissions_payable(db: AsyncSession) -> int:
    """Delivered and past the refund window: pending commission becomes payable."""
    days = (await get_setting(db, "store"))["refund_window_days"]
    cutoff = now() - timedelta(days=days)
    rows = (await db.execute(
        select(CommissionEntry).join(OrderItem, OrderItem.id == CommissionEntry.order_item_id)
        .join(Order, Order.id == OrderItem.order_id)
        .where(CommissionEntry.status == "pending", Order.status == "delivered", Order.delivered_at <= cutoff)
    )).scalars().all()
    for row in rows:
        row.status = "payable"
    await db.flush()
    return len(rows)


async def refund(db: AsyncSession, order: Order) -> None:
    """Mark refunded: reverse commission and the sale in the ledger, and put stock back."""
    order.status = "refunded"
    _log(order, "refunded", "store")
    await set_commission_status(db, order, ("pending", "on_hold", "payable"), "reversed")
    items = (await db.execute(select(OrderItem).where(OrderItem.order_id == order.id))).scalars().all()
    for item in items:
        p = await db.get(Product, item.product_id)
        p.stock += item.qty
        if p.status == "sold_out" and p.stock > 0:
            p.status = "live"
    db.add(LedgerEntry(on_date=now().date(), type="revenue", category="store", amount_minor=-order.subtotal_minor,
                       source_table="refunds", source_id=str(order.id), note=f"Refund {order.number}"))


def unit_economics(subtotal: int, cogs: int, commission: int, fees: int) -> dict:
    net = subtotal - cogs - commission - fees
    total = subtotal or 1
    parts = [("Production", cogs), ("Designer commission", commission), ("Payment fees", fees), ("Net to Streamers Hub", net)]
    return {"subtotal_minor": subtotal, "parts": [{"label": l, "minor": v, "percent": round(v * 100 / total)} for l, v in parts]}


async def create_interest(db: AsyncSession, *, items: list[dict], name: str, email: Optional[str], phone: str,
                          city: Optional[str], message: Optional[str]):
    """Interest-only mode: keep what someone wants. Prices still come from the database; sold-out items can
    still be asked for (the team may restock), but items no longer in the store can't."""
    from app.models.store import StoreInterest

    lines, total = [], 0
    for raw in items:
        qty = int(raw.get("qty") or 0)
        if qty < 1 or qty > 20:
            raise HTTPException(400, "Quantities must be between 1 and 20.")
        p = None
        if raw.get("product_id"):
            try:
                p = await db.get(Product, uuid.UUID(str(raw["product_id"])))
            except ValueError:
                p = None
        elif raw.get("slug"):
            p = (await db.execute(select(Product).where(Product.slug == raw["slug"]))).scalar_one_or_none()
        if not p or p.status not in ("live", "sold_out"):
            raise HTTPException(409, "Something on your list is no longer in the store. Remove it and try again.")
        size = raw.get("size") or None
        if p.sizes and size not in p.sizes:
            raise HTTPException(400, f"Pick a size for {p.name}.")
        lines.append({"product_id": str(p.id), "slug": p.slug, "name": p.name, "size": size, "qty": qty,
                      "unit_minor": p.price_minor, "sold_out": p.status == "sold_out" or p.stock <= 0})
        total += p.price_minor * qty
    if not lines:
        raise HTTPException(400, "Your list is empty.")
    n = await db.scalar(text("SELECT nextval('interest_number_seq')"))
    row = StoreInterest(number=f"SH-INT-{n:04d}", name=name.strip(), email=(email or "").strip().lower() or None,
                        phone_enc=encrypt(phone), phone_last3=digits_tail(phone, 3), city=(city or "").strip() or None,
                        items=lines, total_minor=total, message=(message or "").strip() or None, status="new")
    db.add(row)
    await db.flush()
    return row
