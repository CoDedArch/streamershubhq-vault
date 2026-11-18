"""The Investor Vault: sign in with a Holder ID plus a one-time code, onboarding (the Holder ID is shown
once), and the holder's own stake, company summary, reports, documents, exit requests and messages.
An investor only ever sees their own records."""

import uuid
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import digits_tail
from app.core.database import aget_db
from app.core.security import VAULT_COOKIE, clear_cookie, client_ip, hash_token, now, read_session, start_session
from app.models.investors import (ExitRequest, Investment, Investor, InvestorDocument, InvestorMessage, InvestorReportRead,
                                  InvestorReport, Milestone)
from app.models.people import AuthSession, Department, User
from app.services import AuthService, EmailService, InvestorService, S3Service
from app.services.AuditService import audit
from app.services.CompanyService import company_summary

router = APIRouter(tags=["vault"])
STEP_UP_MINUTES = 10


class VaultLoginIn(BaseModel):
    holder_id: str = Field(min_length=12, max_length=20)
    contact: str = Field(min_length=5, max_length=255)


class CodeIn(BaseModel):
    challenge_id: str
    code: str = Field(min_length=6, max_length=6)


class ResendIn(BaseModel):
    challenge_id: str


def _label(investor: Investor) -> str:
    return f"Holder {investor.internal_number}"


# Sign in ---------------------------------------------------------------------------------------------

@router.post("/auth/investor/login")
async def vault_login(body: VaultLoginIn, request: Request, db: AsyncSession = Depends(aget_db)):
    contact = body.contact.strip().lower()
    await AuthService.check_rate(db, request, "vault", contact)
    investor = await AuthService.find_investor_by_contact(db, contact)
    if investor and investor.locked_until and investor.locked_until > now():
        raise HTTPException(423, "This vault is locked for 15 minutes after too many tries. "
                                 "Strategy & Investors has been told.")
    ok = AuthService.holder_id_matches(body.holder_id, investor.holder_id_hash if investor else None)
    if not ok or not investor or investor.status not in ("active",):
        await AuthService.record_attempt(db, request, "vault", contact, False)
        if investor:
            investor.failed_attempts += 1
            await audit(db, None, "vault.sign_in_failed", "investor", investor.id, request=request,
                        actor_label=_label(investor), summary="Wrong Holder ID or contact")
            if investor.failed_attempts >= AuthService.VAULT_LOCK_AFTER:
                await AuthService.lock_vault(db, investor, request)
                raise HTTPException(423, "This vault is locked for 15 minutes after too many tries. "
                                         "Strategy & Investors has been told.")
        await db.commit()
        raise HTTPException(401, "That Holder ID and contact don't match our records.")
    challenge = await AuthService.issue_code(db, request, purpose="vault", email=investor.email,
                                             name=investor.legal_name, investor_id=investor.id)
    return {"challenge_id": str(challenge.id), "sent_to": AuthService.mask_email(investor.email)}


@router.post("/auth/investor/resend")
async def vault_resend(body: ResendIn, request: Request, db: AsyncSession = Depends(aget_db)):
    old = await AuthService.use_code_for_resend(db, body.challenge_id, "vault")
    investor = await db.get(Investor, old.investor_id)
    challenge = await AuthService.issue_code(db, request, purpose="vault", email=investor.email,
                                             name=investor.legal_name, investor_id=investor.id)
    return {"challenge_id": str(challenge.id), "sent_to": AuthService.mask_email(investor.email)}


@router.post("/auth/investor/verify")
async def vault_verify(body: CodeIn, request: Request, response: Response, db: AsyncSession = Depends(aget_db)):
    challenge = await AuthService.use_code(db, body.challenge_id, body.code, "vault")
    investor = await db.get(Investor, challenge.investor_id)
    if not investor or investor.status != "active":
        raise HTTPException(403, "This vault is not open.")
    investor.failed_attempts = 0
    investor.last_sign_in_at = now()
    await AuthService.record_attempt(db, request, "vault", investor.email, True)
    await start_session(db, response, request, kind="vault", investor_id=investor.id)
    await audit(db, None, "vault.sign_in", "investor", investor.id, request=request, actor_label=_label(investor),
                summary="Opened the vault")
    await db.commit()
    return {"ok": True}


async def vault_session(request: Request, db: AsyncSession = Depends(aget_db)) -> AuthSession:
    session = await read_session(request, db, VAULT_COOKIE)
    if not session or session.kind != "vault" or not session.investor_id:
        raise HTTPException(401, "Your vault is locked. Sign in again.")
    return session


