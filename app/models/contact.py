"""Messages from the website's Contact page (and applications for open roles), and the conversation with
each sender. The team replies from Hub OS; every reply is emailed, and the sender can read the whole
conversation and answer on the website from a private link."""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Row, fk

# new: nobody has answered yet · replied: we answered last · awaiting: they wrote back · closed · spam
CONTACT_STATUSES = {"new": "New", "replied": "Replied", "awaiting": "They replied", "closed": "Closed", "spam": "Spam"}
# For applications to an open role
JOB_STAGES = {"applied": "Applied", "shortlisted": "Shortlisted", "interview": "Interview", "offer": "Offer made",
              "hired": "Hired", "declined": "Not this time"}


class ContactMessage(Row, Base):
    __tablename__ = "contact_messages"
    reference: Mapped[str] = mapped_column(String(20), unique=True, index=True)  # SH-MSG-4K7Q
    reason: Mapped[str] = mapped_column(String(12), index=True)  # ContactService.REASONS
    desk: Mapped[str] = mapped_column(String(16), index=True)  # the Hub OS area that answers it
    subject: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(255), index=True)
    phone_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    phone_last3: Mapped[Optional[str]] = mapped_column(String(3), nullable=True)
    organisation: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)  # the reason's own fields
    files: Mapped[list] = mapped_column(JSON, default=list)
    post_id: Mapped[Optional[uuid.UUID]] = fk("posts")  # the open role applied for, or the event asked about
    status: Mapped[str] = mapped_column(String(10), default="new", index=True)
    stage: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)  # JOB_STAGES, applications only
    assigned_to: Mapped[Optional[uuid.UUID]] = fk("users")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # the sender's private link
    token_enc: Mapped[str] = mapped_column(Text)  # kept so every reply email carries the same link
    last_in_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_out_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    first_reply_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    via: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # how an applicant found the role
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class ContactReply(Row, Base):
    """One item in the conversation: from them (in), from us (out, emailed), or a note only the team sees."""

    __tablename__ = "contact_replies"
    message_id: Mapped[uuid.UUID] = fk("contact_messages", nullable=False, ondelete="CASCADE")
    direction: Mapped[str] = mapped_column(String(4))  # in, out, note
    body: Mapped[str] = mapped_column(Text)
    files: Mapped[list] = mapped_column(JSON, default=list)
    author_user_id: Mapped[Optional[uuid.UUID]] = fk("users")
    author_label: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    emailed: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)  # out only: did the email go
