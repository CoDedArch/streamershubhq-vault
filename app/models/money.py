"""Money: the ledger, budgets, payroll runs and lines, and salary tiers."""

import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import JSON, BigInteger, Date, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Row, fk

INCOME_CATEGORIES = {"platform": "Platform income (ads, subs, tips)", "store": "Store sales", "brand": "Brand deals",
                     "events": "Event tickets"}
COST_CATEGORIES = {
    "salary": "Streamer salary", "internet_power": "Internet and power", "marketing": "Marketing and giveaways",
    "store_cogs": "Store cost of goods", "operations": "Operations", "payouts": "Designer and clipper payouts",
    "equipment": "Equipment", "payment_fees": "Payment fees", "rent": "Rent and studio space",
    "transport": "Transport and delivery", "software": "Software and subscriptions", "events": "Events",
    "professional": "Legal, accounting and fees",
}
# Costs Finance records by hand (the rest come from payroll, the store and payment fees)
RECORDABLE_COSTS = ["equipment", "internet_power", "rent", "marketing", "operations", "transport", "software", "events",
                    "professional", "store_cogs"]


class LedgerEntry(Row, Base):
    __tablename__ = "ledger_entries"
    on_date: Mapped[date] = mapped_column(Date, index=True)
    type: Mapped[str] = mapped_column(String(10))  # revenue, expense
    category: Mapped[str] = mapped_column(String(30), index=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    source_table: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    source_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    entered_by: Mapped[Optional[uuid.UUID]] = fk("users")
    __table_args__ = (UniqueConstraint("source_table", "source_id", "category"),)


class CashBalance(Row, Base):
    """Cash in the bank and mobile money wallets on a date, entered by Finance."""

    __tablename__ = "cash_balances"
    on_date: Mapped[date] = mapped_column(Date, unique=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    entered_by: Mapped[Optional[uuid.UUID]] = fk("users")


class Budget(Row, Base):
    __tablename__ = "budgets"
    month: Mapped[str] = mapped_column(String(7))
    type: Mapped[str] = mapped_column(String(10))  # revenue, expense
    category: Mapped[str] = mapped_column(String(30))
    planned_minor: Mapped[int] = mapped_column(BigInteger)
    __table_args__ = (UniqueConstraint("month", "category"),)


class SalaryTier(Row, Base):
    __tablename__ = "salary_tiers"
    label: Mapped[str] = mapped_column(String(60))
    min_platform_income_minor: Mapped[int] = mapped_column(BigInteger)
    max_platform_income_minor: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)  # None: no top
    salary_minor: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)  # None: still to be decided
    position: Mapped[int] = mapped_column(Integer, default=0)


class PayrollRun(Row, Base):
    __tablename__ = "payroll_runs"
    period: Mapped[str] = mapped_column(String(7), unique=True)  # 2026-11
    status: Mapped[str] = mapped_column(String(12), default="draft")  # draft, submitted, approved, paid
    total_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    payee_count: Mapped[int] = mapped_column(Integer, default=0)
    drafted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    submitted_by: Mapped[Optional[uuid.UUID]] = fk("users")
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by: Mapped[Optional[uuid.UUID]] = fk("users")
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    returned_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class PayrollLine(Row, Base):
    __tablename__ = "payroll_lines"
    run_id: Mapped[uuid.UUID] = fk("payroll_runs", nullable=False, ondelete="CASCADE")
    payee_user_id: Mapped[uuid.UUID] = fk("users", nullable=False, ondelete="RESTRICT")
    kind: Mapped[str] = mapped_column(String(24))  # salary, revenue_share, designer_commission, clipper_payout
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    basis: Mapped[dict] = mapped_column(JSON, default=dict)  # {"text": "November · milestone tier 2", ...}
    method: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)  # momo, bank
    status: Mapped[str] = mapped_column(String(16), default="ready")  # ready, missing_details, carried, paid
    paid_ref: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_by: Mapped[Optional[uuid.UUID]] = fk("users")


class CarryForward(Row, Base):
    """A balance under the minimum payout, carried into next month's run."""

    __tablename__ = "carry_forwards"
    payee_user_id: Mapped[uuid.UUID] = fk("users", nullable=False, ondelete="CASCADE")
    kind: Mapped[str] = mapped_column(String(24))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    from_period: Mapped[str] = mapped_column(String(7))
    used_in_run_id: Mapped[Optional[uuid.UUID]] = fk("payroll_runs")


class Decision(Row, Base):
    """An item waiting for the Founder ("Needs your decision"): budget requests, deal signatures, releases."""

    __tablename__ = "decisions"
    title: Mapped[str] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(200))  # "Production · Producer"
    kind: Mapped[str] = mapped_column(String(30))  # payroll, deal, budget, seat_offer, release, other
    entity_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    amount_minor: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    requested_by: Mapped[Optional[uuid.UUID]] = fk("users")
    status: Mapped[str] = mapped_column(String(12), default="open")  # open, approved, declined
    decided_by: Mapped[Optional[uuid.UUID]] = fk("users")
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class Expense(Row, Base):
    """A purchase or company expense Finance records. It lands in the ledger under its category; when it is
    for a streamer (their gear, their data bundle) it says so, and equipment bought with it is linked."""

    __tablename__ = "expenses"
    on_date: Mapped[date] = mapped_column(Date, index=True)
    category: Mapped[str] = mapped_column(String(30), index=True)
    description: Mapped[str] = mapped_column(String(200))
    vendor: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    paid_with: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)  # momo, bank, cash, card
    reference: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    streamer_id: Mapped[Optional[uuid.UUID]] = fk("streamers")
    receipt_key: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    entered_by: Mapped[Optional[uuid.UUID]] = fk("users")
