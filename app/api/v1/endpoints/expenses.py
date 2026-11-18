"""Finance: purchases and company expenses, what was bought for each streamer, and receipts."""

import uuid
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import Access, require_area
from app.core.database import aget_db
from app.models.money import COST_CATEGORIES, RECORDABLE_COSTS, Expense
from app.models.people import User
from app.models.talent import Streamer
from app.models.tech import Equipment
from app.services import ExpenseService, S3Service
from app.services.AuditService import audit
from app.services.CompanyService import month_bounds, today
from app.services.money import ghs, month_key, month_label

router = APIRouter(prefix="/finance", tags=["expenses"])
finance_desk = require_area("finance")
PAID_WITH = {"momo": "Mobile Money", "bank": "Bank transfer", "cash": "Cash", "card": "Card"}


def _can(access: Access) -> None:
    if not access.does_finance:
        raise HTTPException(403, "Only Finance records purchases.")


async def _row(db: AsyncSession, e: Expense, streamers: dict, base: str) -> dict:
    gear = (await db.execute(select(Equipment).where(Equipment.expense_id == e.id).order_by(Equipment.tag))).scalars().all()
    by = await db.get(User, e.entered_by) if e.entered_by else None
    s = streamers.get(e.streamer_id)
    return {"id": str(e.id), "date": e.on_date.isoformat(), "category": e.category, "category_label": ExpenseService.label(e.category),
            "description": e.description, "vendor": e.vendor, "amount_minor": e.amount_minor, "paid_with": e.paid_with,
            "paid_with_label": PAID_WITH.get(e.paid_with or "", None), "reference": e.reference, "note": e.note,
            "streamer": {"id": str(s.id), "name": s.stage_name} if s else None,
            "equipment": [{"id": str(g.id), "tag": g.tag, "name": g.name, "serial_number": g.serial_number, "condition": g.condition}
                          for g in gear],
            "receipt_url": S3Service.signed_url(e.receipt_key, 900, base) if e.receipt_key else None,
            "entered_by": by.name if by else None}


@router.get("/expenses")
async def expenses(request: Request, month: Optional[str] = None, streamer: Optional[str] = None,
                   access: Access = Depends(finance_desk), db: AsyncSession = Depends(aget_db)):
    """`streamer`: a streamer's id, "company" (not tied to a streamer) or empty for everything."""
    key = month or month_key(today())
    start, end = month_bounds(key)
    q = select(Expense).where(Expense.on_date >= start, Expense.on_date < end)
    if streamer == "company":
        q = q.where(Expense.streamer_id.is_(None))
    elif streamer:
        try:
            q = q.where(Expense.streamer_id == uuid.UUID(streamer))
        except ValueError:
            raise HTTPException(400, "Unknown streamer.")
    rows = (await db.execute(q.order_by(Expense.on_date.desc(), Expense.created_at.desc()))).scalars().all()
    all_streamers = (await db.execute(select(Streamer).order_by(Streamer.signing_number))).scalars().all()
    by_id = {s.id: s for s in all_streamers}
    base = str(request.base_url).rstrip("/")
    month_rows = (await db.execute(select(Expense).where(Expense.on_date >= start, Expense.on_date < end))).scalars().all()
    by_cat: dict[str, int] = {}
    for e in month_rows:
        by_cat[e.category] = by_cat.get(e.category, 0) + e.amount_minor
    totals = dict((await db.execute(select(Expense.streamer_id, func.sum(Expense.amount_minor)).group_by(Expense.streamer_id))).all())
    month_totals: dict = {}
    for e in month_rows:
        month_totals[e.streamer_id] = month_totals.get(e.streamer_id, 0) + e.amount_minor
    gear_counts = dict((await db.execute(select(Equipment.streamer_id, func.count()).where(Equipment.condition != "retired")
                                         .group_by(Equipment.streamer_id))).all())
    spend = [{"id": str(s.id), "name": s.stage_name, "number": f"{s.signing_number:02d}", "month_minor": month_totals.get(s.id, 0),
              "total_minor": int(totals.get(s.id) or 0), "gear": gear_counts.get(s.id, 0)} for s in all_streamers]
    return {
        "month": key, "label": month_label(key),
        "rows": [await _row(db, e, by_id, base) for e in rows],
        "total_minor": sum(e.amount_minor for e in month_rows),
        "company_minor": month_totals.get(None, 0),
        "by_category": sorted([{"category": c, "label": ExpenseService.label(c), "minor": v} for c, v in by_cat.items()],
                              key=lambda r: -r["minor"]),
        "by_streamer": spend,
        "categories": [{"key": c, "label": COST_CATEGORIES[c]} for c in RECORDABLE_COSTS],
        "paid_with": [{"key": k, "label": v} for k, v in PAID_WITH.items()],
        "streamers": [{"id": str(s.id), "name": s.stage_name} for s in all_streamers],
        "can_record": access.does_finance,
    }


class ItemIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    serial_number: Optional[str] = Field(default=None, max_length=80)


class ExpenseIn(BaseModel):
    on_date: date
    category: str
    description: str = Field(min_length=2, max_length=200)
    amount: float = Field(gt=0, le=10_000_000)
    vendor: Optional[str] = Field(default=None, max_length=120)
    paid_with: Optional[str] = Field(default=None, pattern="^(momo|bank|cash|card)$")
    reference: Optional[str] = Field(default=None, max_length=80)
    streamer_id: Optional[uuid.UUID] = None
    note: Optional[str] = Field(default=None, max_length=1000)
    items: list[ItemIn] = Field(default_factory=list, max_length=50)


