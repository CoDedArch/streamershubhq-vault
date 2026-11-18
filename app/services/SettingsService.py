"""Business rules that the Founder can change in OS settings. Every rule has a seed default here, so
nothing is hard-coded elsewhere. Section 14 of the brief lists the ones still to be decided."""

from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.people import AppSetting, User

DEFAULTS: dict[str, dict[str, Any]] = {
    # Designer commission, per product at approval. Streamers earning on merch with their name is open.
    "commission": {"default_rate_bps": 1500, "streamer_share_bps": 0, "rate_public": False},
    # Clipper pay: "fixed" (rate per 1,000 verified views, optional caps) or "pool" (budget split by views)
    "clipper_pay": {"mode": "fixed", "rate_per_1000_minor": 300, "monthly_cap_minor": None,
                    "monthly_budget_minor": None, "pool_minor": 0, "rate_public": False, "strikes_to_pause": 3},
    # Investor exit estimate: always "an estimate, not an offer"
    "exit": {"early_exit_adjustment_bps": 1000, "payment_days": 90, "reply_days": 5, "next_valuation": "January 2027"},
    # mode: "interest" collects what people want so the team can call them and agree payment; "checkout"
    # takes payment online (needs SH_PAYSTACK_SECRET_KEY). The one switch covers merch and event tickets.
    # ghs_per_usd: the Founder's dollar rate. Until it's set, the website shows cedis only. Payment is always in cedis.
    "store": {"mode": "interest", "refund_window_days": 14, "fee_bps": 195, "ghs_per_usd": None, "default_currency": "GHS",
              "rate_updated_on": None,
              "delivery": [{"zone": "accra", "label": "Accra and Tema", "minor": 3000},
                           {"zone": "kumasi", "label": "Kumasi", "minor": 4500},
                           {"zone": "other", "label": "Rest of Ghana", "minor": 6000},
                           {"zone": "pickup", "label": "Pick up at the studio", "minor": 0}]},
    "payroll": {"minimum_payout_minor": 5000, "draft_day": 25, "producer_share_bps": 500},
    # Weights for the monthly performance score, by role. Each set adds up to 100.
    "performance": {"default": {"goals": 50, "on_time": 30, "lead": 20},
                    "streamer": {"goals": 60, "on_time": 30, "lead": 10}},
    # The four 6-month pilot targets the company is measured against
    "pilot": {"ends_on": "2027-03-31"},
}


async def get_setting(db: AsyncSession, key: str) -> dict:
    row = (await db.execute(select(AppSetting).where(AppSetting.key == key))).scalar_one_or_none()
    value = deepcopy(DEFAULTS.get(key, {}))
    if row:
        value.update(row.value or {})
    return value


async def put_setting(db: AsyncSession, key: str, value: dict, actor: User) -> dict:
    row = (await db.execute(select(AppSetting).where(AppSetting.key == key))).scalar_one_or_none()
    if row:
        row.value = {**(row.value or {}), **value}
        row.updated_by = actor.id
    else:
        db.add(AppSetting(key=key, value=value, updated_by=actor.id))
    await db.flush()
    return await get_setting(db, key)
