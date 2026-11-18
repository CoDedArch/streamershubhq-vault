"""Payroll (6.5) and streamer salary tiers (7.1).

draft -> submitted -> approved -> paid. Finance drafts and submits; only the Founder approves; nothing
is paid before approval. Balances under the minimum payout are carried into the next run.
"""

import uuid
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import now
from app.models.clips import ClipperEarning
from app.models.money import CarryForward, Decision, LedgerEntry, PayrollLine, PayrollRun, SalaryTier
from app.models.people import StaffProfile, User
from app.models.store import CommissionEntry, OrderItem
from app.models.talent import PlatformIncome, SalaryTierAssignment, Streamer
from app.services import ClipService, EmailService, StoreService
from app.services.AuditService import audit
from app.services.money import ghs, month_label, prev_month
from app.services.SettingsService import get_setting

KIND_LABEL = {"salary": "Salary", "revenue_share": "Producer share", "designer_commission": "Designer",
              "clipper_payout": "Clipper"}
LEDGER_CATEGORY = {"salary": "salary", "revenue_share": "payouts", "designer_commission": "payouts",
                   "clipper_payout": "payouts"}


async def tiers(db: AsyncSession) -> list[SalaryTier]:
    return list((await db.execute(select(SalaryTier).order_by(SalaryTier.position, SalaryTier.min_platform_income_minor))).scalars())


def pick_tier(rows: list[SalaryTier], income_minor: int) -> Optional[SalaryTier]:
    for t in rows:
        if income_minor >= t.min_platform_income_minor and (t.max_platform_income_minor is None
                                                            or income_minor <= t.max_platform_income_minor):
            return t
    return None


async def platform_income(db: AsyncSession, streamer_id, month: str) -> int:
    return int(await db.scalar(select(func.coalesce(func.sum(PlatformIncome.amount_minor), 0)).where(
        PlatformIncome.streamer_id == streamer_id, PlatformIncome.month == month)) or 0)


async def set_tier(db: AsyncSession, streamer: Streamer, month: str) -> SalaryTierAssignment:
    """Fix a streamer's salary for `month` from last month's platform income (run on the 1st)."""
    existing = (await db.execute(select(SalaryTierAssignment).where(SalaryTierAssignment.streamer_id == streamer.id,
                                                                    SalaryTierAssignment.month == month))).scalar_one_or_none()
    if existing:
        return existing
    income = await platform_income(db, streamer.id, prev_month(month))
    tier = pick_tier(await tiers(db), income)
    row = SalaryTierAssignment(streamer_id=streamer.id, month=month, previous_income_minor=income,
                               tier_label=tier.label if tier else "No tier", salary_minor=tier.salary_minor if tier else None)
    db.add(row)
    await db.flush()
    return row


async def set_tiers_for_month(db: AsyncSession, month: str) -> list[SalaryTierAssignment]:
    streamers = (await db.execute(select(Streamer).where(Streamer.status == "signed"))).scalars().all()
    rows = [await set_tier(db, s, month) for s in streamers]
    finance = list((await db.execute(select(User.email).where(User.role.in_(("finance", "founder")),
                                                              User.status == "active"))).scalars())
    for s, row in zip(streamers, rows):
        user = await db.get(User, s.user_id) if s.user_id else None
        pay = ghs(row.salary_minor) if row.salary_minor is not None else "to be set by the Founder"
        lines = [f"{s.stage_name}: {month_label(month)} salary is {pay} ({row.tier_label}).",
                 f"Set from {month_label(prev_month(month))} platform income of {ghs(row.previous_income_minor)}."]
        await EmailService.notify(([user.email] if user else []) + finance, f"{month_label(month)} salary tier · {s.stage_name}",
                                  "Salary tier set", lines)
    return rows


async def _payout_details(db: AsyncSession, user_id) -> tuple[Optional[str], bool]:
    p = (await db.execute(select(StaffProfile).where(StaffProfile.user_id == user_id))).scalar_one_or_none()
    if not p or not p.payout_method:
        return None, False
    ok = bool(p.momo_number_enc) if p.payout_method == "momo" else bool(p.bank_account_enc)
    return p.payout_method, ok


