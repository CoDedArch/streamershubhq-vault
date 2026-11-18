"""People, teams and performance; goals, tasks and lead ratings; inviting staff; onboarding; and each
person's own profile, pay and vesting. Department heads see their own department; vesting is visible only
to the Founder, Finance and the holder."""

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from app.core.types import Email
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import (ALL_ROLES, AREAS, DEPARTMENTS, OPERATIONAL_AREAS, OS_ROLES, Access, current_access, load_access,
                              require_area)
from app.core.crypto import digits_tail, encrypt
from app.core.database import aget_db
from app.core.security import client_ip, now
from app.models.money import PayrollLine, PayrollRun
from app.models.people import Department, EquityGrant, Goal, LeadRating, OnboardingRun, StaffProfile, Task, User
from app.models.talent import Streamer
from app.services import PerformanceService, S3Service
from app.services.AuditService import audit
from app.services.CompanyService import today
from app.services.MemberService import create_member
from app.services.money import month_key, month_label

router = APIRouter(tags=["people"])
STAFF_ROLES = ("founder", "cofounder", "dept_head", "finance", "staff", "streamer")

STEPS = {
    "staff": ["Welcome", "Your agreement", "Your profile", "Your tools", "Your first week"],
    "designer": ["Welcome", "Designer terms", "Payout details", "Your Studio"],
    "clipper": ["Welcome", "House rules", "Payout details", "Your desk"],
}
TOOLS = {
    "command": ["Hub OS · Command centre", "Shared drive", "Team channel", "Bank and mobile money approvals"],
    "finance": ["Hub OS · Finance desk", "Mobile money business wallet", "Bank portal", "Shared drive"],
    "creative": ["Hub OS · Creative desk", "Brand kit", "Shared drive", "Team channel"],
    "production": ["Hub OS · Production desk", "Streaming software and scenes", "Shared drive", "Team channel"],
    "partnerships": ["Hub OS · Partnerships desk", "Media kit and rate card", "Shared drive", "Team channel"],
    "social": ["Hub OS · Social desk", "Platform manager access", "Brand kit", "Team channel"],
    "tech": ["Hub OS · Tech desk", "Studio network admin", "Platform account vault", "Team channel"],
    "talent": ["Hub OS · Your desk", "Stream keys (from Tech)", "Brand kit", "Team channel"],
    "store": ["Hub OS · Store desk", "Courier account", "Shared drive", "Team channel"],
    "capital": ["Hub OS · Investor relations", "Investor documents folder", "Shared drive", "Team channel"],
}


def _initials(name: str) -> str:
    from app.api.v1.endpoints.auth import initials

    return initials(name)


async def _scope(db: AsyncSession, access: Access) -> Optional[uuid.UUID]:
    """None for everyone; a department id when the viewer only sees their own department."""
    if access.user.role in ("founder", "cofounder") or "people" in access.stepped_in:
        return None
    return access.user.department_id


