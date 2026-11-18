"""Finance and payroll: cash, P&L against budget, income mix, the reporting calendar, the ledger, budgets,
invoices, salary tiers and the monthly payout run. Only the Founder approves a run."""

import uuid
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import Access, require_area, require_founder
from app.core.database import aget_db
from app.models.money import COST_CATEGORIES, INCOME_CATEGORIES, Budget, CashBalance, LedgerEntry, PayrollLine, PayrollRun
from app.models.partnerships import Brand, Invoice
from app.services import PayrollService
from app.services.AuditService import audit
from app.services.CompanyService import ledger_totals, money_summary, today
from app.services.money import ghs, month_key, month_label, next_month, prev_month
from app.services.SettingsService import get_setting

router = APIRouter(tags=["finance"])
finance_desk = require_area("finance")


async def _pnl(db: AsyncSession, key: str) -> list[dict]:
    actual = await ledger_totals(db, key)
    budgets = {b.category: b.planned_minor for b in (await db.execute(select(Budget).where(Budget.month == key))).scalars()}
    rows = [{"label": "Income", "heading": True}]
    for cat, label in INCOME_CATEGORIES.items():
        rows.append({"label": label, "category": cat, "actual_minor": actual["revenue"].get(cat, 0), "budget_minor": budgets.get(cat, 0)})
    income_budget = sum(budgets.get(c, 0) for c in INCOME_CATEGORIES)
    rows.append({"label": "Total income", "total": True, "actual_minor": actual["income_minor"], "budget_minor": income_budget})
    rows.append({"label": "Costs", "heading": True})
    cost_budget = 0
    for cat, label in COST_CATEGORIES.items():
        a, b = actual["expense"].get(cat, 0), budgets.get(cat, 0)
        cost_budget += b
        if a or b:
            rows.append({"label": label, "category": cat, "actual_minor": a, "budget_minor": b})
    rows.append({"label": "Net · month to date", "total": True, "actual_minor": actual["net_minor"],
                 "budget_minor": income_budget - cost_budget})
    return rows


async def _reporting(db: AsyncSession, draft_day: int) -> list[dict]:
    from app.models.investors import InvestorReport

    items = []
    sent = (await db.execute(select(InvestorReport).where(InvestorReport.published_at.is_not(None))
                             .order_by(InvestorReport.period.desc()).limit(1))).scalar_one_or_none()
    due = (await db.execute(select(InvestorReport).where(InvestorReport.published_at.is_(None))
                            .order_by(InvestorReport.period).limit(1))).scalar_one_or_none()
    if sent:
        items.append({"title": f"{month_label(sent.period).split(' ')[0]} investor report", "label": f"Sent {sent.published_at.strftime('%-d %b')}", "tone": "ok"})
    if due:
        items.append({"title": f"{month_label(due.period).split(' ')[0]} investor report",
                      "label": f"Due {due.due_on.strftime('%-d %b')}" if due.due_on else "Draft", "tone": "warn"})
    items.append({"title": "Designer commission statements", "label": "Auto · 1st of month", "tone": "info"})
    items.append({"title": "Payout run drafted", "label": f"Auto · {draft_day}th of month", "tone": "info"})
    exit_terms = await get_setting(db, "exit")
    items.append({"title": "Vault valuation update", "label": exit_terms.get("next_valuation") or "With the next report", "tone": ""})
    return items


def _calendar(on: date, draft_day: int) -> list[dict]:
    def nxt(day: int) -> date:
        d = date(on.year, on.month, min(day, 28))
        if d < on:
            m = next_month(month_key(on))
            d = date(int(m[:4]), int(m[5:]), min(day, 28))
        return d

    m = next_month(month_key(on))
    last_day = date(int(m[:4]), int(m[5:]), 1) - timedelta(days=1)
    items = [("Salary tiers set for the month", nxt(1)), ("Investor report sent", nxt(5)),
             ("Payout run drafted", nxt(draft_day)), ("Month closed and reconciled", last_day if last_day >= on else nxt(28))]
    items.sort(key=lambda x: x[1])
    return [{"title": t, "date": d.isoformat(), "days": (d - on).days} for t, d in items]


