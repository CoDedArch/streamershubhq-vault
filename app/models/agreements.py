"""Agreements: the Founder's templates, and every agreement offered to a person or an investor.

A template is the Founder's wording for one kind of agreement (a streamer's 6-month trial, the full recording contract,
a team member's agreement, an investment agreement...). Offering one copies its text and terms onto an Agreement, so
later edits to the template never change what someone was offered or signed. Terms are free-form rows (equity, salary,
a share of a streamer's platform earnings, anything) so each offer can be different.
"""

import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, Date, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Row, fk


class AgreementTemplate(Row, Base):
    __tablename__ = "agreement_templates"
    # Built-in templates have a key so they are created once and never overwritten; the Founder's own have none.
    key: Mapped[Optional[str]] = mapped_column(String(60), unique=True, nullable=True)
    name: Mapped[str] = mapped_column(String(120))
    audience: Mapped[str] = mapped_column(String(20))  # staff, streamer, designer, clipper, investor
    stage: Mapped[str] = mapped_column(String(20), default="standard")  # trial, contract, standard
    summary: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    body: Mapped[str] = mapped_column(Text)  # "## Heading", paragraphs, "- " bullets and {{placeholders}}
    term_months: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # None: no fixed end
    min_term_months: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    max_term_months: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    renewable: Mapped[bool] = mapped_column(Boolean, default=False)
    default_terms: Mapped[list] = mapped_column(JSON, default=list)  # [{kind, label, value, detail}]
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)  # used automatically for its audience and stage
    status: Mapped[str] = mapped_column(String(12), default="active")  # active, archived
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_by: Mapped[Optional[uuid.UUID]] = fk("users")


class Agreement(Row, Base):
    __tablename__ = "agreements"
    user_id: Mapped[Optional[uuid.UUID]] = fk("users", ondelete="CASCADE")
    investor_id: Mapped[Optional[uuid.UUID]] = fk("investors", ondelete="CASCADE")
    template_id: Mapped[Optional[uuid.UUID]] = fk("agreement_templates")
    template_version: Mapped[int] = mapped_column(Integer, default=1)
    audience: Mapped[str] = mapped_column(String(20))
    stage: Mapped[str] = mapped_column(String(20), default="standard")
    title: Mapped[str] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(Text)  # copied from the template when it was created; frozen once signed
    terms: Mapped[list] = mapped_column(JSON, default=list)
    term_months: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    renewable: Mapped[bool] = mapped_column(Boolean, default=False)
    # draft (only the Founder sees it), offered (waiting for a signature), signed, declined, withdrawn, ended (early)
    status: Mapped[str] = mapped_column(String(12), default="draft", index=True)
    starts_on: Mapped[Optional[date]] = mapped_column(Date, nullable=True)  # the day it was signed, or when the last one ends
    ends_on: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    offered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    offered_by: Mapped[Optional[uuid.UUID]] = fk("users")
    signed_name: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    signed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    signed_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_by: Mapped[Optional[uuid.UUID]] = fk("users")
    end_reason: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    follows_id: Mapped[Optional[uuid.UUID]] = fk("agreements")  # the trial this contract follows, or the term it renews
