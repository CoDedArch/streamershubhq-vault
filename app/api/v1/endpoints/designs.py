"""Merch designs (6.2). Nothing reaches the store unapproved:
creative_review -> [streamer_signoff] -> founder_approval -> live, with changes_requested and rejected."""

import re
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import Access, current_access, require_role
from app.core.database import aget_db
from app.core.security import now
from app.models.people import User
from app.models.store import COLLECTIONS, Design, Product
from app.models.talent import Streamer
from app.services import EmailService, S3Service
from app.services.AuditService import audit
from app.services.SettingsService import get_setting

router = APIRouter(tags=["designs"])
PRODUCT_TYPES = {"tee": "Tee", "hoodie": "Hoodie", "cap": "Cap", "jersey": "Jersey", "mat": "Desk mat", "poster": "Poster",
                 "mug": "Mug", "tote": "Tote bag", "bottle": "Bottle", "keychain": "Keychain", "set": "Gift set"}
STAGE_LABEL = {"submitted": "Uploaded", "creative_review": "Creative check", "changes_requested": "Changes asked",
               "streamer_signoff": "Streamer sign-off", "founder_approval": "Final approval", "live": "Live",
               "rejected": "Declined"}
STAGE_TONE = {"creative_review": "warn", "changes_requested": "warn", "streamer_signoff": "info",
              "founder_approval": "info", "live": "ok", "rejected": "no"}


def _note(design: Design, actor: User, stage: str, note: Optional[str]):
    if note:
        design.review_notes = [*(design.review_notes or []), {"at": now().isoformat(), "by": actor.name,
                                                               "stage": stage, "note": note.strip()}]


async def design_out(db: AsyncSession, d: Design, base: str = "", private: bool = True) -> dict:
    designer = await db.get(User, d.designer_user_id) if d.designer_user_id else None
    streamer = await db.get(Streamer, d.uses_streamer_id) if d.uses_streamer_id else None
    product = (await db.execute(select(Product).where(Product.design_id == d.id))).scalar_one_or_none()
    out = {"id": str(d.id), "reference": d.reference, "title": d.title, "product_type": d.product_type,
           "type_label": PRODUCT_TYPES.get(d.product_type, d.product_type), "status": d.status,
           "stage": STAGE_LABEL[d.status], "tone": STAGE_TONE.get(d.status, ""),
           "designer": designer.handle or designer.name if designer else "In-house",
           "designer_id": str(designer.id) if designer else None, "asked_price_minor": d.asked_price_minor,
           "price_minor": d.price_minor, "commission_rate_bps": d.commission_rate_bps, "collection": d.collection,
           "collection_label": COLLECTIONS.get(d.collection or ""), "uses_streamer": streamer.stage_name if streamer else None,
           "uses_streamer_id": str(streamer.id) if streamer else None, "version": d.version,
           "description": d.description, "decline_reason": d.decline_reason, "notes": d.review_notes or [],
           "created_at": d.created_at.isoformat(), "product_slug": product.slug if product else None,
           "production_cost_minor": d.production_cost_minor}
    if private:
        out["files"] = [{"name": f["name"], "kind": f.get("kind"), "url": S3Service.signed_url(f["key"], 300, base)}
                        for f in d.files or []]
    return out


async def _store_files(files: list[UploadFile]) -> list[dict]:
    stored = []
    for i, f in enumerate(files):
        body = await f.read()
        try:
            S3Service.check_file(body, f.content_type or "", ("image", "document"))
        except S3Service.UploadRejected as exc:
            raise HTTPException(400, f"{f.filename}: {exc}")
        stored.append({"key": S3Service.put("designs", f.filename or "design", body, f.content_type),
                       "name": (f.filename or "design")[:120], "size": len(body),
                       "kind": "artwork" if i == 0 else "mockup"})
    return stored


