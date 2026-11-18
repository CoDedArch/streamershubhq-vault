"""Agreements: offering, signing, tracking and what comes next.

The flow for a streamer: the 6-month trial is offered with the seat and signed in onboarding. It starts the day it is
signed. Thirty days before it ends, the full recording contract (3 to 5 years, renewable) is drafted for the Founder,
who edits the terms and sends it; the streamer is then shown it on their next sign-in. A contract signed before the
trial ends starts the day after the trial does. Renewable contracts get a renewal drafted 90 days before they end.
"""

import re
import uuid
from datetime import date, timedelta
from typing import Optional

from dateutil.relativedelta import relativedelta
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import now
from app.models.agreements import Agreement, AgreementTemplate
from app.models.investors import Investment, Investor
from app.models.money import Decision
from app.models.people import Department, EquityGrant, StaffProfile, User
from app.models.talent import RosterSeat, Streamer
from app.services import EmailService
from app.services.agreement_texts import BUILT_IN
from app.services.AuditService import audit
from app.services.CompanyService import today
from app.services.money import ghs

COMPANY = "Streamers Hub Media"
AUDIENCES = {"staff": "Team", "streamer": "Streamers", "designer": "Designers", "clipper": "Clippers", "investor": "Investors"}
STAGES = {"trial": "Trial", "contract": "Contract", "standard": "Agreement"}
TERM_KINDS = {"equity": "Equity", "salary": "Salary", "revenue_share": "Revenue share", "commission": "Commission",
              "bonus": "Bonus", "benefit": "Benefit", "other": "Other"}
PLACEHOLDERS = [
    ("name", "Full name"), ("stage_name", "Streamer's stage name"), ("handle", "Designer or clipper handle"),
    ("title", "Job title"), ("department", "Department"), ("seat_number", "Roster seat, e.g. No. 01"),
    ("term", "Length, e.g. 6 months or 3 years"), ("start_date", "Start date (the day they sign)"),
    ("end_date", "End date"), ("terms", "The list of terms on this agreement"), ("signed_date", "The day it was signed"),
    ("company", COMPANY), ("founder", "The Founder's name"),
]
TRIAL_NOTICE_DAYS = 30
RENEWAL_NOTICE_DAYS = 90


async def ensure_templates(conn) -> None:
    """Create the built-in templates once. Existing ones (the Founder may have edited them) are left alone."""
    rows = [{"id": uuid.uuid4(), "is_default": True, "version": 1, "status": "active", "min_term_months": None,
             "max_term_months": None, **t} for t in BUILT_IN]
    await conn.execute(insert(AgreementTemplate).values(rows).on_conflict_do_nothing(index_elements=["key"]))


# Terms ------------------------------------------------------------------------------------------------------

def clean_terms(terms: Optional[list]) -> list[dict]:
    out = []
    for t in terms or []:
        label = str(t.get("label") or "").strip()[:80]
        value = str(t.get("value") or "").strip()[:120]
        if not label and not value:
            continue
        if not label or not value:
            raise HTTPException(400, "Each term needs a name and a value.")
        kind = t.get("kind") if t.get("kind") in TERM_KINDS else "other"
        out.append({"kind": kind, "label": label, "value": value, "detail": str(t.get("detail") or "").strip()[:200]})
    return out


def term_label(months: Optional[int]) -> str:
    if not months:
        return "No fixed end"
    if months % 12 == 0:
        years = months // 12
        return f"{years} year{'s' if years != 1 else ''}"
    return f"{months} month{'s' if months != 1 else ''}"


def percent_of(value: str) -> Optional[float]:
    m = re.search(r"\d+(?:\.\d+)?", value or "")
    return float(m.group()) if m else None


def _long(d: Optional[date]) -> str:
    return f"{d.day} {d.strftime('%B %Y')}" if d else ""


# Text -------------------------------------------------------------------------------------------------------

async def planned_dates(db: AsyncSession, a: Agreement) -> tuple[Optional[date], Optional[date]]:
    """Before signing, a contract that follows a running trial already knows its dates: it starts the day after."""
    if a.starts_on or not a.follows_id:
        return a.starts_on, a.ends_on
    prev = await db.get(Agreement, a.follows_id)
    if not prev or prev.status != "signed" or not prev.ends_on or prev.ends_on < today():
        return None, None
    start = prev.ends_on + timedelta(days=1)
    return start, (start + relativedelta(months=a.term_months) - timedelta(days=1) if a.term_months else None)


