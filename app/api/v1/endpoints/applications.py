"""The Join page's applications and the decisions on them."""

import json
import uuid
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import Access, current_access
from app.core.crypto import encrypt
from app.core.database import aget_db
from app.core.security import client_ip, now
from app.models.applications import Application
from app.models.money import Decision
from app.models.people import SignInAttempt
from app.services import EmailService, S3Service
from app.services.AuditService import audit
from app.services.MemberService import clean_handle, create_member, short_code

router = APIRouter(tags=["applications"])
TRACK_CODE = {"streamer": "STR", "designer": "DSN", "clipper": "CLP"}
FIELDS = {
    "streamer": {"required": ["stage_name", "full_name", "email", "whatsapp", "city", "channel_url", "content",
                              "weekly_hours", "why"],
                 "name": "full_name", "handle": "stage_name"},
    "designer": {"required": ["full_name", "handle", "email", "portfolio_url", "tools", "first_design"],
                 "name": "full_name", "handle": "handle"},
    "clipper": {"required": ["handle", "email", "tiktok_url", "software", "best_clip_url", "what_travels"],
                "name": "handle", "handle": "handle"},
}
LIMITS = {"why": 2000, "first_design": 2000, "what_travels": 2000}


def _clean(track: str, data: dict) -> dict:
    spec = FIELDS[track]
    out = {}
    for key, value in data.items():
        if not isinstance(value, (str, int, float, bool)) or key.startswith("_"):
            continue
        value = str(value).strip() if not isinstance(value, bool) else value
        if isinstance(value, str) and len(value) > LIMITS.get(key, 300):
            raise HTTPException(400, f"{key.replace('_', ' ').capitalize()} is too long.")
        out[key] = value
    missing = [k for k in spec["required"] if not out.get(k)]
    if missing:
        raise HTTPException(400, {"message": "Fill in every field.", "missing": missing})
    if out.get("agree") not in (True, "true", "on", "1"):
        raise HTTPException(400, "Tick the box to confirm.")
    if "@" not in out["email"] or "." not in out["email"].split("@")[-1]:
        raise HTTPException(400, {"message": "Enter a valid email.", "missing": ["email"]})
    if track == "streamer":
        try:
            hours = int(out["weekly_hours"])
        except ValueError:
            raise HTTPException(400, {"message": "Hours must be a number.", "missing": ["weekly_hours"]})
        if not 1 <= hours <= 100:
            raise HTTPException(400, {"message": "Hours must be between 1 and 100.", "missing": ["weekly_hours"]})
    return out


@router.post("/applications")
async def apply(request: Request, track: str = Form(...), data: str = Form(...),
                files: list[UploadFile] = File(default=[]), db: AsyncSession = Depends(aget_db)):
    if track not in FIELDS:
        raise HTTPException(400, "Pick streamer, merch designer or clipper.")
    ip = client_ip(request)
    recent = await db.scalar(select(func.count()).select_from(SignInAttempt).where(
        SignInAttempt.kind == "apply", SignInAttempt.ip == ip, SignInAttempt.created_at >= now() - timedelta(hours=1)))
    if (recent or 0) >= 10:
        raise HTTPException(429, "Too many applications from this connection. Try again later.")
    try:
        raw = json.loads(data)
    except json.JSONDecodeError:
        raise HTTPException(400, "Something went wrong with the form. Refresh and try again.")
    fields = _clean(track, raw if isinstance(raw, dict) else {})
    email = fields["email"].lower()
    handle = clean_handle(fields.get("handle")) if track != "streamer" else fields.get("stage_name")
    existing = (await db.execute(select(Application).where(Application.email == email, Application.track == track,
                                                           Application.status.in_(("new", "reviewing"))))).scalars().first()
    if existing:
        raise HTTPException(409, {"message": f"You already have an application in the queue ({existing.reference}).",
                                  "reference": existing.reference})
    stored = []
    if track == "designer":
        if not 1 <= len(files) <= 3:
            raise HTTPException(400, {"message": "Upload up to 3 samples (at least one).", "missing": ["samples"]})
        for f in files:
            body = await f.read()
            try:
                S3Service.check_file(body, f.content_type or "", ("image", "document"))
            except S3Service.UploadRejected as exc:
                raise HTTPException(400, {"message": f"{f.filename}: {exc}", "missing": ["samples"]})
            stored.append({"key": S3Service.put("applications", f.filename or "sample", body, f.content_type),
                           "name": (f.filename or "sample")[:120], "size": len(body), "type": f.content_type})
    contact = fields.pop("whatsapp", None)
    reference = ""
    for _ in range(5):
        reference = f"SH-APP-{TRACK_CODE[track]}-{short_code()}"
        if not await db.scalar(select(func.count()).select_from(Application).where(Application.reference == reference)):
            break
    name = fields.get(FIELDS[track]["name"]) or handle or email
    app_row = Application(reference=reference, track=track, name=name, email=email, contact_enc=encrypt(contact),
                          handle=handle, payload={k: v for k, v in fields.items() if k not in ("email", "agree")},
                          files=stored, ip=ip)
    db.add(app_row)
    db.add(SignInAttempt(kind="apply", contact=email, ip=ip, ok=True))
    await db.commit()
    await EmailService.application_received(email, name, reference, track)
    return {"reference": reference, "track": track}