async def current_investor(session: AuthSession = Depends(vault_session), db: AsyncSession = Depends(aget_db)) -> Investor:
    investor = await db.get(Investor, session.investor_id)
    if not investor or investor.status != "active":
        raise HTTPException(401, "Your vault is locked. Sign in again.")
    return investor


def _require_fresh(session: AuthSession) -> None:
    if not session.step_up_at or session.step_up_at < now() - timedelta(minutes=STEP_UP_MINUTES):
        raise HTTPException(428, "Confirm it's you with a fresh one-time code first.")


@router.post("/auth/investor/lock")
async def vault_lock(request: Request, response: Response, db: AsyncSession = Depends(aget_db)):
    session = await read_session(request, db, VAULT_COOKIE)
    if session:
        session.revoked_at = now()
        investor = await db.get(Investor, session.investor_id) if session.investor_id else None
        if investor:
            await audit(db, None, "vault.lock", "investor", investor.id, request=request, actor_label=_label(investor),
                        summary="Locked the vault")
        await db.commit()
    clear_cookie(response, VAULT_COOKIE)
    return {"ok": True}


@router.post("/investor/step-up")
async def step_up(request: Request, session: AuthSession = Depends(vault_session),
                  investor: Investor = Depends(current_investor), db: AsyncSession = Depends(aget_db)):
    challenge = await AuthService.issue_code(db, request, purpose="step_up", email=investor.email,
                                             name=investor.legal_name, investor_id=investor.id, session_id=session.id)
    return {"challenge_id": str(challenge.id), "sent_to": AuthService.mask_email(investor.email)}


@router.post("/investor/step-up/verify")
async def step_up_verify(body: CodeIn, session: AuthSession = Depends(vault_session), db: AsyncSession = Depends(aget_db)):
    challenge = await AuthService.use_code(db, body.challenge_id, body.code, "step_up")
    if challenge.session_id != session.id:
        raise HTTPException(400, "That code is for another session.")
    session.step_up_at = now()
    await db.commit()
    return {"ok": True, "fresh_for_minutes": STEP_UP_MINUTES}


# Onboarding (invite link) ------------------------------------------------------------------------------

async def _invitee(db: AsyncSession, token: str) -> Investor:
    investor = (await db.execute(select(Investor).where(Investor.invite_token_hash == hash_token(token)))).scalar_one_or_none()
    if not investor or not investor.invite_expires_at or investor.invite_expires_at <= now():
        raise HTTPException(400, "This link has expired. Ask Strategy & Investors for a new one.")
    return investor


async def _welcome_state(db: AsyncSession, investor: Investor) -> dict:
    inv = (await db.execute(select(Investment).where(Investment.investor_id == investor.id)
                            .order_by(Investment.created_at.desc()))).scalars().first()
    docs = list((await db.execute(select(InvestorDocument).where(InvestorDocument.investor_id == investor.id))).scalars())
    return {
        "step": investor.onboarding_step, "reissue": investor.id_rotated_at is not None and investor.agreement_signed_at is not None,
        "legal_name": investor.legal_name, "email": AuthService.mask_email(investor.email),
        "phone": f"••• {investor.phone_last4}" if investor.phone_last4 else None,
        "kyc_status": investor.kyc_status, "identity_confirmed": investor.identity_confirmed_at is not None,
        "agreement_signed": investor.agreement_signed_at is not None,
        "investment": None if not inv else {"amount_minor": inv.amount_minor, "percent": float(inv.percent),
                                            "shares": inv.shares, "payment_confirmed": inv.payment_confirmed_at is not None,
                                            "payment_ref": inv.payment_ref},
        "documents": [{"kind": d.kind, "title": d.title, "detail": d.detail} for d in docs],
        "holder_id_shown": investor.id_revealed_at is not None,
        "agreement": await _agreement(db, investor),
    }


async def _agreement(db: AsyncSession, investor: Investor) -> Optional[dict]:
    from app.models.agreements import Agreement
    from app.services import AgreementService

    a = await db.scalar(select(Agreement).where(Agreement.investor_id == investor.id, Agreement.status.in_(("offered", "signed")))
                        .order_by(Agreement.created_at.desc()).limit(1))
    if not a:
        return None
    return {"title": a.title, "text": await AgreementService.rendered(db, a), "terms": a.terms, "status": a.status}


@router.get("/vault/welcome/{token}")
async def welcome(token: str, db: AsyncSession = Depends(aget_db)):
    return await _welcome_state(db, await _invitee(db, token))


