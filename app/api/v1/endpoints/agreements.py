"""Agreements (Founder only): every agreement with every person and investor, and the templates they come from.
Plus each person's own agreements: read, sign, and see how far through the term they are."""

import re
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import Access, current_access, require_area
from app.core.database import aget_db
from app.core.security import client_ip, now
from app.models.agreements import Agreement, AgreementTemplate
from app.models.investors import Investor
from app.models.people import User
from app.models.talent import Streamer
from app.services import AgreementService as A
from app.services.AuditService import audit
from app.services.CompanyService import today

router = APIRouter(tags=["agreements"])
founder_desk = require_area("agreements")


class TermIn(BaseModel):
    kind: str = "other"
    label: str = Field(default="", max_length=80)
    value: str = Field(default="", max_length=120)
    detail: Optional[str] = Field(default="", max_length=200)


# The tracker ------------------------------------------------------------------------------------------------

@router.get("/agreements")
async def tracker(access: Access = Depends(founder_desk), db: AsyncSession = Depends(aget_db)):
    rows = [await A.row(db, a) for a in (await db.execute(select(Agreement).order_by(Agreement.created_at.desc()))).scalars()]
    # What needs the Founder first: unsigned, then fixed terms by how soon they end, then open-ended, then the past
    who = ["streamer", "staff", "investor", "designer", "clipper"]
    rank = {"offered": 0, "draft": 1, "expired": 2, "ending": 3, "active": 4, "upcoming": 4, "ended": 6, "withdrawn": 7, "declined": 7}
    rows.sort(key=lambda r: (rank.get(r["phase"], 5) + (1 if r["phase"] == "active" and not r["ends_on"] else 0),
                             r["ends_on"] or "9999", who.index(r["audience"]) if r["audience"] in who else 9, r["party"]["name"].lower()))
    live = [r for r in rows if r["status"] == "signed" and r["phase"] in ("active", "ending", "upcoming")]
    trials = [r for r in live if r["stage"] == "trial"]
    return {
        "kpis": {"active": len(live), "waiting": sum(r["status"] == "offered" for r in rows),
                 "drafts": sum(r["status"] == "draft" for r in rows), "trials": len(trials),
                 "trials_ending": sum(r["phase"] == "ending" for r in trials),
                 "ending": sum(r["phase"] in ("ending", "expired") and r["next"] is None for r in rows if r["status"] == "signed")},
        "agreements": rows,
        "audiences": A.AUDIENCES,
    }


@router.get("/agreements/parties")
async def parties(access: Access = Depends(founder_desk), db: AsyncSession = Depends(aget_db)):
    people = (await db.execute(select(User).where(User.role.not_in(("customer", "investor")), User.status.in_(("active", "invited")))
                               .order_by(User.name))).scalars().all()
    investors = (await db.execute(select(Investor).where(Investor.status != "exited").order_by(Investor.internal_number))).scalars().all()
    return {"people": [{"id": str(u.id), "name": u.name, "role": u.role} for u in people],
            "investors": [{"id": str(i.id), "name": i.legal_name, "number": i.internal_number} for i in investors]}


def _uuid(value: str) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


async def _get(db: AsyncSession, agreement_id) -> Agreement:
    key = _uuid(agreement_id)
    a = await db.get(Agreement, key) if key else None
    if not a:
        raise HTTPException(404, "Agreement not found.")
    return a


@router.get("/agreements/{agreement_id}")
async def one(agreement_id: str, access: Access = Depends(founder_desk), db: AsyncSession = Depends(aget_db)):
    return await A.full(db, await _get(db, agreement_id))


class NewIn(BaseModel):
    template_id: str
    user_id: Optional[str] = None
    investor_id: Optional[str] = None
    title: Optional[str] = Field(default=None, max_length=160)
    terms: Optional[list[TermIn]] = None
    term_months: Optional[int] = None
    send: bool = False


@router.post("/agreements")
async def new(body: NewIn, access: Access = Depends(founder_desk), db: AsyncSession = Depends(aget_db)):
    template = await _template(db, body.template_id)
    user = await db.get(User, _uuid(body.user_id)) if body.user_id and _uuid(body.user_id) else None
    investor = await db.get(Investor, _uuid(body.investor_id)) if body.investor_id and _uuid(body.investor_id) else None
    if body.user_id and not user or body.investor_id and not investor:
        raise HTTPException(404, "Person not found.")
    a = await A.create(db, template, user=user, investor=investor, by=access.user, send=body.send, title=body.title,
                       terms=[t.model_dump() for t in body.terms] if body.terms is not None else None, term_months=body.term_months)
    await db.commit()
    return {"id": str(a.id)}