@router.get("/people")
async def people(access: Access = Depends(require_area("people")), db: AsyncSession = Depends(aget_db)):
    scope = await _scope(db, access)
    depts = (await db.execute(select(Department).order_by(Department.sort))).scalars().all()
    q = select(User).where(User.role.in_(STAFF_ROLES), User.status.in_(("active", "invited")))
    if scope:
        q = q.where(User.department_id == scope)
    users = (await db.execute(q.order_by(User.created_at))).scalars().all()
    period = month_key(today())
    week_start = datetime.combine(today() - timedelta(days=today().weekday()), datetime.min.time(), timezone.utc)
    dept_name = {d.id: d.name for d in depts}
    team = []
    for u in users:
        a = await load_access(db, u)
        perf = await PerformanceService.work_out(db, u, period)
        run = (await db.execute(select(OnboardingRun).where(OnboardingRun.user_id == u.id, OnboardingRun.kind == "staff"))).scalar_one_or_none()
        onboarding = run is not None and run.completed_at is None
        latest = (await db.execute(select(Task).where(Task.owner_user_id == u.id, Task.status == "done")
                                   .order_by(Task.closed_at.desc()).limit(1))).scalar_one_or_none()
        week_closed = await db.scalar(select(func.count()).select_from(Task).where(
            Task.owner_user_id == u.id, Task.status == "done", Task.closed_at >= week_start)) or 0
        status, tone = ("Onboarding", "info") if onboarding else PerformanceService.status_for(perf["score"])
        team.append({"id": str(u.id), "initials": _initials(u.name), "name": u.name, "title": a.title,
                     "department": dept_name.get(u.department_id, "—"), "status": status, "tone": tone,
                     "headline": (f"Onboarding step {run.current_step} of 5" if onboarding else (latest.title if latest else "No tasks closed yet")),
                     "tasks_week": week_closed, "score": perf["score"], "inputs": perf["inputs"], "invited": u.status == "invited",
                     "steps_in": len(a.stepped_in) if u.role != "founder" else None})
    weeks = []
    for i in range(7, -1, -1):
        ws = week_start - timedelta(days=7 * i)
        we = ws + timedelta(days=7)
        tq = select(func.count()).select_from(Task).where(Task.status == "done", Task.closed_at >= ws, Task.closed_at < we)
        if scope:
            tq = tq.where(Task.department_id == scope)
        weeks.append({"label": f"W{ws.isocalendar()[1]}", "closed": await db.scalar(tq) or 0, "current": i == 0})
    top = max((w["closed"] for w in weeks), default=0) or 1
    for w in weeks:
        w["height"] = max(5, round(w["closed"] * 90 / top))
    runs = (await db.execute(select(OnboardingRun, User).join(User, User.id == OnboardingRun.user_id)
                             .where(OnboardingRun.kind == "staff", OnboardingRun.completed_at.is_(None)))).all()
    pipeline = []
    for i, (title, detail) in enumerate([("Invited", "Invite link sent"), ("Agreement signed", "Role, vesting and confidentiality"),
                                         ("Profile and payout details", "Mobile Money or bank account"),
                                         ("Tools and access", "Accounts created by Tech"), ("First week plan", "Set by the department lead")], 1):
        at = [u for r, u in runs if (r.current_step == i and u.status == "active") or (i == 1 and u.status == "invited")]
        if i == 1:
            at = [u for r, u in runs if u.status == "invited" or r.current_step == 1]
        pipeline.append({"title": title, "detail": detail, "count": len(at), "people": [u.name for u in at]})
    dept_cards = []
    for d in depts:
        if scope and d.id != scope:
            continue
        lead = await db.get(User, d.lead_user_id) if d.lead_user_id else None
        count = sum(1 for u in users if u.department_id == d.id)
        dept_cards.append({"slug": d.slug, "name": d.name, "lead": lead.name if lead else None, "members": count})
    last_start = week_start - timedelta(days=7)

    async def closed(since, until):
        q = select(Task).where(Task.status == "done", Task.closed_at >= since, Task.closed_at < until)
        if scope:
            q = q.where(Task.department_id == scope)
        return (await db.execute(q)).scalars().all()

    this_week, last_week = await closed(week_start, week_start + timedelta(days=7)), await closed(last_start, week_start)
    pct = lambda rows: round(sum(1 for t in rows if t.on_time) * 100 / len(rows)) if rows else None  # noqa: E731
    oq = select(Task).where(Task.status != "done", Task.due_on < today())
    if scope:
        oq = oq.where(Task.department_id == scope)
    overdue = (await db.execute(oq)).scalars().all()
    by_dept: dict = {}
    for t in overdue:
        by_dept[t.department_id] = by_dept.get(t.department_id, 0) + 1
    worst = max(by_dept.items(), key=lambda kv: kv[1]) if by_dept else None
    onboarding_names = [u.name for r, u in runs]
    kpis = {"tasks_week": len(this_week), "tasks_change": len(this_week) - len(last_week),
            "on_time": pct(this_week), "on_time_change": (pct(this_week) - pct(last_week)) if pct(this_week) is not None and pct(last_week) is not None else None,
            "overdue": len(overdue), "overdue_where": f"{worst[1]} in {dept_name.get(worst[0], 'no department')}" if worst else None,
            "onboarding": len(onboarding_names), "onboarding_names": onboarding_names}
    # Streamer profiles on the website with no account yet: an invite can be linked to one of them
    profiles = (await db.execute(select(Streamer).where(Streamer.user_id.is_(None), Streamer.status == "signed")
                                 .order_by(Streamer.signing_number))).scalars().all() if access.is_founder else []
    return {"departments": dept_cards, "team": team, "weeks": weeks, "pipeline": pipeline, "period": month_label(period), "kpis": kpis,
            "can_invite": access.user.role in ("founder", "cofounder", "dept_head"), "scoped": scope is not None,
            "profiles": [{"id": str(p.id), "stage_name": p.stage_name, "known_as": p.known_as, "number": p.signing_number,
                          "public": p.is_public} for p in profiles]}


class InviteIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: Email
    role: str
    department: Optional[str] = None
    title: str = Field(min_length=2, max_length=120)
    desk: Optional[str] = None
    pay_type: str = Field(default="equity_vesting", pattern="^(salary|equity_vesting|revenue_share|none)$")
    # The agreement they sign in onboarding: a template, plus this person's own terms and length
    template_id: Optional[str] = None
    terms: Optional[list[dict]] = None
    term_months: Optional[int] = None
    # Streamers: the public profile this account belongs to (e.g. one already on the website), or a new one
    streamer_id: Optional[uuid.UUID] = None
    stage_name: Optional[str] = Field(default=None, max_length=80)


@router.post("/people/invite")
async def invite(body: InviteIn, request: Request, access: Access = Depends(require_area("people")), db: AsyncSession = Depends(aget_db)):
    role = body.role
    if role not in OS_ROLES - {"founder"}:
        raise HTTPException(400, "Pick a Hub OS role.")
    if access.user.role != "founder":
        mine = (await db.get(Department, access.user.department_id)).slug if access.user.department_id else None
        if role != "staff" or body.department != mine:
            raise HTTPException(403, "You can invite staff to your own department. Ask the Founder for other roles.")
    if body.department and body.department not in dict(DEPARTMENTS):
        raise HTTPException(400, "Unknown department.")
    from app.api.v1.endpoints.auth import issue_invite

    from app.models.agreements import AgreementTemplate
    from app.services import AgreementService

    template = None
    if body.template_id:
        if access.user.role != "founder":
            raise HTTPException(403, "Only the Founder sets agreement terms. Leave the agreement as it is.")
        try:
            template = await db.get(AgreementTemplate, uuid.UUID(body.template_id))
        except ValueError:
            template = None
        if not template or template.status != "active" or template.audience not in ("staff", "streamer"):
            raise HTTPException(400, "Choose a team or streamer agreement template.")
    else:
        template = await AgreementService.default_template(db, "streamer", "trial") if role == "streamer" else \
            await AgreementService.default_template(db, "staff")
    if body.terms is not None and access.user.role != "founder":
        raise HTTPException(403, "Only the Founder sets agreement terms.")
    profile = None
    if role == "streamer" and body.streamer_id:
        profile = await db.get(Streamer, body.streamer_id, with_for_update=True)
        if not profile:
            raise HTTPException(400, "Pick their profile from the list.")
        if profile.user_id:
            raise HTTPException(409, f"{profile.stage_name} already has an account.")
    user = await create_member(db, name=body.name, email=body.email, role=role, department=body.department, title=body.title,
                               desk=body.desk, pay_type=body.pay_type, onboarding="staff")
    if role == "streamer":
        if profile:  # their profile is already on the website; the account takes it over
            profile.user_id = user.id
        else:  # a new profile, kept off the website until it's ready
            number = ((await db.scalar(select(func.max(Streamer.signing_number)))) or 0) + 1
            profile = Streamer(user_id=user.id, stage_name=(body.stage_name or body.name).strip(), category="gaming", signing_number=number,
                               status="signed", is_public=False)
            db.add(profile)
        await db.flush()
    if template:
        await AgreementService.create(db, template, user=user, by=access.user, terms=body.terms, term_months=body.term_months,
                                      email=False)
    await audit(db, access.user, "people.invited", "user", user.id, request=request,
                summary=f"{body.name} · {body.title} · {role}" + (f" · profile {profile.stage_name}" if profile else ""))
    await issue_invite(db, user, "staff")
    await db.commit()
    return {"id": str(user.id)}


