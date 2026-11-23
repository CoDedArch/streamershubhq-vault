"""Talent and production: streamers, honours, streams, run of show, marked moments, the edit queue,
platform income (which sets the salary tier) and stream health."""

import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import JSON, BigInteger, Boolean, Date, DateTime, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Row, fk


class Streamer(Row, Base):
    __tablename__ = "streamers"
    user_id: Mapped[Optional[uuid.UUID]] = fk("users")
    stage_name: Mapped[str] = mapped_column(String(80))
    known_as: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)  # "Known off-stream as Lamar"
    category: Mapped[str] = mapped_column(String(40))  # gaming, sport, music, lifestyle, comedy, tech
    content: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)  # "Apex Legends · NBA 2K · EA FC"
    bio: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    signing_number: Mapped[int] = mapped_column(Integer, unique=True)
    image_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    contract_start: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    contract_end: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="signed")  # signed, paused, ended
    is_public: Mapped[bool] = mapped_column(Boolean, default=True)
    # Stream setup, kept by Production and shown on the streamer's desk (the playbook points here)
    producer_user_id: Mapped[Optional[uuid.UUID]] = fk("users")
    segments: Mapped[list] = mapped_column(JSON, default=list)  # repeat segments that reliably make clips
    moderators: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    chat_rules: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class RosterSeat(Row, Base):
    """An open seat on the public roster ("The Competitor", "The Wildcard")."""

    __tablename__ = "roster_seats"
    number: Mapped[int] = mapped_column(Integer, unique=True)
    title: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(200))
    streamer_id: Mapped[Optional[uuid.UUID]] = fk("streamers")


class Honour(Row, Base):
    __tablename__ = "honours"
    streamer_id: Mapped[uuid.UUID] = fk("streamers", nullable=False, ondelete="CASCADE")
    position: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(120))
    detail: Mapped[str] = mapped_column(String(200))
    is_public: Mapped[bool] = mapped_column(Boolean, default=True)


class Stream(Row, Base):
    __tablename__ = "streams"
    streamer_id: Mapped[uuid.UUID] = fk("streamers", nullable=False, ondelete="CASCADE")
    title: Mapped[str] = mapped_column(String(160))
    category: Mapped[str] = mapped_column(String(60))
    brand_moment: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    scheduled_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    actual_start: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    platforms: Mapped[list] = mapped_column(JSON, default=list)
    avg_viewers: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    peak_viewers: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    watching_now: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    chat_per_min: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    new_followers: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    uptime_percent: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="scheduled")  # scheduled, live, done, cancelled
    recording_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    scheduled_end: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # The streamer's own notice before a stream: running late (by notice_minutes) or can't stream
    notice_kind: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)  # late, cant
    notice_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    notice_note: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    notice_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class RunOfShowItem(Row, Base):
    __tablename__ = "run_of_show_items"
    stream_id: Mapped[uuid.UUID] = fk("streams", nullable=False, ondelete="CASCADE")
    at_time: Mapped[str] = mapped_column(String(5))  # 19:00
    title: Mapped[str] = mapped_column(String(160))
    note: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="queued")  # queued, next, done, skipped
    position: Mapped[int] = mapped_column(Integer, default=0)


class MarkedMoment(Row, Base):
    """Marked by the producer during a stream; feeds the edit queue and clipper footage."""

    __tablename__ = "marked_moments"
    stream_id: Mapped[uuid.UUID] = fk("streams", nullable=False, ondelete="CASCADE")
    at_seconds: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(160))
    marked_by: Mapped[Optional[uuid.UUID]] = fk("users")
    footage_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    source: Mapped[str] = mapped_column(String(12), default="producer", server_default="producer")  # producer, streamer
    kind: Mapped[str] = mapped_column(String(16), default="highlight", server_default="highlight")  # see MomentService.KINDS
    note: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)  # context for clippers


class MomentClaim(Row, Base):
    """A clipper saying "I'm cutting this one", so others can pick a different moment."""

    __tablename__ = "moment_claims"
    moment_id: Mapped[uuid.UUID] = fk("marked_moments", nullable=False, ondelete="CASCADE")
    clipper_user_id: Mapped[uuid.UUID] = fk("users", nullable=False, ondelete="CASCADE")
    __table_args__ = (UniqueConstraint("moment_id", "clipper_user_id"),)


