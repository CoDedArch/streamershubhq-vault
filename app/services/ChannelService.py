"""A streamer's own channels: the monthly earnings report and Finance's check of it.

The streamer's own channels (Twitch, YouTube, TikTok...) pay the streamer directly. Under the streamer agreement
everything their content earns is shared, so they pass those payouts to the label and report each month what every
channel earned and paid them, with the platform's statement, even in a month with no payout. Finance checks the
report against the statements and what arrived, then confirms the amount the label received. That amount is the
streamer's platform income for the month it's confirmed (the month the label has the money), so payroll's shares
are worked out on money the label actually holds, never counted twice and never missed by a late check.
"""

from datetime import date
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import now
from app.models.money import LedgerEntry
from app.models.people import User
from app.models.talent import ChannelReport, PlatformIncome, Streamer
from app.services import EmailService, S3Service
from app.services.CompanyService import today
from app.services.money import ghs, month_key, month_label, prev_month

PLATFORMS = {"youtube": "YouTube", "twitch": "Twitch", "kick": "Kick", "tiktok": "TikTok", "facebook": "Facebook",
             "instagram": "Instagram", "x": "X", "other": "Other"}
CURRENCIES = ("USD", "GHS")
OWN_CHANNELS = "own"  # the platform_income row that holds what the label received from the streamer's own channels
STATUS = {"submitted": ("With Finance", "warn"), "returned": ("Sent back", "no"), "confirmed": ("Confirmed", "ok")}


def money(minor: int, currency: str) -> str:
    return ghs(minor) if currency == "GHS" else f"${minor / 100:,.2f}"


def first_month(s: Streamer) -> Optional[str]:
    """Reports start with the month the streamer signed. Before signing there's nothing to report."""
    return month_key(s.contract_start) if s.contract_start else None


def months_open(s: Streamer) -> list[str]:
    """The months a report can be sent for: this month and the five before it, from the month they signed."""
    start = first_month(s)
    if not start:
        return []
    out, m = [], month_key(today())
    for _ in range(6):
        if m < start:
            break
        out.append(m)
        m = prev_month(m)
    return out


def due_month(s: Streamer, sent: set[str]) -> Optional[str]:
    """Last month's report, once the month is over, until it's sent."""
    last = prev_month(month_key(today()))
    start = first_month(s)
    return last if start and last >= start and last not in sent else None


