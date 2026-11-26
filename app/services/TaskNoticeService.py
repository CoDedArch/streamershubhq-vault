"""Emails about work someone was given, kept from becoming a nuisance.

New tasks and goals a lead sets for someone are bundled into one email:
- it goes once the lead has stopped adding for QUIET (or MAX_WAIT after the first one, if they keep going);
- never more than one every GAP to the same person; anything given in between waits for the next email,
  so nothing is dropped;
- nobody is emailed about a task they gave themselves, about a task ticked off before the email goes, or
  about older tasks and the sample data (they have no `assigned_by_id`).

Each morning one email per person lists their open tasks due today and those that went overdue yesterday,
so a task is mentioned at most twice and never chased after that.
"""

from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import now
from app.models.people import Goal, Task, User

QUIET = timedelta(minutes=10)
MAX_WAIT = timedelta(minutes=30)
GAP = timedelta(minutes=30)
REMIND_FROM_HOUR = 7  # Accra time: the due-today email goes in the morning, not just after midnight
MY_WORK = ("Open my tasks and goals", "/os/me")


def _day(d: date) -> str:
    return f"{d:%a} {d.day} {d:%b}"


def _num(x) -> str:
    return f"{float(x):g}"


def _line(x) -> str:
    if isinstance(x, Task):
        return f"Task: {x.title} · {f'due {_day(x.due_on)}' if x.due_on else 'no due date'}"
    return f"Goal: {x.title} · {_num(x.target)}{f' {x.unit}' if x.unit else ''} by {_day(x.period_end)}"


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


async def _last_sent(db: AsyncSession, owner_id) -> Optional[datetime]:
    t = await db.scalar(select(func.max(Task.notified_at)).where(Task.owner_user_id == owner_id))
    g = await db.scalar(select(func.max(Goal.notified_at)).where(Goal.owner_user_id == owner_id))
    return max((x for x in (t, g) if x), default=None)


async def assignment_mail(db: AsyncSession, at: Optional[datetime] = None) -> list[dict]:
    """The bundled emails due now (marks what they cover as sent; the caller commits, then delivers)."""
    at = at or now()
    tasks = (await db.execute(select(Task).where(
        Task.assigned_by_id.is_not(None), Task.owner_user_id.is_not(None), Task.assigned_by_id != Task.owner_user_id,
        Task.notified_at.is_(None), Task.status != "done"))).scalars().all()
    goals = (await db.execute(select(Goal).where(
        Goal.assigned_by_id.is_not(None), Goal.owner_user_id.is_not(None), Goal.assigned_by_id != Goal.owner_user_id,
        Goal.notified_at.is_(None), Goal.status == "open", Goal.is_pilot_target.is_(False)))).scalars().all()
    waiting: dict = {}
    for x in [*tasks, *goals]:
        waiting.setdefault(x.owner_user_id, []).append(x)
    mail = []
    for owner_id, items in waiting.items():
        owner = await db.get(User, owner_id)
        if not owner or owner.status != "active":
            continue  # not signed in yet: it waits until they can open it
        made = [x.created_at for x in items]
        if at - max(made) < QUIET and at - min(made) < MAX_WAIT:
            continue  # the lead is still adding: wait, so it all goes in one email
        last = await _last_sent(db, owner_id)
        if last and at - last < GAP:
            continue  # emailed recently: these go in the next one
        items.sort(key=lambda x: (isinstance(x, Goal), x.created_at))
        n_tasks = sum(1 for x in items if isinstance(x, Task))
        n_goals = len(items) - n_tasks
        givers = []
        for giver_id in dict.fromkeys(x.assigned_by_id for x in items):
            giver = await db.get(User, giver_id)
            if giver:
                givers.append(giver.name)
        what = " and ".join(p for p in (_count(n_tasks, "task") if n_tasks else "", _count(n_goals, "goal") if n_goals else "") if p)
        subject = (f"New {'task' if n_tasks else 'goal'}: {items[0].title}" if len(items) == 1 else f"{what} for you in Hub OS")
        lines = [f"{' and '.join(givers) or 'Your lead'} added {'this' if len(items) == 1 else 'these'} for you:",
                 *[_line(x) for x in items],
                 "Tick tasks off and update your goals in Hub OS, under My profile and pay."]
        mail.append({"kind": "team", "to": [owner.email], "subject": subject[:150], "title": f"New in Hub OS: {what}",
                     "lines": lines, "cta": MY_WORK})
        for x in items:
            x.notified_at = at
    return mail


async def due_reminder_mail(db: AsyncSession, d: date) -> list[dict]:
    """One email per person: open tasks due today, and those that went overdue yesterday (each said once)."""
    yesterday = d - timedelta(days=1)
    rows = (await db.execute(select(Task, User).join(User, User.id == Task.owner_user_id).where(
        Task.status != "done", Task.due_on.in_((d, yesterday)), User.status == "active")
        .order_by(Task.due_on.desc(), Task.created_at))).all()
    per: dict = {}
    for t, u in rows:
        per.setdefault(u.id, (u, [], []))[1 if t.due_on == d else 2].append(t)
    mail = []
    for u, today_tasks, late in per.values():
        parts = [p for p in (f"{len(today_tasks)} due today" if today_tasks else "", f"{len(late)} overdue" if late else "") if p]
        subject = f"Due today: {today_tasks[0].title}" if len(today_tasks) == 1 and not late else f"Your tasks: {' and '.join(parts)}"
        lines = []
        if today_tasks:
            lines += ["Due today:", *[f"• {t.title}" for t in today_tasks]]
        if late:
            lines += [f"Overdue since {_day(yesterday)}:", *[f"• {t.title}" for t in late]]
        lines.append("Tick them off in Hub OS when they're done. We won't remind you about these again.")
        mail.append({"kind": "team", "to": [u.email], "subject": subject[:150], "title": " and ".join(parts).capitalize(),
                     "lines": lines, "cta": MY_WORK})
    return mail
