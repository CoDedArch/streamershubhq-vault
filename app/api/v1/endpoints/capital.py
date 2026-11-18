"""Investor relations in Hub OS (Founder, Finance, Strategy & Investors): the round, the register, investor
onboarding, payment confirmation, Holder ID reissue, valuations, reports, exit requests and messages.
Staff never see a Holder ID: only its last four characters."""

import uuid
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from app.core.types import Email
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import Access, require_area
from app.core.crypto import digits_tail, encrypt, lookup_hash
from app.core.database import aget_db
from app.core.security import hash_token, new_token, now
from app.models.investors import (ExitRequest, FundingRound, Investment, Investor, InvestorDocument, InvestorMessage,
                                  InvestorReport, Milestone, Valuation)
from app.models.people import AuthSession
from app.services import EmailService, InvestorService, S3Service
from app.services.AuditService import audit
from app.services.money import ghs

router = APIRouter(prefix="/capital", tags=["capital"])
capital_desk = require_area("capital")


def _can_finance(access: Access):
    if not access.does_finance:
        raise HTTPException(403, "Only Finance or the Founder can do this.")


@router.get("/overview")
async def overview(access: Access = Depends(capital_desk), db: AsyncSession = Depends(aget_db)):
    investors = (await db.execute(select(Investor).order_by(Investor.internal_number))).scalars().all()
    register = []
    for inv in investors:
        h = await InvestorService.holding(db, inv)
        latest = (await db.execute(select(Investment).where(Investment.investor_id == inv.id).order_by(Investment.created_at.desc()))).scalars().first()
        register.append({"id": str(inv.id), "number": inv.internal_number, "name": inv.legal_name, "status": inv.status,
                         "holder_id": f"••••-••••-{inv.holder_id_last4}" if inv.holder_id_last4 else "Not issued",
                         "invested_minor": h["invested_minor"], "percent": h["percent"], "shares": h["shares"], "value_minor": h["value_minor"],
                         "kyc": inv.kyc_status, "step": inv.onboarding_step,
                         "investment_id": str(latest.id) if latest else None,
                         "payment_confirmed": bool(latest and latest.payment_confirmed_at), "payment_ref": latest.payment_ref if latest else None,
                         "last_sign_in_at": inv.last_sign_in_at.isoformat() if inv.last_sign_in_at else None,
                         "locked": bool(inv.locked_until and inv.locked_until > now())})
    vals = await InvestorService.valuations(db)
    reports = (await db.execute(select(InvestorReport).order_by(InvestorReport.period.desc()))).scalars().all()
    exits = (await db.execute(select(ExitRequest, Investor).join(Investor, Investor.id == ExitRequest.investor_id)
                              .order_by(ExitRequest.created_at.desc()))).all()
    threads = (await db.execute(select(InvestorMessage.investor_id, func.max(InvestorMessage.created_at),
                                       func.count().filter(InvestorMessage.from_investor.is_(True), InvestorMessage.read_at.is_(None)))
                                .group_by(InvestorMessage.investor_id))).all()
    numbers = {i.id: i for i in investors}
    miles = (await db.execute(select(Milestone).order_by(Milestone.position))).scalars().all()
    return {
        "round": await InvestorService.round_progress(db), "register": register,
        "valuations": [{"id": str(v.id), "amount_minor": v.amount_minor, "date": v.effective_date.isoformat(), "basis": v.basis_note,
                        "label": v.label} for v in reversed(vals)],
        "reports": [{"id": str(r.id), "title": r.title, "period": r.period, "published": r.published_at is not None,
                     "published_at": r.published_at.isoformat() if r.published_at else None, "due_on": r.due_on.isoformat() if r.due_on else None}
                    for r in reports],
        "exit_requests": [{"id": str(e.id), "number": i.internal_number, "status": e.status, "estimate_minor": e.estimate_minor,
                           "note": e.note, "at": e.created_at.isoformat(), "decision_note": e.decision_note} for e, i in exits],
        "threads": [{"investor_id": str(t[0]), "number": numbers[t[0]].internal_number if t[0] in numbers else "?",
                     "last_at": t[1].isoformat(), "unread": t[2]} for t in sorted(threads, key=lambda t: t[1], reverse=True)],
        "milestones": [{"id": str(m.id), "title": m.title, "status": m.status, "detail": m.detail} for m in miles],
        "can_value": access.is_founder, "can_confirm": access.does_finance,
    }


class InvestorIn(BaseModel):
    legal_name: str = Field(min_length=3, max_length=160)
    email: Email
    phone: Optional[str] = Field(default=None, max_length=20)
    amount: float = Field(gt=0, le=100_000_000)
    shares: int = Field(gt=0)
    percent: float = Field(gt=0, le=100)
    tier_label: Optional[str] = Field(default=None, max_length=60)
    template_id: Optional[str] = None  # the Founder's investment agreement template; the default if not given
    extra_terms: list[dict] = []  # anything else agreed with this holder


