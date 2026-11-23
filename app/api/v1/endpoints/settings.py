"""OS settings (Founder only): commission, clipper pay, salary tiers, exit terms, store rules, payroll
and performance weights. Every change is audited."""

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import Access, require_founder
from app.core.database import aget_db
from app.models.money import SalaryTier
from app.services import PayrollService
from app.services.AuditService import audit
from app.services.SettingsService import DEFAULTS, get_setting, put_setting

router = APIRouter(prefix="/settings", tags=["settings"])
EDITABLE = ("commission", "clipper_pay", "exit", "store", "payroll", "performance")


@router.get("")
async def all_settings(access: Access = Depends(require_founder), db: AsyncSession = Depends(aget_db)):
    tiers = await PayrollService.tiers(db)
    return {"settings": {k: await get_setting(db, k) for k in EDITABLE},
            "tiers": [{"label": t.label, "min_minor": t.min_platform_income_minor, "max_minor": t.max_platform_income_minor,
                       "salary_minor": t.salary_minor} for t in tiers]}


def _check(key: str, value: dict) -> None:
    v = {**DEFAULTS[key], **value}
    if key == "commission" and not 0 <= int(v["default_rate_bps"]) <= 5000:
        raise HTTPException(400, "Commission must be between 0% and 50%.")
    if key == "clipper_pay":
        if v["mode"] not in ("fixed", "pool"):
            raise HTTPException(400, "Pay mode is fixed or pool.")
        if int(v["rate_per_1000_minor"]) < 0 or int(v["strikes_to_pause"]) < 1:
            raise HTTPException(400, "Check the rate and strikes.")
    if key == "exit" and not 0 <= int(v["early_exit_adjustment_bps"]) <= 5000:
        raise HTTPException(400, "The adjustment must be between 0% and 50%.")
    if key == "store":
        from app.models.store import STORE_MODES
        from app.services.PaymentProvider import PaymentError, provider

        if v["mode"] not in STORE_MODES:
            raise HTTPException(400, "The store either takes interest or takes payment.")
        rate = v.get("ghs_per_usd")
        if rate is not None:
            try:
                ok = 0.5 <= float(rate) <= 1000
            except (TypeError, ValueError):
                ok = False
            if not ok:
                raise HTTPException(400, "Enter how many cedis one US dollar is, like 15.50.")
        if v.get("default_currency") not in ("GHS", "USD"):
            raise HTTPException(400, "Show prices in cedis or dollars.")
        if v["default_currency"] == "USD" and not rate:
            raise HTTPException(400, "Set the dollar rate before showing dollars first.")
        if v["mode"] == "checkout":
            try:
                provider()
            except PaymentError:
                raise HTTPException(400, "Payments aren't connected yet. Add the Paystack key (SH_PAYSTACK_SECRET_KEY), "
                                         "then switch the store to taking payment.")
    if key == "payroll" and not 1 <= int(v["draft_day"]) <= 28:
        raise HTTPException(400, "The draft day must be between the 1st and the 28th.")
    if key == "payroll" and not (0 <= int(v["producer_share_bps"]) <= 5000 and 0 <= int(v["streamer_share_bps"]) <= 10000):
        raise HTTPException(400, "The producer's share can be up to 50% and the streamer's up to 100%.")
    if key == "performance":
        for role, weights in v.items():
            if sum(int(x) for x in weights.values()) != 100:
                raise HTTPException(400, f"The {role} weights must add up to 100.")


@router.put("/{key}")
async def update(key: str, value: dict[str, Any], request: Request, access: Access = Depends(require_founder),
                 db: AsyncSession = Depends(aget_db)):
    if key not in EDITABLE:
        raise HTTPException(404, "Unknown setting.")
    _check(key, value)
    before = await get_setting(db, key)
    if key == "store" and "ghs_per_usd" in value and value["ghs_per_usd"] != before.get("ghs_per_usd"):
        from app.services.CompanyService import today

        value = {**value, "ghs_per_usd": float(value["ghs_per_usd"]) if value["ghs_per_usd"] else None, "rate_updated_on": today().isoformat()}
    after = await put_setting(db, key, value, access.user)
    await audit(db, access.user, f"settings.{key}", "app_setting", key, request=request, before=before, after=after)
    await db.commit()
    return after


class TierIn(BaseModel):
    label: str = Field(min_length=2, max_length=60)
    min: float = Field(ge=0)
    max: Optional[float] = Field(default=None, ge=0)
    salary: Optional[float] = Field(default=None, ge=0)


@router.put("/salary-tiers/all")
async def set_tiers(tiers: list[TierIn], request: Request, access: Access = Depends(require_founder), db: AsyncSession = Depends(aget_db)):
    if not tiers:
        raise HTTPException(400, "Add at least one tier.")
    ordered = sorted(tiers, key=lambda t: t.min)
    for a, b in zip(ordered, ordered[1:]):
        if a.max is None or a.max >= b.min:
            raise HTTPException(400, "Tiers must not overlap: each tier's top must be below the next tier's start.")
    before = [{"label": t.label, "salary_minor": t.salary_minor} for t in await PayrollService.tiers(db)]
    await db.execute(delete(SalaryTier))
    for i, t in enumerate(ordered):
        db.add(SalaryTier(label=t.label, min_platform_income_minor=round(t.min * 100),
                          max_platform_income_minor=round(t.max * 100) if t.max is not None else None,
                          salary_minor=round(t.salary * 100) if t.salary is not None else None, position=i))
    await audit(db, access.user, "settings.salary_tiers", "salary_tier", None, request=request, before={"tiers": before},
                after={"tiers": [t.model_dump() for t in ordered]})
    await db.commit()
    return {"ok": True}