async def out(db: AsyncSession, r: ChannelReport, base: str, streamer: Optional[Streamer] = None) -> dict:
    s = streamer or await db.get(Streamer, r.streamer_id)
    reviewer = await db.get(User, r.reviewed_by) if r.reviewed_by else None
    label, tone = STATUS[r.status]
    lines = []
    for ln in r.lines or []:
        lines.append({"platform": ln["platform"], "platform_label": PLATFORMS.get(ln["platform"], ln["platform"]),
                      "earned_minor": ln["earned_minor"], "earned_currency": ln["earned_currency"],
                      "earned": money(ln["earned_minor"], ln["earned_currency"]),
                      "paid_minor": ln["paid_minor"], "paid_currency": ln["paid_currency"],
                      "paid": money(ln["paid_minor"], ln["paid_currency"]) if ln["paid_minor"] else None,
                      "statement_key": ln.get("statement_key"), "statement_name": ln.get("statement_name"),
                      "statement_url": S3Service.signed_url(ln.get("statement_key"), 900, base)})
    return {"id": str(r.id), "month": r.month, "month_label": month_label(r.month), "status": r.status, "status_label": label,
            "tone": tone, "streamer": {"id": str(s.id), "name": s.stage_name} if s else None, "lines": lines,
            "paid_out": any(ln["paid_minor"] for ln in r.lines or []),
            "passed_minor": r.passed_minor, "transfer_ref": r.transfer_ref, "note": r.note,
            "submitted_at": r.submitted_at.isoformat() if r.submitted_at else None,
            "received_minor": r.received_minor, "income_month_label": month_label(r.income_month) if r.income_month else None,
            "review_note": r.review_note, "reviewed_by": reviewer.name if reviewer else None,
            "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None}


async def confirm(db: AsyncSession, r: ChannelReport, received_minor: int, actor: User, note: Optional[str]) -> None:
    """Record what the label received. It counts as platform income this month, the month the label has it."""
    s = await db.get(Streamer, r.streamer_id)
    month = month_key(today())
    r.status, r.received_minor, r.income_month = "confirmed", received_minor, month
    r.reviewed_by, r.reviewed_at, r.review_note = actor.id, now(), (note or "").strip() or None
    if not received_minor:
        return
    row = (await db.execute(select(PlatformIncome).where(PlatformIncome.streamer_id == r.streamer_id, PlatformIncome.month == month,
                                                         PlatformIncome.platform == OWN_CHANNELS))).scalar_one_or_none()
    if row:
        row.amount_minor += received_minor
    else:
        db.add(PlatformIncome(streamer_id=r.streamer_id, month=month, platform=OWN_CHANNELS, amount_minor=received_minor,
                              note="Passed to the label from the streamer's own channels"))
    db.add(LedgerEntry(on_date=today(), type="revenue", category="platform", amount_minor=received_minor,
                       source_table="channel_reports", source_id=str(r.id), entered_by=actor.id,
                       note=f"{s.stage_name if s else 'Streamer'}'s own channels · {month_label(r.month)} report"))


async def finance_emails(db: AsyncSession) -> list[str]:
    return list((await db.execute(select(User.email).where(User.role.in_(("finance", "founder")), User.status == "active"))).scalars())


async def tell_finance(db: AsyncSession, r: ChannelReport, s: Streamer, resent: bool) -> None:
    lines = [f"{s.stage_name} {'sent again' if resent else 'sent'} the {month_label(r.month)} report for their own channels."]
    for ln in r.lines or []:
        paid = f", paid {money(ln['paid_minor'], ln['paid_currency'])}" if ln["paid_minor"] else ", no payout"
        lines.append(f"{PLATFORMS.get(ln['platform'], ln['platform'])}: earned {money(ln['earned_minor'], ln['earned_currency'])}{paid}.")
    lines.append(f"Passed to the label: {ghs(r.passed_minor)}" + (f" (reference {r.transfer_ref})." if r.transfer_ref else "."))
    lines.append("Check the statements and what arrived, then confirm it.")
    await EmailService.notify(await finance_emails(db), f"{s.stage_name}: {month_label(r.month)} channel earnings",
                              "A channel earnings report to check", lines, ("Check it", "/os/finance"))


async def tell_streamer(db: AsyncSession, r: ChannelReport, s: Streamer) -> None:
    user = await db.get(User, s.user_id) if s.user_id else None
    if not user:
        return
    if r.status == "confirmed":
        lines = [f"Finance confirmed your {month_label(r.month)} channel earnings report."]
        if r.received_minor:
            lines.append(f"The label received {ghs(r.received_minor)}. Your share of it is paid in next month's payout run.")
        subject, title = f"{month_label(r.month)} channel earnings confirmed", "Report confirmed"
    else:
        lines = [f"Finance sent back your {month_label(r.month)} channel earnings report.", f"What to fix: {r.review_note}",
                 "Correct it and send it again from your desk."]
        subject, title = f"{month_label(r.month)} channel earnings: please check", "Your report needs a change"
    await EmailService.notify([user.email], subject, title, lines, ("Open my desk", "/os/talent"))


async def remind(db: AsyncSession, d: date) -> int:
    """On the 1st: every signed streamer with an account who hasn't sent last month's report is reminded."""
    month = prev_month(month_key(d))
    sent = set((await db.execute(select(ChannelReport.streamer_id).where(ChannelReport.month == month))).scalars())
    count = 0
    for s in (await db.execute(select(Streamer).where(Streamer.status == "signed", Streamer.user_id.is_not(None)))).scalars():
        start = first_month(s)
        if s.id in sent or not start or month < start:
            continue
        user = await db.get(User, s.user_id)
        if not user or user.status != "active":
            continue
        await EmailService.notify([user.email], f"Your {month_label(month)} channel earnings report", "Your monthly report is due",
                                  [f"Send your {month_label(month)} report from your desk in Hub OS: what each of your own channels "
                                   "earned and paid you, with the platform's statement for each.",
                                   "Send it even if nothing was paid out this month.",
                                   "Pass any payout to the label within 7 days of being paid."], ("Open my desk", "/os/talent"))
        count += 1
    return count