async def _invite(db: AsyncSession, inv: Investor) -> None:
    token = new_token()
    inv.invite_token_hash, inv.invite_expires_at = hash_token(token), now() + timedelta(days=7)
    await db.flush()
    await EmailService.send_invite(inv.email, inv.legal_name, "investor", f"/vault/welcome?token={token}")


@router.post("/investors")
async def add_investor(body: InvestorIn, request: Request, access: Access = Depends(capital_desk), db: AsyncSession = Depends(aget_db)):
    """Record a committed investor and their terms from the signed agreement, then send the invite."""
    email = body.email.lower()
    if await db.scalar(select(func.count()).select_from(Investor).where(Investor.email == email)):
        raise HTTPException(409, "This investor is already on the register.")
    n = (await db.scalar(select(func.count()).select_from(Investor)) or 0) + 1
    inv = Investor(internal_number=f"SH-INV-{n:03d}", legal_name=body.legal_name.strip(), email=email, phone_enc=encrypt(body.phone),
                   phone_last4=digits_tail(body.phone, 4), phone_hash=lookup_hash(body.phone) if body.phone else None,
                   tier_label=body.tier_label, status="invited", onboarding_step=1)
    db.add(inv)
    await db.flush()
    rnd = (await db.execute(select(FundingRound).where(FundingRound.status == "open").order_by(FundingRound.created_at.desc()))).scalars().first()
    db.add(Investment(investor_id=inv.id, round_id=rnd.id if rnd else None, amount_minor=round(body.amount * 100), shares=body.shares,
                      percent=body.percent))
    db.add_all([InvestorDocument(investor_id=inv.id, kind="agreement", title="Investment agreement", detail="To sign · PDF"),
                InvestorDocument(investor_id=inv.id, kind="certificate", title="Share certificate", detail=f"{body.shares:,} shares · PDF"),
                InvestorDocument(investor_id=inv.id, kind="receipt", title="Payment receipt", detail=f"{ghs(round(body.amount * 100))} · PDF")])
    from app.models.agreements import AgreementTemplate
    from app.services import AgreementService

    template = None
    if body.template_id:
        try:
            template = await db.get(AgreementTemplate, uuid.UUID(body.template_id))
        except ValueError:
            template = None
        if not template or template.audience != "investor" or template.status != "active":
            raise HTTPException(400, "Choose an investment agreement template.")
    template = template or await AgreementService.default_template(db, "investor")
    if template:
        await db.flush()
        investment = await db.scalar(select(Investment).where(Investment.investor_id == inv.id))
        terms = await AgreementService.investor_terms(investment, body.tier_label) + list(body.extra_terms or [])
        await AgreementService.create(db, template, investor=inv, by=access.user, terms=terms, email=False)
    await audit(db, access.user, "investor.created", "investor", inv.id, request=request, summary=f"{inv.internal_number}",
                after={"amount_minor": round(body.amount * 100), "percent": body.percent, "shares": body.shares})
    await _invite(db, inv)
    await db.commit()
    return {"id": str(inv.id), "number": inv.internal_number}


class ConfirmIn(BaseModel):
    reference: str = Field(min_length=3, max_length=80)


@router.post("/investments/{investment_id}/confirm-payment")
async def confirm_payment(investment_id: uuid.UUID, body: ConfirmIn, request: Request, access: Access = Depends(capital_desk),
                          db: AsyncSession = Depends(aget_db)):
    _can_finance(access)
    inv = await db.get(Investment, investment_id, with_for_update=True)
    if not inv or inv.payment_confirmed_at:
        raise HTTPException(409, "This payment is already confirmed.")
    inv.payment_ref, inv.payment_confirmed_at, inv.payment_confirmed_by = body.reference.strip(), now(), access.user.id
    if inv.round_id:
        rnd = await db.get(FundingRound, inv.round_id, with_for_update=True)
        rnd.raised_minor += inv.amount_minor
    investor = await db.get(Investor, inv.investor_id)
    await audit(db, access.user, "investment.payment_confirmed", "investment", inv.id, request=request,
                summary=f"{investor.internal_number}: {ghs(inv.amount_minor)} ref {inv.payment_ref}")
    await db.commit()
    await EmailService.notify([investor.email], "Your payment is confirmed", "Payment received",
                              ["Finance matched your transfer to your agreement. Open your welcome link to get your Holder ID."])
    return {"ok": True}


class ReissueIn(BaseModel):
    checked_by: str = Field(min_length=3, max_length=200, description="How identity was checked out of band, e.g. a call")