@router.post("/designs")
async def upload_design(request: Request, title: str = Form(..., max_length=160), product_type: str = Form(...),
                        asked_price: Optional[int] = Form(None), description: Optional[str] = Form(None, max_length=2000),
                        uses_streamer_id: Optional[str] = Form(None), brief_id: Optional[str] = Form(None),
                        files: list[UploadFile] = File(...), access: Access = Depends(current_access),
                        db: AsyncSession = Depends(aget_db)):
    user = access.user
    in_house = user.role != "designer"
    if in_house and not (access.is_founder or access.can("creative")):
        raise HTTPException(403, "Only designers and the Creative team upload designs.")
    if product_type not in PRODUCT_TYPES:
        raise HTTPException(400, "Pick a product type.")
    if not 1 <= len(files) <= 4:
        raise HTTPException(400, "Upload the print file, plus up to three mock-ups.")
    if asked_price is not None and not 10 <= asked_price <= 5000:
        raise HTTPException(400, "Suggest a price between GHS 10 and GHS 5,000.")
    streamer = None
    if uses_streamer_id:
        try:
            streamer = await db.get(Streamer, uuid.UUID(uses_streamer_id))
        except ValueError:
            streamer = None
        if not streamer:
            raise HTTPException(400, "Pick a streamer from the roster.")
    n = await db.scalar(text("SELECT nextval('design_ref_seq')"))
    design = Design(reference=f"SH-DSN-{n:04d}", designer_user_id=None if in_house else user.id, title=title.strip(),
                    product_type=product_type, asked_price_minor=asked_price * 100 if asked_price else None,
                    description=(description or "").strip() or None, files=await _store_files(files),
                    status="creative_review", uses_streamer_id=streamer.id if streamer else None,
                    brief_id=uuid.UUID(brief_id) if brief_id else None)
    db.add(design)
    await db.flush()
    await audit(db, user, "design.uploaded", "design", design.id, request=request, summary=f"{design.reference} {design.title}")
    await db.commit()
    return await design_out(db, design, str(request.base_url).rstrip("/"))


@router.get("/designs/mine")
async def my_designs(request: Request, access: Access = Depends(require_role("designer")), db: AsyncSession = Depends(aget_db)):
    rows = (await db.execute(select(Design).where(Design.designer_user_id == access.user.id)
                             .order_by(Design.created_at.desc()))).scalars().all()
    base = str(request.base_url).rstrip("/")
    return [await design_out(db, d, base) for d in rows]


@router.post("/designs/{design_id}/resubmit")
async def resubmit(design_id: uuid.UUID, request: Request, note: Optional[str] = Form(None, max_length=2000),
                   files: list[UploadFile] = File(...), access: Access = Depends(require_role("designer")),
                   db: AsyncSession = Depends(aget_db)):
    d = await db.get(Design, design_id)
    if not d or d.designer_user_id != access.user.id:
        raise HTTPException(404, "Design not found.")
    if d.status != "changes_requested":
        raise HTTPException(409, "You can upload a new version only when changes were asked for.")
    d.files, d.version, d.status = await _store_files(files), d.version + 1, "creative_review"
    _note(d, access.user, "resubmitted", note or f"Version {d.version} uploaded")
    await audit(db, access.user, "design.resubmitted", "design", d.id, request=request, summary=f"{d.reference} v{d.version}")
    await db.commit()
    return await design_out(db, d, str(request.base_url).rstrip("/"))


@router.get("/designs")
async def list_designs(request: Request, status: Optional[str] = None, access: Access = Depends(current_access),
                       db: AsyncSession = Depends(aget_db)):
    if "design" not in access.review_lists:
        raise HTTPException(403, "You don't review merch designs.")
    if status:
        stages = status.split(",")
    else:
        stages = (["creative_review"] if access.can("creative") else []) + (["founder_approval", "streamer_signoff"] if access.is_founder else [])
    rows = (await db.execute(select(Design).where(Design.status.in_(stages)).order_by(Design.created_at))).scalars().all()
    base = str(request.base_url).rstrip("/")
    return [await design_out(db, d, base) for d in rows]