@router.get("/finance/summary")
async def finance_summary(access: Access = Depends(finance_desk), db: AsyncSession = Depends(aget_db)):
    key = month_key(today())
    money = await money_summary(db)
    run = (await db.execute(select(PayrollRun).where(PayrollRun.period == key))).scalar_one_or_none()
    totals = await ledger_totals(db, key)
    income = totals["income_minor"] or 1
    mix = [{"label": INCOME_CATEGORIES[c].split(" (")[0], "minor": totals["revenue"].get(c, 0),
            "percent": round(totals["revenue"].get(c, 0) * 100 / income)} for c in INCOME_CATEGORIES]
    mix.sort(key=lambda r: -r["minor"])
    overdue = (await db.execute(select(Invoice).where(Invoice.status == "issued", Invoice.due_at < today()))).scalars().all()
    draft_day = (await get_setting(db, "payroll"))["draft_day"]
    last = await ledger_totals(db, prev_month(key))
    budget_spend = sum(b.planned_minor for b in (await db.execute(select(Budget).where(Budget.month == key, Budget.type == "expense"))).scalars())
    kpis = {"income_change": round((totals["income_minor"] - last["income_minor"]) * 100 / last["income_minor"]) if last["income_minor"] else None,
            "last_month": month_label(prev_month(key)).split(" ")[0],
            "spend_of_budget": round(totals["spend_minor"] * 100 / budget_spend) if budget_spend else None}
    return {"month": month_label(key), "money": money, "pnl": await _pnl(db, key), "mix": mix, "kpis": kpis,
            "reporting": await _reporting(db, draft_day),
            "calendar": _calendar(today(), draft_day), "overdue_invoices": len(overdue),
            "run": await _run_out(db, run, access) if run else None, "period": key,
            "can_approve": access.is_founder}


async def _run_out(db: AsyncSession, run: PayrollRun, access: Access) -> dict:
    rows = await PayrollService.run_rows(db, run)
    return {"id": str(run.id), "period": run.period, "label": month_label(run.period), "status": run.status,
            "total_minor": run.total_minor, "payee_count": run.payee_count, "returned_note": run.returned_note,
            "submitted_at": run.submitted_at.isoformat() if run.submitted_at else None,
            "approved_at": run.approved_at.isoformat() if run.approved_at else None,
            "lines": rows, "missing": sum(1 for r in rows if r["status"] == "missing_details"),
            "can_submit": run.status == "draft" and access.does_finance,
            "can_approve": run.status == "submitted" and access.is_founder,
            "can_pay": run.status in ("approved", "paid") and access.does_finance}


@router.get("/payroll/runs")
async def runs(access: Access = Depends(finance_desk), db: AsyncSession = Depends(aget_db)):
    rows = (await db.execute(select(PayrollRun).order_by(PayrollRun.period.desc()))).scalars().all()
    return [{"id": str(r.id), "period": r.period, "label": month_label(r.period), "status": r.status,
             "total_minor": r.total_minor, "payee_count": r.payee_count} for r in rows]


@router.get("/payroll/runs/current")
async def current_run(access: Access = Depends(finance_desk), db: AsyncSession = Depends(aget_db)):
    run = (await db.execute(select(PayrollRun).where(PayrollRun.period == month_key(today())))).scalar_one_or_none()
    return await _run_out(db, run, access) if run else None


@router.get("/payroll/runs/{run_id}")
async def get_run(run_id: uuid.UUID, access: Access = Depends(finance_desk), db: AsyncSession = Depends(aget_db)):
    run = await db.get(PayrollRun, run_id)
    if not run:
        raise HTTPException(404, "Run not found.")
    return await _run_out(db, run, access)


class DraftIn(BaseModel):
    period: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}$")


def _finance_only(access: Access):
    if not access.does_finance:
        raise HTTPException(403, "Only Finance prepares payout runs.")


@router.post("/payroll/runs/draft")
async def draft(body: DraftIn, access: Access = Depends(finance_desk), db: AsyncSession = Depends(aget_db)):
    _finance_only(access)
    run = await PayrollService.draft_run(db, body.period or month_key(today()), access.user)
    await db.commit()
    return await _run_out(db, run, access)


@router.post("/payroll/runs/{run_id}/submit")
async def submit(run_id: uuid.UUID, access: Access = Depends(finance_desk), db: AsyncSession = Depends(aget_db)):
    _finance_only(access)
    run = await db.get(PayrollRun, run_id, with_for_update=True)
    if not run:
        raise HTTPException(404, "Run not found.")
    await PayrollService.submit(db, run, access.user)
    await db.commit()
    return await _run_out(db, run, access)