def _row(a: Application) -> dict:
    p = a.payload or {}
    if a.track == "streamer":
        detail = " · ".join(x for x in [p.get("content"), p.get("city"), f"{p.get('weekly_hours')} hours a week" if p.get("weekly_hours") else None] if x)
    elif a.track == "designer":
        detail = " · ".join(x for x in [p.get("tools"), f"{len(a.files)} sample{'s' if len(a.files) != 1 else ''}", p.get("portfolio_url")] if x)
    else:
        detail = " · ".join(x for x in [p.get("software"), p.get("tiktok_url"), p.get("shorts_url")] if x)
    return {"id": str(a.id), "reference": a.reference, "track": a.track, "name": a.name if a.track != "clipper" else (a.handle or a.name),
            "handle": a.handle, "detail": detail, "tag": a.tag or {"new": "New", "reviewing": "Reviewing"}.get(a.status, a.status.title()),
            "tag_tone": a.tag_tone or ("info" if a.status == "new" else ""), "status": a.status,
            "submitted_at": a.created_at.isoformat(), "decision_note": a.decision_note}


@router.get("/applications")
async def list_applications(track: str, status: Optional[str] = None, access: Access = Depends(current_access),
                            db: AsyncSession = Depends(aget_db)):
    if track not in access.review_lists:
        raise HTTPException(403, "You don't review this list.")
    q = select(Application).where(Application.track == track)
    q = q.where(Application.status.in_(status.split(","))) if status else q.where(Application.status.in_(("new", "reviewing")))
    rows = (await db.execute(q.order_by(Application.created_at))).scalars().all()
    return [_row(a) for a in rows]


@router.get("/applications/{app_id}")
async def get_application(app_id: uuid.UUID, request: Request, access: Access = Depends(current_access),
                          db: AsyncSession = Depends(aget_db)):
    a = await db.get(Application, app_id)
    if not a or a.track not in access.review_lists:
        raise HTTPException(404, "Application not found.")
    base = str(request.base_url).rstrip("/")
    return {**_row(a), "email": a.email, "payload": a.payload,
            "files": [{"name": f["name"], "url": S3Service.signed_url(f["key"], 300, base)} for f in a.files or []]}


class TagIn(BaseModel):
    tag: Optional[str] = Field(default=None, max_length=60)
    tag_tone: Optional[str] = Field(default=None, pattern="^(ok|warn|no|info)?$")


@router.patch("/applications/{app_id}")
async def tag_application(app_id: uuid.UUID, body: TagIn, access: Access = Depends(current_access),
                          db: AsyncSession = Depends(aget_db)):
    a = await db.get(Application, app_id)
    if not a or a.track not in access.review_lists:
        raise HTTPException(404, "Application not found.")
    if a.status not in ("new", "reviewing"):
        raise HTTPException(409, "This application has been decided.")
    a.tag, a.tag_tone, a.status = body.tag, body.tag_tone, "reviewing"
    await db.commit()
    return _row(a)


class DecisionIn(BaseModel):
    decision: str = Field(pattern="^(approve|decline)$")
    note: Optional[str] = Field(default=None, max_length=2000)


@router.post("/applications/{app_id}/decision")
async def decide(app_id: uuid.UUID, body: DecisionIn, request: Request, access: Access = Depends(current_access),
                 db: AsyncSession = Depends(aget_db)):
    a = await db.get(Application, app_id, with_for_update=True)
    if not a or a.track not in access.review_lists:
        raise HTTPException(404, "Application not found.")
    if a.status not in ("new", "reviewing"):
        raise HTTPException(409, "This application has already been decided.")
    if body.decision == "decline" and not (body.note or "").strip():
        raise HTTPException(400, "A decline needs a written reason.")
    actor = access.user
    a.reviewed_by, a.reviewed_at, a.decision_note = actor.id, now(), (body.note or "").strip() or None
    if body.decision == "decline":
        a.status = "declined"
        await audit(db, actor, "application.declined", "application", a.id, request=request,
                    summary=f"{a.reference} declined: {a.decision_note}")
        await db.commit()
        await EmailService.application_declined(a.email, a.name, a.reference, a.decision_note)
        return _row(a)
    a.status = "approved"
    if a.track in ("clipper", "designer"):
        from app.api.v1.endpoints.auth import issue_invite

        user = await create_member(db, name=a.payload.get("full_name") or a.handle or a.name, email=a.email,
                                   role=a.track, handle=a.handle, onboarding=a.track)
        a.created_user_id = user.id
        await audit(db, actor, "application.approved", "application", a.id, request=request,
                    summary=f"{a.reference} approved; {a.track} account created for {a.handle}")
        from app.services import AgreementService

        template = await AgreementService.default_template(db, a.track)
        if template:
            await AgreementService.create(db, template, user=user, by=actor, email=False)
        await issue_invite(db, user, a.track)
    else:
        db.add(Decision(title=f"Offer a seat to applicant {a.reference.split('-')[-1]} ({a.name})",
                        source=f"Talent · invited to a studio assessment by {actor.name}", kind="seat_offer",
                        entity_id=str(a.id), requested_by=actor.id))
        await audit(db, actor, "application.approved", "application", a.id, request=request,
                    summary=f"{a.reference} invited to a studio assessment")
        await db.flush()
        await EmailService.streamer_assessment(a.email, a.name, a.reference)
    await db.commit()
    return _row(a)
