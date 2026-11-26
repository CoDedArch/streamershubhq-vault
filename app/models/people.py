"""People and access: users, departments, staff profiles, vesting, onboarding, sign-in and the audit log.
Also goals, performance scores, KPI snapshots, tasks and the business-rule settings."""

import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, Date, DateTime, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Row, fk


class Department(Row, Base):
    __tablename__ = "departments"
    name: Mapped[str] = mapped_column(String(80), unique=True)
    slug: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    # No foreign key on purpose: users point at departments, so this would be a cycle.
    lead_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)


class User(Row, Base):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    handle: Mapped[Optional[str]] = mapped_column(String(60), unique=True, nullable=True)  # @ama.draws, @kwesi.cuts
    phone_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    phone_last4: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)
    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(20), index=True)
    department_id: Mapped[Optional[uuid.UUID]] = fk("departments")
    status: Mapped[str] = mapped_column(String(20), default="invited")  # invited, active, suspended
    # A sample account (the placeholder clippers on the Clip Table): can't sign in, is never paid, and is removed in one go
    is_sample: Mapped[bool] = mapped_column(Boolean, default=False)
    # The photo's file key, signed each time it's shown (S3Service.signed_url passes a full link through unchanged)
    avatar_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    invite_token_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    invite_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Desks this person may step into and work on besides their own: ["*"] for every operational desk, or
    # a list of areas. The Founder always can. Set by the Founder in People.
    step_in: Mapped[list] = mapped_column(JSON, default=list)


class StaffProfile(Row, Base):
    __tablename__ = "staff_profiles"
    user_id: Mapped[uuid.UUID] = fk("users", nullable=False, ondelete="CASCADE")
    title: Mapped[str] = mapped_column(String(120))
    # Which desk the person lands on: command, people, review, finance, store, production, partnerships,
    # social, creative, tech, talent, capital. Department sets a default; the title can refine it.
    desk: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    reports_to_id: Mapped[Optional[uuid.UUID]] = fk("users")
    start_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    pay_type: Mapped[str] = mapped_column(String(20), default="none")  # salary, equity_vesting, none
    payout_method: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # momo, bank
    momo_number_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    momo_last3: Mapped[Optional[str]] = mapped_column(String(3), nullable=True)
    momo_network: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # mtn, telecel, airteltigo
    bank_name: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    bank_account_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bank_last4: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)
    emergency_contact: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    whatsapp_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class EquityGrant(Row, Base):
    __tablename__ = "equity_grants"
    holder_user_id: Mapped[uuid.UUID] = fk("users", nullable=False, ondelete="CASCADE")
    percent: Mapped[float] = mapped_column(Numeric(7, 4))
    vest_months: Mapped[int] = mapped_column(Integer, default=48)
    cliff_months: Mapped[int] = mapped_column(Integer, default=12)
    start_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active, cancelled
    agreement_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)  # the agreement that granted it


class OnboardingRun(Row, Base):
    __tablename__ = "onboarding_runs"
    user_id: Mapped[uuid.UUID] = fk("users", nullable=False, ondelete="CASCADE")
    kind: Mapped[str] = mapped_column(String(20))  # staff, investor, designer, clipper
    current_step: Mapped[int] = mapped_column(Integer, default=1)
    completed_steps: Mapped[list] = mapped_column(JSON, default=list)
    data: Mapped[dict] = mapped_column(JSON, default=dict)  # signatures, tool confirmations
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("user_id", "kind"),)


class AuthSession(Row, Base):
    """One signed-in session. The cookie carries its id, so signing out or "Lock vault" ends it for good."""

    __tablename__ = "auth_sessions"
    user_id: Mapped[Optional[uuid.UUID]] = fk("users", ondelete="CASCADE")
    investor_id: Mapped[Optional[uuid.UUID]] = fk("investors", ondelete="CASCADE")
    kind: Mapped[str] = mapped_column(String(10))  # os, portal, vault
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    step_up_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)


class LoginChallenge(Row, Base):
    """A pending one-time code. Only a hash of the code is kept."""

    __tablename__ = "login_challenges"
    user_id: Mapped[Optional[uuid.UUID]] = fk("users", ondelete="CASCADE")
    investor_id: Mapped[Optional[uuid.UUID]] = fk("investors", ondelete="CASCADE")
    session_id: Mapped[Optional[uuid.UUID]] = fk("auth_sessions", ondelete="CASCADE")
    purpose: Mapped[str] = mapped_column(String(20))  # login, vault, step_up, reset
    code_hash: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class SignInAttempt(Row, Base):
    """Every failed or blocked sign-in, for rate limits by IP and by contact."""

    __tablename__ = "sign_in_attempts"
    kind: Mapped[str] = mapped_column(String(10))  # os, portal, vault
    contact: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    ip: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)