class WelcomeStepIn(BaseModel):
    step: int = Field(ge=1, le=4)
    legal_name: Optional[str] = None
    signature: Optional[str] = None
    agree: Optional[bool] = None


@router.post("/vault/welcome/{token}/step")
async def welcome_step(token: str, body: WelcomeStepIn, request: Request, db: AsyncSession = Depends(aget_db)):
    investor = await _invitee(db, token)
    if body.step > investor.onboarding_step:
        raise HTTPException(400, "Finish the earlier steps first.")
    label = _label(investor)
    if body.step == 2:
        if not body.legal_name or body.legal_name.strip().lower() != investor.legal_name.strip().lower():
            raise HTTPException(400, "Type your full legal name exactly as it is on your agreement.")
        investor.identity_confirmed_at = now()
        if investor.kyc_status == "pending":
            investor.kyc_status = "provided"
    if body.step == 3:
        if not body.agree or not body.signature or body.signature.strip().lower() != investor.legal_name.strip().lower():
            raise HTTPException(400, "Tick that you have read the agreement, and sign with your full legal name.")
        from app.models.agreements import Agreement
        from app.services import AgreementService

        agreement = await db.scalar(select(Agreement).where(Agreement.investor_id == investor.id, Agreement.status == "offered")
                                    .order_by(Agreement.created_at.desc()).limit(1))
        if agreement:  # also stamps the investor and the investment, so Founder and Finance see it
            await AgreementService.sign(db, agreement, body.signature, investor.legal_name, client_ip(request), actor_label=label)
        else:
            investor.agreement_signed_name, investor.agreement_signed_at = body.signature.strip(), now()
            inv = (await db.execute(select(Investment).where(Investment.investor_id == investor.id))).scalars().first()
            if inv and not inv.signed_at:
                inv.signed_at = now()
        await audit(db, None, "investor.agreement_signed", "investor", investor.id, request=request, actor_label=label,
                    summary="Signed the investment agreement on screen")
    if body.step == 4:
        inv = (await db.execute(select(Investment).where(Investment.investor_id == investor.id))).scalars().first()
        if not inv or not inv.payment_confirmed_at:
            raise HTTPException(409, "Finance hasn't matched your payment yet. We'll email you when it's done.")
    investor.onboarding_step = max(investor.onboarding_step, body.step + 1)
    investor.status = "onboarding"
    await db.commit()
    return await _welcome_state(db, investor)


@router.post("/vault/welcome/{token}/holder-id")
async def reveal_holder_id(token: str, request: Request, db: AsyncSession = Depends(aget_db)):
    """Generate the Holder ID and return it this once. Only its hash and last four are kept."""
    investor = await _invitee(db, token)
    if investor.onboarding_step < 5:
        raise HTTPException(400, "Finish the earlier steps first.")
    if investor.id_revealed_at:
        raise HTTPException(409, "Your Holder ID was already shown. If you lost it, contact Strategy & Investors.")
    holder_id = AuthService.new_holder_id()
    investor.holder_id_hash = AuthService.hash_holder_id(holder_id)
    investor.holder_id_last4 = holder_id[-4:]
    investor.id_issued_at = investor.id_revealed_at = now()
    await audit(db, None, "investor.holder_id_issued", "investor", investor.id, request=request,
                actor_label=_label(investor), summary=f"Holder ID ending {holder_id[-4:]} issued and shown once")
    await db.commit()
    return {"holder_id": holder_id}


class FinishIn(BaseModel):
    saved: bool


@router.post("/vault/welcome/{token}/finish")
async def welcome_finish(token: str, body: FinishIn, db: AsyncSession = Depends(aget_db)):
    investor = await _invitee(db, token)
    if not body.saved or not investor.id_revealed_at:
        raise HTTPException(400, "Tick that you have saved your Holder ID.")
    investor.status = "active"
    investor.invite_token_hash = investor.invite_expires_at = None
    await db.commit()
    return {"ok": True}


# The holder's own vault ----------------------------------------------------------------------------------

@router.get("/investor/me")
async def investor_me(investor: Investor = Depends(current_investor), db: AsyncSession = Depends(aget_db)):
    h = await InvestorService.holding(db, investor)
    first = investor.legal_name.split(" ")[0]
    return {"name": investor.legal_name, "first_name": first, "internal_number": investor.internal_number,
            "holder_id_hint": f"••••-••••-{investor.holder_id_last4}" if investor.holder_id_last4 else None,
            "tier_label": investor.tier_label, "stake": h,
            "unread": await _unread(db, investor), **await _new_items(db, investor)}