async def draft_run(db: AsyncSession, period: str, actor: Optional[User]) -> PayrollRun:
    """Build (or rebuild) the run for a period from the rules. Only a draft can be rebuilt."""
    run = (await db.execute(select(PayrollRun).where(PayrollRun.period == period))).scalar_one_or_none()
    if run and run.status != "draft":
        raise HTTPException(409, "This run has been submitted. Only a draft can be recalculated.")
    if not run:
        run = PayrollRun(period=period, status="draft")
        db.add(run)
        await db.flush()
    # Free anything a previous draft of this run had claimed
    old_lines = list((await db.execute(select(PayrollLine.id).where(PayrollLine.run_id == run.id))).scalars())
    if old_lines:
        for row in (await db.execute(select(CommissionEntry).where(CommissionEntry.payroll_line_id.in_(old_lines)))).scalars():
            row.payroll_line_id = None
        for row in (await db.execute(select(ClipperEarning).where(ClipperEarning.payroll_line_id.in_(old_lines)))).scalars():
            row.payroll_line_id = None
        for row in (await db.execute(select(CarryForward).where(CarryForward.used_in_run_id == run.id))).scalars():
            row.used_in_run_id = None
        await db.execute(delete(CarryForward).where(CarryForward.from_period == period))
        await db.execute(delete(PayrollLine).where(PayrollLine.run_id == run.id))
    pay_rules = await get_setting(db, "payroll")
    minimum = int(pay_rules["minimum_payout_minor"])
    lines: list[tuple[PayrollLine, list]] = []  # line, claims

    # Streamer salaries, from the tier set for the month
    for s in (await db.execute(select(Streamer).where(Streamer.status == "signed", Streamer.user_id.is_not(None)))).scalars():
        tier = await set_tier(db, s, period)
        basis = {"text": f"{month_label(period).split(' ')[0]} · {tier.tier_label}",
                 "previous_income_minor": tier.previous_income_minor}
        line = PayrollLine(run_id=run.id, payee_user_id=s.user_id, kind="salary", amount_minor=tier.salary_minor or 0,
                           basis=basis, status="ready" if tier.salary_minor is not None else "on_hold")
        if tier.salary_minor is None:
            basis["text"] += " · salary for this tier not set"
        lines.append((line, []))

    # Revenue shares (the producer's 5% of platform income)
    share_bps = int(pay_rules.get("producer_share_bps") or 0)
    if share_bps:
        income = int(await db.scalar(select(func.coalesce(func.sum(PlatformIncome.amount_minor), 0)).where(
            PlatformIncome.month == prev_month(period))) or 0)
        for p in (await db.execute(select(StaffProfile).where(StaffProfile.pay_type == "revenue_share"))).scalars():
            amount = int((Decimal(income) * share_bps / Decimal(10000)).quantize(Decimal(1), ROUND_HALF_UP))
            lines.append((PayrollLine(run_id=run.id, payee_user_id=p.user_id, kind="revenue_share", amount_minor=amount,
                                      basis={"text": f"{share_bps / 100:g}% of platform income"}), []))

    # Designer commission: payable entries not yet claimed
    await StoreService.make_commissions_payable(db)
    by_designer: dict = defaultdict(list)
    for entry in (await db.execute(select(CommissionEntry).where(CommissionEntry.status == "payable",
                                                                CommissionEntry.payroll_line_id.is_(None)))).scalars():
        by_designer[entry.designer_user_id].append(entry)
    rate = (await get_setting(db, "commission"))["default_rate_bps"]
    for uid, entries in by_designer.items():
        units = int(await db.scalar(select(func.coalesce(func.sum(OrderItem.qty), 0)).where(
            OrderItem.id.in_([e.order_item_id for e in entries]))) or 0)
        lines.append((PayrollLine(run_id=run.id, payee_user_id=uid, kind="designer_commission",
                                  amount_minor=sum(e.amount_minor for e in entries),
                                  basis={"text": f"{units} unit{'s' if units != 1 else ''} · {rate / 100:g}% commission", "units": units}),
                      entries))

    # Clipper payouts for the period's verified views
    for earning in await ClipService.close_period(db, period):
        if earning.status == "paid" or not earning.amount_minor:
            continue
        views = earning.verified_views
        text = f"{views / 1000:.1f}K verified views" if views >= 1000 else f"{views} verified views"
        lines.append((PayrollLine(run_id=run.id, payee_user_id=earning.clipper_user_id, kind="clipper_payout",
                                  amount_minor=earning.amount_minor, basis={"text": text, "views": views}), [earning]))

    # Balances carried in from earlier months
    carried = list((await db.execute(select(CarryForward).where(CarryForward.used_in_run_id.is_(None),
                                                                CarryForward.from_period < period))).scalars())
    for line, _ in lines:
        extra = [c for c in carried if c.payee_user_id == line.payee_user_id and c.kind == line.kind]
        for c in extra:
            line.amount_minor += c.amount_minor
            c.used_in_run_id = run.id
            line.basis = {**line.basis, "text": f"{line.basis['text']} · includes {ghs(c.amount_minor)} carried from {month_label(c.from_period)}"}
            carried.remove(c)
    for c in carried:  # someone with only a carried balance this month
        line = PayrollLine(run_id=run.id, payee_user_id=c.payee_user_id, kind=c.kind, amount_minor=c.amount_minor,
                           basis={"text": f"Carried from {month_label(c.from_period)}"})
        c.used_in_run_id = run.id
        lines.append((line, []))

    total, payees = 0, set()
    for line, claims in lines:
        method, ok = await _payout_details(db, line.payee_user_id)
        line.method = method
        if line.status != "on_hold":
            if 0 < line.amount_minor < minimum:
                line.status = "carried"
            elif not ok:
                line.status = "missing_details"
        db.add(line)
        await db.flush()
        for claim in claims:
            claim.payroll_line_id = line.id
        if line.status == "carried":
            db.add(CarryForward(payee_user_id=line.payee_user_id, kind=line.kind, amount_minor=line.amount_minor,
                                from_period=period))
        else:
            total += line.amount_minor
        payees.add(line.payee_user_id)
    run.total_minor, run.payee_count, run.drafted_at = total, len(payees), now()
    await audit(db, actor, "payroll.drafted", "payroll_run", run.id, summary=f"{month_label(period)} run drafted: {ghs(total)} for {len(payees)} payees")
    await db.flush()
    return run