@router.get("/designs/{design_id}")
async def get_design(design_id: uuid.UUID, request: Request, access: Access = Depends(current_access),
                     db: AsyncSession = Depends(aget_db)):
    d = await db.get(Design, design_id)
    if not d:
        raise HTTPException(404, "Design not found.")
    own = d.designer_user_id == access.user.id
    streamer_owner = False
    if d.uses_streamer_id and access.user.role == "streamer":
        s = await db.get(Streamer, d.uses_streamer_id)
        streamer_owner = bool(s and s.user_id == access.user.id)
    if not (own or streamer_owner or "design" in access.review_lists):
        raise HTTPException(404, "Design not found.")
    return await design_out(db, d, str(request.base_url).rstrip("/"))


class ReviewIn(BaseModel):
    action: str = Field(pattern="^(pass|changes|reject)$")
    note: Optional[str] = Field(default=None, max_length=2000)
    price: Optional[int] = Field(default=None, ge=10, le=5000)
    commission_bps: Optional[int] = Field(default=None, ge=0, le=5000)
    collection: Optional[str] = Field(default=None, pattern="^(label|roster|designer)$")
    production_cost: Optional[int] = Field(default=None, ge=0, le=5000)


@router.post("/designs/{design_id}/review")
async def creative_review(design_id: uuid.UUID, body: ReviewIn, request: Request, access: Access = Depends(current_access),
                          db: AsyncSession = Depends(aget_db)):
    """The Creative Lead's quality check: pass (setting price, commission and product details), ask for
    changes, or decline."""
    if not (access.is_founder or access.can("creative")):
        raise HTTPException(403, "Only the Creative team checks designs.")
    d = await db.get(Design, design_id, with_for_update=True)
    if not d or d.status != "creative_review":
        raise HTTPException(409, "This design isn't waiting for the creative check.")
    actor = access.user
    if body.action in ("changes", "reject") and not (body.note or "").strip():
        raise HTTPException(400, "Write a note for the designer.")
    if body.action == "changes":
        d.status = "changes_requested"
        _note(d, actor, "changes", body.note)
    elif body.action == "reject":
        d.status, d.decline_reason, d.decided_at = "rejected", body.note.strip(), now()
        _note(d, actor, "declined", body.note)
    else:
        if not body.price:
            raise HTTPException(400, "Set the store price.")
        default_bps = (await get_setting(db, "commission"))["default_rate_bps"]
        d.price_minor = body.price * 100
        d.commission_rate_bps = 0 if d.designer_user_id is None else (body.commission_bps if body.commission_bps is not None else default_bps)
        d.collection = body.collection or ("roster" if d.uses_streamer_id else ("designer" if d.designer_user_id else "label"))
        d.production_cost_minor = (body.production_cost or 0) * 100
        d.status = "streamer_signoff" if d.uses_streamer_id else "founder_approval"
        _note(d, actor, "passed", body.note or "Passed the creative check")
    await audit(db, actor, f"design.creative_{body.action}", "design", d.id, request=request,
                summary=f"{d.reference} → {d.status}", after={"price_minor": d.price_minor, "commission_bps": d.commission_rate_bps})
    await db.commit()
    await _tell_designer(db, d)
    return await design_out(db, d, str(request.base_url).rstrip("/"))


class SignoffIn(BaseModel):
    approve: bool
    note: Optional[str] = Field(default=None, max_length=2000)


@router.post("/designs/{design_id}/streamer-signoff")
async def streamer_signoff(design_id: uuid.UUID, body: SignoffIn, request: Request,
                           access: Access = Depends(require_role("streamer")), db: AsyncSession = Depends(aget_db)):
    d = await db.get(Design, design_id, with_for_update=True)
    s = await db.get(Streamer, d.uses_streamer_id) if d and d.uses_streamer_id else None
    if not d or not s or s.user_id != access.user.id:
        raise HTTPException(404, "Design not found.")
    if d.status != "streamer_signoff":
        raise HTTPException(409, "This design isn't waiting for your sign-off.")
    if not body.approve and not (body.note or "").strip():
        raise HTTPException(400, "Say why, so the designer can fix it.")
    if body.approve:
        d.status = "founder_approval"
        _note(d, access.user, "streamer_approved", body.note or f"{s.stage_name} signed off")
    else:
        d.status, d.decline_reason, d.decided_at = "rejected", body.note.strip(), now()
        _note(d, access.user, "streamer_declined", body.note)
    await audit(db, access.user, "design.streamer_signoff", "design", d.id, request=request,
                summary=f"{d.reference} {'approved' if body.approve else 'rejected'} by {s.stage_name}")
    await db.commit()
    await _tell_designer(db, d)
    return await design_out(db, d, str(request.base_url).rstrip("/"))


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:100] or "item"