async def _new_items(db: AsyncSession, investor: Investor) -> dict:
    """Reports the holder hasn't opened, and documents they haven't downloaded yet."""
    read = select(InvestorReportRead.report_id).where(InvestorReportRead.investor_id == investor.id)
    reports = await db.scalar(select(func.count()).select_from(InvestorReport).where(
        InvestorReport.published_at.is_not(None), InvestorReport.id.not_in(read))) or 0
    docs = await db.scalar(select(func.count()).select_from(InvestorDocument).where(
        InvestorDocument.investor_id == investor.id, InvestorDocument.file.is_not(None), InvestorDocument.opened_at.is_(None))) or 0
    return {"reports_unread": reports, "documents_new": docs}


async def _unread(db: AsyncSession, investor: Investor) -> int:
    rows = (await db.execute(select(InvestorMessage).where(InvestorMessage.investor_id == investor.id,
                                                           InvestorMessage.from_investor.is_(False),
                                                           InvestorMessage.read_at.is_(None)))).scalars().all()
    return len(rows)


@router.get("/investor/company")
async def investor_company(investor: Investor = Depends(current_investor), db: AsyncSession = Depends(aget_db)):
    summary = await company_summary(db)
    vals = await InvestorService.valuations(db)
    top = max((v.amount_minor for v in vals), default=1)
    miles = list((await db.execute(select(Milestone).order_by(Milestone.position))).scalars())
    return {
        "kpis": summary["pilot_targets"], "money": summary["money"], "round": await InvestorService.round_progress(db),
        "valuations": [{"label": v.label or v.effective_date.strftime("%b"), "amount_minor": v.amount_minor,
                        "date": v.effective_date.isoformat(), "basis": v.basis_note,
                        "height": max(5, round(v.amount_minor * 100 / top * 0.65))} for v in vals],
        "milestones": [{"title": m.title, "status": m.status, "detail": m.detail} for m in miles],
    }


@router.get("/investor/exit-estimate")
async def investor_exit(investor: Investor = Depends(current_investor), db: AsyncSession = Depends(aget_db)):
    return await InvestorService.exit_estimate(db, investor)


class ExitIn(BaseModel):
    note: Optional[str] = Field(default=None, max_length=2000)


@router.post("/investor/exit-requests")
async def start_exit(body: ExitIn, request: Request, session: AuthSession = Depends(vault_session),
                     investor: Investor = Depends(current_investor), db: AsyncSession = Depends(aget_db)):
    _require_fresh(session)
    open_one = (await db.execute(select(ExitRequest).where(ExitRequest.investor_id == investor.id,
                                                           ExitRequest.status == "open"))).scalars().first()
    if open_one:
        raise HTTPException(409, "You already have an exit request open. The founders will reply.")
    est = await InvestorService.exit_estimate(db, investor)
    req = ExitRequest(investor_id=investor.id, valuation_id=uuid.UUID(est["valuation_id"]) if est["valuation_id"] else None,
                      estimate_minor=est["estimate_minor"], note=body.note)
    db.add(req)
    await db.flush()
    await audit(db, None, "investor.exit_requested", "exit_request", req.id, request=request,
                actor_label=_label(investor), summary="Started an exit request (no money moved, no shares changed)",
                after={"estimate_minor": req.estimate_minor})
    founders = list((await db.execute(select(User.email).where(User.role == "founder", User.status == "active"))).scalars())
    await db.commit()
    await EmailService.notify(founders, f"Exit request · {investor.internal_number}", "An investor asked to exit",
                              [f"Holder {investor.internal_number} started an exit request.",
                               "It moves no money and changes no shares. Reply from Investor relations."],
                              ("Open Investor relations", "/os/capital"))
    return {"id": str(req.id), "status": req.status, "estimate_minor": req.estimate_minor}


@router.get("/investor/exit-requests")
async def my_exits(investor: Investor = Depends(current_investor), db: AsyncSession = Depends(aget_db)):
    rows = (await db.execute(select(ExitRequest).where(ExitRequest.investor_id == investor.id)
                             .order_by(ExitRequest.created_at.desc()))).scalars().all()
    return [{"id": str(r.id), "status": r.status, "estimate_minor": r.estimate_minor, "at": r.created_at.isoformat(),
             "decision_note": r.decision_note} for r in rows]


