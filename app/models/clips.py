"""Clippers: profiles, clips, verification and monthly earnings."""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Row, fk


class ClipperProfile(Row, Base):
    __tablename__ = "clipper_profiles"
    user_id: Mapped[uuid.UUID] = fk("users", nullable=False, ondelete="CASCADE")
    handle: Mapped[str] = mapped_column(String(60), unique=True)
    platforms: Mapped[dict] = mapped_column(JSON, default=dict)  # {"tiktok": "tiktok.com/@x", ...}
    status: Mapped[str] = mapped_column(String(20), default="active")  # active, paused
    strikes: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (UniqueConstraint("user_id"),)


class Clip(Row, Base):
    __tablename__ = "clips"
    clipper_user_id: Mapped[uuid.UUID] = fk("users", nullable=False, ondelete="CASCADE")
    source_stream_id: Mapped[Optional[uuid.UUID]] = fk("streams")
    url: Mapped[str] = mapped_column(String(500), unique=True)
    platform: Mapped[str] = mapped_column(String(30))  # tiktok, instagram, youtube_shorts, x
    title: Mapped[str] = mapped_column(String(200))
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    views_reported: Mapped[int] = mapped_column(BigInteger, default=0)
    views_verified: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="submitted", index=True)  # submitted, checking, verified, flagged, rejected
    flag_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    verified_by: Mapped[Optional[uuid.UUID]] = fk("users")
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    period: Mapped[str] = mapped_column(String(7), index=True)  # month the clip counts in, from posted_at
    moment_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)  # the marked moment it cuts
    # Read from the platform where we can (YouTube for now). A guide for the Social Media Manager, never paid on its own.
    views_fetched: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    views_fetched_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    fetch_note: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)  # why the last fetch didn't give a number
    platform_channel: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)  # the channel that posted it
    # The verified count is first set at the check, then updated at the month-end count (or corrected) until the run is submitted
    count_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    count_note: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)  # why it last changed, shown to the clipper


class ClipperEarning(Row, Base):
    """A closed month's earnings for one clipper. Open months are worked out live."""

    __tablename__ = "clipper_earnings"
    clipper_user_id: Mapped[uuid.UUID] = fk("users", nullable=False, ondelete="CASCADE")
    period: Mapped[str] = mapped_column(String(7))
    verified_views: Mapped[int] = mapped_column(BigInteger)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), default="payable")  # payable, carried, paid
    payroll_line_id: Mapped[Optional[uuid.UUID]] = fk("payroll_lines")
    __table_args__ = (UniqueConstraint("clipper_user_id", "period"),)


class LeaderboardSnapshot(Row, Base):
    """Last week's ranks, so the Clip Table can show movement."""

    __tablename__ = "leaderboard_snapshots"
    week: Mapped[str] = mapped_column(String(8), index=True)  # 2026-W46
    clipper_user_id: Mapped[uuid.UUID] = fk("users", nullable=False, ondelete="CASCADE")
    rank: Mapped[int] = mapped_column(Integer)
    __table_args__ = (UniqueConstraint("week", "clipper_user_id"),)