async def context_for(db: AsyncSession, a: Agreement) -> dict[str, str]:
    founder = await db.scalar(select(User).where(User.role == "founder"))
    starts, ends = await planned_dates(db, a)
    ctx = {"company": COMPANY, "founder": founder.name if founder else "the Founder", "term": term_label(a.term_months),
           "start_date": _long(starts) or "the day this is signed",
           "end_date": _long(ends) or ("the end of the term" if a.term_months else "no fixed end date"),
           "signed_date": _long(a.signed_at.date()) if a.signed_at else "the day of signing",
           "terms": "\n".join(f"- {t['label']}: {t['value']}{' (' + t['detail'] + ')' if t.get('detail') else ''}" for t in a.terms or [])
                    or "- As set out by the label in writing"}
    if a.user_id:
        user = await db.get(User, a.user_id)
        profile = await db.scalar(select(StaffProfile).where(StaffProfile.user_id == a.user_id))
        dept = await db.get(Department, user.department_id) if user.department_id else None
        streamer = await db.scalar(select(Streamer).where(Streamer.user_id == a.user_id))
        seat = await db.scalar(select(RosterSeat).where(RosterSeat.streamer_id == streamer.id)) if streamer else None
        ctx.update(name=user.name, handle=user.handle or user.name, title=(profile.title if profile else None) or "team member",
                   department=dept.name if dept else "", stage_name=streamer.stage_name if streamer else user.name,
                   seat_number=f"No. {seat.number:02d}" if seat else (f"No. {streamer.signing_number:02d}" if streamer else ""))
    elif a.investor_id:
        inv = await db.get(Investor, a.investor_id)
        ctx.update(name=inv.legal_name, handle=inv.internal_number, title="holder", department="", stage_name=inv.legal_name, seat_number="")
    return ctx


def fill(body: str, ctx: dict[str, str]) -> str:
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: ctx.get(m.group(1), m.group(0)), body)


async def rendered(db: AsyncSession, a: Agreement) -> str:
    return fill(a.body, await context_for(db, a))


# Where an agreement stands ----------------------------------------------------------------------------------

def standing(a: Agreement, on: Optional[date] = None) -> dict:
    """The phase, a label and tone for the tracker, and how far through its term it is."""
    on = on or today()
    out = {"phase": a.status, "day": None, "days": None, "progress": None, "days_left": None}
    labels = {"draft": ("Draft", ""), "offered": ("Waiting for signature", "warn"), "declined": ("Declined", "no"),
              "withdrawn": ("Withdrawn", ""), "ended": ("Ended early", "")}
    if a.status in labels:
        out["label"], out["tone"] = labels[a.status]
        return out
    if a.starts_on and a.starts_on > on:
        out.update(phase="upcoming", label=f"Starts {_long(a.starts_on)}", tone="info")
        return out
    if not a.ends_on:
        out.update(phase="active", label="Active · no fixed end", tone="ok")
        return out
    total = (a.ends_on - a.starts_on).days + 1
    day = min((on - a.starts_on).days + 1, total)
    left = (a.ends_on - on).days
    out.update(day=max(day, 1), days=total, progress=round(100 * max(day, 0) / total), days_left=max(left, 0))
    if left < 0:
        out.update(phase="expired", label=f"Ended {_long(a.ends_on)}", tone="no")
    elif left <= (TRIAL_NOTICE_DAYS if a.stage == "trial" else RENEWAL_NOTICE_DAYS):
        out.update(phase="ending", label=f"Ends in {left} day{'s' if left != 1 else ''}", tone="warn")
    else:
        out.update(phase="active", label="Active", tone="ok")
    return out


async def party(db: AsyncSession, a: Agreement) -> dict:
    if a.investor_id:
        inv = await db.get(Investor, a.investor_id)
        return {"name": inv.legal_name, "sub": inv.internal_number, "email": inv.email, "href": "/os/capital", "kind": "investor"}
    user = await db.get(User, a.user_id)
    profile = await db.scalar(select(StaffProfile).where(StaffProfile.user_id == a.user_id))
    href = f"/os/people/{user.id}" if user.role not in ("designer", "clipper") else None
    return {"name": user.name, "sub": (profile.title if profile else None) or user.role, "email": user.email, "href": href,
            "kind": "person", "user_id": str(user.id)}


