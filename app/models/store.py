"""The store: designs and their approval path, products, orders, payments and designer commission."""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Row, fk

COLLECTIONS = {"label": "Label Essentials", "roster": "Roster Collection", "designer": "Designer Series"}
# submitted -> creative_review -> [streamer_signoff] -> founder_approval -> live; or changes_requested / rejected
DESIGN_STATUSES = ("submitted", "creative_review", "changes_requested", "streamer_signoff", "founder_approval",
                   "live", "rejected")


class Design(Row, Base):
    __tablename__ = "designs"
    reference: Mapped[str] = mapped_column(String(20), unique=True)  # SH-DSN-0031
    designer_user_id: Mapped[Optional[uuid.UUID]] = fk("users")  # None for in-house work
    title: Mapped[str] = mapped_column(String(160))
    product_type: Mapped[str] = mapped_column(String(30))  # tee, hoodie, cap, jersey, mat, poster, mug, tote, bottle, keychain, set
    asked_price_minor: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    files: Mapped[list] = mapped_column(JSON, default=list)  # [{key, name, kind, size}] private keys in S3
    status: Mapped[str] = mapped_column(String(20), default="submitted", index=True)
    uses_streamer_id: Mapped[Optional[uuid.UUID]] = fk("streamers")
    review_notes: Mapped[list] = mapped_column(JSON, default=list)  # [{at, by, stage, note}]
    # Set at creative review, locked at founder approval
    price_minor: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    commission_rate_bps: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    collection: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    production_cost_minor: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    decline_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    brief_id: Mapped[Optional[uuid.UUID]] = fk("design_briefs")


class DesignBrief(Row, Base):
    __tablename__ = "design_briefs"
    title: Mapped[str] = mapped_column(String(160))
    detail: Mapped[str] = mapped_column(Text)
    due_on: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True)


class Product(Row, Base):
    __tablename__ = "products"
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    price_minor: Mapped[int] = mapped_column(BigInteger)
    category: Mapped[str] = mapped_column(String(20))  # label, roster, designer (COLLECTIONS)
    product_type: Mapped[str] = mapped_column(String(30))  # tee, hoodie, cap, jersey, mat, poster, mug, tote, bottle, keychain, set
    tone: Mapped[str] = mapped_column(String(2), default="")  # tile colour: "", k, y, t
    tag: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)  # Bestseller, Drop 001, Limited · 50
    images: Mapped[list] = mapped_column(JSON, default=list)
    sizes: Mapped[list] = mapped_column(JSON, default=list)  # [] when one size
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft, live, sold_out, archived
    design_id: Mapped[Optional[uuid.UUID]] = fk("designs")
    streamer_id: Mapped[Optional[uuid.UUID]] = fk("streamers")
    commission_rate_bps: Mapped[int] = mapped_column(Integer, default=0)
    production_cost_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    stock: Mapped[int] = mapped_column(Integer, default=0)
    reorder_at: Mapped[int] = mapped_column(Integer, default=5)
    position: Mapped[int] = mapped_column(Integer, default=0)
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)  # a placeholder in the store until the real line replaces it


class Order(Row, Base):
    __tablename__ = "orders"
    number: Mapped[str] = mapped_column(String(20), unique=True, index=True)  # SH-1009
    customer_user_id: Mapped[Optional[uuid.UUID]] = fk("users")
    customer_name: Mapped[str] = mapped_column(String(120))
    customer_email: Mapped[str] = mapped_column(String(255), index=True)
    customer_phone_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    customer_phone_last3: Mapped[Optional[str]] = mapped_column(String(3), nullable=True)
    address: Mapped[str] = mapped_column(String(300))
    city: Mapped[str] = mapped_column(String(80))
    delivery_zone: Mapped[str] = mapped_column(String(40))
    subtotal_minor: Mapped[int] = mapped_column(BigInteger)
    delivery_minor: Mapped[int] = mapped_column(BigInteger)
    total_minor: Mapped[int] = mapped_column(BigInteger)
    fees_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    payment_method: Mapped[str] = mapped_column(String(20))  # momo, card
    payment_ref: Mapped[str] = mapped_column(String(60), unique=True)
    # pending, paid, packing, to_ship, shipped, delivered, refund_requested, refunded, cancelled
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    tracking_code: Mapped[str] = mapped_column(String(40), index=True)  # given to the customer to look up the order
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    shipped_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    courier_ref: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    refund_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    history: Mapped[list] = mapped_column(JSON, default=list)  # [{at, status, by}]


class OrderItem(Row, Base):
    __tablename__ = "order_items"
    order_id: Mapped[uuid.UUID] = fk("orders", nullable=False, ondelete="CASCADE")
    product_id: Mapped[uuid.UUID] = fk("products", nullable=False, ondelete="RESTRICT")
    product_name: Mapped[str] = mapped_column(String(160))
    size: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    qty: Mapped[int] = mapped_column(Integer)
    unit_price_minor: Mapped[int] = mapped_column(BigInteger)
    commission_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    production_cost_minor: Mapped[int] = mapped_column(BigInteger, default=0)


class CommissionEntry(Row, Base):
    __tablename__ = "commission_entries"
    designer_user_id: Mapped[uuid.UUID] = fk("users", nullable=False, ondelete="RESTRICT")
    order_item_id: Mapped[uuid.UUID] = fk("order_items", nullable=False, ondelete="CASCADE")
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending, on_hold, payable, paid, reversed
    payroll_line_id: Mapped[Optional[uuid.UUID]] = fk("payroll_lines")
    __table_args__ = (UniqueConstraint("order_item_id"),)


class PaymentEvent(Row, Base):
    """Each gateway event handled once (webhook and verify calls are idempotent by reference + event)."""

    __tablename__ = "payment_events"
    provider: Mapped[str] = mapped_column(String(20))
    event_key: Mapped[str] = mapped_column(String(160), unique=True)
    reference: Mapped[str] = mapped_column(String(60), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


STORE_MODES = {"interest": "Taking interest", "checkout": "Taking payment"}


class StoreInterest(Row, Base):
    """While the store takes interest only: what someone wants, so the team can call and agree payment."""

    __tablename__ = "store_interests"
    number: Mapped[str] = mapped_column(String(20), unique=True, index=True)  # SH-INT-0004
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    phone_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    phone_last3: Mapped[Optional[str]] = mapped_column(String(3), nullable=True)
    city: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    items: Mapped[list] = mapped_column(JSON, default=list)  # [{product_id, name, size, qty, unit_minor}]
    total_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="new", index=True)  # new, contacted, converted, closed
    notes: Mapped[list] = mapped_column(JSON, default=list)  # [{at, by, note}]
    handled_by: Mapped[Optional[uuid.UUID]] = fk("users")