@router.get("/investor/reports")
async def investor_reports(investor: Investor = Depends(current_investor), db: AsyncSession = Depends(aget_db)):
    rows = (await db.execute(select(InvestorReport).order_by(InvestorReport.period.desc()))).scalars().all()
    read = set((await db.execute(select(InvestorReportRead.report_id).where(InvestorReportRead.investor_id == investor.id))).scalars())
    return [{"id": str(r.id), "title": r.title, "period": r.period, "read_minutes": r.read_minutes, "read": r.id in read,
             "published_at": r.published_at.isoformat() if r.published_at else None,
             "due_on": r.due_on.isoformat() if r.due_on else None} for r in rows]


@router.get("/investor/reports/{report_id}")
async def investor_report(report_id: uuid.UUID, request: Request, investor: Investor = Depends(current_investor),
                          db: AsyncSession = Depends(aget_db)):
    r = await db.get(InvestorReport, report_id)
    if not r or not r.published_at:
        raise HTTPException(404, "Report not found.")
    await audit(db, None, "investor.report_read", "investor_report", r.id, request=request, actor_label=_label(investor))
    await db.execute(pg_insert(InvestorReportRead).values(id=uuid.uuid4(), investor_id=investor.id, report_id=r.id)
                     .on_conflict_do_nothing(index_elements=["investor_id", "report_id"]))
    await db.commit()
    return {"id": str(r.id), "title": r.title, "period": r.period, "body": r.body,
            "published_at": r.published_at.isoformat(), "has_file": bool(r.file)}


@router.get("/investor/documents")
async def investor_documents(investor: Investor = Depends(current_investor), db: AsyncSession = Depends(aget_db)):
    rows = (await db.execute(select(InvestorDocument).where(InvestorDocument.investor_id == investor.id)
                             .order_by(InvestorDocument.created_at))).scalars().all()
    return [{"id": str(d.id), "kind": d.kind, "title": d.title, "detail": d.detail, "has_file": bool(d.file),
             "new": bool(d.file) and not d.opened_at} for d in rows]


@router.post("/investor/documents/{doc_id}/download")
async def investor_document_download(doc_id: uuid.UUID, request: Request, session: AuthSession = Depends(vault_session),
                                     investor: Investor = Depends(current_investor), db: AsyncSession = Depends(aget_db)):
    _require_fresh(session)
    doc = await db.get(InvestorDocument, doc_id)
    if not doc or doc.investor_id != investor.id:
        raise HTTPException(404, "Document not found.")
    if not doc.file:
        raise HTTPException(404, "Finance hasn't uploaded this file yet.")
    await audit(db, None, "investor.document_read", "investor_document", doc.id, request=request,
                actor_label=_label(investor), summary=f"Downloaded {doc.title}")
    doc.opened_at = doc.opened_at or now()
    await db.commit()
    return {"url": S3Service.signed_url(doc.file, 120, str(request.base_url).rstrip("/"))}


class MessageIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


@router.get("/investor/messages")
async def investor_messages(investor: Investor = Depends(current_investor), db: AsyncSession = Depends(aget_db)):
    rows = (await db.execute(select(InvestorMessage).where(InvestorMessage.investor_id == investor.id)
                             .order_by(InvestorMessage.created_at))).scalars().all()
    for r in rows:
        if not r.from_investor and not r.read_at:
            r.read_at = now()
    await db.commit()
    return [{"id": str(r.id), "from_investor": r.from_investor, "body": r.body, "at": r.created_at.isoformat()} for r in rows]


@router.post("/investor/messages")
async def investor_send(body: MessageIn, request: Request, investor: Investor = Depends(current_investor),
                        db: AsyncSession = Depends(aget_db)):
    msg = InvestorMessage(investor_id=investor.id, from_investor=True, body=body.body.strip())
    db.add(msg)
    await db.flush()
    strategy = (await db.execute(select(Department).where(Department.slug == "strategy"))).scalar_one_or_none()
    to = list((await db.execute(select(User.email).where(
        (User.role == "founder") | (User.department_id == (strategy.id if strategy else None)),
        User.status == "active"))).scalars())
    await db.commit()
    await EmailService.notify(to, f"Message from holder {investor.internal_number}", "A holder sent a message",
                              [f"Holder {investor.internal_number} wrote to the founders. Reply in Investor relations."],
                              ("Open Investor relations", "/os/capital"))
    return {"id": str(msg.id), "at": msg.created_at.isoformat()}


def investor_contact_tail(phone: Optional[str]) -> Optional[str]:
    return digits_tail(phone, 4)
