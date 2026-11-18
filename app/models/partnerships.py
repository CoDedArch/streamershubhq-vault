"""Partnerships: brands, deals, deliverables, invoices and the rate card."""

import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Row, fk

BRAND_STAGES = ("contacted", "replied", "in_talks", "at_contract", "signed", "lost")
# "listed": shown in the public Brand Partners grid only, not worked in the pipeline


class Brand(Row, Base):
    __tablename__ = "brands"
    name: Mapped[str] = mapped_column(String(120), unique=True)
    logo_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    sector: Mapped[str] = mapped_column(String(60))  # Telecoms and fintech, Food and drink, Devices and gear ...
    status: Mapped[str] = mapped_column(String(20), default="contacted")  # BRAND_STAGES
    note: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    next_step: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    next_step_on: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    owner_user_id: Mapped[Optional[uuid.UUID]] = fk("users")
    contact_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    contact_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)  # shown in the Brand Partners grid
    wordmark: Mapped[str] = mapped_column(String(2), default="a")  # a, b, c, e, f: the grid's type styles
    public_order: Mapped[int] = mapped_column(Integer, default=0)
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)  # a placeholder shown until real partners replace it
    last_contact_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class Deal(Row, Base):
    __tablename__ = "deals"
    brand_id: Mapped[uuid.UUID] = fk("brands", nullable=False, ondelete="CASCADE")
    title: Mapped[str] = mapped_column(String(160))
    value_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    start: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    end: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft, trial (a paid one-off test), at_contract, signed, done, cancelled
    signed_by: Mapped[Optional[uuid.UUID]] = fk("users")
    signed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    contract_file: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)


class Deliverable(Row, Base):
    __tablename__ = "deliverables"
    deal_id: Mapped[Optional[uuid.UUID]] = fk("deals", ondelete="CASCADE")
    brand_id: Mapped[Optional[uuid.UUID]] = fk("brands", ondelete="CASCADE")
    streamer_id: Mapped[Optional[uuid.UUID]] = fk("streamers")
    title: Mapped[str] = mapped_column(String(160))
    detail: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    due_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="planned")  # planned, due, done, sent
    proof_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)


class Invoice(Row, Base):
    __tablename__ = "invoices"
    brand_id: Mapped[uuid.UUID] = fk("brands", nullable=False, ondelete="RESTRICT")
    deal_id: Mapped[Optional[uuid.UUID]] = fk("deals")
    number: Mapped[str] = mapped_column(String(20), unique=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    issued_at: Mapped[date] = mapped_column(Date)
    due_at: Mapped[date] = mapped_column(Date)
    paid_at: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="issued")  # issued, paid, overdue, void
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class RateCardItem(Row, Base):
    __tablename__ = "rate_card_items"
    title: Mapped[str] = mapped_column(String(120))
    detail: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    price_minor: Mapped[int] = mapped_column(BigInteger)
    unit: Mapped[str] = mapped_column(String(40), default="per stream")
    position: Mapped[int] = mapped_column(Integer, default=0)