async def row(db: AsyncSession, a: Agreement, on: Optional[date] = None) -> dict:
    nxt = await db.scalar(select(Agreement).where(Agreement.follows_id == a.id, Agreement.status.not_in(("withdrawn", "declined")))
                          .order_by(Agreement.created_at.desc()).limit(1))
    return {"id": str(a.id), "title": a.title, "audience": a.audience, "audience_label": AUDIENCES.get(a.audience, a.audience),
            "stage": a.stage, "stage_label": STAGES.get(a.stage, a.stage), "status": a.status, "terms": a.terms,
            "term_months": a.term_months, "term": term_label(a.term_months), "renewable": a.renewable,
            "starts_on": a.starts_on.isoformat() if a.starts_on else None, "ends_on": a.ends_on.isoformat() if a.ends_on else None,
            "offered_at": a.offered_at.isoformat() if a.offered_at else None, "signed_at": a.signed_at.isoformat() if a.signed_at else None,
            "signed_name": a.signed_name, "party": await party(db, a), **standing(a, on),
            "next": None if not nxt else {"id": str(nxt.id), "title": nxt.title, "status": nxt.status},
            "follows_id": str(a.follows_id) if a.follows_id else None,
            "can_follow_up": a.status == "signed" and nxt is None and (a.stage == "trial" or a.renewable)}


async def full(db: AsyncSession, a: Agreement) -> dict:
    out = await row(db, a)
    chain, prev = [], a
    while prev.follows_id:
        prev = await db.get(Agreement, prev.follows_id)
        chain.append(prev)
    later = list((await db.execute(select(Agreement).where(Agreement.follows_id == a.id))).scalars())
    template = await db.get(AgreementTemplate, a.template_id) if a.template_id else None
    out.update(body=a.body, text=await rendered(db, a), template={"id": str(template.id), "name": template.name,
               "version": a.template_version, "current_version": template.version} if template else None,
               min_term_months=template.min_term_months if template else None, max_term_months=template.max_term_months if template else None,
               history=[{"id": str(x.id), "title": x.title, "status": x.status, **standing(x)} for x in reversed(chain)]
               + [{"id": str(x.id), "title": x.title, "status": x.status, **standing(x)} for x in later],
               ended_at=a.ended_at.isoformat() if a.ended_at else None, end_reason=a.end_reason)
    return out


# Offering ---------------------------------------------------------------------------------------------------

async def default_template(db: AsyncSession, audience: str, stage: str = "standard") -> Optional[AgreementTemplate]:
    q = select(AgreementTemplate).where(AgreementTemplate.audience == audience, AgreementTemplate.stage == stage,
                                        AgreementTemplate.status == "active")
    return await db.scalar(q.order_by(AgreementTemplate.is_default.desc(), AgreementTemplate.updated_at.desc()).limit(1))


def check_term(template: Optional[AgreementTemplate], months: Optional[int]) -> None:
    if months is not None and not 1 <= months <= 120:
        raise HTTPException(400, "The length must be between 1 and 120 months.")
    if template and months and template.min_term_months and months < template.min_term_months:
        raise HTTPException(400, f"{template.name} runs for at least {term_label(template.min_term_months)}.")
    if template and months and template.max_term_months and months > template.max_term_months:
        raise HTTPException(400, f"{template.name} runs for at most {term_label(template.max_term_months)}.")


async def create(db: AsyncSession, template: AgreementTemplate, *, user: Optional[User] = None, investor: Optional[Investor] = None,
                 terms: Optional[list] = None, term_months: Optional[int] = None, title: Optional[str] = None,
                 by: Optional[User] = None, follows: Optional[Agreement] = None, send: bool = True, email: bool = True) -> Agreement:
    if (user is None) == (investor is None):
        raise HTTPException(400, "Choose one person or one investor.")
    if template.audience == "investor" and not investor or template.audience != "investor" and investor:
        raise HTTPException(400, "Investment agreements are for investors only.")
    months = term_months if term_months is not None else template.term_months
    check_term(template, months)
    a = Agreement(user_id=user.id if user else None, investor_id=investor.id if investor else None, template_id=template.id,
                  template_version=template.version, audience=template.audience, stage=template.stage, title=(title or template.name).strip(),
                  body=template.body, terms=clean_terms(template.default_terms if terms is None else terms), term_months=months,
                  renewable=template.renewable, status="draft", follows_id=follows.id if follows else None)
    db.add(a)
    await db.flush()
    await audit(db, by, "agreement.created", "agreement", a.id, actor_label=None if by else "System",
                summary=f"{a.title} for {user.name if user else investor.legal_name}")
    if send:
        await offer(db, a, by, email=email)
    return a


