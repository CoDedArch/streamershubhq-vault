"""Applications from the Join page: streamers, merch designers and clippers."""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Row, fk


class Application(Row, Base):
    __tablename__ = "applications"
    reference: Mapped[str] = mapped_column(String(24), unique=True)  # SH-APP-STR-4K7Q
    track: Mapped[str] = mapped_column(String(10), index=True)  # streamer, designer, clipper
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(255), index=True)
    contact_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # WhatsApp number, encrypted
    handle: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)  # the track's own fields
    files: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(10), default="new", index=True)  # new, reviewing, approved, declined
    tag: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)  # reviewer's label: "Portfolio 4.5 / 5"
    tag_tone: Mapped[Optional[str]] = mapped_column(String(6), nullable=True)  # ok, warn, no
    reviewed_by: Mapped[Optional[uuid.UUID]] = fk("users")
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_user_id: Mapped[Optional[uuid.UUID]] = fk("users")
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