@router.post("/expenses")
async def add_expense(body: ExpenseIn, request: Request, access: Access = Depends(finance_desk), db: AsyncSession = Depends(aget_db)):
    _can(access)
    if body.on_date > today():
        raise HTTPException(400, "The date can't be in the future.")
    e, gear = await ExpenseService.record(db, access.user, on_date=body.on_date, category=body.category,
                                          description=body.description, amount_minor=round(body.amount * 100), vendor=body.vendor,
                                          paid_with=body.paid_with, reference=body.reference, streamer_id=body.streamer_id,
                                          note=body.note, items=[i.model_dump() for i in body.items])
    s = await db.get(Streamer, e.streamer_id) if e.streamer_id else None
    await audit(db, access.user, "expense.recorded", "expense", e.id, request=request,
                summary=f"{e.description} · {ghs(e.amount_minor)}" + (f" · for {s.stage_name}" if s else "")
                + (f" · {len(gear)} item{'s' if len(gear) > 1 else ''} added to the register" if gear else ""))
    await db.commit()
    return {"id": str(e.id), "equipment": [{"id": str(g.id), "tag": g.tag} for g in gear]}


class ExpenseUpdate(BaseModel):
    on_date: Optional[date] = None
    category: Optional[str] = None
    description: Optional[str] = Field(default=None, min_length=2, max_length=200)
    amount: Optional[float] = Field(default=None, gt=0, le=10_000_000)
    vendor: Optional[str] = Field(default=None, max_length=120)
    paid_with: Optional[str] = Field(default=None, pattern="^(momo|bank|cash|card)$")
    reference: Optional[str] = Field(default=None, max_length=80)
    streamer_id: Optional[uuid.UUID] = None
    note: Optional[str] = Field(default=None, max_length=1000)


@router.patch("/expenses/{expense_id}")
async def edit_expense(expense_id: uuid.UUID, body: ExpenseUpdate, request: Request, access: Access = Depends(finance_desk),
                       db: AsyncSession = Depends(aget_db)):
    _can(access)
    e = await db.get(Expense, expense_id, with_for_update=True)
    if not e:
        raise HTTPException(404, "Not found.")
    before = {"amount_minor": e.amount_minor, "category": e.category, "on_date": e.on_date, "streamer_id": e.streamer_id}
    changes = body.model_dump(exclude_unset=True)
    if "amount" in changes:
        changes["amount_minor"] = round(changes.pop("amount") * 100)
    if changes.get("on_date") and changes["on_date"] > today():
        raise HTTPException(400, "The date can't be in the future.")
    await ExpenseService.update(db, e, changes)
    await audit(db, access.user, "expense.edited", "expense", e.id, request=request, before=before,
                after={"amount_minor": e.amount_minor, "category": e.category, "on_date": e.on_date, "streamer_id": e.streamer_id},
                summary=f"Edited {e.description} · {ghs(e.amount_minor)}")
    await db.commit()
    return {"ok": True}


@router.delete("/expenses/{expense_id}")
async def delete_expense(expense_id: uuid.UUID, request: Request, access: Access = Depends(finance_desk), db: AsyncSession = Depends(aget_db)):
    _can(access)
    e = await db.get(Expense, expense_id, with_for_update=True)
    if not e:
        raise HTTPException(404, "Not found.")
    text = f"Deleted {e.description} · {ghs(e.amount_minor)}"
    kept = await ExpenseService.remove(db, e)
    await audit(db, access.user, "expense.deleted", "expense", expense_id, request=request, summary=text)
    await db.commit()
    return {"ok": True, "equipment_kept": kept}


@router.post("/expenses/{expense_id}/receipt")
async def upload_receipt(expense_id: uuid.UUID, request: Request, file: UploadFile = File(...), access: Access = Depends(finance_desk),
                         db: AsyncSession = Depends(aget_db)):
    _can(access)
    e = await db.get(Expense, expense_id)
    if not e:
        raise HTTPException(404, "Not found.")
    data = await file.read()
    try:
        S3Service.check_file(data, file.content_type or "", ("image", "document"))
    except S3Service.UploadRejected as exc:
        raise HTTPException(400, str(exc))
    e.receipt_key = S3Service.put("receipts", file.filename or "receipt", data, file.content_type or "application/octet-stream")
    await audit(db, access.user, "expense.receipt", "expense", e.id, request=request, summary=f"Receipt for {e.description}")
    await db.commit()
    return {"receipt_url": S3Service.signed_url(e.receipt_key, 900, str(request.base_url).rstrip("/"))}


@router.get("/streamers/{streamer_id}/spend")
async def streamer_spend(streamer_id: uuid.UUID, request: Request, access: Access = Depends(finance_desk), db: AsyncSession = Depends(aget_db)):
    """Everything spent on one streamer: purchases and the gear they hold, with what it cost."""
    s = await db.get(Streamer, streamer_id)
    if not s:
        raise HTTPException(404, "Not found.")
    rows = (await db.execute(select(Expense).where(Expense.streamer_id == s.id).order_by(Expense.on_date.desc()))).scalars().all()
    gear = (await db.execute(select(Equipment).where(Equipment.streamer_id == s.id).order_by(Equipment.tag))).scalars().all()
    base = str(request.base_url).rstrip("/")
    return {"streamer": {"id": str(s.id), "name": s.stage_name}, "total_minor": sum(e.amount_minor for e in rows),
            "rows": [await _row(db, e, {s.id: s}, base) for e in rows],
            "equipment": [{"id": str(g.id), "tag": g.tag, "name": g.name, "condition": g.condition, "cost_minor": g.purchase_cost_minor,
                           "purchased_on": g.purchased_on.isoformat() if g.purchased_on else None} for g in gear]}
