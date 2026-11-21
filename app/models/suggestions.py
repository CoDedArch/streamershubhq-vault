"""The Suggestion Bank: ideas from everyone who works with the label (staff, streamers, clippers, designers and
investors). Each idea is seen only by the desks it concerns (private ones by the Founder alone), answered within a
week, and anything worth doing is recommended by the desk and decided by the Founder. See SuggestionService."""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Row, fk


class Suggestion(Row, Base):
    __tablename__ = "suggestions"
    reference: Mapped[str] = mapped_column(String(20), unique=True, index=True)  # SH-IDEA-4K7Q2
    topic: Mapped[str] = mapped_column(String(16), index=True)  # SuggestionService.TOPICS
    desks: Mapped[list] = mapped_column(JSON, default=list)  # the Hub OS areas that see it; none when private
    private: Mapped[bool] = mapped_column(Boolean, default=False)  # the Founder alone
    title: Mapped[str] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(Text)
    benefit: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # what it fixes or improves, who it helps
    link: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    files: Mapped[list] = mapped_column(JSON, default=list)
    sender_group: Mapped[str] = mapped_column(String(10))  # staff, streamer, clipper, designer, investor
    author_user_id: Mapped[Optional[uuid.UUID]] = fk("users")
    author_investor_id: Mapped[Optional[uuid.UUID]] = fk("investors")
    hide_name: Mapped[bool] = mapped_column(Boolean, default=False)  # the team, the Founder included, sees the group only
    # new, looking, recommended (waiting for the Founder), planned, done, closed ("Not now"), merged
    status: Mapped[str] = mapped_column(String(12), default="new", index=True)
    merged_into_id: Mapped[Optional[uuid.UUID]] = fk("suggestions")
    closed_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    closed_by: Mapped[Optional[uuid.UUID]] = fk("users")
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # What the desk proposes: {action, owner_user_id, owner, due_on, cost_minor, effort, benefit, by, at}
    recommendation: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    decision_id: Mapped[Optional[uuid.UUID]] = fk("decisions")  # its line in the Founder's "Needs your decision"
    decided_by: Mapped[Optional[uuid.UUID]] = fk("users")
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    task_id: Mapped[Optional[uuid.UUID]] = fk("tasks")  # the owner's task once approved
    first_response_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    reminded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    done_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class SuggestionReply(Row, Base):
    """One line under an idea: the sender (in), the team answering them (out, emailed), a team note, or an event
    (moved, merged, closed, decided). Notes and events are for the team only."""

    __tablename__ = "suggestion_replies"
    suggestion_id: Mapped[uuid.UUID] = fk("suggestions", nullable=False, ondelete="CASCADE")
    direction: Mapped[str] = mapped_column(String(6))  # in, out, note, event
    body: Mapped[str] = mapped_column(Text)
    author_user_id: Mapped[Optional[uuid.UUID]] = fk("users")
    author_label: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