class RoleIn(BaseModel):
    role: Optional[str] = None
    department: Optional[str] = None
    desk: Optional[str] = None
    title: Optional[str] = Field(default=None, max_length=120)
    status: Optional[str] = Field(default=None, pattern="^(active|suspended)$")
    # ["*"] for every operational desk, a list of desks, or [] for none
    step_in: Optional[list[str]] = Field(default=None, max_length=len(OPERATIONAL_AREAS))


@router.patch("/people/{user_id}")
async def change_role(user_id: uuid.UUID, body: RoleIn, request: Request, access: Access = Depends(require_area("people")),
                      db: AsyncSession = Depends(aget_db)):
    if not access.is_founder:
        raise HTTPException(403, "Only the Founder changes roles.")
    u = await db.get(User, user_id)
    if not u:
        raise HTTPException(404, "Person not found.")
    if u.id == access.user.id and (body.role or body.status):
        raise HTTPException(400, "You can't change your own role or status.")
    profile = (await db.execute(select(StaffProfile).where(StaffProfile.user_id == u.id))).scalar_one_or_none()
    before = {"role": u.role, "department_id": str(u.department_id) if u.department_id else None, "status": u.status,
              "desk": profile.desk if profile else None, "step_in": list(u.step_in or [])}
    if body.step_in is not None:
        wanted = set(body.step_in)
        if wanted - set(OPERATIONAL_AREAS) - {"*"}:
            raise HTTPException(400, "Pick desks from the list.")
        role_after = body.role or u.role
        if wanted and (role_after not in OS_ROLES or role_after in ("streamer", "founder")):
            raise HTTPException(400, "Step-in access is for Hub OS staff.")
        u.step_in = ["*"] if "*" in wanted else [a for a in OPERATIONAL_AREAS if a in wanted]
    if body.role:
        if body.role not in ALL_ROLES:
            raise HTTPException(400, "Unknown role.")
        u.role = body.role
    if body.department is not None:
        d = (await db.execute(select(Department).where(Department.slug == body.department))).scalar_one_or_none() if body.department else None
        u.department_id = d.id if d else None
    if body.status:
        u.status = body.status
    if profile and body.desk is not None:
        profile.desk = body.desk or None
    if profile and body.title:
        profile.title = body.title
    await audit(db, access.user, "people.role_changed", "user", u.id, request=request, before=before,
                after={"role": u.role, "status": u.status, "desk": profile.desk if profile else None, "step_in": u.step_in})
    await db.commit()
    return {"ok": True}


@router.get("/people/{user_id}")
async def person(user_id: uuid.UUID, access: Access = Depends(require_area("people")), db: AsyncSession = Depends(aget_db)):
    u = await db.get(User, user_id)
    scope = await _scope(db, access)
    if not u or (scope and u.department_id != scope):
        raise HTTPException(404, "Person not found.")
    a = await load_access(db, u)
    period = month_key(today())
    perf = await PerformanceService.work_out(db, u, period)
    goals = (await db.execute(select(Goal).where(Goal.owner_user_id == u.id).order_by(Goal.period_end.desc()).limit(20))).scalars().all()
    tasks = (await db.execute(select(Task).where(Task.owner_user_id == u.id).order_by(Task.created_at.desc()).limit(30))).scalars().all()
    out = {"id": str(u.id), "name": u.name, "email": u.email, "role": u.role, "title": a.title, "department": a.department,
           "desk": a.desk, "status": u.status, "performance": perf,
           "step_in": {"grant": list(u.step_in or []), "desks": a.stepped_in,
                       "options": [{"area": x, "label": AREAS[x][0]} for x in OPERATIONAL_AREAS],
                       "can_edit": access.is_founder and u.role in OS_ROLES and u.role not in ("founder", "streamer")},
           "goals": [_goal(g) for g in goals], "tasks": [_task(t) for t in tasks]}
    if access.user.role in ("founder", "finance") or access.user.id == u.id:
        out["vesting"] = await _vesting(db, u.id)
    if access.is_founder:
        from app.services import AgreementService

        out["agreements"] = [await AgreementService.row(db, a) for a in await AgreementService.for_user(db, u.id)]
    return out


def _goal(g: Goal) -> dict:
    target, current = float(g.target), float(g.current)
    return {"id": str(g.id), "title": g.title, "target": target, "current": current, "unit": g.unit, "status": g.status,
            "period_start": g.period_start.isoformat(), "period_end": g.period_end.isoformat(),
            "percent": min(100, round(current * 100 / target)) if target else 0}