@router.post("/investors/{investor_id}/reissue-holder-id")
async def reissue(investor_id: uuid.UUID, body: ReissueIn, request: Request, access: Access = Depends(capital_desk),
                  db: AsyncSession = Depends(aget_db)):
    """A lost Holder ID: revoke it, end every vault session, and send a link that shows a new one once."""
    if access.user.role not in ("founder",) and access.department != "strategy" and "capital" not in access.stepped_in:
        raise HTTPException(403, "Only the Founder or Strategy & Investors can reissue a Holder ID.")
    inv = await db.get(Investor, investor_id, with_for_update=True)
    if not inv:
        raise HTTPException(404, "Investor not found.")
    old = inv.holder_id_last4
    inv.holder_id_hash = inv.holder_id_last4 = inv.id_revealed_at = None
    inv.id_rotated_at, inv.onboarding_step, inv.status, inv.locked_until, inv.failed_attempts = now(), 5, "onboarding", None, 0
    for s in (await db.execute(select(AuthSession).where(AuthSession.investor_id == inv.id, AuthSession.revoked_at.is_(None)))).scalars():
        s.revoked_at = now()
    await audit(db, access.user, "investor.holder_id_revoked", "investor", inv.id, request=request,
                summary=f"{inv.internal_number}: ID ending {old or '—'} revoked; identity checked: {body.checked_by}")
    await _invite(db, inv)
    await db.commit()
    return {"ok": True}


class ValuationIn(BaseModel):
    amount: float = Field(gt=0, le=10_000_000_000)
    effective_date: date
    basis_note: str = Field(min_length=10, max_length=4000)
    label: Optional[str] = Field(default=None, max_length=40)


@router.post("/valuations")
async def add_valuation(body: ValuationIn, request: Request, access: Access = Depends(capital_desk), db: AsyncSession = Depends(aget_db)):
    if not access.is_founder:
        raise HTTPException(403, "Valuations are set by the Founder.")
    v = Valuation(amount_minor=round(body.amount * 100), effective_date=body.effective_date, basis_note=body.basis_note.strip(),
                  label=body.label, set_by=access.user.id)
    db.add(v)
    await audit(db, access.user, "valuation.set", "valuation", v.id, request=request, summary=f"{ghs(v.amount_minor)} from {body.effective_date}",
                after={"basis": v.basis_note})
    await db.commit()
    return {"id": str(v.id)}


class ReportIn(BaseModel):
    title: str = Field(min_length=3, max_length=160)
    period: str = Field(pattern=r"^\d{4}-\d{2}$")
    body: str = Field(min_length=20, max_length=50000)
    read_minutes: int = Field(default=5, ge=1, le=60)
    publish: bool = False


@router.post("/reports")
async def add_report(body: ReportIn, request: Request, access: Access = Depends(capital_desk), db: AsyncSession = Depends(aget_db)):
    if body.publish:
        _can_finance(access)
    r = InvestorReport(title=body.title, period=body.period, body=body.body, read_minutes=body.read_minutes,
                       published_at=now() if body.publish else None, published_by=access.user.id if body.publish else None)
    db.add(r)
    await audit(db, access.user, "report.saved", "investor_report", r.id, request=request, summary=f"{body.title} ({'published' if body.publish else 'draft'})")
    await db.commit()
    if body.publish:
        await _announce(db, r)
    return {"id": str(r.id)}


@router.post("/reports/{report_id}/publish")
async def publish_report(report_id: uuid.UUID, request: Request, access: Access = Depends(capital_desk), db: AsyncSession = Depends(aget_db)):
    _can_finance(access)
    r = await db.get(InvestorReport, report_id)
    if not r or r.published_at:
        raise HTTPException(409, "This report is already published.")
    r.published_at, r.published_by = now(), access.user.id
    await audit(db, access.user, "report.published", "investor_report", r.id, request=request, summary=r.title)
    await db.commit()
    await _announce(db, r)
    return {"ok": True}


async def _announce(db: AsyncSession, r: InvestorReport) -> None:
    emails = list((await db.execute(select(Investor.email).where(Investor.status == "active"))).scalars())
    for email in emails:  # one email per holder, so nobody sees the others' addresses
        await EmailService.notify([email], f"Streamers Hub: {r.title}", r.title,
                                  ["A new report is in your vault. Sign in with your Holder ID to read it."], ("Open my vault", "/vault"))


class ExitDecisionIn(BaseModel):
    status: str = Field(pattern="^(accepted|declined)$")
    note: str = Field(min_length=5, max_length=2000)