class EditJob(Row, Base):
    __tablename__ = "edit_jobs"
    source_stream_id: Mapped[Optional[uuid.UUID]] = fk("streams")
    title: Mapped[str] = mapped_column(String(160))
    format: Mapped[str] = mapped_column(String(40))  # highlight, short, thumbnail
    assignee_id: Mapped[Optional[uuid.UUID]] = fk("users")
    due_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="todo")  # todo, editing, review, done


class StreamHealth(Row, Base):
    __tablename__ = "stream_health"
    stream_id: Mapped[uuid.UUID] = fk("streams", nullable=False, ondelete="CASCADE")
    bitrate_kbps: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    dropped_frames_pct: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True)
    upload_mbps: Mapped[Optional[float]] = mapped_column(Numeric(6, 1), nullable=True)
    power: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # mains, backup
    backup_ready: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    audio_db: Mapped[Optional[float]] = mapped_column(Numeric(5, 1), nullable=True)


class PlatformIncome(Row, Base):
    """A streamer's platform income (ads, subs, tips) for a month. Sets next month's salary tier."""

    __tablename__ = "platform_income"
    streamer_id: Mapped[uuid.UUID] = fk("streamers", nullable=False, ondelete="CASCADE")
    month: Mapped[str] = mapped_column(String(7))  # 2026-10
    platform: Mapped[str] = mapped_column(String(20))  # youtube, twitch, kick, tiktok
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    note: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    __table_args__ = (UniqueConstraint("streamer_id", "month", "platform"),)


class SalaryTierAssignment(Row, Base):
    """The tier set on the 1st for a month, so the salary is fixed even if the tier table changes later."""

    __tablename__ = "salary_tier_assignments"
    streamer_id: Mapped[uuid.UUID] = fk("streamers", nullable=False, ondelete="CASCADE")
    month: Mapped[str] = mapped_column(String(7))
    previous_income_minor: Mapped[int] = mapped_column(BigInteger)
    tier_label: Mapped[str] = mapped_column(String(60))
    salary_minor: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)  # None: tier not set yet
    __table_args__ = (UniqueConstraint("streamer_id", "month"),)


class SocialPost(Row, Base):
    """The social calendar: posts, brand posts and live promos."""

    __tablename__ = "social_posts"
    on_date: Mapped[date] = mapped_column(Date, index=True)
    title: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(20), default="post")  # post, brand, live_promo
    platform: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="planned")  # planned, posted
    owner_id: Mapped[Optional[uuid.UUID]] = fk("users")


class CreativeRequest(Row, Base):
    """The Creative work board: requests from other departments."""

    __tablename__ = "creative_requests"
    title: Mapped[str] = mapped_column(String(160))
    detail: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    from_area: Mapped[str] = mapped_column(String(30))  # Social, Store, Production, Partnerships
    requested_by: Mapped[Optional[uuid.UUID]] = fk("users")
    assignee_id: Mapped[Optional[uuid.UUID]] = fk("users")
    due_on: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="requested")  # requested, in_progress, in_review, delivered
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    on_time: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class ChannelReport(Row, Base):
    """A streamer's monthly report on their own channels: what each earned and paid them, with the platform's
    statement. Their own channels pay them directly, and they pass those payouts to the label. Finance confirms what
    the label received, and that is counted as the streamer's platform income in the month it's confirmed."""

    __tablename__ = "channel_reports"
    streamer_id: Mapped[uuid.UUID] = fk("streamers", nullable=False, ondelete="CASCADE")
    month: Mapped[str] = mapped_column(String(7))  # the month the report covers
    status: Mapped[str] = mapped_column(String(12), default="submitted")  # submitted, returned, confirmed
    # [{platform, earned_minor, earned_currency, paid_minor, paid_currency, statement_key, statement_name}]
    lines: Mapped[list] = mapped_column(JSON, default=list)
    passed_minor: Mapped[int] = mapped_column(BigInteger, default=0)  # what the streamer says they passed on, in cedis
    transfer_ref: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    submitted_by: Mapped[Optional[uuid.UUID]] = fk("users")
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    received_minor: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)  # what Finance confirmed, in cedis
    income_month: Mapped[Optional[str]] = mapped_column(String(7), nullable=True)  # the month it was counted in
    reviewed_by: Mapped[Optional[uuid.UUID]] = fk("users")
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    review_note: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    __table_args__ = (UniqueConstraint("streamer_id", "month"),)