async def submit(db: AsyncSession, run: PayrollRun, actor: User) -> PayrollRun:
    if run.status != "draft":
        raise HTTPException(409, "Only a draft run can be sent for approval.")
    run.status, run.submitted_by, run.submitted_at, run.returned_note = "submitted", actor.id, now(), None
    moved = await ClipService.roll_pending(db, run.period)  # unchecked clips are paid next month instead of never
    db.add(Decision(title=f"Approve {month_label(run.period).split(' ')[0]} payout run · {ghs(run.total_minor)}",
                    source="Finance · CFO · payroll, designers, clippers", kind="payroll", entity_id=str(run.id),
                    amount_minor=run.total_minor, requested_by=actor.id))
    await audit(db, actor, "payroll.submitted", "payroll_run", run.id, summary=f"Sent for Founder approval: {ghs(run.total_minor)}"
                + (f" · {moved} unchecked clip{'s' if moved != 1 else ''} moved to next month" if moved else ""))
    founders = list((await db.execute(select(User.email).where(User.role == "founder", User.status == "active"))).scalars())
    await db.flush()
    await EmailService.notify(founders, f"Payout run waiting for you · {ghs(run.total_minor)}", "A payout run needs your approval",
                              [f"{month_label(run.period)}: {run.payee_count} payees, {ghs(run.total_minor)}.",
                               "Nothing is paid until you approve it."], ("Review the run", "/os/finance"))
    return run


async def _close_decision(db: AsyncSession, run: PayrollRun, status: str, actor: User, note: Optional[str] = None):
    for d in (await db.execute(select(Decision).where(Decision.kind == "payroll", Decision.entity_id == str(run.id),
                                                      Decision.status == "open"))).scalars():
        d.status, d.decided_by, d.decided_at, d.note = status, actor.id, now(), note


async def approve(db: AsyncSession, run: PayrollRun, actor: User) -> PayrollRun:
    if actor.role != "founder":
        raise HTTPException(403, "Only the Founder can approve a payout run.")
    if run.status != "submitted":
        raise HTTPException(409, "Only a submitted run can be approved.")
    run.status, run.approved_by, run.approved_at = "approved", actor.id, now()
    await _close_decision(db, run, "approved", actor)
    await audit(db, actor, "payroll.approved", "payroll_run", run.id, summary=f"Approved {ghs(run.total_minor)}",
                before={"status": "submitted"}, after={"status": "approved"})
    await db.flush()
    return run