class AuditLog(Row, Base):
    __tablename__ = "audit_log"
    actor_user_id: Mapped[Optional[uuid.UUID]] = fk("users")
    actor_label: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    action: Mapped[str] = mapped_column(String(80), index=True)
    entity: Mapped[str] = mapped_column(String(60))
    entity_id: Mapped[Optional[str]] = mapped_column(String(160), nullable=True, index=True)  # a slug or reference, not only a UUID
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    before: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    after: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class Goal(Row, Base):
    __tablename__ = "goals"
    owner_user_id: Mapped[Optional[uuid.UUID]] = fk("users")
    department_id: Mapped[Optional[uuid.UUID]] = fk("departments")
    title: Mapped[str] = mapped_column(String(200))
    metric_key: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    target: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    current: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    unit: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open, done, missed
    is_pilot_target: Mapped[bool] = mapped_column(Boolean, default=False)
    sort: Mapped[int] = mapped_column(Integer, default=0)
    assigned_by_id: Mapped[Optional[uuid.UUID]] = fk("users")  # who set it (empty on older goals and the sample data)
    notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)  # owner emailed about it


class Task(Row, Base):
    __tablename__ = "tasks"
    owner_user_id: Mapped[Optional[uuid.UUID]] = fk("users")
    department_id: Mapped[Optional[uuid.UUID]] = fk("departments")
    title: Mapped[str] = mapped_column(String(200))
    due_on: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open, doing, done
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    on_time: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    is_weekly_milestone: Mapped[bool] = mapped_column(Boolean, default=False)
    progress_label: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)  # "6 of 7"
    assigned_by_id: Mapped[Optional[uuid.UUID]] = fk("users")  # who gave it (empty on older tasks and the sample data)
    notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)  # owner emailed about it


class PerformanceScore(Row, Base):
    __tablename__ = "performance_scores"
    user_id: Mapped[uuid.UUID] = fk("users", nullable=False, ondelete="CASCADE")
    period: Mapped[str] = mapped_column(String(7))  # 2026-11
    score: Mapped[int] = mapped_column(Integer)
    inputs: Mapped[dict] = mapped_column(JSON, default=dict)
    set_by: Mapped[Optional[uuid.UUID]] = fk("users")
    __table_args__ = (UniqueConstraint("user_id", "period"),)


class LeadRating(Row, Base):
    """A lead's 0-100 rating of one person for one month, one input to the performance score."""

    __tablename__ = "lead_ratings"
    user_id: Mapped[uuid.UUID] = fk("users", nullable=False, ondelete="CASCADE")
    period: Mapped[str] = mapped_column(String(7))
    rating: Mapped[int] = mapped_column(Integer)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rated_by: Mapped[Optional[uuid.UUID]] = fk("users")
    __table_args__ = (UniqueConstraint("user_id", "period"),)


class KpiSnapshot(Row, Base):
    __tablename__ = "kpi_snapshots"
    metric_key: Mapped[str] = mapped_column(String(60), index=True)  # yt_subscribers, avg_live_viewers, ...
    value: Mapped[float] = mapped_column(Numeric(16, 2))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source: Mapped[str] = mapped_column(String(30), default="manual")  # manual, youtube_api, twitch_api
    entered_by: Mapped[Optional[uuid.UUID]] = fk("users")


class AppSetting(Row, Base):
    """Business rules the Founder can change: commission, clipper pay, salary tiers, exit terms, refund window."""

    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(60), unique=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_by: Mapped[Optional[uuid.UUID]] = fk("users")



class StepInAction(Row, Base):
    """Something done on a desk by someone stepping in (the Founder, or someone given step-in access), so
    the desk's own team sees it was not one of them."""

    __tablename__ = "step_in_actions"
    area: Mapped[str] = mapped_column(String(30), index=True)
    actor_user_id: Mapped[Optional[uuid.UUID]] = fk("users")
    actor_label: Mapped[str] = mapped_column(String(160))
    action: Mapped[str] = mapped_column(String(200))
    method: Mapped[str] = mapped_column(String(8))
    path: Mapped[str] = mapped_column(String(300))