class EditIn(BaseModel):
    title: Optional[str] = Field(default=None, min_length=3, max_length=160)
    body: Optional[str] = Field(default=None, min_length=20, max_length=40000)
    terms: Optional[list[TermIn]] = None
    term_months: Optional[int] = None
    no_end: bool = False  # clear the length: no fixed end date


@router.patch("/agreements/{agreement_id}")
async def edit(agreement_id: str, body: EditIn, access: Access = Depends(founder_desk), db: AsyncSession = Depends(aget_db)):
    a = await _get(db, agreement_id)
    if a.status not in ("draft", "offered"):
        raise HTTPException(409, "A signed agreement can't be changed. Draft a new one instead.")
    before = {"title": a.title, "terms": a.terms, "term_months": a.term_months}
    template = await db.get(AgreementTemplate, a.template_id) if a.template_id else None
    if body.title:
        a.title = body.title.strip()
    if body.body:
        a.body = body.body
    if body.terms is not None:
        a.terms = A.clean_terms([t.model_dump() for t in body.terms])
    if body.no_end:
        a.term_months = None
    elif body.term_months is not None:
        A.check_term(template, body.term_months)
        a.term_months = body.term_months
    await audit(db, access.user, "agreement.changed", "agreement", a.id, summary=f"{a.title} ({a.status})", before=before,
                after={"title": a.title, "terms": a.terms, "term_months": a.term_months})
    await db.commit()
    return await A.full(db, a)


@router.post("/agreements/{agreement_id}/send")
async def send(agreement_id: str, access: Access = Depends(founder_desk), db: AsyncSession = Depends(aget_db)):
    a = await _get(db, agreement_id)
    await A.offer(db, a, access.user)
    await _close_decision(db, a, access.user, "approved")
    await db.commit()
    return await A.full(db, a)


@router.post("/agreements/{agreement_id}/withdraw")
async def withdraw(agreement_id: str, access: Access = Depends(founder_desk), db: AsyncSession = Depends(aget_db)):
    a = await _get(db, agreement_id)
    if a.status not in ("draft", "offered"):
        raise HTTPException(409, "Only an unsigned agreement can be withdrawn.")
    a.status = "withdrawn"
    await audit(db, access.user, "agreement.withdrawn", "agreement", a.id, summary=a.title)
    await _close_decision(db, a, access.user, "declined")
    await db.commit()
    return await A.full(db, a)