async def send_back(db: AsyncSession, run: PayrollRun, actor: User, note: str) -> PayrollRun:
    if actor.role != "founder":
        raise HTTPException(403, "Only the Founder can send a run back.")
    if run.status != "submitted":
        raise HTTPException(409, "Only a submitted run can be sent back.")
    if not note.strip():
        raise HTTPException(400, "Say what needs to change.")
    run.status, run.returned_note = "draft", note.strip()
    await _close_decision(db, run, "declined", actor, note)
    await audit(db, actor, "payroll.sent_back", "payroll_run", run.id, summary=f"Sent back: {note.strip()}")
    await db.flush()
    return run


async def mark_paid(db: AsyncSession, line: PayrollLine, run: PayrollRun, actor: User, reference: str) -> PayrollLine:
    if run.status not in ("approved", "paid"):
        raise HTTPException(409, "Lines can be paid only after the Founder approves the run.")
    if line.status == "paid":
        raise HTTPException(409, "This line is already paid.")
    if line.status in ("carried", "on_hold"):
        raise HTTPException(409, "This line isn't due this month.")
    if line.status == "missing_details":
        method, ok = await _payout_details(db, line.payee_user_id)
        if not ok:
            raise HTTPException(409, "Add the payee's payout details first.")
        line.method = method
    if not reference.strip():
        raise HTTPException(400, "Enter the mobile money or bank reference.")
    line.status, line.paid_ref, line.paid_at, line.paid_by = "paid", reference.strip(), now(), actor.id
    db.add(LedgerEntry(on_date=now().date(), type="expense", category=LEDGER_CATEGORY[line.kind],
                       amount_minor=line.amount_minor, source_table="payroll_lines", source_id=str(line.id),
                       note=f"{KIND_LABEL[line.kind]} · {run.period}"))
    # This line, plus any small balances from earlier months that were carried into it
    paid_lines = [line.id]
    for c in (await db.execute(select(CarryForward).where(CarryForward.used_in_run_id == run.id,
                                                          CarryForward.payee_user_id == line.payee_user_id,
                                                          CarryForward.kind == line.kind))).scalars():
        paid_lines += list((await db.execute(
            select(PayrollLine.id).join(PayrollRun, PayrollRun.id == PayrollLine.run_id)
            .where(PayrollRun.period == c.from_period, PayrollLine.payee_user_id == line.payee_user_id,
                   PayrollLine.kind == line.kind, PayrollLine.status == "carried"))).scalars())
    for entry in (await db.execute(select(CommissionEntry).where(CommissionEntry.payroll_line_id.in_(paid_lines)))).scalars():
        entry.status = "paid"
    for earning in (await db.execute(select(ClipperEarning).where(ClipperEarning.payroll_line_id.in_(paid_lines)))).scalars():
        earning.status = "paid"
    await audit(db, actor, "payroll.line_paid", "payroll_line", line.id, summary=f"Paid {ghs(line.amount_minor)} · ref {reference.strip()}")
    await db.flush()
    due = (await db.execute(select(func.count()).select_from(PayrollLine).where(
        PayrollLine.run_id == run.id, PayrollLine.status.in_(("ready", "missing_details"))))).scalar()
    if not due:
        run.status = "paid"
    return line


async def run_rows(db: AsyncSession, run: PayrollRun) -> list[dict]:
    lines = (await db.execute(select(PayrollLine, User).join(User, User.id == PayrollLine.payee_user_id)
                              .where(PayrollLine.run_id == run.id))).all()
    order = {"salary": 0, "revenue_share": 1, "designer_commission": 2, "clipper_payout": 3}
    lines.sort(key=lambda r: (order.get(r[0].kind, 9), -r[0].amount_minor))
    return [{"id": str(l.id), "payee": u.handle or u.name, "payee_id": str(u.id), "kind": l.kind, "type": KIND_LABEL[l.kind],
             "basis": l.basis.get("text"), "method": {"momo": "Mobile Money", "bank": "Bank"}.get(l.method or "", "Not set"),
             "amount_minor": l.amount_minor, "status": l.status, "paid_ref": l.paid_ref,
             "paid_at": l.paid_at.isoformat() if l.paid_at else None} for l, u in lines]


def new_id() -> uuid.UUID:
    return uuid.uuid4()
