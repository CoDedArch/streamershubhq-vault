"""Investors: the register, investments, rounds, valuations, reports, documents, exit requests and messages."""

import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Row, fk


class Investor(Row, Base):
    __tablename__ = "investors"
    user_id: Mapped[Optional[uuid.UUID]] = fk("users")
    internal_number: Mapped[str] = mapped_column(String(12), unique=True)  # SH-INV-003
    legal_name: Mapped[str] = mapped_column(String(160))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    phone_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    phone_last4: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)
    phone_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)  # lookup by phone
    # The Holder ID: an Argon2id hash plus its last four characters. The full ID is never stored.
    holder_id_hash: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    holder_id_last4: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)
    id_issued_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    id_rotated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    id_revealed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    kyc_status: Mapped[str] = mapped_column(String(20), default="pending")  # pending, provided, verified
    tier_label: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="invited")  # invited, onboarding, active, locked, exited
    invite_token_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    invite_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    onboarding_step: Mapped[int] = mapped_column(Integer, default=1)
    agreement_signed_name: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    agreement_signed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    identity_confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sign_in_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class FundingRound(Row, Base):
    __tablename__ = "funding_rounds"
    name: Mapped[str] = mapped_column(String(80))
    target_minor: Mapped[int] = mapped_column(BigInteger)
    founders_committed_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    raised_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(12), default="open")  # open, closed
    opened_on: Mapped[Optional[date]] = mapped_column(Date, nullable=True)


class Investment(Row, Base):
    __tablename__ = "investments"
    investor_id: Mapped[uuid.UUID] = fk("investors", nullable=False, ondelete="RESTRICT")
    round_id: Mapped[Optional[uuid.UUID]] = fk("funding_rounds")
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    shares: Mapped[int] = mapped_column(BigInteger)
    percent: Mapped[float] = mapped_column(Numeric(7, 4))  # from the signed agreement; never computed by the UI
    signed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    payment_ref: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    payment_confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    payment_confirmed_by: Mapped[Optional[uuid.UUID]] = fk("users")
    agreement_file: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)


class Valuation(Row, Base):
    __tablename__ = "valuations"
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    effective_date: Mapped[date] = mapped_column(Date, index=True)
    basis_note: Mapped[str] = mapped_column(Text)
    label: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)  # "Sep · entry"
    set_by: Mapped[Optional[uuid.UUID]] = fk("users")


class InvestorReport(Row, Base):
    __tablename__ = "investor_reports"
    title: Mapped[str] = mapped_column(String(160))
    period: Mapped[str] = mapped_column(String(7))
    body: Mapped[str] = mapped_column(Text)
    file: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    read_minutes: Mapped[int] = mapped_column(Integer, default=5)
    due_on: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    published_by: Mapped[Optional[uuid.UUID]] = fk("users")


class InvestorDocument(Row, Base):
    __tablename__ = "investor_documents"
    investor_id: Mapped[uuid.UUID] = fk("investors", nullable=False, ondelete="CASCADE")
    kind: Mapped[str] = mapped_column(String(30))  # agreement, certificate, receipt
    title: Mapped[str] = mapped_column(String(160))
    detail: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    file: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)  # private S3 key
    opened_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)  # first download by the holder


class InvestorReportRead(Row, Base):
    """A holder has opened a report (for the unread count beside Reports)."""

    __tablename__ = "investor_report_reads"
    investor_id: Mapped[uuid.UUID] = fk("investors", nullable=False, ondelete="CASCADE")
    report_id: Mapped[uuid.UUID] = fk("investor_reports", nullable=False, ondelete="CASCADE")
    __table_args__ = (UniqueConstraint("investor_id", "report_id"),)


class ExitRequest(Row, Base):
    __tablename__ = "exit_requests"
    investor_id: Mapped[uuid.UUID] = fk("investors", nullable=False, ondelete="CASCADE")
    valuation_id: Mapped[Optional[uuid.UUID]] = fk("valuations")
    estimate_minor: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(12), default="open")  # open, accepted, declined, withdrawn
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    decision_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    decided_by: Mapped[Optional[uuid.UUID]] = fk("users")
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class InvestorMessage(Row, Base):
    __tablename__ = "investor_messages"
    investor_id: Mapped[uuid.UUID] = fk("investors", nullable=False, ondelete="CASCADE")
    from_investor: Mapped[bool] = mapped_column(Boolean, default=True)
    author_user_id: Mapped[Optional[uuid.UUID]] = fk("users")
    body: Mapped[str] = mapped_column(Text)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class Milestone(Row, Base):
    """Company milestones shown to investors."""

    __tablename__ = "milestones"
    title: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(10), default="next")  # done, doing, next
    detail: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)  # "Sep", "At contract", "78%"
    position: Mapped[int] = mapped_column(Integer, default=0)