class ApproveIn(BaseModel):
    approve: bool
    note: Optional[str] = Field(default=None, max_length=2000)
    stock: int = Field(default=20, ge=0, le=10000)


@router.post("/designs/{design_id}/approve")
async def founder_approval(design_id: uuid.UUID, body: ApproveIn, request: Request,
                           access: Access = Depends(require_role("founder")), db: AsyncSession = Depends(aget_db)):
    """The final gate. Approval locks the price and commission and puts the product in the store."""
    d = await db.get(Design, design_id, with_for_update=True)
    if not d or d.status != "founder_approval":
        raise HTTPException(409, "This design isn't waiting for final approval.")
    if not body.approve:
        if not (body.note or "").strip():
            raise HTTPException(400, "A decline always carries a reason.")
        d.status, d.decline_reason, d.decided_at = "rejected", body.note.strip(), now()
        _note(d, access.user, "declined", body.note)
        await audit(db, access.user, "design.declined", "design", d.id, request=request, summary=f"{d.reference}: {body.note}")
        await db.commit()
        await _tell_designer(db, d)
        return await design_out(db, d, str(request.base_url).rstrip("/"))
    d.status, d.decided_at = "live", now()
    _note(d, access.user, "approved", body.note or "Approved for the store")
    slug, n = _slugify(d.title), 1
    while await db.scalar(select(func.count()).select_from(Product).where(Product.slug == (slug if n == 1 else f"{slug}-{n}"))):
        n += 1
    slug = slug if n == 1 else f"{slug}-{n}"
    sizes = ["S", "M", "L", "XL", "XXL"] if d.product_type in ("tee", "hoodie", "jersey") else []
    tone = {"tee": "k", "hoodie": "t", "cap": "t", "jersey": "", "mat": "", "poster": "y"}.get(d.product_type, "")
    images = [f["key"] for f in (d.files or [])[1:]]
    product = Product(slug=slug, name=d.title, description=d.description, price_minor=d.price_minor,
                      category=d.collection or "designer", product_type=d.product_type, tone=tone,
                      tag="Designer" if d.collection == "designer" else ("Roster" if d.collection == "roster" else "New"),
                      images=images, sizes=sizes, status="live", design_id=d.id, streamer_id=d.uses_streamer_id,
                      commission_rate_bps=d.commission_rate_bps or 0, production_cost_minor=d.production_cost_minor or 0,
                      stock=body.stock, position=100)
    db.add(product)
    await db.flush()
    await audit(db, access.user, "design.approved", "design", d.id, request=request,
                summary=f"{d.reference} live as /store/{slug}", after={"price_minor": d.price_minor, "commission_bps": d.commission_rate_bps})
    await db.commit()
    await _tell_designer(db, d)
    return await design_out(db, d, str(request.base_url).rstrip("/"))


async def _tell_designer(db: AsyncSession, d: Design) -> None:
    if not d.designer_user_id:
        return
    designer = await db.get(User, d.designer_user_id)
    lines = {"changes_requested": ["The Creative team asked for changes. Open your Studio to read the note and upload a new version."],
             "rejected": [f"This design won't go to the store. Reason: {d.decline_reason}"],
             "founder_approval": ["It passed the checks and is waiting for final approval."],
             "streamer_signoff": ["It passed the creative check and is with the streamer for sign-off."],
             "live": ["It's live in the store. You'll see every sale and your commission in your Studio."]}.get(d.status)
    if designer and lines:
        await EmailService.notify([designer.email], f"{d.title}: {STAGE_LABEL[d.status]}", d.title, lines,
                                  ("Open my Studio", "/studio"))