class EndIn(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


@router.post("/agreements/{agreement_id}/end")
async def end(agreement_id: str, body: EndIn, access: Access = Depends(founder_desk), db: AsyncSession = Depends(aget_db)):
    a = await _get(db, agreement_id)
    if a.status != "signed":
        raise HTTPException(409, "Only a signed agreement can be ended.")
    a.status, a.ended_at, a.ended_by, a.end_reason = "ended", now(), access.user.id, body.reason.strip()
    if a.ends_on is None or a.ends_on > today():
        a.ends_on = today()
    if a.audience == "streamer" and a.user_id:
        streamer = await db.scalar(select(Streamer).where(Streamer.user_id == a.user_id))
        if streamer:
            streamer.contract_end = a.ends_on
    for nxt in (await db.execute(select(Agreement).where(Agreement.follows_id == a.id, Agreement.status.in_(("draft", "offered"))))).scalars():
        nxt.status = "withdrawn"  # a contract or renewal drafted for it no longer applies
        await _close_decision(db, nxt, access.user, "declined")
    await audit(db, access.user, "agreement.ended", "agreement", a.id, summary=f"{a.title}: {a.end_reason}")
    await db.commit()
    return await A.full(db, a)


@router.post("/agreements/{agreement_id}/follow-up")
async def follow_up(agreement_id: str, access: Access = Depends(founder_desk), db: AsyncSession = Depends(aget_db)):
    nxt = await A.follow_up(db, await _get(db, agreement_id), access.user)
    await db.commit()
    return {"id": str(nxt.id)}


async def _close_decision(db: AsyncSession, a: Agreement, user: User, status: str) -> None:
    from app.models.money import Decision

    await db.execute(update(Decision).where(Decision.kind == "agreement", Decision.entity_id == str(a.id), Decision.status == "open")
                     .values(status=status, decided_by=user.id, decided_at=now()))


# Templates --------------------------------------------------------------------------------------------------

async def _template(db: AsyncSession, template_id) -> AgreementTemplate:
    key = _uuid(template_id)
    t = await db.get(AgreementTemplate, key) if key else None
    if not t:
        raise HTTPException(404, "Template not found.")
    return t


async def _template_row(db: AsyncSession, t: AgreementTemplate) -> dict:
    used = await db.scalar(select(func.count()).select_from(Agreement).where(Agreement.template_id == t.id)) or 0
    return {"id": str(t.id), "name": t.name, "audience": t.audience, "audience_label": A.AUDIENCES.get(t.audience, t.audience),
            "stage": t.stage, "stage_label": A.STAGES.get(t.stage, t.stage), "summary": t.summary, "body": t.body,
            "term_months": t.term_months, "term": A.term_label(t.term_months), "min_term_months": t.min_term_months,
            "max_term_months": t.max_term_months, "renewable": t.renewable, "default_terms": t.default_terms,
            "is_default": t.is_default, "status": t.status, "version": t.version, "built_in": t.key is not None,
            "updated_at": t.updated_at.isoformat() if t.updated_at else None, "used": used}


@router.get("/agreement-templates")
async def templates(access: Access = Depends(founder_desk), db: AsyncSession = Depends(aget_db)):
    rows = (await db.execute(select(AgreementTemplate).order_by(AgreementTemplate.status, AgreementTemplate.audience,
                                                                AgreementTemplate.stage, AgreementTemplate.name))).scalars().all()
    return {"templates": [await _template_row(db, t) for t in rows], "audiences": A.AUDIENCES, "stages": A.STAGES,
            "term_kinds": A.TERM_KINDS, "placeholders": [{"key": k, "detail": d} for k, d in A.PLACEHOLDERS]}


@router.get("/agreement-templates/{template_id}")
async def template(template_id: str, access: Access = Depends(founder_desk), db: AsyncSession = Depends(aget_db)):
    return await _template_row(db, await _template(db, template_id))


class TemplateIn(BaseModel):
    name: str = Field(min_length=3, max_length=120)
    audience: str
    stage: str = "standard"
    summary: Optional[str] = Field(default=None, max_length=300)
    body: str = Field(min_length=20, max_length=40000)
    term_months: Optional[int] = None
    min_term_months: Optional[int] = None
    max_term_months: Optional[int] = None
    renewable: bool = False
    default_terms: list[TermIn] = []
    is_default: bool = False
    status: str = Field(default="active", pattern="^(active|archived)$")


def _check_template(body: TemplateIn) -> None:
    if body.audience not in A.AUDIENCES:
        raise HTTPException(400, "Choose who this template is for.")
    if body.stage not in A.STAGES:
        raise HTTPException(400, "Choose trial, contract or agreement.")
    for m in (body.term_months, body.min_term_months, body.max_term_months):
        if m is not None and not 1 <= m <= 120:
            raise HTTPException(400, "Lengths must be between 1 and 120 months.")
    if body.min_term_months and body.max_term_months and body.min_term_months > body.max_term_months:
        raise HTTPException(400, "The shortest length can't be longer than the longest.")
    if body.term_months and (body.min_term_months and body.term_months < body.min_term_months
                             or body.max_term_months and body.term_months > body.max_term_months):
        raise HTTPException(400, "The usual length must sit between the shortest and the longest.")
    if body.stage == "trial" and not body.term_months:
        raise HTTPException(400, "A trial needs a length.")


async def _set_default(db: AsyncSession, t: AgreementTemplate) -> None:
    await db.execute(update(AgreementTemplate).where(AgreementTemplate.audience == t.audience, AgreementTemplate.stage == t.stage,
                                                     AgreementTemplate.id != t.id).values(is_default=False))


@router.post("/agreement-templates")
async def create_template(body: TemplateIn, access: Access = Depends(founder_desk), db: AsyncSession = Depends(aget_db)):
    _check_template(body)
    data = body.model_dump()
    data["default_terms"] = A.clean_terms(data["default_terms"])
    t = AgreementTemplate(**data, version=1, updated_by=access.user.id)
    db.add(t)
    await db.flush()
    if t.is_default:
        await _set_default(db, t)
    await audit(db, access.user, "agreement_template.created", "agreement_template", t.id, summary=t.name)
    await db.commit()
    return await _template_row(db, t)


@router.put("/agreement-templates/{template_id}")
async def save_template(template_id: str, body: TemplateIn, access: Access = Depends(founder_desk), db: AsyncSession = Depends(aget_db)):
    t = await _template(db, template_id)
    _check_template(body)
    data = body.model_dump()
    data["default_terms"] = A.clean_terms(data["default_terms"])
    wording = t.body != data["body"] or t.default_terms != data["default_terms"] or t.term_months != data["term_months"]
    before = {"name": t.name, "version": t.version}
    for k, v in data.items():
        setattr(t, k, v)
    if wording:
        t.version += 1  # agreements keep the version they were made from
    t.updated_by = access.user.id
    if t.is_default:
        await _set_default(db, t)
    await audit(db, access.user, "agreement_template.changed", "agreement_template", t.id, summary=f"{t.name} · version {t.version}",
                before=before, after={"name": t.name, "version": t.version})
    await db.commit()
    return await _template_row(db, t)


@router.post("/agreement-templates/{template_id}/duplicate")
async def duplicate(template_id: str, access: Access = Depends(founder_desk), db: AsyncSession = Depends(aget_db)):
    src = await _template(db, template_id)
    t = AgreementTemplate(name=f"{src.name} (copy)", audience=src.audience, stage=src.stage, summary=src.summary, body=src.body,
                          term_months=src.term_months, min_term_months=src.min_term_months, max_term_months=src.max_term_months,
                          renewable=src.renewable, default_terms=list(src.default_terms or []), is_default=False, version=1,
                          updated_by=access.user.id)
    db.add(t)
    await db.flush()
    await audit(db, access.user, "agreement_template.created", "agreement_template", t.id, summary=f"{t.name}, from {src.name}")
    await db.commit()
    return {"id": str(t.id)}


class PreviewIn(BaseModel):
    body: str = Field(max_length=40000)
    terms: list[TermIn] = []
    term_months: Optional[int] = None
    audience: str = "staff"


SAMPLE = {"staff": ("Ama Mensah", "Video editor", "@ama"), "streamer": ("Kwame Asante", "Streamer", "@kwame"),
          "designer": ("Efua Lines", "Community designer", "@efua.lines"), "clipper": ("Kwesi Cuts", "Clipper", "@kwesi.cuts"),
          "investor": ("Esi Owusu", "holder", "SH-INV-005")}


@router.post("/agreement-templates/preview")
async def preview(body: PreviewIn, access: Access = Depends(founder_desk), db: AsyncSession = Depends(aget_db)):
    """The text as a sample person would see it, and any {{placeholders}} that won't be filled in."""
    name, title, handle = SAMPLE.get(body.audience, SAMPLE["staff"])
    probe = Agreement(audience=body.audience, stage="standard", title="", body=body.body, term_months=body.term_months,
                      terms=A.clean_terms([t.model_dump() for t in body.terms]))
    ctx = await A.context_for(db, probe)
    ctx.update(name=name, title=title, handle=handle, stage_name=name.split()[0], department="Creative", seat_number="No. 02")
    unknown = sorted({m for m in re.findall(r"\{\{\s*(\w+)\s*\}\}", body.body)} - set(ctx))
    return {"text": A.fill(body.body, ctx), "unknown": unknown}


# A person's own agreements ----------------------------------------------------------------------------------

@router.get("/me/agreements")
async def mine(access: Access = Depends(current_access), db: AsyncSession = Depends(aget_db)):
    rows = await A.for_user(db, access.user.id, ("offered", "signed", "ended"))
    out = []
    for a in rows:
        r = await A.row(db, a)
        r["text"] = await A.rendered(db, a)
        r.pop("next", None)
        out.append(r)
    return {"waiting": [r for r in out if r["status"] == "offered"],
            "current": [r for r in out if r["status"] == "signed" and r["phase"] in ("active", "ending", "upcoming")],
            "past": [r for r in out if r["status"] == "ended" or r["phase"] == "expired"],
            "name": access.user.name}


class SignIn(BaseModel):
    signature: str = Field(min_length=2, max_length=160)


@router.post("/me/agreements/{agreement_id}/sign")
async def sign_mine(agreement_id: str, body: SignIn, request: Request, access: Access = Depends(current_access),
                    db: AsyncSession = Depends(aget_db)):
    a = await _get(db, agreement_id)
    if a.user_id != access.user.id:
        raise HTTPException(404, "Agreement not found.")
    await A.sign(db, a, body.signature, access.user.name, client_ip(request), actor=access.user)
    await db.commit()
    return await mine(access, db)
