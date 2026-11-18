"""Background jobs, run in the API process like the school systems' schedulers.

Hourly it checks the date in Accra and runs each job once per day it is due:
- daily: commission past the refund window becomes payable
- daily: YouTube Shorts view counts refreshed for clips in months not yet paid (a guide for verification)
- daily: contracts drafted for streamer trials ending within 30 days, renewals for contracts ending within 90
- the 1st: streamer salary tiers for the new month (and Finance and the streamer are told)
- the 25th (payroll draft_day): the payout run is drafted
- Sundays: the Clip Table's ranks are saved so next week shows movement
"""

import asyncio
import logging

from sqlalchemy import select

from app.core.database import session_manager
from app.models.people import AppSetting
from app.services import AgreementService, ClipService, PayrollService, StoreService
from app.services.CompanyService import today
from app.services.money import month_key
from app.services.SettingsService import get_setting

logger = logging.getLogger(__name__)


async def _once(db, job: str, day_key: str) -> bool:
    key = f"job:{job}"
    row = (await db.execute(select(AppSetting).where(AppSetting.key == key))).scalar_one_or_none()
    if row and row.value.get("last") == day_key:
        return False
    if row:
        row.value = {"last": day_key}
    else:
        db.add(AppSetting(key=key, value={"last": day_key}))
    return True


async def run_due_jobs() -> list[str]:
    ran = []
    d = today()
    stamp = d.isoformat()
    async with session_manager.session_factory() as db:
        if await _once(db, "commission_payable", stamp):
            await StoreService.make_commissions_payable(db)
            ran.append("commission_payable")
        if await _once(db, "youtube_views", stamp):
            await ClipService.refresh_open_youtube_clips(db)
            ran.append("youtube_views")
        if await _once(db, "agreement_follow_ups", stamp):
            await AgreementService.draft_follow_ups(db, d)
            ran.append("agreement_follow_ups")
        if d.day == 1 and await _once(db, "salary_tiers", stamp):
            await PayrollService.set_tiers_for_month(db, month_key(d))
            ran.append("salary_tiers")
        draft_day = (await get_setting(db, "payroll"))["draft_day"]
        if d.day == draft_day and await _once(db, "payroll_draft", stamp):
            try:
                await PayrollService.draft_run(db, month_key(d), None)
                ran.append("payroll_draft")
            except Exception:
                logger.exception("Payroll draft skipped")
        if d.weekday() == 6 and await _once(db, "clip_table_snapshot", stamp):
            await ClipService.snapshot_week(db, d)
            ran.append("clip_table_snapshot")
        await db.commit()
    return ran


async def run_forever() -> None:
    while True:
        try:
            ran = await run_due_jobs()
            if ran:
                logger.info("Scheduler ran: %s", ", ".join(ran))
        except Exception:
            logger.exception("Scheduler pass failed")
        await asyncio.sleep(3600)
