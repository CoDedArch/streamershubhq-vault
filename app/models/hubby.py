"""Hubby, the Hub OS helper: every question asked, how it was answered, and what it cost.

The question text is cleared after 30 days (HubbyService.tidy); the numbers stay a little longer so the
monthly spending cap and the Founder's totals are still right at the end of a long month.
"""

import uuid
from typing import Optional

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Row, fk


class HubbyQuestion(Row, Base):
    __tablename__ = "hubby_questions"
    user_id: Mapped[Optional[uuid.UUID]] = fk("users", ondelete="CASCADE")
    investor_id: Mapped[Optional[uuid.UUID]] = fk("investors", ondelete="CASCADE")
    role: Mapped[str] = mapped_column(String(20))
    page: Mapped[str] = mapped_column(String(200), default="")
    question: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)  # cleared after 30 days
    mode: Mapped[str] = mapped_column(String(10))  # ai | guide
    # Why it was the guide: no_key, no_sdk, off, cap, daily, cooldown, failed, refused
    reason: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    entry_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)  # the guide entry the answer links to
    model: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_micro_usd: Mapped[int] = mapped_column(Integer, default=0)  # an estimate from list prices: $1 = 1,000,000
    ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    helpful: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)  # the 👍 / 👎 on the answer