def _task(t: Task) -> dict:
    return {"id": str(t.id), "title": t.title, "due_on": t.due_on.isoformat() if t.due_on else None, "status": t.status,
            "on_time": t.on_time, "progress": t.progress_label, "weekly": t.is_weekly_milestone,
            "closed_at": t.closed_at.isoformat() if t.closed_at else None}


async def _vesting(db: AsyncSession, user_id) -> list[dict]:
    rows = (await db.execute(select(EquityGrant).where(EquityGrant.holder_user_id == user_id, EquityGrant.status == "active"))).scalars().all()
    out = []
    for g in rows:
        months = (today().year - g.start_date.year) * 12 + today().month - g.start_date.month
        vested_months = 0 if months < g.cliff_months else min(months, g.vest_months)
        pct = float(g.percent)
        out.append({"percent": pct, "vest_months": g.vest_months, "cliff_months": g.cliff_months,
                    "start_date": g.start_date.isoformat(), "months_in": max(0, months),
                    "vested_percent": round(pct * vested_months / g.vest_months, 4),
                    "progress": round(vested_months * 100 / g.vest_months)})
    return out


class GoalIn(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    owner_user_id: Optional[uuid.UUID] = None
    target: float = Field(ge=0)
    current: float = Field(default=0, ge=0)
    unit: Optional[str] = Field(default=None, max_length=30)
    period_start: date
    period_end: date
    metric_key: Optional[str] = None


async def _can_manage(db: AsyncSession, access: Access, owner_id: Optional[uuid.UUID]) -> None:
    if access.is_founder or "people" in access.stepped_in:
        return
    if access.user.role not in ("cofounder", "dept_head"):
        raise HTTPException(403, "Only leads set goals.")
    if owner_id:
        owner = await db.get(User, owner_id)
        if not owner or owner.department_id != access.user.department_id:
            raise HTTPException(403, "You manage goals for your own department.")


@router.post("/goals")
async def add_goal(body: GoalIn, request: Request, access: Access = Depends(require_area("people")), db: AsyncSession = Depends(aget_db)):
    await _can_manage(db, access, body.owner_user_id)
    if body.period_end < body.period_start:
        raise HTTPException(400, "The end date is before the start.")
    owner = await db.get(User, body.owner_user_id) if body.owner_user_id else None
    g = Goal(title=body.title, owner_user_id=body.owner_user_id, department_id=owner.department_id if owner else access.user.department_id,
             target=body.target, current=body.current, unit=body.unit, period_start=body.period_start,
             period_end=body.period_end, metric_key=body.metric_key)
    db.add(g)
    await audit(db, access.user, "goal.added", "goal", g.id, request=request, summary=body.title)
    await db.commit()
    return _goal(g)


class GoalUpdate(BaseModel):
    current: Optional[float] = Field(default=None, ge=0)
    target: Optional[float] = Field(default=None, ge=0)
    status: Optional[str] = Field(default=None, pattern="^(open|done|missed)$")


@router.patch("/goals/{goal_id}")
async def update_goal(goal_id: uuid.UUID, body: GoalUpdate, access: Access = Depends(current_access), db: AsyncSession = Depends(aget_db)):
    g = await db.get(Goal, goal_id)
    if not g:
        raise HTTPException(404, "Goal not found.")
    if g.owner_user_id != access.user.id:
        await _can_manage(db, access, g.owner_user_id)
    for field in ("current", "target", "status"):
        if getattr(body, field) is not None:
            setattr(g, field, getattr(body, field))
    await db.commit()
    return _goal(g)


class TaskIn(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    owner_user_id: uuid.UUID
    due_on: Optional[date] = None
    weekly: bool = False


@router.post("/tasks")
async def add_task(body: TaskIn, access: Access = Depends(require_area("people", "me")), db: AsyncSession = Depends(aget_db)):
    if body.owner_user_id != access.user.id:
        await _can_manage(db, access, body.owner_user_id)
    owner = await db.get(User, body.owner_user_id)
    t = Task(title=body.title, owner_user_id=body.owner_user_id, department_id=owner.department_id if owner else None,
             due_on=body.due_on, is_weekly_milestone=body.weekly)
    db.add(t)
    await db.commit()
    return _task(t)


class TaskUpdate(BaseModel):
    status: str = Field(pattern="^(open|doing|done)$")
    progress: Optional[str] = Field(default=None, max_length=40)


@router.patch("/tasks/{task_id}")
async def update_task(task_id: uuid.UUID, body: TaskUpdate, access: Access = Depends(current_access), db: AsyncSession = Depends(aget_db)):
    t = await db.get(Task, task_id)
    if not t:
        raise HTTPException(404, "Task not found.")
    if t.owner_user_id != access.user.id:
        await _can_manage(db, access, t.owner_user_id)
    t.status = body.status
    if body.progress is not None:
        t.progress_label = body.progress
    if body.status == "done" and not t.closed_at:
        t.closed_at = now()
        t.on_time = (t.due_on is None) or (today() <= t.due_on)
    if body.status != "done":
        t.closed_at, t.on_time = None, None
    await db.commit()
    return _task(t)


class RatingIn(BaseModel):
    rating: int = Field(ge=0, le=100)
    note: Optional[str] = Field(default=None, max_length=500)


@router.post("/people/{user_id}/rating")
async def rate(user_id: uuid.UUID, body: RatingIn, request: Request, access: Access = Depends(require_area("people")),
               db: AsyncSession = Depends(aget_db)):
    await _can_manage(db, access, user_id)
    if user_id == access.user.id:
        raise HTTPException(400, "You can't rate yourself.")
    period = month_key(today())
    row = (await db.execute(select(LeadRating).where(LeadRating.user_id == user_id, LeadRating.period == period))).scalar_one_or_none()
    if not row:
        row = LeadRating(user_id=user_id, period=period, rating=body.rating)
        db.add(row)
    row.rating, row.note, row.rated_by = body.rating, body.note, access.user.id
    u = await db.get(User, user_id)
    await db.flush()
    result = await PerformanceService.save(db, u, period, access.user)
    await audit(db, access.user, "performance.rated", "user", user_id, request=request, summary=f"{u.name}: {body.rating} for {period}")
    await db.commit()
    return result


# Onboarding ------------------------------------------------------------------------------------------

async def _run(db: AsyncSession, user: User) -> OnboardingRun:
    run = (await db.execute(select(OnboardingRun).where(OnboardingRun.user_id == user.id))).scalars().first()
    if not run:
        raise HTTPException(404, "There's no onboarding for this account.")
    return run


async def _onboarding_agreement(db: AsyncSession, user: User) -> Optional[dict]:
    """The agreement to sign in onboarding: the one waiting for them, else the one they signed."""
    from app.services import AgreementService

    a = await AgreementService.waiting_for(db, user.id) or next(iter(await AgreementService.for_user(db, user.id, ("signed",))), None)
    if not a:
        return None
    r = await AgreementService.row(db, a)
    return {"id": r["id"], "title": a.title, "text": await AgreementService.rendered(db, a), "terms": a.terms, "term": r["term"],
            "stage": a.stage, "status": a.status, "signed_at": r["signed_at"], "starts_on": r["starts_on"], "ends_on": r["ends_on"]}


@router.get("/onboarding/me")
async def my_onboarding(request: Request, access: Access = Depends(current_access), db: AsyncSession = Depends(aget_db)):
    run = await _run(db, access.user)
    profile = (await db.execute(select(StaffProfile).where(StaffProfile.user_id == access.user.id))).scalar_one_or_none()
    lead = None
    if profile and profile.reports_to_id:
        lead = await db.get(User, profile.reports_to_id)
    week = (await db.execute(select(Task).where(Task.owner_user_id == access.user.id, Task.status != "done")
                             .order_by(Task.due_on).limit(8))).scalars().all()
    return {"kind": run.kind, "step": run.current_step, "completed": run.completed_steps, "done": run.completed_at is not None,
            "steps": STEPS[run.kind], "agreement": await _onboarding_agreement(db, access.user), "tools": TOOLS.get(access.desk or "", TOOLS["talent"]),
            "tools_confirmed": run.data.get("tools", []), "title": access.title, "desk": access.desk, "home": access.home,
            "name": access.user.name, "lead": lead.name if lead else None, "first_week": [_task(t) for t in week],
            "profile": {"payout_method": profile.payout_method if profile else None,
                        "momo_hint": f"MoMo ··· {profile.momo_last3}" if profile and profile.momo_last3 else None,
                        "momo_network": profile.momo_network if profile else None,
                        "bank_hint": f"{profile.bank_name} ··· {profile.bank_last4}" if profile and profile.bank_last4 else None,
                        "emergency_contact": profile.emergency_contact if profile else None,
                        "phone_hint": f"··· {access.user.phone_last4}" if access.user.phone_last4 else None,
                        "avatar_url": S3Service.signed_url(access.user.avatar_url, 3600, str(request.base_url).rstrip("/"))}}


class StepIn(BaseModel):
    signature: Optional[str] = Field(default=None, max_length=160)
    display_name: Optional[str] = Field(default=None, max_length=120)
    phone: Optional[str] = Field(default=None, max_length=20)
    whatsapp: Optional[str] = Field(default=None, max_length=20)
    emergency_contact: Optional[str] = Field(default=None, max_length=200)
    payout_method: Optional[str] = Field(default=None, pattern="^(momo|bank)$")
    momo_number: Optional[str] = Field(default=None, max_length=20)
    momo_network: Optional[str] = Field(default=None, pattern="^(mtn|telecel|airteltigo)$")
    bank_name: Optional[str] = Field(default=None, max_length=80)
    bank_account: Optional[str] = Field(default=None, max_length=30)
    tools: Optional[list[str]] = None


async def save_payout(db: AsyncSession, user: User, body: StepIn, required: bool) -> None:
    profile = (await db.execute(select(StaffProfile).where(StaffProfile.user_id == user.id))).scalar_one_or_none()
    if not profile:
        profile = StaffProfile(user_id=user.id, title="Team member")
        db.add(profile)
    if body.payout_method == "momo":
        digits = "".join(c for c in body.momo_number or "" if c.isdigit())
        if len(digits) < 9 or not body.momo_network:
            raise HTTPException(400, {"message": "Enter your mobile money number and network.", "missing": ["momo_number"]})
        profile.payout_method, profile.momo_network = "momo", body.momo_network
        profile.momo_number_enc, profile.momo_last3 = encrypt(digits), digits[-3:]
    elif body.payout_method == "bank":
        digits = "".join(c for c in body.bank_account or "" if c.isdigit())
        if len(digits) < 6 or not body.bank_name:
            raise HTTPException(400, {"message": "Enter your bank and account number.", "missing": ["bank_account"]})
        profile.payout_method, profile.bank_name = "bank", body.bank_name.strip()
        profile.bank_account_enc, profile.bank_last4 = encrypt(digits), digits[-4:]
    elif required and not profile.payout_method:
        raise HTTPException(400, {"message": "Choose how you want to be paid.", "missing": ["payout_method"]})
    if body.emergency_contact is not None:
        profile.emergency_contact = body.emergency_contact.strip() or None
    if body.whatsapp:
        profile.whatsapp_enc = encrypt(body.whatsapp)
    if body.phone:
        user.phone_enc, user.phone_last4 = encrypt(body.phone), digits_tail(body.phone, 4)
    if body.display_name and body.display_name.strip():
        user.name = body.display_name.strip()


@router.post("/onboarding/me/steps/{n}")
async def onboarding_step(n: int, body: StepIn, request: Request, access: Access = Depends(current_access),
                          db: AsyncSession = Depends(aget_db)):
    run = await _run(db, access.user)
    steps = STEPS[run.kind]
    if run.completed_at:
        raise HTTPException(409, "Onboarding is already finished.")
    if not 1 <= n <= len(steps) or n > run.current_step:
        raise HTTPException(400, "Finish the earlier steps first.")
    title = steps[n - 1]
    data = dict(run.data or {})
    if title in ("Your agreement", "Designer terms", "House rules"):
        from app.services import AgreementService

        waiting = await AgreementService.waiting_for(db, access.user.id)
        if waiting:
            await AgreementService.sign(db, waiting, body.signature or "", access.user.name, client_ip(request), actor=access.user)
    elif title in ("Your profile", "Payout details"):
        await save_payout(db, access.user, body, required=True)
    elif title == "Your tools":
        tools = TOOLS.get(access.desk or "", TOOLS["talent"])
        if sorted(body.tools or []) != sorted(tools):
            raise HTTPException(400, "Confirm each tool works before you continue.")
        data["tools"] = tools
    run.data = data
    run.completed_steps = sorted(set([*(run.completed_steps or []), n]))
    if n == len(steps):
        run.completed_at = now()
        await audit(db, access.user, "onboarding.finished", "user", access.user.id, request=request, summary=run.kind)
    else:
        run.current_step = max(run.current_step, n + 1)
    await db.commit()
    return await my_onboarding(request, access, db)


@router.post("/me/photo")
async def upload_photo(request: Request, file: UploadFile = File(...), access: Access = Depends(current_access),
                       db: AsyncSession = Depends(aget_db)):
    body = await file.read()
    try:
        S3Service.check_file(body, file.content_type or "", ("image",))
    except S3Service.UploadRejected as exc:
        raise HTTPException(400, str(exc))
    # The file key is kept and a fresh link signed each time it's shown: a saved signed link stops working
    # once the credentials that signed it expire (hours, with the ECS task role)
    access.user.avatar_url = S3Service.put("avatars", file.filename or "photo", body, file.content_type)
    await db.commit()
    return {"avatar_url": S3Service.signed_url(access.user.avatar_url, 3600, str(request.base_url).rstrip("/"))}


# Me --------------------------------------------------------------------------------------------------

@router.get("/me/overview")
async def my_overview(access: Access = Depends(current_access), db: AsyncSession = Depends(aget_db)):
    u = access.user
    lines = (await db.execute(select(PayrollLine, PayrollRun).join(PayrollRun, PayrollRun.id == PayrollLine.run_id)
                              .where(PayrollLine.payee_user_id == u.id).order_by(PayrollRun.period.desc()).limit(12))).all()
    profile = (await db.execute(select(StaffProfile).where(StaffProfile.user_id == u.id))).scalar_one_or_none()
    tasks = (await db.execute(select(Task).where(Task.owner_user_id == u.id, Task.status != "done").order_by(Task.due_on))).scalars().all()
    return {
        "name": u.name, "email": u.email, "title": access.title, "role": u.role, "department": access.department,
        "performance": await PerformanceService.work_out(db, u, month_key(today())) if u.role in OS_ROLES else None,
        "vesting": await _vesting(db, u.id),
        "pay": [{"period": month_label(r.period), "kind": l.kind, "amount_minor": l.amount_minor, "basis": l.basis.get("text"),
                 "status": l.status if r.status in ("approved", "paid") or l.status == "carried" else "awaiting approval",
                 "paid_ref": l.paid_ref} for l, r in lines],
        "payout": {"method": profile.payout_method if profile else None,
                   "momo_hint": f"MoMo ··· {profile.momo_last3}" if profile and profile.momo_last3 else None,
                   "bank_hint": f"{profile.bank_name} ··· {profile.bank_last4}" if profile and profile.bank_last4 else None},
        "tasks": [_task(t) for t in tasks],
    }


@router.put("/me/payout")
async def update_payout(body: StepIn, request: Request, access: Access = Depends(current_access), db: AsyncSession = Depends(aget_db)):
    await save_payout(db, access.user, body, required=True)
    await audit(db, access.user, "payout_details.changed", "user", access.user.id, request=request,
                summary=f"Payout method set to {body.payout_method}")
    await db.commit()
    return {"ok": True}


@router.get("/step-ins")
async def step_ins(area: str, access: Access = Depends(current_access), db: AsyncSession = Depends(aget_db)):
    """Recent changes on a desk made by someone stepping in, for the banner on that desk."""
    from app.models.people import StepInAction

    if area not in AREAS or not access.can(area):
        raise HTTPException(404, "Not found.")
    since = now() - timedelta(days=14)
    rows = (await db.execute(select(StepInAction).where(StepInAction.area == area, StepInAction.created_at >= since)
                             .order_by(StepInAction.created_at.desc()).limit(12))).scalars().all()
    return {"area": area, "label": AREAS[area][0], "you_step_in": area in access.stepped_in,
            "actions": [{"id": str(r.id), "who": r.actor_label, "mine": r.actor_user_id == access.user.id, "action": r.action,
                         "at": r.created_at.isoformat()} for r in rows]}