@router.post("/payroll/runs/{run_id}/approve")
async def approve(run_id: uuid.UUID, access: Access = Depends(require_founder), db: AsyncSession = Depends(aget_db)):
    run = await db.get(PayrollRun, run_id, with_for_update=True)
    if not run:
        raise HTTPException(404, "Run not found.")
    await PayrollService.approve(db, run, access.user)
    await db.commit()
    return await _run_out(db, run, access)


class BackIn(BaseModel):
    note: str = Field(min_length=3, max_length=1000)


@router.post("/payroll/runs/{run_id}/send-back")
async def send_back(run_id: uuid.UUID, body: BackIn, access: Access = Depends(require_founder), db: AsyncSession = Depends(aget_db)):
    run = await db.get(PayrollRun, run_id, with_for_update=True)
    if not run:
        raise HTTPException(404, "Run not found.")
    await PayrollService.send_back(db, run, access.user, body.note)
    await db.commit()
    return await _run_out(db, run, access)


class PaidIn(BaseModel):
    reference: str = Field(min_length=3, max_length=80)


@router.post("/payroll/lines/{line_id}/mark-paid")
async def mark_paid(line_id: uuid.UUID, body: PaidIn, access: Access = Depends(finance_desk), db: AsyncSession = Depends(aget_db)):
    _finance_only(access)
    line = await db.get(PayrollLine, line_id, with_for_update=True)
    if not line:
        raise HTTPException(404, "Line not found.")
    run = await db.get(PayrollRun, line.run_id, with_for_update=True)
    await PayrollService.mark_paid(db, line, run, access.user, body.reference)
    await db.commit()
    return await _run_out(db, run, access)


@router.get("/payroll/tiers")
async def get_tiers(access: Access = Depends(require_area("finance", "settings", "talent")), db: AsyncSession = Depends(aget_db)):
    return [{"id": str(t.id), "label": t.label, "min_minor": t.min_platform_income_minor, "max_minor": t.max_platform_income_minor,
             "salary_minor": t.salary_minor} for t in await PayrollService.tiers(db)]


# Ledger, cash, budgets, invoices -----------------------------------------------------------------------

class LedgerIn(BaseModel):
    on_date: date
    type: str = Field(pattern="^(revenue|expense)$")
    category: str
    amount: float = Field(gt=0, le=10_000_000)
    note: Optional[str] = Field(default=None, max_length=300)


@router.get("/ledger")
async def ledger(month: Optional[str] = None, access: Access = Depends(finance_desk), db: AsyncSession = Depends(aget_db)):
    key = month or month_key(today())
    from app.services.CompanyService import month_bounds

    start, end = month_bounds(key)
    rows = (await db.execute(select(LedgerEntry).where(LedgerEntry.on_date >= start, LedgerEntry.on_date < end)
                             .order_by(LedgerEntry.on_date.desc(), LedgerEntry.created_at.desc()))).scalars().all()
    labels = {**INCOME_CATEGORIES, **COST_CATEGORIES}
    return [{"id": str(r.id), "date": r.on_date.isoformat(), "type": r.type, "category": r.category,
             "category_label": labels.get(r.category, r.category), "amount_minor": r.amount_minor, "note": r.note,
             "automatic": r.source_table is not None} for r in rows]


@router.post("/ledger")
async def add_ledger(body: LedgerIn, request: Request, access: Access = Depends(finance_desk), db: AsyncSession = Depends(aget_db)):
    _finance_only(access)
    cats = INCOME_CATEGORIES if body.type == "revenue" else COST_CATEGORIES
    if body.category not in cats:
        raise HTTPException(400, "Pick a category from the list.")
    row = LedgerEntry(on_date=body.on_date, type=body.type, category=body.category, amount_minor=round(body.amount * 100),
                      note=body.note, entered_by=access.user.id, source_table=None, source_id=str(uuid.uuid4()))
    db.add(row)
    await audit(db, access.user, "ledger.added", "ledger_entry", row.id, request=request,
                summary=f"{body.type} {body.category} {ghs(row.amount_minor)}")
    await db.commit()
    return {"id": str(row.id)}


class CashIn(BaseModel):
    on_date: date
    amount: float = Field(ge=0, le=100_000_000)


