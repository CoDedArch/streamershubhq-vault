"""Stake values and exit estimates. Valuations are entered by the Founder, never calculated; the stake
percentage comes from the signed agreement. Everything here is "an estimate, not an offer"."""

from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investors import FundingRound, Investment, Investor, Valuation
from app.services.SettingsService import get_setting

ESTIMATE_NOTE = ("An estimate, not an offer. A buy-back follows the exit terms in your signed agreement and needs "
                 "the founders' approval.")


def pct_of(amount_minor: int, percent) -> int:
    return int((Decimal(amount_minor) * Decimal(str(percent)) / Decimal(100)).quantize(Decimal(1), ROUND_HALF_UP))


async def valuations(db: AsyncSession) -> list[Valuation]:
    return list((await db.execute(select(Valuation).order_by(Valuation.effective_date))).scalars())


async def latest_valuation(db: AsyncSession) -> Optional[Valuation]:
    return (await db.execute(select(Valuation).order_by(Valuation.effective_date.desc()).limit(1))).scalar_one_or_none()


async def holding(db: AsyncSession, investor: Investor) -> dict:
    rows = list((await db.execute(select(Investment).where(Investment.investor_id == investor.id))).scalars())
    invested = sum(r.amount_minor for r in rows)
    shares = sum(r.shares for r in rows)
    percent = sum((Decimal(str(r.percent)) for r in rows), Decimal(0))
    vals = await valuations(db)
    latest = vals[-1] if vals else None
    value = pct_of(latest.amount_minor, percent) if latest else None
    change_minor = (value - invested) if value is not None else None
    change_pct = round(change_minor * 100 / invested, 1) if invested and change_minor is not None else None
    return {"invested_minor": invested, "shares": shares, "percent": float(percent),
            "valuation_minor": latest.amount_minor if latest else None,
            "valuation_date": latest.effective_date.isoformat() if latest else None,
            "value_minor": value, "change_minor": change_minor, "change_percent": change_pct,
            "valuation_id": str(latest.id) if latest else None}


async def exit_estimate(db: AsyncSession, investor: Investor) -> dict:
    h = await holding(db, investor)
    terms = await get_setting(db, "exit")
    bps = int(terms["early_exit_adjustment_bps"])
    value = h["value_minor"] or 0
    adjustment = int((Decimal(value) * Decimal(bps) / Decimal(10000)).quantize(Decimal(1), ROUND_HALF_UP))
    receive = value - adjustment
    pct = f"{bps / 100:g}%"
    return {
        "hold": {"value_minor": value, "rows": [
            {"t": "Your stake", "v": f"{h['percent']:.2f}%"},
            {"t": "Company valuation", "money": h["valuation_minor"]},
            {"t": "Estimated value of your stake", "money": value},
            {"t": "Next valuation", "v": terms.get("next_valuation") or "With the next report"},
        ], "note": "An estimate from the latest valuation. It moves up or down with each update."},
        "exit": {"value_minor": receive, "rows": [
            {"t": "Your stake at today's valuation", "money": value},
            {"t": f"Early-exit adjustment ({pct})", "money": -adjustment},
            {"t": "You would receive", "money": receive},
            {"t": "Paid within", "v": f"{terms['payment_days']} days of approval"},
        ], "note": f"{ESTIMATE_NOTE} We reply to a request within {terms['reply_days']} working days."},
        "adjustment_bps": bps, "estimate_minor": receive, "valuation_id": h["valuation_id"],
    }


async def round_progress(db: AsyncSession) -> Optional[dict]:
    rnd = (await db.execute(select(FundingRound).order_by(FundingRound.created_at.desc()).limit(1))).scalar_one_or_none()
    if not rnd:
        return None
    holders = await db.scalar(select(func.count(func.distinct(Investment.investor_id)))
                              .join(Investor, Investor.id == Investment.investor_id)
                              .where(Investment.round_id == rnd.id, Investment.payment_confirmed_at.is_not(None),
                                     Investor.status == "active"))
    committed = rnd.founders_committed_minor + rnd.raised_minor
    return {"id": str(rnd.id), "name": rnd.name, "target_minor": rnd.target_minor,
            "founders_committed_minor": rnd.founders_committed_minor, "raised_minor": rnd.raised_minor,
            "committed_minor": committed, "percent": round(committed * 100 / rnd.target_minor) if rnd.target_minor else 0,
            "holders": holders or 0, "status": rnd.status}