def _portal_path(user: User) -> str:
    return {"designer": "/studio", "clipper": "/desk"}.get(user.role, "/os/me")


async def offer(db: AsyncSession, a: Agreement, by: Optional[User], email: bool = True) -> None:
    if a.status not in ("draft",):
        raise HTTPException(409, "Only a draft can be sent.")
    a.status, a.offered_at, a.offered_by = "offered", now(), by.id if by else None
    await audit(db, by, "agreement.offered", "agreement", a.id, actor_label=None if by else "System", summary=a.title)
    if email and a.user_id:
        user = await db.get(User, a.user_id)
        if user.status == "active":  # invited people get it in onboarding, with their invite
            await EmailService.agreement_offered(user.email, user.name, a.title, _portal_path(user))


# Signing ----------------------------------------------------------------------------------------------------

async def sign(db: AsyncSession, a: Agreement, signature: str, expected_name: str, ip: Optional[str] = None,
               actor: Optional[User] = None, actor_label: Optional[str] = None) -> None:
    if a.status != "offered":
        raise HTTPException(409, "This agreement isn't waiting for a signature.")
    if (signature or "").strip().lower() != expected_name.strip().lower():
        raise HTTPException(400, {"message": f"Sign by typing your full name exactly: {expected_name}.", "missing": ["signature"]})
    d = today()
    start = d
    if a.follows_id:
        prev = await db.get(Agreement, a.follows_id)
        if prev and prev.status == "signed" and prev.ends_on and prev.ends_on >= d:
            start = prev.ends_on + timedelta(days=1)  # the contract picks up the day after the trial ends
    a.status, a.signed_name, a.signed_at, a.signed_ip = "signed", signature.strip(), now(), ip
    a.starts_on = start
    a.ends_on = start + relativedelta(months=a.term_months) - timedelta(days=1) if a.term_months else None
    a.body = await rendered(db, a)  # freeze the exact words that were signed
    await audit(db, actor, "agreement.signed", "agreement", a.id, actor_label=actor_label,
                summary=f"{a.title} signed as {a.signed_name}, from {_long(a.starts_on)}{' to ' + _long(a.ends_on) if a.ends_on else ''}")
    if a.user_id:
        await _after_person_signs(db, a)
    if a.investor_id:
        inv = await db.get(Investor, a.investor_id)
        inv.agreement_signed_name, inv.agreement_signed_at = a.signed_name, a.signed_at
        investment = await db.scalar(select(Investment).where(Investment.investor_id == inv.id).order_by(Investment.created_at.desc()).limit(1))
        if investment and not investment.signed_at:
            investment.signed_at = a.signed_at


async def _after_person_signs(db: AsyncSession, a: Agreement) -> None:
    streamer = await db.scalar(select(Streamer).where(Streamer.user_id == a.user_id))
    if streamer and a.audience == "streamer":
        streamer.contract_start, streamer.contract_end = a.starts_on, a.ends_on
    profile = await db.scalar(select(StaffProfile).where(StaffProfile.user_id == a.user_id))
    if profile and not profile.start_date:
        profile.start_date = a.starts_on
    for t in a.terms or []:
        pct = percent_of(t["value"]) if t["kind"] == "equity" else None
        if not pct:
            continue
        # One active grant per person. A renewal that repeats the same equity doesn't grant it twice; a different
        # amount is a decision for the Founder to make on the person's page, not something to guess here.
        if not await db.scalar(select(EquityGrant).where(EquityGrant.holder_user_id == a.user_id, EquityGrant.status == "active").limit(1)):
            months = re.search(r"(\d+)\s*months?", t.get("detail") or "")
            cliff = re.search(r"(\d+)-month cliff", t.get("detail") or "")
            db.add(EquityGrant(holder_user_id=a.user_id, percent=pct, vest_months=int(months.group(1)) if months else 48,
                               cliff_months=int(cliff.group(1)) if cliff else 12, start_date=a.starts_on, agreement_id=a.id))