@router.post("/finance/cash")
async def set_cash(body: CashIn, request: Request, access: Access = Depends(finance_desk), db: AsyncSession = Depends(aget_db)):
    _finance_only(access)
    row = (await db.execute(select(CashBalance).where(CashBalance.on_date == body.on_date))).scalar_one_or_none()
    before = row.amount_minor if row else None
    if not row:
        row = CashBalance(on_date=body.on_date, amount_minor=0)
        db.add(row)
    row.amount_minor, row.entered_by = round(body.amount * 100), access.user.id
    await audit(db, access.user, "cash.recorded", "cash_balance", body.on_date.isoformat(), request=request,
                before={"amount_minor": before}, after={"amount_minor": row.amount_minor})
    await db.commit()
    return {"ok": True}


class BudgetIn(BaseModel):
    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    lines: dict[str, float]


@router.put("/budgets")
async def set_budgets(body: BudgetIn, request: Request, access: Access = Depends(finance_desk), db: AsyncSession = Depends(aget_db)):
    _finance_only(access)
    for cat, amount in body.lines.items():
        t = "revenue" if cat in INCOME_CATEGORIES else ("expense" if cat in COST_CATEGORIES else None)
        if not t or amount < 0:
            raise HTTPException(400, f"Unknown category {cat}.")
        row = (await db.execute(select(Budget).where(Budget.month == body.month, Budget.category == cat))).scalar_one_or_none()
        if not row:
            row = Budget(month=body.month, category=cat, type=t, planned_minor=0)
            db.add(row)
        row.planned_minor = round(amount * 100)
    await audit(db, access.user, "budget.set", "budget", body.month, request=request, after=body.lines)
    await db.commit()
    return {"ok": True}


@router.get("/invoices")
async def invoices(access: Access = Depends(require_area("finance", "partnerships")), db: AsyncSession = Depends(aget_db)):
    rows = (await db.execute(select(Invoice, Brand).join(Brand, Brand.id == Invoice.brand_id).order_by(Invoice.issued_at.desc()))).all()
    return [{"id": str(i.id), "number": i.number, "brand": b.name, "amount_minor": i.amount_minor,
             "issued_at": i.issued_at.isoformat(), "due_at": i.due_at.isoformat(),
             "status": "overdue" if i.status == "issued" and i.due_at < today() else i.status,
             "paid_at": i.paid_at.isoformat() if i.paid_at else None} for i, b in rows]


class InvoiceIn(BaseModel):
    brand_id: uuid.UUID
    deal_id: Optional[uuid.UUID] = None
    amount: float = Field(gt=0, le=10_000_000)
    due_in_days: int = Field(default=14, ge=0, le=120)
    note: Optional[str] = Field(default=None, max_length=500)


@router.post("/invoices")
async def issue_invoice(body: InvoiceIn, request: Request, access: Access = Depends(finance_desk), db: AsyncSession = Depends(aget_db)):
    _finance_only(access)
    if not await db.get(Brand, body.brand_id):
        raise HTTPException(400, "Pick a brand.")
    n = (await db.scalar(select(func.count()).select_from(Invoice)) or 0) + 1
    inv = Invoice(brand_id=body.brand_id, deal_id=body.deal_id, number=f"SH-INV-{today().year}-{n:03d}",
                  amount_minor=round(body.amount * 100), issued_at=today(), due_at=today() + timedelta(days=body.due_in_days),
                  note=body.note)
    db.add(inv)
    await audit(db, access.user, "invoice.issued", "invoice", inv.number, request=request, summary=f"{inv.number} {ghs(inv.amount_minor)}")
    await db.commit()
    return {"id": str(inv.id), "number": inv.number}


@router.post("/invoices/{invoice_id}/paid")
async def invoice_paid(invoice_id: uuid.UUID, request: Request, access: Access = Depends(finance_desk), db: AsyncSession = Depends(aget_db)):
    _finance_only(access)
    inv = await db.get(Invoice, invoice_id, with_for_update=True)
    if not inv or inv.status == "paid":
        raise HTTPException(409, "This invoice is already paid.")
    inv.status, inv.paid_at = "paid", today()
    db.add(LedgerEntry(on_date=today(), type="revenue", category="brand", amount_minor=inv.amount_minor,
                       source_table="invoices", source_id=str(inv.id), note=inv.number))
    await audit(db, access.user, "invoice.paid", "invoice", inv.number, request=request, summary=f"{inv.number} paid")
    await db.commit()
    return {"ok": True}

