"""Tech and Dev: equipment, incidents and the accounts register."""

import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import BigInteger, Date, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Row, fk


class Equipment(Row, Base):
    __tablename__ = "equipment"
    tag: Mapped[str] = mapped_column(String(20), unique=True)  # SH-EQ-001
    name: Mapped[str] = mapped_column(String(120))
    assigned_to: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)  # "Seat 01 · Galacta"
    location: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    condition: Mapped[str] = mapped_column(String(30), default="good")  # good, fault, on_order, retired
    condition_note: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    purchase_cost_minor: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)  # never shown to streamers
    streamer_id: Mapped[Optional[uuid.UUID]] = fk("streamers")  # bought for, or given to, this streamer
    expense_id: Mapped[Optional[uuid.UUID]] = fk("expenses")
    purchased_on: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    serial_number: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)


class Incident(Row, Base):
    __tablename__ = "incidents"
    stream_id: Mapped[Optional[uuid.UUID]] = fk("streams")
    title: Mapped[str] = mapped_column(String(160))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    downtime_minutes: Mapped[int] = mapped_column(Integer, default=0)
    cause: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    fix: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reported_by: Mapped[Optional[uuid.UUID]] = fk("users")


class AccessAccount(Row, Base):
    """Accounts and access: who manages each platform account."""

    __tablename__ = "access_accounts"
    name: Mapped[str] = mapped_column(String(80))
    managers: Mapped[str] = mapped_column(String(160))  # "3 managers", "Finance only"
    position: Mapped[int] = mapped_column(Integer, default=0)


class EquipmentIssue(Row, Base):
    """A problem with a piece of gear, reported by the streamer who uses it (or by Tech)."""

    __tablename__ = "equipment_issues"
    equipment_id: Mapped[uuid.UUID] = fk("equipment", nullable=False, ondelete="CASCADE")
    reported_by: Mapped[Optional[uuid.UUID]] = fk("users")
    detail: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), default="open")  # open, resolved
    resolution: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[Optional[uuid.UUID]] = fk("users")