async def for_user(db: AsyncSession, user_id: uuid.UUID, status: Optional[tuple] = None) -> list[Agreement]:
    q = select(Agreement).where(Agreement.user_id == user_id)
    if status:
        q = q.where(Agreement.status.in_(status))
    return list((await db.execute(q.order_by(Agreement.created_at.desc()))).scalars())


async def waiting_for(db: AsyncSession, user_id: uuid.UUID) -> Optional[Agreement]:
    return await db.scalar(select(Agreement).where(Agreement.user_id == user_id, Agreement.status == "offered")
                           .order_by(Agreement.offered_at).limit(1))


# What comes next --------------------------------------------------------------------------------------------

async def follow_up(db: AsyncSession, a: Agreement, by: Optional[User] = None) -> Agreement:
    """Draft what follows a signed agreement: the full contract after a trial, or a renewal of a renewable one."""
    if a.status != "signed":
        raise HTTPException(409, "Only a signed agreement can be followed up.")
    if await db.scalar(select(Agreement).where(Agreement.follows_id == a.id, Agreement.status.not_in(("withdrawn", "declined"))).limit(1)):
        raise HTTPException(409, "There's already an agreement following this one.")
    user = await db.get(User, a.user_id) if a.user_id else None
    investor = await db.get(Investor, a.investor_id) if a.investor_id else None
    if a.stage == "trial":
        template = await default_template(db, a.audience, "contract")
        if not template:
            raise HTTPException(409, "There's no contract template for this kind of agreement. Add one in Templates first.")
        return await create(db, template, user=user, investor=investor, by=by, follows=a, send=False)
    if not a.renewable:
        raise HTTPException(409, "This agreement isn't renewable.")
    template = await db.get(AgreementTemplate, a.template_id) if a.template_id else None
    renewal = Agreement(user_id=a.user_id, investor_id=a.investor_id, template_id=a.template_id,
                        template_version=template.version if template else a.template_version, audience=a.audience, stage=a.stage,
                        title=a.title if a.title.endswith("renewal") else f"{a.title} · renewal",
                        body=template.body if template else a.body, terms=list(a.terms or []), term_months=a.term_months,
                        renewable=True, status="draft", follows_id=a.id)
    db.add(renewal)
    await db.flush()
    await audit(db, by, "agreement.created", "agreement", renewal.id, actor_label=None if by else "System", summary=f"Renewal of {a.title}")
    return renewal


async def draft_follow_ups(db: AsyncSession, on: Optional[date] = None) -> int:
    """Daily: draft the contract for trials ending within 30 days, and renewals for contracts ending within 90,
    and put each in front of the Founder."""
    on = on or today()
    made = 0
    rows = (await db.execute(select(Agreement).where(Agreement.status == "signed", Agreement.ends_on.is_not(None)))).scalars().all()
    for a in rows:
        notice = TRIAL_NOTICE_DAYS if a.stage == "trial" else RENEWAL_NOTICE_DAYS
        if not (a.stage == "trial" or a.renewable) or (a.ends_on - on).days > notice:
            continue
        if await db.scalar(select(Agreement).where(Agreement.follows_id == a.id).limit(1)):
            continue  # already drafted, sent, signed, or withdrawn by the Founder on purpose
        try:
            nxt = await follow_up(db, a)
        except HTTPException:
            continue
        who = (await party(db, a))["name"]
        what = "trial ends" if a.stage == "trial" else "contract ends"
        db.add(Decision(title=f"{who}'s {what} {_long(a.ends_on)}: review and send {nxt.title}",
                        source="Agreements · drafted automatically", kind="agreement", entity_id=str(nxt.id)))
        made += 1
    return made


async def investor_terms(investment: Optional[Investment], tier: Optional[str]) -> list[dict]:
    if not investment:
        return []
    terms = [{"kind": "other", "label": "Amount invested", "value": ghs(investment.amount_minor), "detail": ""},
             {"kind": "equity", "label": "Stake", "value": f"{float(investment.percent):g}%", "detail": f"{investment.shares:,} shares"}]
    if tier:
        terms.append({"kind": "other", "label": "Tier", "value": tier, "detail": ""})
    return terms
