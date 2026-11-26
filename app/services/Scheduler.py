"""Background jobs, run in the API process like the school systems' schedulers.

Hourly it checks the date in Accra and runs each job once per day it is due:
- daily: commission past the refund window becomes payable
- daily: YouTube Shorts view counts refreshed for clips in months not yet paid (a guide for verification)
- daily: contracts drafted for streamer trials ending within 30 days, renewals for contracts ending within 90
- the 1st: streamer salary tiers for the new month (and Finance and the streamer are told)
- the 1st: streamers who haven't sent last month's channel earnings report are reminded
- the 25th (payroll draft_day): the payout run is drafted
- Sundays: the Clip Table's ranks are saved so next week shows movement
- daily: ideas with no answer after 5 days remind their department heads (once)
- Mondays: the Founder gets the ideas the departments closed as "Not now" in the past week
- daily from 07:00: each person's tasks due today, and those overdue since yesterday (TaskNoticeService)

Every 2 minutes a second, lighter loop sends the bundled emails about new tasks and goals (TaskNoticeService).

Only one API instance runs a pass at a time (a Postgres advisory lock), so rolling deploys and more than one
task are safe: an instance that finds the lock taken skips the pass, and the jobs are marked done for the day
by the one holding it.
"""

import asyncio
import logging
from datetime import datetime

from sqlalchemy import select, text

from app.core.database import session_manager
from app.models.people import AppSetting
from app.services import AgreementService, ChannelService, ClipService, PayrollService, StoreService, SuggestionService, TaskNoticeService
from app.services.CompanyService import ACCRA, today
from app.services.money import month_key
from app.services.SettingsService import get_setting

logger = logging.getLogger(__name__)
LOCK_KEY = 5_348_000_001  # any fixed number; only the scheduler takes it (advisory locks are per database)
NOTICE_LOCK_KEY = 5_348_000_002  # the 2-minute notices loop
NOTICE_EVERY_SECONDS = 120


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
    """Run what is due, unless another instance is already running a pass."""
    async with session_manager.engine.connect() as lock:
        got = (await lock.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY})).scalar()
        await lock.commit()  # the lock belongs to the connection, not the transaction: don't sit idle in one
        if not got:
            logger.info("Scheduler pass skipped: another instance is running one")
            return []
        try:
            return await _run_due_jobs()
        finally:
            await lock.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
            await lock.commit()


async def _run_due_jobs() -> list[str]:
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
        if d.day == 1 and await _once(db, "channel_report_reminders", stamp):
            await ChannelService.remind(db, d)
            ran.append("channel_report_reminders")
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
        mail = []
        if await _once(db, "suggestion_reminders", stamp):
            mail += await SuggestionService.remind(db)
            ran.append("suggestion_reminders")
        if d.weekday() == 0 and await _once(db, "suggestions_closed_week", stamp):
            mail += await SuggestionService.weekly_closed(db)
            ran.append("suggestions_closed_week")
        if datetime.now(ACCRA).hour >= TaskNoticeService.REMIND_FROM_HOUR and await _once(db, "task_reminders", stamp):
            mail += await TaskNoticeService.due_reminder_mail(db, d)
            ran.append("task_reminders")
        await db.commit()
    await SuggestionService.deliver(mail)
    return ran


async def send_notices() -> int:
    """The bundled task and goal emails that are due (skipped if another instance holds the lock)."""
    async with session_manager.engine.connect() as lock:
        got = (await lock.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": NOTICE_LOCK_KEY})).scalar()
        await lock.commit()
        if not got:
            return 0
        try:
            async with session_manager.session_factory() as db:
                mail = await TaskNoticeService.assignment_mail(db)
                await db.commit()
            await SuggestionService.deliver(mail)
            return len(mail)
        finally:
            await lock.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": NOTICE_LOCK_KEY})
            await lock.commit()


async def notices_forever() -> None:
    while True:
        try:
            sent = await send_notices()
            if sent:
                logger.info("Task emails sent: %s", sent)
        except Exception:
            logger.exception("Task emails pass failed")
        await asyncio.sleep(NOTICE_EVERY_SECONDS)


async def run_forever() -> None:
    while True:
        try:
            ran = await run_due_jobs()
            if ran:
                logger.info("Scheduler ran: %s", ", ".join(ran))
        except Exception:
            logger.exception("Scheduler pass failed")
        await asyncio.sleep(3600)
