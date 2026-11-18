"""Purchases and company expenses. Each one is a ledger entry under its cost category, so it shows in the
P&L and budgets straight away. Equipment bought with it goes on the register, linked to the purchase and,
when it was bought for a streamer, to them. Streamers see their gear, never what it cost."""

import uuid
from datetime import date
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.money import COST_CATEGORIES, RECORDABLE_COSTS, Expense, LedgerEntry
from app.models.people import User
from app.models.talent import Streamer
from app.models.tech import Equipment


async def next_tag(db: AsyncSession) -> str:
    n = (await db.scalar(select(func.count()).select_from(Equipment)) or 0) + 1
    tag = f"SH-EQ-{n:03d}"
    while await db.scalar(select(func.count()).select_from(Equipment).where(Equipment.tag == tag)):
        n += 1
        tag = f"SH-EQ-{n:03d}"
    return tag


async def _streamer(db: AsyncSession, streamer_id: Optional[uuid.UUID]) -> Optional[Streamer]:
    if not streamer_id:
        return None
    s = await db.get(Streamer, streamer_id)
    if not s:
        raise HTTPException(400, "Pick a streamer from the list.")
    return s


def _check_category(category: str) -> None:
    if category not in RECORDABLE_COSTS:
        raise HTTPException(400, "Pick a category from the list.")


async def _ledger_for(db: AsyncSession, e: Expense) -> Optional[LedgerEntry]:
    return (await db.execute(select(LedgerEntry).where(LedgerEntry.source_table == "expenses",
                                                       LedgerEntry.source_id == str(e.id)))).scalar_one_or_none()


async def record(db: AsyncSession, by: User, *, on_date: date, category: str, description: str, amount_minor: int,
                 vendor: Optional[str] = None, paid_with: Optional[str] = None, reference: Optional[str] = None,
                 streamer_id: Optional[uuid.UUID] = None, note: Optional[str] = None,
                 items: Optional[list[dict]] = None) -> tuple[Expense, list[Equipment]]:
    """`items`: equipment bought with this, each {name, serial_number?}. The cost is split evenly across them."""
    _check_category(category)
    if amount_minor <= 0:
        raise HTTPException(400, "Enter the amount paid.")
    streamer = await _streamer(db, streamer_id)
    e = Expense(on_date=on_date, category=category, description=description.strip(), vendor=(vendor or "").strip() or None,
                amount_minor=amount_minor, paid_with=paid_with, reference=(reference or "").strip() or None,
                streamer_id=streamer.id if streamer else None, note=(note or "").strip() or None, entered_by=by.id)
    db.add(e)
    await db.flush()
    who = f" · for {streamer.stage_name}" if streamer else ""
    db.add(LedgerEntry(on_date=on_date, type="expense", category=category, amount_minor=amount_minor,
                       source_table="expenses", source_id=str(e.id), note=f"{e.description}{who}"[:300], entered_by=by.id))
    gear: list[Equipment] = []
    items = [i for i in (items or []) if (i.get("name") or "").strip()]
    if items:
        share, extra = divmod(amount_minor, len(items))
        for n, item in enumerate(items):
            g = Equipment(tag=await next_tag(db), name=item["name"].strip()[:120], condition="good",
                          purchase_cost_minor=share + (extra if n == 0 else 0), streamer_id=e.streamer_id, expense_id=e.id,
                          purchased_on=on_date, serial_number=(item.get("serial_number") or "").strip()[:80] or None,
                          assigned_to=f"{streamer.stage_name}" if streamer else None)
            db.add(g)
            await db.flush()
            gear.append(g)
    return e, gear


async def update(db: AsyncSession, e: Expense, changes: dict) -> Expense:
    if "category" in changes and changes["category"] is not None:
        _check_category(changes["category"])
    if "streamer_id" in changes:
        await _streamer(db, changes["streamer_id"])
    for field in ("on_date", "category", "description", "vendor", "amount_minor", "paid_with", "reference", "streamer_id", "note"):
        if field in changes:
            setattr(e, field, changes[field])
    if e.amount_minor <= 0:
        raise HTTPException(400, "Enter the amount paid.")
    entry = await _ledger_for(db, e)
    streamer = await db.get(Streamer, e.streamer_id) if e.streamer_id else None
    note = f"{e.description}{' · for ' + streamer.stage_name if streamer else ''}"[:300]
    if entry:
        entry.on_date, entry.category, entry.amount_minor, entry.note = e.on_date, e.category, e.amount_minor, note
    else:
        db.add(LedgerEntry(on_date=e.on_date, type="expense", category=e.category, amount_minor=e.amount_minor,
                           source_table="expenses", source_id=str(e.id), note=note, entered_by=e.entered_by))
    if "streamer_id" in changes:
        for g in (await db.execute(select(Equipment).where(Equipment.expense_id == e.id))).scalars():
            g.streamer_id = e.streamer_id
            g.assigned_to = streamer.stage_name if streamer else g.assigned_to
    await db.flush()
    return e


async def remove(db: AsyncSession, e: Expense) -> int:
    """Delete a purchase recorded by mistake: its ledger entry goes too. Gear bought with it stays on the
    register (unlinked), since it physically exists; retire it there if it doesn't. Returns how many items."""
    entry = await _ledger_for(db, e)
    if entry:
        await db.delete(entry)
    gear = (await db.execute(select(Equipment).where(Equipment.expense_id == e.id))).scalars().all()
    for g in gear:
        g.expense_id = None
    await db.delete(e)
    await db.flush()
    return len(gear)


def label(category: str) -> str:
    return COST_CATEGORIES.get(category, category)