@router.post("/exit-requests/{request_id}/decision")
async def decide_exit(request_id: uuid.UUID, body: ExitDecisionIn, request: Request, access: Access = Depends(capital_desk),
                      db: AsyncSession = Depends(aget_db)):
    """Record the founders' answer. This moves no money and changes no shares; any buy-back follows the agreement."""
    if not access.is_founder:
        raise HTTPException(403, "Exit requests are answered by the Founder.")
    e = await db.get(ExitRequest, request_id, with_for_update=True)
    if not e or e.status != "open":
        raise HTTPException(409, "This request has been answered.")
    e.status, e.decision_note, e.decided_by, e.decided_at = body.status, body.note.strip(), access.user.id, now()
    inv = await db.get(Investor, e.investor_id)
    db.add(InvestorMessage(investor_id=inv.id, from_investor=False, author_user_id=access.user.id,
                           body=f"About your exit request: {body.note.strip()}"))
    await audit(db, access.user, f"exit_request.{body.status}", "exit_request", e.id, request=request, summary=f"{inv.internal_number}: {body.note.strip()}")
    await db.commit()
    return {"ok": True}


@router.get("/messages/{investor_id}")
async def thread(investor_id: uuid.UUID, access: Access = Depends(capital_desk), db: AsyncSession = Depends(aget_db)):
    rows = (await db.execute(select(InvestorMessage).where(InvestorMessage.investor_id == investor_id).order_by(InvestorMessage.created_at))).scalars().all()
    for r in rows:
        if r.from_investor and not r.read_at:
            r.read_at = now()
    await db.commit()
    return [{"id": str(r.id), "from_investor": r.from_investor, "body": r.body, "at": r.created_at.isoformat()} for r in rows]


class ReplyIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


@router.post("/messages/{investor_id}")
async def reply(investor_id: uuid.UUID, body: ReplyIn, access: Access = Depends(capital_desk), db: AsyncSession = Depends(aget_db)):
    inv = await db.get(Investor, investor_id)
    if not inv:
        raise HTTPException(404, "Investor not found.")
    db.add(InvestorMessage(investor_id=inv.id, from_investor=False, author_user_id=access.user.id, body=body.body.strip()))
    await db.commit()
    await EmailService.notify([inv.email], "A reply from the founders", "You have a new message",
                              ["The founders replied in your vault."], ("Open my vault", "/vault"))
    return {"ok": True}


@router.post("/investors/{investor_id}/documents/{kind}")
async def upload_document(investor_id: uuid.UUID, kind: str, request: Request, file: UploadFile = File(...),
                          detail: Optional[str] = Form(None), access: Access = Depends(capital_desk), db: AsyncSession = Depends(aget_db)):
    _can_finance(access)
    doc = (await db.execute(select(InvestorDocument).where(InvestorDocument.investor_id == investor_id,
                                                           InvestorDocument.kind == kind))).scalars().first()
    if not doc:
        raise HTTPException(404, "Document not found.")
    data = await file.read()
    try:
        S3Service.check_file(data, file.content_type or "", ("document",))
    except S3Service.UploadRejected as exc:
        raise HTTPException(400, str(exc))
    doc.file = S3Service.put(f"investors/{investor_id}", file.filename or kind, data, file.content_type)
    if detail:
        doc.detail = detail
    await audit(db, access.user, "investor_document.uploaded", "investor_document", doc.id, request=request, summary=doc.title)
    await db.commit()
    return {"ok": True}


class RoundIn(BaseModel):
    target: Optional[float] = Field(default=None, gt=0)
    founders_committed: Optional[float] = Field(default=None, ge=0)
    status: Optional[str] = Field(default=None, pattern="^(open|closed)$")


@router.patch("/round/{round_id}")
async def update_round(round_id: uuid.UUID, body: RoundIn, request: Request, access: Access = Depends(capital_desk),
                       db: AsyncSession = Depends(aget_db)):
    if not access.is_founder:
        raise HTTPException(403, "Only the Founder changes the round.")
    rnd = await db.get(FundingRound, round_id)
    if not rnd:
        raise HTTPException(404, "Round not found.")
    if body.target is not None:
        rnd.target_minor = round(body.target * 100)
    if body.founders_committed is not None:
        rnd.founders_committed_minor = round(body.founders_committed * 100)
    if body.status:
        rnd.status = body.status
    await audit(db, access.user, "round.updated", "funding_round", rnd.id, request=request, after=body.model_dump(exclude_none=True))
    await db.commit()
    return {"ok": True}


class MilestoneIn(BaseModel):
    status: str = Field(pattern="^(done|doing|next)$")
    detail: Optional[str] = Field(default=None, max_length=60)


@router.patch("/milestones/{milestone_id}")
async def update_milestone(milestone_id: uuid.UUID, body: MilestoneIn, access: Access = Depends(capital_desk), db: AsyncSession = Depends(aget_db)):
    m = await db.get(Milestone, milestone_id)
    if not m:
        raise HTTPException(404, "Milestone not found.")
    m.status = body.status
    if body.detail is not None:
        m.detail = body.detail
    await db.commit()
    return {"ok": True}
