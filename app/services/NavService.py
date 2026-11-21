"""The small counts beside sidebar items."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import Access
from app.models.applications import Application
from app.models.clips import Clip
from app.models.investors import ExitRequest, InvestorMessage
from app.models.people import User
from app.models.store import Design, Order

REVIEW_DESIGN_STATUSES = {"design": ("creative_review", "founder_approval")}


async def review_count(db: AsyncSession, access: Access) -> int:
    total = 0
    lists = access.review_lists
    if "design" in lists:
        stages = ["founder_approval"] if access.is_founder else []
        if "creative" in access.areas:
            stages.append("creative_review")
        stages = stages or ["creative_review"]
        total += await db.scalar(select(func.count()).select_from(Design).where(Design.status.in_(stages))) or 0
    tracks = [t for t in ("designer", "clipper", "streamer") if t in lists]
    if tracks:
        total += await db.scalar(select(func.count()).select_from(Application).where(
            Application.track.in_(tracks), Application.status.in_(("new", "reviewing")))) or 0
    return total


async def _n(db: AsyncSession, model, *where) -> int:
    return await db.scalar(select(func.count()).select_from(model).where(*where)) or 0


async def nav_counts(db: AsyncSession, access: Access) -> dict:
    """What is waiting on each desk: the items its page asks someone to act on. People is the headcount."""
    from datetime import timedelta

    from app.core.security import now
    from app.models.agreements import Agreement
    from app.models.money import Decision, PayrollRun
    from app.models.partnerships import Deliverable
    from app.models.people import Task
    from app.models.posts import Post, TicketBooking
    from app.models.store import StoreInterest
    from app.models.talent import CreativeRequest, EditJob, Streamer
    from app.models.tech import EquipmentIssue, Incident

    counts: dict = {}
    week = now() + timedelta(days=7)
    if access.can("command"):
        counts["command"] = await _n(db, Decision, Decision.status == "open")
    if access.can("people"):
        counts["people"] = await _n(db, User, User.role.in_(("founder", "cofounder", "dept_head", "finance", "staff", "streamer")),
                                    User.status.in_(("active", "invited")))
    if access.can("review"):
        counts["review"] = await review_count(db, access)
    if access.can("inbox"):
        from app.api.v1.endpoints.contact import inbox_count

        counts["inbox"] = await inbox_count(db, access)
    if access.can("capital"):
        counts["capital"] = await _n(db, ExitRequest, ExitRequest.status == "open") + await _n(
            db, InvestorMessage, InvestorMessage.from_investor.is_(True), InvestorMessage.read_at.is_(None))
    if access.can("agreements"):
        counts["agreements"] = await _n(db, Agreement, Agreement.status == "draft")
    if access.can("production"):
        counts["production"] = await _n(db, EditJob, EditJob.status != "done")  # the edit queue
    if access.can("partnerships"):  # deliverables owed in the next week, or late
        counts["partnerships"] = await _n(db, Deliverable, Deliverable.status != "done",
                                          (Deliverable.due_at.is_(None)) | (Deliverable.due_at <= week))
    if access.can("social"):
        counts["social"] = await _n(db, Clip, Clip.status.in_(("submitted", "checking")))
    if access.can("creative"):  # designs to quality-check, and requests nobody has picked up
        counts["creative"] = await _n(db, Design, Design.status == "creative_review") + await _n(
            db, CreativeRequest, CreativeRequest.status == "requested")
    if access.can("tech"):  # problems streamers reported, and incidents still open
        counts["tech"] = await _n(db, EquipmentIssue, EquipmentIssue.status == "open") + await _n(db, Incident, Incident.resolved_at.is_(None))
    if access.can("finance"):  # a payout run to submit, or approved and waiting to be paid
        counts["finance"] = await _n(db, PayrollRun, PayrollRun.status.in_(("draft", "approved")))
    if access.can("store"):  # orders to pack and ship, and new interest to call back
        counts["store"] = await _n(db, Order, Order.status.in_(("paid", "packing", "to_ship"))) + await _n(
            db, StoreInterest, StoreInterest.status == "new")
    if access.can("posts"):  # ticket reservations to call about, for events still to come
        counts["posts"] = await db.scalar(select(func.coalesce(func.count(TicketBooking.id), 0)).join(Post, Post.id == TicketBooking.post_id).where(
            TicketBooking.status == "interest", Post.status == "published",
            func.coalesce(Post.ends_at, Post.starts_at + timedelta(hours=3)) >= now())) or 0
    if access.can("talent") and access.user.role == "streamer":  # brand work the streamer owes this week
        s = (await db.execute(select(Streamer).where(Streamer.user_id == access.user.id))).scalar_one_or_none()
        if s:
            counts["talent"] = await _n(db, Deliverable, Deliverable.streamer_id == s.id, Deliverable.status != "done",
                                        (Deliverable.due_at.is_(None)) | (Deliverable.due_at <= week))
    if access.can("suggestions"):  # new ideas waiting for a first answer; for the Founder, ideas waiting for a decision too
        from app.services.SuggestionService import nav_count

        counts["suggestions"] = await nav_count(db, access)
    if access.can("me"):  # open tasks, and agreements waiting for their signature
        counts["me"] = await _n(db, Task, Task.owner_user_id == access.user.id, Task.status != "done") + await _n(
            db, Agreement, Agreement.user_id == access.user.id, Agreement.status == "offered")
    return {k: v or None for k, v in counts.items()}


ACCESS_LABEL = {
    "command": "Executive · full access", "finance": "Finance · money access", "tech": "Tech and Dev · systems access",
    "creative": "Creative · photo, design, video", "production": "Content and Production", "store": "Operations · store access",
    "partnerships": "Marketing and Partnerships", "social": "Marketing and Partnerships", "capital": "Strategy and Investors",
    "talent": "Talent", "people": "People and teams",
}


async def footer(db: AsyncSession, access: Access) -> str:
    """The status line at the bottom of the sidebar, for the person's own desk."""
    from datetime import date as _date

    from app.models.money import PayrollRun
    from app.models.store import Product
    from app.models.talent import Streamer
    from app.services import CompanyService
    from app.services.money import month_key, prev_month

    desk = access.desk
    today = CompanyService.today()
    if desk == "command":
        start = _date(2026, 10, 1)
        month = (today.year - start.year) * 12 + today.month - start.month + 1
        return f"Pilot · Month {max(1, min(month, 6))} of 6"
    if desk == "finance":
        last = (await db.execute(select(PayrollRun).where(PayrollRun.status == "paid").order_by(PayrollRun.period.desc()).limit(1))).scalar_one_or_none()
        key = last.period if last else prev_month(month_key(today))
        y, m = map(int, key.split("-"))
        import calendar
        return f"Books closed to {calendar.monthrange(y, m)[1]} {calendar.month_abbr[m]}"
    if desk == "production":
        cards = {c["slug"]: c for c in await CompanyService.department_cards(db)}
        return f"{cards['production']['number']} this month"
    if desk == "social":
        streak = await CompanyService.latest_kpi(db, "posting_streak_days")
        return f"{streak:,.0f}-day posting streak" if streak else "Posting streak starts today"
    if desk == "creative":
        cards = {c["slug"]: c for c in await CompanyService.department_cards(db)}
        return f"{cards['creative']['number']} delivered this month"
    if desk == "tech":
        cards = {c["slug"]: c for c in await CompanyService.department_cards(db)}
        return f"{cards['tech']['number']} · 30 days"
    if desk == "store":
        live = await db.scalar(select(func.count()).select_from(Product).where(Product.status == "live")) or 0
        return f"{live} products live"
    if desk == "partnerships":
        return "Target: 1+ paid deal by month 6"
    if desk == "talent" and access.user.role == "streamer":
        s = (await db.execute(select(Streamer).where(Streamer.user_id == access.user.id))).scalar_one_or_none()
        from app.services.AgreementService import for_user, standing

        current = [a for a in await for_user(db, access.user.id, ("signed",)) if standing(a, today)["phase"] in ("active", "ending")]
        if current and current[0].stage == "trial" and current[0].ends_on:
            st = standing(current[0], today)
            return f"Trial · day {st['day']} of {st['days']}"
        if s and s.contract_start:
            months = (today.year - s.contract_start.year) * 12 + today.month - s.contract_start.month + 1
            return f"Signing {s.signing_number:03d} · month {months}"
    if access.can("review"):
        n = await review_count(db, access)
        return f"{n} waiting for review"
    return "Streamers Hub · Accra"
