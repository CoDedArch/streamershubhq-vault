"""The Founder's command centre: company KPIs, pilot targets, revenue by month, a card per department,
"Needs your decision", the pilot round and the label network."""

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import Access, require_area
from app.core.database import aget_db
from app.core.security import now
from app.models.applications import Application
from app.models.money import Decision, PayrollRun
from app.models.partnerships import Brand, Deal
from app.models.talent import RosterSeat, Streamer
from app.services import CompanyService, InvestorService, PayrollService
from app.services.AuditService import audit
from app.services.MemberService import create_member
from app.services.money import ghs

router = APIRouter(tags=["command"])


async def _decisions(db: AsyncSession) -> list[dict]:
    rows = (await db.execute(select(Decision).where(Decision.status == "open").order_by(Decision.created_at))).scalars().all()
    recent = (await db.execute(select(Decision).where(Decision.status != "open").order_by(Decision.decided_at.desc()).limit(5))).scalars().all()
    out = lambda d: {"id": str(d.id), "title": d.title, "source": d.source, "kind": d.kind, "status": d.status,
                     "amount_minor": d.amount_minor, "note": d.note, "href": {"payroll": "/os/finance", "deal": "/os/partnerships",
                                                                               "seat_offer": "/os/review", "release": "/os/creative"}.get(d.kind)
                                                                               or (f"/os/agreements/{d.entity_id}" if d.kind == "agreement" else None)}
    return [out(d) for d in rows] + [out(d) for d in recent]


@router.get("/os/command")
async def command(access: Access = Depends(require_area("command")), db: AsyncSession = Depends(aget_db)):
    return {
        "kpis": await CompanyService.company_kpis(db),
        "targets": await CompanyService.pilot_targets(db),
        "revenue": await CompanyService.revenue_by_month(db),
        "departments": await CompanyService.department_cards(db),
        "decisions": await _decisions(db),
        "round": await InvestorService.round_progress(db),
        "network": await CompanyService.network(db),
        "live": await CompanyService.live_now(db),
        "pilot_month": _pilot_month(),
        "can_decide": access.is_founder,
    }


def _pilot_month() -> int:
    from app.services.CompanyService import today

    t = today()
    return max(1, min(6, (t.year - 2026) * 12 + t.month - 10 + 1))


@router.get("/kpis/company")
async def company_kpis(access: Access = Depends(require_area("command", "people", "me")), db: AsyncSession = Depends(aget_db)):
    return {"kpis": await CompanyService.company_kpis(db), "targets": await CompanyService.pilot_targets(db)}


class DecideIn(BaseModel):
    approve: bool
    note: Optional[str] = Field(default=None, max_length=1000)
    seat_number: Optional[int] = None
    stage_name: Optional[str] = Field(default=None, max_length=80)


@router.post("/decisions/{decision_id}")
async def decide(decision_id: uuid.UUID, body: DecideIn, request: Request, access: Access = Depends(require_area("command")),
                 db: AsyncSession = Depends(aget_db)):
    if not access.is_founder:
        raise HTTPException(403, "Only the Founder decides these.")
    d = await db.get(Decision, decision_id, with_for_update=True)
    if not d or d.status != "open":
        raise HTTPException(409, "This has already been decided.")
    if not body.approve and not (body.note or "").strip():
        raise HTTPException(400, "A decline needs a reason.")
    actor = access.user
    if d.kind == "payroll":
        run = await db.get(PayrollRun, uuid.UUID(d.entity_id), with_for_update=True)
        if body.approve:
            await PayrollService.approve(db, run, actor)
        else:
            await PayrollService.send_back(db, run, actor, body.note)
        await db.commit()
        return {"id": str(d.id), "status": "approved" if body.approve else "declined"}
    if d.kind == "deal" and d.entity_id:
        deal = await db.get(Deal, uuid.UUID(d.entity_id), with_for_update=True)
        brand = await db.get(Brand, deal.brand_id)
        if body.approve:
            deal.status, deal.signed_by, deal.signed_at = "signed", actor.id, now()
            brand.status = "signed"
        else:
            deal.status = "draft"
            brand.status = "in_talks"
    if d.kind == "agreement" and d.entity_id:
        from app.models.agreements import Agreement
        from app.services import AgreementService

        agreement = await db.get(Agreement, uuid.UUID(d.entity_id))
        if agreement and agreement.status == "draft":
            if body.approve:
                await AgreementService.offer(db, agreement, actor)
            else:
                agreement.status = "withdrawn"
    if d.kind == "seat_offer" and d.entity_id and body.approve:
        a = await db.get(Application, uuid.UUID(d.entity_id))
        seat = (await db.execute(select(RosterSeat).where(RosterSeat.streamer_id.is_(None))
                                 .order_by(RosterSeat.number))).scalars().first() if not body.seat_number else \
            (await db.execute(select(RosterSeat).where(RosterSeat.number == body.seat_number))).scalar_one_or_none()
        if not seat or seat.streamer_id:
            raise HTTPException(409, "There's no open seat to offer.")
        from app.api.v1.endpoints.auth import issue_invite

        user = await create_member(db, name=a.payload.get("full_name") or a.name, email=a.email, role="streamer",
                                   department="talent", title=f"Streamer · Signing No. {seat.number:02d}", desk="talent",
                                   onboarding="staff")
        streamer = Streamer(user_id=user.id, stage_name=body.stage_name or a.payload.get("stage_name") or a.name,
                            category="gaming" if "gam" in (a.payload.get("content") or "").lower() else "variety",
                            content=a.payload.get("content"), signing_number=seat.number, status="signed", is_public=False)
        db.add(streamer)
        await db.flush()
        seat.streamer_id = streamer.id
        from app.services import AgreementService

        trial = await AgreementService.default_template(db, "streamer", "trial")
        if trial:  # signed in onboarding; the six months count from that day
            await AgreementService.create(db, trial, user=user, by=actor, email=False)
        await issue_invite(db, user, "streamer")
    d.status, d.decided_by, d.decided_at, d.note = ("approved" if body.approve else "declined"), actor.id, now(), body.note
    await audit(db, actor, f"decision.{d.status}", "decision", d.id, request=request, summary=f"{d.title}: {d.status}")
    await db.commit()
    return {"id": str(d.id), "status": d.status}


class RequestIn(BaseModel):
    title: str = Field(min_length=3, max_length=160)
    amount: Optional[float] = Field(default=None, ge=0, le=10_000_000)
    note: Optional[str] = Field(default=None, max_length=1000)


@router.post("/decisions")
async def raise_request(body: RequestIn, request: Request, access: Access = Depends(require_area("me")),
                        db: AsyncSession = Depends(aget_db)):
    """Any lead can put a budget request or other ask in front of the Founder."""
    amount = round(body.amount * 100) if body.amount else None
    title = body.title.strip() + (f" · {ghs(amount)}" if amount else "")
    area = {"production": "Production", "partnerships": "Partnerships", "creative": "Creative", "tech": "Tech",
            "social": "Social", "finance": "Finance", "store": "Store", "talent": "Talent", "capital": "Investors"}.get(access.desk or "", "Team")
    d = Decision(title=title, source=f"{area} · {access.title or access.user.name}", kind="budget" if amount else "other",
                 amount_minor=amount, requested_by=access.user.id, note=body.note)
    db.add(d)
    await audit(db, access.user, "decision.raised", "decision", d.id, request=request, summary=title)
    await db.commit()
    return {"id": str(d.id)}
